"""P1 — issue-tracker abstraction + Linear provider + config + doctor probe.

Every provider path runs against a mocked httpx GraphQL transport (no live
network). A live-credentialed end-to-end fetch is covered separately under
``@pytest.mark.integration`` (excluded from ``pytest -m 'not integration'``).
"""

from __future__ import annotations

import json

import httpx
import pytest

from gauntlet.engine.config import IssueTrackerConfig, RunConfig
from gauntlet.trackers import (
    available_trackers,
    get_tracker,
    get_tracker_class,
    render_intent,
)
from gauntlet.trackers.base import (
    Issue,
    IssueNotFound,
    IssueRef,
    IssueTracker,
    IssueTrackerAuthError,
    IssueTrackerError,
    IssueTrackerUnavailable,
)
from gauntlet.trackers.linear import LinearIssueTracker


# ---- transport helpers ------------------------------------------------------


def _transport(handler):
    """A MockTransport wrapping a handler(request) -> httpx.Response."""
    return httpx.MockTransport(handler)


def _json_response(payload: dict, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return _transport(handler)


def _tracker(transport, *, api_key="lin_api_test", timeout_s=10.0):
    return LinearIssueTracker(
        api_key=api_key,
        api_key_env="LINEAR_API_KEY",
        timeout_s=timeout_s,
        transport=transport,
    )


# ---- parse_ref --------------------------------------------------------------


def test_parse_ref_bare_key_normalizes_to_upper():
    tk = _tracker(_json_response({}))
    ref = tk.parse_ref("  eng-1234 ")
    assert ref == IssueRef(provider="linear", raw="  eng-1234 ", key="ENG-1234")


def test_parse_ref_url_reduces_to_key_discarding_slug_and_query():
    tk = _tracker(_json_response({}))
    url = "https://linear.app/acme/issue/ENG-1234/fix-the-widget?foo=bar#c"
    ref = tk.parse_ref(url)
    assert ref.key == "ENG-1234"
    assert ref.provider == "linear"


def test_parse_ref_url_and_bare_key_normalize_identically():
    tk = _tracker(_json_response({}))
    a = tk.parse_ref("ENG-1234")
    b = tk.parse_ref("linear.app/acme/issue/eng-1234/some-slug")
    assert a.key == b.key == "ENG-1234"


def test_parse_ref_malformed_raises_valueerror_not_notfound():
    tk = _tracker(_json_response({}))
    with pytest.raises(ValueError):
        tk.parse_ref("not-a-ref")
    # A usage-class error is explicitly NOT part of the tracker taxonomy.
    with pytest.raises(ValueError) as exc:
        tk.parse_ref("")
    assert not isinstance(exc.value, IssueTrackerError)


# ---- extract_refs -----------------------------------------------------------


def test_extract_refs_textual_order_and_dedup():
    tk = _tracker(_json_response({}))
    body = "Fixes ENG-1234 and relates to OPS-9. Also ENG-1234 again, then ENG-2."
    refs = [r.key for r in tk.extract_refs(body)]
    assert refs == ["ENG-1234", "OPS-9", "ENG-2"]


def test_extract_refs_empty_text():
    tk = _tracker(_json_response({}))
    assert tk.extract_refs("") == []
    assert tk.extract_refs("no refs here") == []


def test_extract_refs_ignores_false_keys_in_url_slug():
    # F-002: the slug of a Linear URL must not yield spurious bare keys.
    tk = _tracker(_json_response({}))
    body = "See https://linear.app/acme/issue/ENG-1/fix-eng-2-regression for context."
    refs = [r.key for r in tk.extract_refs(body)]
    assert refs == ["ENG-1"]


def test_extract_refs_url_and_surrounding_bare_keys_in_order():
    # The URL contributes only its own key; bare keys outside the URL span are
    # still harvested, in textual order, de-duplicated.
    tk = _tracker(_json_response({}))
    body = (
        "OPS-9 before, then https://linear.app/acme/issue/ENG-1/fix-eng-2-bug "
        "and ENG-3 after."
    )
    refs = [r.key for r in tk.extract_refs(body)]
    assert refs == ["OPS-9", "ENG-1", "ENG-3"]


# ---- fetch normalization ----------------------------------------------------


def test_fetch_passes_human_key_as_id_and_normalizes_issue():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {
                    "issue": {
                        "identifier": "ENG-1234",
                        "title": "Fix the widget",
                        "description": "The widget explodes when clicked twice.",
                        "url": "https://linear.app/acme/issue/ENG-1234",
                        "state": {"name": "In Progress"},
                    }
                }
            },
        )

    tk = _tracker(_transport(handler))
    ref = tk.parse_ref("eng-1234")
    issue = tk.fetch(ref)

    # The token passed to issue(id:) is the human key, not a UUID/URL.
    assert seen["body"]["variables"] == {"id": "ENG-1234"}
    # Personal API key goes raw in Authorization (no Bearer prefix).
    assert seen["auth"] == "lin_api_test"
    assert issue == Issue(
        identifier="ENG-1234",
        title="Fix the widget",
        description="The widget explodes when clicked twice.",
        url="https://linear.app/acme/issue/ENG-1234",
        state="In Progress",
    )


def test_fetch_null_issue_maps_to_not_found():
    tk = _tracker(_json_response({"data": {"issue": None}}))
    with pytest.raises(IssueNotFound):
        tk.fetch(IssueRef(provider="linear", raw="ENG-9", key="ENG-9"))


def test_fetch_graphql_entity_not_found_code_maps_to_not_found():
    payload = {
        "data": {"issue": None},
        "errors": [{"message": "Entity not found", "extensions": {"code": "NOT_FOUND"}}],
    }
    tk = _tracker(_json_response(payload))
    with pytest.raises(IssueNotFound):
        tk.fetch(IssueRef(provider="linear", raw="ENG-9", key="ENG-9"))


# ---- error taxonomy ---------------------------------------------------------


def test_http_401_maps_to_auth_error():
    tk = _tracker(_json_response({}, status=401))
    with pytest.raises(IssueTrackerAuthError):
        tk.verify_auth()


def test_http_403_maps_to_auth_error():
    tk = _tracker(_json_response({}, status=403))
    with pytest.raises(IssueTrackerAuthError):
        tk.verify_auth()


def test_graphql_authentication_error_maps_to_auth_error():
    payload = {"errors": [{"message": "Authentication required", "extensions": {}}]}
    tk = _tracker(_json_response(payload, status=400))
    with pytest.raises(IssueTrackerAuthError):
        tk.verify_auth()


def test_http_500_maps_to_unavailable():
    tk = _tracker(_json_response({}, status=503))
    with pytest.raises(IssueTrackerUnavailable):
        tk.verify_auth()


def test_transport_error_maps_to_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    tk = _tracker(_transport(handler))
    with pytest.raises(IssueTrackerUnavailable):
        tk.verify_auth()


def test_timeout_maps_to_unavailable():
    """A call that blocks past the per-call timeout (FR-6.4) fails closed as
    Unavailable — simulated by the transport raising httpx's timeout type."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    tk = _tracker(_transport(handler), timeout_s=1.0)
    with pytest.raises(IssueTrackerUnavailable):
        tk.fetch(IssueRef(provider="linear", raw="ENG-1", key="ENG-1"))


def test_missing_token_maps_to_auth_error_without_network():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"data": {}})

    tk = LinearIssueTracker(
        api_key=None, api_key_env="LINEAR_API_KEY", transport=_transport(handler)
    )
    with pytest.raises(IssueTrackerAuthError):
        tk.verify_auth()
    # No network call is attempted when the token is absent.
    assert calls["n"] == 0


def test_verify_auth_success_is_silent():
    tk = _tracker(_json_response({"data": {"viewer": {"id": "u_1"}}}))
    assert tk.verify_auth() is None


# ---- render_intent determinism ----------------------------------------------


def test_render_intent_tracker_golden():
    issue = Issue(
        identifier="ENG-1234",
        title="Fix the widget",
        description="The widget explodes when clicked twice.",
        url="https://linear.app/acme/issue/ENG-1234",
        state="In Progress",
    )
    out = render_intent(
        issue,
        provenance="tracker",
        independent=True,
        source="issue",
        provider="linear",
    )
    assert out == (
        "# Intent — ENG-1234 · Fix the widget\n"
        "<source: linear ENG-1234 · https://linear.app/acme/issue/ENG-1234>\n"
        "<provenance: tracker · independent>\n"
        "\n"
        "## Problem\n"
        "The widget explodes when clicked twice.\n"
    )
    # No reserved sections in v1.
    assert "## Repro" not in out
    assert "## Expected" not in out


def test_render_intent_manual_golden_omits_source_line():
    out = render_intent(
        "Users report the export button does nothing.",
        provenance="author-session-summary",
        independent=False,
        source="message",
    )
    assert out == (
        "# Intent — (manual)\n"
        "<provenance: author-session-summary · non-independent>\n"
        "\n"
        "## Problem\n"
        "Users report the export button does nothing.\n"
    )
    assert "<source:" not in out


def test_render_intent_is_deterministic():
    issue = Issue("ENG-1", "T", "body", "https://x/ENG-1", None)
    a = render_intent(issue, provenance="tracker", independent=True, source="issue",
                      provider="linear")
    b = render_intent(issue, provenance="tracker", independent=True, source="issue",
                      provider="linear")
    assert a == b


def test_render_intent_rejects_unknown_source():
    with pytest.raises(ValueError):
        render_intent("x", provenance="none", independent=False, source="bogus")


# ---- registry / factory -----------------------------------------------------


def test_registry_has_only_linear():
    assert sorted(available_trackers()) == ["linear"]
    assert get_tracker_class("linear") is LinearIssueTracker


def test_get_tracker_class_unknown_raises():
    with pytest.raises(KeyError):
        get_tracker_class("jira")


def test_get_tracker_resolves_token_by_env_name():
    cfg = IssueTrackerConfig(provider="linear", api_key_env="LINEAR_API_KEY")
    tk = get_tracker(cfg, env={"LINEAR_API_KEY": "lin_secret"})
    assert isinstance(tk, LinearIssueTracker)
    assert tk.api_key == "lin_secret"


def test_get_tracker_missing_env_leaves_key_none():
    cfg = IssueTrackerConfig(provider="linear", api_key_env="LINEAR_API_KEY")
    tk = get_tracker(cfg, env={})
    assert tk.api_key is None


def test_linear_satisfies_protocol():
    tk = _tracker(_json_response({}))
    assert isinstance(tk, IssueTracker)


# ---- IssueTrackerConfig -----------------------------------------------------


def test_config_defaults():
    cfg = IssueTrackerConfig()
    assert cfg.provider == "linear"
    assert cfg.api_key_env == "LINEAR_API_KEY"
    assert cfg.timeout_s == 10.0
    assert cfg.enabled is True


def test_config_provider_none_disables():
    cfg = IssueTrackerConfig(provider="none")
    assert cfg.enabled is False


def test_config_rejects_unsupported_provider():
    with pytest.raises(ValueError) as exc:
        IssueTrackerConfig(provider="jira")
    assert "linear" in str(exc.value)


def test_config_accepts_valid_env_var_name():
    cfg = IssueTrackerConfig(api_key_env="MY_LINEAR_CREDS")
    assert cfg.api_key_env == "MY_LINEAR_CREDS"
    # Surrounding whitespace is stripped.
    assert IssueTrackerConfig(api_key_env="  LINEAR_API_KEY  ").api_key_env == (
        "LINEAR_API_KEY"
    )


def test_config_rejects_empty_api_key_env():
    # F-001: the field names an env var; empty/whitespace is not a name.
    for bad in ("", "   "):
        with pytest.raises(ValueError):
            IssueTrackerConfig(api_key_env=bad)


def test_config_rejects_token_shaped_api_key_env():
    # F-001: a pasted Linear token must not be persisted as an env-var NAME,
    # and the error must not echo the raw (secret) value.
    token = "lin_api_0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(ValueError) as exc:
        IssueTrackerConfig(api_key_env=token)
    assert token not in str(exc.value)


def test_config_rejects_api_key_env_with_invalid_chars():
    # F-001: names with spaces / punctuation (typical of pasted secrets) fail.
    for bad in ("has space", "with-dash", "with/slash", "9leadingdigit"):
        with pytest.raises(ValueError):
            IssueTrackerConfig(api_key_env=bad)


def test_config_rejects_non_positive_timeout():
    with pytest.raises(ValueError):
        IssueTrackerConfig(timeout_s=0)
    with pytest.raises(ValueError):
        IssueTrackerConfig(timeout_s=-5)


def test_config_rejects_below_one_second_timeout():
    # FR-6.4 / §6: the stated floor is 1 second.
    with pytest.raises(ValueError):
        IssueTrackerConfig(timeout_s=0.5)
    assert IssueTrackerConfig(timeout_s=1).timeout_s == 1


def test_runconfig_rejects_unsupported_provider_at_load():
    with pytest.raises(ValueError):
        RunConfig.model_validate({"issue_tracker": {"provider": "github"}})


def test_runconfig_rejects_token_shaped_api_key_env_without_echo():
    # F-001: the real load path (nested under RunConfig) must reject a pasted
    # token and never echo it back in the ValidationError text.
    token = "lin_api_" + "0123456789abcdef" * 2
    with pytest.raises(ValueError) as exc:
        RunConfig.model_validate(
            {"issue_tracker": {"provider": "linear", "api_key_env": token}}
        )
    assert token not in str(exc.value)


def test_runconfig_without_tracker_block():
    cfg = RunConfig.model_validate({})
    assert cfg.issue_tracker is None


def test_runconfig_redaction_covers_custom_tracker_env_name():
    """§7: a custom api_key_env that misses the KEY/TOKEN heuristic is still
    added to the redaction list so the token can never leak to an artifact."""
    cfg = RunConfig.model_validate(
        {"issue_tracker": {"provider": "linear", "api_key_env": "MY_LINEAR_CREDS"}}
    )
    assert "MY_LINEAR_CREDS" in cfg.redaction.extra_env_vars


def test_runconfig_redaction_not_augmented_when_disabled():
    cfg = RunConfig.model_validate(
        {"issue_tracker": {"provider": "none", "api_key_env": "MY_LINEAR_CREDS"}}
    )
    assert "MY_LINEAR_CREDS" not in cfg.redaction.extra_env_vars

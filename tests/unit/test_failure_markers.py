"""Failure-marker allowlist + classification (harness-efficiency FR-3.1, §6).

Contract: every allowlist rule has a captured fixture beside `.gauntlet/
pins.yaml`, and every fixture classifies to its rule's kind + marker. Fail
closed: an unlisted/unstructured error is `terminal`, never auto-continued.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapters import failure_markers as fm
from gauntlet.adapters.base import (
    FAILURE_TERMINAL,
    FAILURE_TRANSIENT_DEPENDENCY,
    FAILURE_TRANSIENT_OVERLOAD,
    FAILURE_TRANSIENT_USAGE_LIMIT,
    FailureInfo,
)

FIXTURES = Path(__file__).resolve().parents[2] / ".gauntlet" / "failure-fixtures"


@pytest.mark.parametrize("rule", fm.ALL_RULES, ids=lambda r: r.name)
def test_every_rule_has_a_fixture_that_classifies_to_it(rule):
    # The contract (§6, BOOTSTRAP-NOTES #26): a marker exists only with a
    # captured fixture, and the fixture classifies to the rule's kind + marker.
    path = FIXTURES / rule.fixture
    assert path.exists(), f"missing fixture for {rule.name}: {rule.fixture}"
    data = json.loads(path.read_text())
    info = fm.classify_captured(rule.adapter, data)
    assert info.kind == rule.kind
    assert info.marker == rule.name


# The full required coverage matrix (plan.md:33 — "one per adapter per kind"):
# every classifier-carrying adapter × every transient kind. The
# `transient_dependency` kind was ADDED by recovery-redesign P5 (plan §5.2) —
# typed transport/dependency envelopes per adapter.
_REQUIRED_ADAPTER_KINDS = frozenset(
    (adapter, kind)
    for adapter in ("claude-code", "codex", "api")
    for kind in (
        FAILURE_TRANSIENT_USAGE_LIMIT,
        FAILURE_TRANSIENT_OVERLOAD,
        FAILURE_TRANSIENT_DEPENDENCY,
    )
)

# (adapter, kind) pairs that have NO live-captured fixture yet — a TRACKED phase
# gap (an upstream/phase conflict, F-002), NOT silent acceptance: these adapter
# failure-modes were never observed in a real failed run, so no envelope exists
# in the harvestable transcripts (2026-06-29..07-01) to redact and pin. The
# synthesized fixtures are fail-closed-safe (a non-matching real error still
# halts terminally); each MUST be re-pinned with a live capture when one is
# observed — at which point it is removed from this set (shrinking it is a
# conscious "we now have a real capture", never automatic).
_UNCAPTURED_PENDING_LIVE = frozenset({
    ("codex", FAILURE_TRANSIENT_OVERLOAD),
    ("api", FAILURE_TRANSIENT_USAGE_LIMIT),
    ("api", FAILURE_TRANSIENT_OVERLOAD),
    # P5 dependency pins (plan §5.2): the api pair carries the LIVE issue-#63
    # timeout capture (api/timeout.json), so only claude-code remains
    # synthesized — no live claude transport-failure envelope has been
    # observed. The codex pair was REMOVED from this set (conscious "we now
    # have a real capture") when issue #96 delivered a live envelope: the
    # 2026-08-09 websocket-503 agent error event pinned as
    # codex/provider-unavailable.json (rule codex_provider_unavailable_message,
    # real_capture=True).
    ("claude-code", FAILURE_TRANSIENT_DEPENDENCY),
})


def _real_capture_coverage() -> dict[tuple[str, str], bool]:
    """Per (adapter, kind): True iff ≥1 rule carries a live-captured fixture."""
    coverage: dict[tuple[str, str], bool] = {}
    for rule in fm.ALL_RULES:
        key = (rule.adapter, rule.kind)
        coverage[key] = coverage.get(key, False) or rule.real_capture
    return coverage


def test_required_adapter_kinds_have_real_capture_or_a_tracked_gap():
    # F-002: the contract must ENFORCE real-captured coverage per (adapter, kind)
    # — not merely that every rule has *some* fixture. Every required pair either
    # has a live capture (real_capture=True) or is an explicitly tracked,
    # documented gap; a synthesized-only pair can never SILENTLY satisfy the phase.
    coverage = _real_capture_coverage()
    assert set(coverage) == _REQUIRED_ADAPTER_KINDS  # every rule maps to a required pair
    for key in _REQUIRED_ADAPTER_KINDS:
        if key in _UNCAPTURED_PENDING_LIVE:
            continue  # tracked gap (surfaced as an upstream/phase conflict)
        assert coverage[key], (
            f"required coverage {key} has no real-captured fixture and is not a "
            "declared gap; harvest+redact a live envelope or add it to "
            "_UNCAPTURED_PENDING_LIVE with justification (F-002 / plan.md:33)"
        )


def test_uncaptured_gap_set_is_exactly_the_pairs_missing_a_live_capture():
    # The tracked-gap set must stay honest: it is EXACTLY the pairs with no live
    # capture. A newly synthesized-only pair (a silent regression) makes this fail
    # loudly; obtaining a real capture for a listed pair also fails it until the
    # pair is removed from _UNCAPTURED_PENDING_LIVE — so the record can neither
    # over- nor under-state real coverage (F-002).
    coverage = _real_capture_coverage()
    actually_uncaptured = {k for k, has_real in coverage.items() if not has_real}
    assert actually_uncaptured == _UNCAPTURED_PENDING_LIVE


def test_real_captured_usage_limit_envelopes_are_transient():
    # The three envelopes harvested from live failed runs (real_capture=True).
    claude = json.loads((FIXTURES / "claude/usage-limit.json").read_text())
    assert fm.classify_claude_failure(claude, 1).kind == FAILURE_TRANSIENT_USAGE_LIMIT
    claude_ov = json.loads((FIXTURES / "claude/overload.json").read_text())
    assert fm.classify_claude_failure(claude_ov, 1).kind == FAILURE_TRANSIENT_OVERLOAD
    codex = json.loads((FIXTURES / "codex/usage-limit.json").read_text())
    assert (
        fm.classify_codex_failure(codex["events"], 1).kind
        == FAILURE_TRANSIENT_USAGE_LIMIT
    )


def test_typed_envelope_with_only_an_unlisted_message_is_terminal():
    # A real is_error envelope whose only signal is an unrecognized message
    # (the captured "Connection closed" case) fails closed to terminal (§7).
    neg = json.loads((FIXTURES / "claude/terminal-connection-closed.json").read_text())
    assert fm.classify_claude_failure(neg, 1).kind == FAILURE_TERMINAL
    # A codex turn.failed whose message matches no pinned phrasing is terminal.
    codex_unknown = [
        {"type": "turn.failed", "error": {"message": "disk full writing scratch file"}}
    ]
    assert fm.classify_codex_failure(codex_unknown, 1).kind == FAILURE_TERMINAL


def test_agent_error_event_503_classifies_provider_unavailable():
    # Issue #96: provider unavailability surfaced through the agent's own
    # error event (codex's websocket retry loop giving up against an upstream
    # 503) must classify to the dependency kind — routing into the persisted
    # retry budget and the R7 provider_unavailable park — never
    # terminal/unmatched, which forces a human `--response` for pure
    # infrastructure. The message is the live capture from the #96 run.
    events = [
        {"type": "thread.started", "thread_id": "th_abc123"},
        {"type": "error",
         "message": "Reconnecting... 2/5 (unexpected status 503 "
                    "Service Unavailable)"},
    ]
    info = fm.classify_codex_failure(events, 1)
    assert info.kind == FAILURE_TRANSIENT_DEPENDENCY
    assert info.marker == "codex_provider_unavailable_message"


@pytest.mark.parametrize("message", [
    "unexpected status 502 Bad Gateway",
    "upstream returned 503 Service Unavailable",
    "Reconnecting... 5/5 (stream closed)",
    "stream disconnected: connection reset by peer",
])
def test_provider_unavailability_signatures_in_error_message(message):
    # The pinned 5xx / Service Unavailable / reconnect-exhaustion /
    # connection-reset signatures all classify dependency-transient whether
    # they arrive via `turn.failed` or the bare-message error event.
    events = [{"type": "turn.failed", "error": {"message": message}}]
    info = fm.classify_codex_failure(events, 1)
    assert info.kind == FAILURE_TRANSIENT_DEPENDENCY
    assert info.marker == "codex_provider_unavailable_message"


@pytest.mark.parametrize("message", [
    "disk full writing scratch file",
    "user asked about reconnecting the printer",
    "unexpected status 404 Not Found",
])
def test_provider_unavailable_rule_does_not_absorb_unlisted_errors(message):
    # Fail closed: prose that merely mentions reconnecting (no n/m retry
    # counter), a client-side 4xx, and genuinely unlisted errors all stay
    # terminal — the #96 rule must not widen the allowlist past provider
    # unavailability.
    events = [{"type": "error", "message": message}]
    assert fm.classify_codex_failure(events, 1).kind == FAILURE_TERMINAL


def test_unknown_or_unstructured_error_is_terminal():
    # No result event / no failure event at all → terminal (fail closed).
    assert fm.classify_claude_failure(None, 1).kind == FAILURE_TERMINAL
    assert fm.classify_codex_failure([], 1).kind == FAILURE_TERMINAL


def test_api_exception_class_classification():
    class RateLimitError(Exception):
        retry_after = 42

    class InternalServerError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    rl = fm.classify_api_failure(RateLimitError("429"))
    assert rl.kind == FAILURE_TRANSIENT_USAGE_LIMIT and rl.retry_after_s == 42
    ov = fm.classify_api_failure(InternalServerError("Overloaded"))
    assert ov.kind == FAILURE_TRANSIENT_OVERLOAD
    # An unrecognized exception class fails closed to terminal.
    assert fm.classify_api_failure(AuthenticationError("bad key")).kind == FAILURE_TERMINAL


def test_retry_after_only_from_structured_field_never_prose():
    # The claude/codex usage-limit prose says "resets 5:40pm" / "try again at
    # 2:57 AM" — never scraped (§7): retry_after_s stays None.
    claude = json.loads((FIXTURES / "claude/usage-limit.json").read_text())
    assert fm.classify_claude_failure(claude, 1).retry_after_s is None
    codex = json.loads((FIXTURES / "codex/usage-limit.json").read_text())
    assert fm.classify_codex_failure(codex["events"], 1).retry_after_s is None


def test_failure_info_is_transient_property():
    assert FailureInfo(kind=FAILURE_TRANSIENT_USAGE_LIMIT).is_transient
    assert FailureInfo(kind=FAILURE_TRANSIENT_OVERLOAD).is_transient
    assert not FailureInfo(kind=FAILURE_TERMINAL).is_transient
    assert not FailureInfo().is_transient  # defaults to terminal (fail closed)


def test_session_not_found_regex():
    assert fm.looks_like_session_not_found("No conversation found for session x")
    assert fm.looks_like_session_not_found("session abc-123 not found")
    assert fm.looks_like_session_not_found("Could not resume the session")
    assert not fm.looks_like_session_not_found("API Error: Overloaded")
    assert not fm.looks_like_session_not_found(None)

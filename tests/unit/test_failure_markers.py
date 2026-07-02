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

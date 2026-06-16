"""Judge runner classifier-status messaging (FR-7.2).

A judge with no classifier model fails closed on everything the policy.yaml
fast-path does not match. That is correct, but it must be LOUD, not silent
(CLAUDE.md §2 "data over inference") — these tests pin the startup message so a
disabled classifier can never be mistaken for an enabled one.
"""

from __future__ import annotations

from gauntlet.judge.runner import classifier_status_message


def test_classifier_status_enabled_names_the_model():
    msg = classifier_status_message("gpt-5-mini")
    assert "enabled" in msg
    assert "gpt-5-mini" in msg
    assert "WARNING" not in msg


def test_classifier_status_disabled_warns_loudly():
    msg = classifier_status_message(None)
    assert "WARNING" in msg
    assert "FAIL CLOSED" in msg
    assert "--judge-model" in msg  # tells the operator exactly how to fix it

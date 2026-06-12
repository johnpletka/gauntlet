"""Judge decision ladder + audit: policy -> LLM -> fail-closed (FR-7.2)."""

import json
from pathlib import Path

import pytest

from gauntlet.adapters.base import AdapterError, AgentResult
from gauntlet.judge.classifier import LLMClassifier
from gauntlet.judge.core import JudgeCore
from gauntlet.judge.policy import Policy, PolicyEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY = REPO_ROOT / "policy.yaml"


def engine():
    return PolicyEngine(Policy.load(POLICY))


class FakeAdapter:
    """Stands in for an ApiAdapter; returns or raises a scripted result."""

    def __init__(self, structured=None, exc=None):
        self._structured = structured
        self._exc = exc
        self.calls = []

    def run(self, prompt, *, schema=None, **kw):
        self.calls.append(prompt)
        if self._exc is not None:
            raise self._exc
        return AgentResult(text="", structured=self._structured, exit_code=0)


def test_policy_deny_is_terminal_no_llm():
    adapter = FakeAdapter(structured={"decision": "allow", "risk_category": "x", "rationale": "y"})
    core = JudgeCore(engine(), classifier=LLMClassifier(adapter))
    d = core.decide("Bash", {"command": "rm -rf /"}, repo_root=REPO_ROOT)
    assert d.decision == "deny"
    assert d.source == "fast-path"
    assert adapter.calls == []  # LLM never consulted


def test_policy_allow_is_terminal_no_llm():
    adapter = FakeAdapter()
    core = JudgeCore(engine(), classifier=LLMClassifier(adapter))
    d = core.decide("Bash", {"command": "git status"}, repo_root=REPO_ROOT)
    assert d.decision == "allow"
    assert adapter.calls == []


def test_ask_routes_to_llm():
    adapter = FakeAdapter(
        structured={"decision": "allow", "risk_category": "package-install", "rationale": "safe"}
    )
    core = JudgeCore(engine(), classifier=LLMClassifier(adapter))
    d = core.decide("Bash", {"command": "pip install requests"}, repo_root=REPO_ROOT)
    assert d.source == "llm"
    assert d.decision == "allow"
    assert len(adapter.calls) == 1


def test_unmatched_routes_to_llm():
    adapter = FakeAdapter(
        structured={"decision": "deny", "risk_category": "unknown", "rationale": "weird"}
    )
    core = JudgeCore(engine(), classifier=LLMClassifier(adapter))
    d = core.decide("Bash", {"command": "telnet bbs.example.org"}, repo_root=REPO_ROOT)
    assert d.source == "llm"
    assert d.decision == "deny"


def test_no_classifier_fails_closed_on_unmatched():
    core = JudgeCore(engine(), classifier=None)
    d = core.decide("Bash", {"command": "telnet x"}, repo_root=REPO_ROOT)
    assert d.decision == "deny"
    assert d.source == "fail-closed"


def test_llm_error_fails_closed():
    adapter = FakeAdapter(exc=AdapterError("boom"))
    core = JudgeCore(engine(), classifier=LLMClassifier(adapter))
    d = core.decide("Bash", {"command": "telnet x"}, repo_root=REPO_ROOT)
    assert d.decision == "deny"
    assert d.source == "fail-closed"


def test_llm_invalid_output_fails_closed():
    adapter = FakeAdapter(structured={"decision": "maybe"})
    core = JudgeCore(engine(), classifier=LLMClassifier(adapter))
    d = core.decide("Bash", {"command": "telnet x"}, repo_root=REPO_ROOT)
    assert d.decision == "deny"
    assert d.source == "fail-closed"


def test_audit_line_written_and_redacted(tmp_path):
    audit = tmp_path / "judge-audit.jsonl"
    core = JudgeCore(engine(), audit_path=audit)
    core.decide(
        "Bash",
        {"command": "git status"},
        repo_root=REPO_ROOT,
        run_id="run1",
        step_id="step1",
    )
    core.decide("Bash", {"command": "rm -rf /"}, repo_root=REPO_ROOT)
    lines = audit.read_text().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["decision"] == "allow"
    assert first["source"] == "fast-path"
    assert first["run_id"] == "run1"
    assert "latency_ms" in first
    assert first["matched_rule"]
    assert json.loads(lines[1])["decision"] == "deny"


def test_classifier_adapter_bounded_under_hook_timeout():
    # F-007: the LLM rung must answer within the CLI hook timeout (8 s)
    from gauntlet.adapters.api import ApiAdapter
    from gauntlet.judge.hook_client import HOOK_TIMEOUT_S
    from gauntlet.judge.runner import JUDGE_LLM_TIMEOUT_S, build_core

    assert JUDGE_LLM_TIMEOUT_S < HOOK_TIMEOUT_S
    core = build_core(policy_path=POLICY, judge_model="test/model")
    adapter = core.classifier._adapter
    assert isinstance(adapter, ApiAdapter)
    assert adapter.timeout_s == JUDGE_LLM_TIMEOUT_S
    # gpt-5-family models reject any non-default temperature; passing temp=0
    # made every classifier call fail closed (notes #26). Latency is bounded
    # via minimal reasoning effort instead.
    assert adapter.temperature is None
    assert adapter.reasoning_effort == "minimal"
    assert adapter.max_tokens is not None
    # single attempt only, so worst case (1 x timeout) stays under the hook
    # timeout — no retry can push total latency past it (F-007 round 2)
    assert adapter.max_schema_retries == 0
    worst_case = adapter.timeout_s * (1 + adapter.max_schema_retries)
    assert worst_case < HOOK_TIMEOUT_S


def test_audit_redacts_secret_in_command(tmp_path, monkeypatch):
    from gauntlet.logging.redact import RedactingWriter, Redactor

    secret = "ghp_" + "Z" * 36
    writer = RedactingWriter(Redactor(env={}))
    audit = tmp_path / "a.jsonl"
    core = JudgeCore(engine(), audit_path=audit, writer=writer)
    core.decide(
        "Bash", {"command": f"echo {secret}"}, repo_root=REPO_ROOT
    )
    text = audit.read_text()
    assert secret not in text
    assert "[REDACTED:github-token]" in text

"""Hook client decision logic + degraded modes (FR-7.3/7.4; review F-004)."""

import json
import urllib.error

import pytest

from gauntlet.judge import hook_client


@pytest.fixture
def env():
    return {
        "GAUNTLET_JUDGE_URL": "http://127.0.0.1:9999",
        "GAUNTLET_JUDGE_TOKEN": "tok",
    }


def patch_judge(monkeypatch, result=None, exc=None):
    def fake(url, token, body):
        fake.body = body
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(hook_client, "_ask_judge", fake)
    return fake


def test_allow_decision(monkeypatch, env):
    patch_judge(monkeypatch, result={"decision": "allow", "rationale": "fine"})
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": "/repo"},
        env=env,
    )
    assert decision == "allow"
    assert code == 0


def test_deny_decision_exit_2(monkeypatch, env):
    patch_judge(monkeypatch, result={"decision": "deny", "rationale": "nope"})
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, env=env
    )
    assert decision == "deny"
    assert code == 2
    assert reason == "nope"


def test_payload_forwarded_with_context(monkeypatch):
    fake = patch_judge(monkeypatch, result={"decision": "allow", "rationale": "ok"})
    env = {
        "GAUNTLET_JUDGE_TOKEN": "tok",
        "GAUNTLET_RUN_ID": "run9",
        "GAUNTLET_STEP_ID": "step3",
    }
    hook_client.decide_from_payload(
        {"tool_name": "Write", "tool_input": {"file_path": "/repo/x"}, "cwd": "/repo"},
        env=env,
    )
    assert fake.body["tool_name"] == "Write"
    assert fake.body["repo_root"] == "/repo"
    assert fake.body["run_id"] == "run9"
    assert fake.body["step_id"] == "step3"


def test_unreachable_unattended_fails_closed(monkeypatch):
    patch_judge(monkeypatch, exc=urllib.error.URLError("conn refused"))
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        env={"GAUNTLET_JUDGE_MODE": "unattended", "GAUNTLET_JUDGE_TOKEN": "tok"},
    )
    assert decision == "deny"
    assert code == 2
    assert "failing closed" in reason


def test_unreachable_interactive_asks_with_warning(monkeypatch):
    patch_judge(monkeypatch, exc=urllib.error.URLError("conn refused"))
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        env={"GAUNTLET_JUDGE_MODE": "interactive", "GAUNTLET_JUDGE_TOKEN": "tok"},
    )
    assert decision == "ask"
    assert code == 0
    assert "UNREACHABLE" in reason


def test_invalid_decision_from_judge_fails_closed(monkeypatch, env):
    patch_judge(monkeypatch, result={"decision": "perhaps"})
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "x"}}, env=env
    )
    assert decision == "deny"
    assert "failing closed" in reason


def test_http_error_fails_closed_both_modes(monkeypatch, env):
    # F-002: HTTPError (401 bad/foreign token, 5xx) must deny, not degrade to ask
    import urllib.error

    err = urllib.error.HTTPError("u", 401, "unauthorized", {}, None)
    for mode in ("unattended", "interactive"):
        patch_judge(monkeypatch, exc=err)
        e = dict(env, GAUNTLET_JUDGE_MODE=mode)
        decision, reason, code = hook_client.decide_from_payload(
            {"tool_name": "Bash", "tool_input": {"command": "git status"}}, env=e
        )
        assert decision == "deny", mode
        assert code == 2
        assert "401" in reason


def test_list_payload_fails_closed(env):
    # F-003: a JSON list payload must fail closed, not crash
    decision, reason, code = hook_client.decide_from_payload(["not", "a", "dict"], env=env)
    assert decision == "deny"
    assert code == 2


def test_non_dict_judge_response_fails_closed(monkeypatch, env):
    # F-003: /decide returning a list/string must fail closed
    patch_judge(monkeypatch, result=["unexpected"])
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "x"}}, env=env
    )
    assert decision == "deny"


def test_no_token_defers_to_ask(monkeypatch):
    # F-006: with no judge token configured, defer to normal handling (ask),
    # never deny — a plain session must not be bricked. Judge is not consulted.
    called = {"v": False}

    def fake(url, token, body):
        called["v"] = True
        return {"decision": "allow"}

    monkeypatch.setattr(hook_client, "_ask_judge", fake)
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        env={},  # no GAUNTLET_JUDGE_TOKEN
    )
    assert decision == "ask"
    assert code == 0
    assert called["v"] is False  # judge not even contacted


def test_emit_deny_writes_json_and_stderr(capsys):
    code = hook_client._emit("deny", "blocked because reasons")
    assert code == 2
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert payload["hookSpecificOutput"]["permissionDecisionReason"] == "blocked because reasons"
    assert "blocked because reasons" in out.err


def test_emit_allow_exit_0(capsys):
    code = hook_client._emit("allow", "ok")
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_main_denies_on_invalid_json(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _FakeStdin("not json{"))
    code = hook_client.main()
    assert code == 2
    assert "failing closed" in capsys.readouterr().out


class _FakeStdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text

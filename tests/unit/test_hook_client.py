"""Hook client decision logic + degraded modes (FR-7.3/7.4; review F-004)."""

import json
import urllib.error

import pytest

from gauntlet.judge import hook_client


@pytest.fixture
def env():
    # An in-run env: GAUNTLET_RUN_ID marks "this is a gauntlet run", which is what
    # makes the hook consult the judge at all (the engine injects it alongside the
    # token/url). Without it the hook defers — see test_no_run_id_defers_*.
    return {
        "GAUNTLET_JUDGE_URL": "http://127.0.0.1:9999",
        "GAUNTLET_JUDGE_TOKEN": "tok",
        "GAUNTLET_RUN_ID": "run1",
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
        env={
            "GAUNTLET_JUDGE_MODE": "unattended",
            "GAUNTLET_JUDGE_TOKEN": "tok",
            "GAUNTLET_RUN_ID": "r",
        },
    )
    assert decision == "deny"
    assert code == 2
    assert "failing closed" in reason


def test_unreachable_interactive_asks_with_warning(monkeypatch):
    patch_judge(monkeypatch, exc=urllib.error.URLError("conn refused"))
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        env={
            "GAUNTLET_JUDGE_MODE": "interactive",
            "GAUNTLET_JUDGE_TOKEN": "tok",
            "GAUNTLET_RUN_ID": "r",
        },
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


def test_no_run_id_defers_even_with_global_token(monkeypatch):
    # The interactive-session fix: a global GAUNTLET_JUDGE_TOKEN (e.g. exported in
    # ~/.zshenv) must NOT make a non-run session consult the judge. The judge
    # gates ONLY gauntlet-run sessions, marked by GAUNTLET_RUN_ID. Without it we
    # DEFER and never contact the judge, so an unreachable/foreign judge can never
    # brick an interactive session (the bug fixed on sec/judge-interactive-fallback).
    called = {"v": False}

    def fake(url, token, body):
        called["v"] = True
        return {"decision": "deny"}

    monkeypatch.setattr(hook_client, "_ask_judge", fake)
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        env={"GAUNTLET_JUDGE_TOKEN": "global-tok"},  # token but NO run id
    )
    assert decision == "defer"
    assert code == 0
    assert "not a gauntlet run" in reason
    assert called["v"] is False  # judge not even contacted


def test_in_run_without_token_defers(monkeypatch):
    # F-006 (now secondary): inside a run (GAUNTLET_RUN_ID set) but with no token
    # we cannot authenticate, so DEFER rather than call with an empty token. The
    # engine always injects both together, so this is a should-not-happen guard.
    called = {"v": False}

    def fake(url, token, body):
        called["v"] = True
        return {"decision": "allow"}

    monkeypatch.setattr(hook_client, "_ask_judge", fake)
    decision, reason, code = hook_client.decide_from_payload(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}},
        env={"GAUNTLET_RUN_ID": "r"},  # run id but no token
    )
    assert decision == "defer"
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


def test_emit_defer_writes_nothing_and_exit_0(capsys):
    # "defer" emits NO permissionDecision, so the CLI's own permission flow runs
    # unchanged. Emitting anything here (especially "ask") would override the
    # user's settings and force a prompt on every call — the bug this guards.
    code = hook_client._emit("defer", "no judge configured")
    assert code == 0
    out = capsys.readouterr()
    assert out.out == ""  # nothing on stdout -> hook expresses no opinion
    assert out.err == ""


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


def test_repo_root_prefers_engine_env_over_agent_cwd(monkeypatch):
    # P5 deny-loop regression (notes #29): an agent working from a scratch/toy
    # directory must not have its in-repo edits judged against that cwd. The
    # engine-injected GAUNTLET_REPO_ROOT defines the boundary for the run.
    fake = patch_judge(monkeypatch, result={"decision": "allow", "rationale": "ok"})
    env = {
        "GAUNTLET_JUDGE_TOKEN": "tok",
        "GAUNTLET_RUN_ID": "r",
        "GAUNTLET_REPO_ROOT": "/real/repo",
    }
    hook_client.decide_from_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": "/real/repo/src/x.py"},
         "cwd": "/tmp/toy-project"},
        env=env,
    )
    assert fake.body["repo_root"] == "/real/repo"


def test_repo_root_falls_back_to_cwd_without_engine_env(monkeypatch):
    fake = patch_judge(monkeypatch, result={"decision": "allow", "rationale": "ok"})
    env = {"GAUNTLET_JUDGE_TOKEN": "tok", "GAUNTLET_RUN_ID": "r"}
    hook_client.decide_from_payload(
        {"tool_name": "Edit", "tool_input": {"file_path": "/repo/x"}, "cwd": "/repo"},
        env=env,
    )
    assert fake.body["repo_root"] == "/repo"

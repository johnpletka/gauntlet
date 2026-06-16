"""Engine-managed judge env teardown (review F-009).

stop() must restore EVERY managed GAUNTLET_* var — including the per-step
GAUNTLET_STEP_ID the orchestrator sets — to its pre-run value, so nothing leaks
into the parent session on success or failure.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gauntlet.engine import judgeproc
from gauntlet.engine.judgeproc import (
    _MANAGED_ENV_VARS,
    ManagedJudge,
    classifier_disabled_warning,
)
from gauntlet.judge.hook_client import STEP_ID_ENV_VAR
from gauntlet.judge.service import TOKEN_ENV_VAR


def test_classifier_disabled_warning_is_actionable():
    # The engine-managed judge's parallel to the standalone warning: when no
    # judge_llm model is configured, the classifier is disabled and the run
    # fails closed off the fast-path. The remedy is config-shaped, not a flag.
    msg = classifier_disabled_warning()
    assert "WARNING" in msg
    assert "FAIL CLOSED" in msg
    assert "judge_llm" in msg


class _FakeProc:
    returncode = 0

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _judge() -> ManagedJudge:
    mj = ManagedJudge(policy_path=Path("policy.yaml"), audit_path=Path("a.jsonl"), run_id="r")
    mj._proc = _FakeProc()
    return mj


def test_stop_clears_unset_vars_including_step_id():
    mj = _judge()
    mj._env_snapshot = {v: None for v in _MANAGED_ENV_VARS}
    os.environ[TOKEN_ENV_VAR] = "tok"
    os.environ[STEP_ID_ENV_VAR] = "implement"  # set by the orchestrator
    try:
        mj.stop()
        assert TOKEN_ENV_VAR not in os.environ
        assert STEP_ID_ENV_VAR not in os.environ
        for var in _MANAGED_ENV_VARS:
            assert var not in os.environ
    finally:
        for v in _MANAGED_ENV_VARS:
            os.environ.pop(v, None)


def test_stop_restores_prior_values():
    mj = _judge()
    os.environ[TOKEN_ENV_VAR] = "outer-token"
    mj._env_snapshot = {v: None for v in _MANAGED_ENV_VARS}
    mj._env_snapshot[TOKEN_ENV_VAR] = "outer-token"  # pre-run value
    os.environ[TOKEN_ENV_VAR] = "run-token"  # overwritten during the run
    try:
        mj.stop()
        assert os.environ[TOKEN_ENV_VAR] == "outer-token"  # restored, not deleted
    finally:
        os.environ.pop(TOKEN_ENV_VAR, None)


# --- free-port coexistence with an already-running judge -------------------------
@pytest.fixture
def clean_managed_env():
    """Save/restore every managed GAUNTLET_* var so a test can set them freely."""
    saved = {v: os.environ.get(v) for v in _MANAGED_ENV_VARS}
    for v in _MANAGED_ENV_VARS:
        os.environ.pop(v, None)
    try:
        yield
    finally:
        for v, val in saved.items():
            if val is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = val


def test_spawn_moves_off_taken_port(monkeypatch, clean_managed_env):
    # The default port is busy (e.g. the operator's own standalone judge) → the
    # run spawns its OWN run-scoped judge on a free port instead of colliding and
    # dying ("judge exited during startup"). The run never attaches to that other
    # judge (PR #16 review: that would lose the run's policy/audit/repo_root).
    captured: dict = {}

    class _Proc:
        returncode = 0

        def poll(self):
            return None

    monkeypatch.setattr(ManagedJudge, "_port_is_free", staticmethod(lambda h, p: False))
    monkeypatch.setattr(ManagedJudge, "_free_port", staticmethod(lambda h: 54321))
    monkeypatch.setattr(ManagedJudge, "_await_healthy", lambda self: None)

    def _popen(argv, env=None):
        captured["argv"] = argv
        return _Proc()

    monkeypatch.setattr(judgeproc.subprocess, "Popen", _popen)

    os.environ[TOKEN_ENV_VAR] = "operator-global-token"  # port conflict → NOT reused
    mj = ManagedJudge(policy_path=Path("p.yaml"), audit_path=Path("a.jsonl"), run_id="r")
    try:
        mj.start()
        assert mj.port == 54321
        assert "54321" in captured["argv"]
        assert mj.url == "http://127.0.0.1:54321"
        # On a port CONFLICT the run keeps its freshly minted token: the listener
        # on the default port is someone else's judge, so reusing the operator's
        # global token for our moved-port judge would be misleading.
        assert mj.token != "operator-global-token"
    finally:
        mj._proc = None  # don't let teardown touch the fake proc
        for v in _MANAGED_ENV_VARS:
            os.environ.pop(v, None)


def _patch_spawn(monkeypatch, captured):
    class _Proc:
        returncode = 0

        def poll(self):
            return None

    monkeypatch.setattr(ManagedJudge, "_await_healthy", lambda self: None)

    def _popen(argv, env=None):
        captured["argv"] = argv
        captured["env"] = env
        return _Proc()

    monkeypatch.setattr(judgeproc.subprocess, "Popen", _popen)


def test_spawn_reuses_global_token_when_port_free(monkeypatch, clean_managed_env):
    # No port conflict + a global GAUNTLET_JUDGE_TOKEN already in the env (e.g.
    # exported in ~/.zshenv) → the run adopts THAT token instead of minting a
    # fresh one, so a manually-started judge / external tooling sharing it stays
    # consistent with the run's judge. The run still starts its OWN judge on the
    # default port; only the token value is reused.
    captured: dict = {}
    monkeypatch.setattr(ManagedJudge, "_port_is_free", staticmethod(lambda h, p: True))
    _patch_spawn(monkeypatch, captured)
    os.environ[TOKEN_ENV_VAR] = "operator-global-token"

    mj = ManagedJudge(policy_path=Path("p.yaml"), audit_path=Path("a.jsonl"), run_id="r")
    try:
        injected = mj.start()
        assert mj.port == judgeproc.DEFAULT_PORT  # stayed on the default port
        assert mj.token == "operator-global-token"  # reused, not minted
        assert injected[TOKEN_ENV_VAR] == "operator-global-token"
        assert captured["env"][TOKEN_ENV_VAR] == "operator-global-token"
    finally:
        mj._proc = None
        for v in _MANAGED_ENV_VARS:
            os.environ.pop(v, None)


def test_spawn_mints_token_when_port_free_but_no_global_token(monkeypatch, clean_managed_env):
    # No port conflict and NO global token set → mint a fresh per-run token as
    # before (clean_managed_env clears GAUNTLET_JUDGE_TOKEN). Guards against
    # accidentally injecting an empty token when the operator hasn't exported one.
    captured: dict = {}
    monkeypatch.setattr(ManagedJudge, "_port_is_free", staticmethod(lambda h, p: True))
    _patch_spawn(monkeypatch, captured)

    mj = ManagedJudge(policy_path=Path("p.yaml"), audit_path=Path("a.jsonl"), run_id="r")
    try:
        injected = mj.start()
        assert mj.port == judgeproc.DEFAULT_PORT
        assert mj.token  # a non-empty minted token
        assert injected[TOKEN_ENV_VAR] == mj.token
    finally:
        mj._proc = None
        for v in _MANAGED_ENV_VARS:
            os.environ.pop(v, None)

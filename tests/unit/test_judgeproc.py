"""Engine-managed judge env teardown (review F-009).

stop() must restore EVERY managed GAUNTLET_* var — including the per-step
GAUNTLET_STEP_ID the orchestrator sets — to its pre-run value, so nothing leaks
into the parent session on success or failure.
"""

from __future__ import annotations

import os
from pathlib import Path

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

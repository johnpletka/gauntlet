"""Engine-managed judge env teardown (review F-009).

stop() must restore EVERY managed GAUNTLET_* var — including the per-step
GAUNTLET_STEP_ID the orchestrator sets — to its pre-run value, so nothing leaks
into the parent session on success or failure.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gauntlet.engine import judgeproc
from gauntlet.engine.judgeproc import (
    _MANAGED_ENV_VARS,
    JUDGE_RECORD_NAME,
    JudgeRecord,
    ManagedJudge,
    classifier_disabled_warning,
    operator_session_env,
    read_judge_record,
)
from gauntlet.judge.hook_client import (
    MODE_ENV_VAR,
    RUN_ID_ENV_VAR,
    STEP_ID_ENV_VAR,
    URL_ENV_VAR,
)
from gauntlet.judge.service import TOKEN_ENV_VAR
from gauntlet.procident import ProcessIdentity


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

    def _popen(argv, env=None, **kwargs):
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

    def _popen(argv, env=None, **kwargs):
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


# --- judge.json lifecycle (FR-5, §6.2) -------------------------------------------
class _LiveProc:
    """A stand-in subprocess whose pid is THIS test process — so getpgid and
    read_process_identity resolve a real, live identity for the record."""

    returncode = 0

    def __init__(self):
        self.pid = os.getpid()

    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _spawn_live(monkeypatch, run_dir):
    monkeypatch.setattr(ManagedJudge, "_port_is_free", staticmethod(lambda h, p: True))
    monkeypatch.setattr(ManagedJudge, "_await_healthy", lambda self: None)
    monkeypatch.setattr(
        judgeproc.subprocess, "Popen", lambda argv, env=None, **kwargs: _LiveProc()
    )
    # Stub the identity read so the record write does not shell out to `ps`
    # (which the patched Popen would otherwise intercept). A real platform-tagged
    # identity is returned so proc_identity serialises like production.
    monkeypatch.setattr(
        judgeproc,
        "read_process_identity",
        lambda pid: ProcessIdentity(platform="darwin", value=1750000000, unit="epoch_seconds"),
    )
    return ManagedJudge(
        policy_path=Path("p.yaml"),
        audit_path=Path("a.jsonl"),
        run_id="run-2026-06-25T16-41-22",
        run_dir=run_dir,
    )


def test_judge_record_written_on_start_removed_on_clean_stop(
    monkeypatch, clean_managed_env, tmp_path
):
    # FR-5.1: judge.json exists with the recorded identity/endpoint fields while
    # the judge runs and is absent after a clean stop().
    mj = _spawn_live(monkeypatch, tmp_path)
    mj.start()
    try:
        rec = read_judge_record(tmp_path)
        assert rec is not None
        assert rec.pid == os.getpid()
        assert rec.pgid == os.getpgid(os.getpid())
        assert rec.proc_identity is not None  # darwin/linux test host
        assert rec.host  # the machine hostname (FR-6 host-match datum)
        assert rec.port == judgeproc.DEFAULT_PORT
        assert rec.url == mj.url
        assert rec.token == mj.token
        assert rec.run_id == "run-2026-06-25T16-41-22"
        assert rec.started_at
    finally:
        mj.stop()
    # Clean stop removes the sidecar — it must not outlive the judge (FR-5.1).
    assert not (tmp_path / JUDGE_RECORD_NAME).exists()
    assert read_judge_record(tmp_path) is None


def test_judge_spawned_in_own_session_group(monkeypatch, clean_managed_env, tmp_path):
    # F-001 regression: ManagedJudge must launch the judge in its OWN session/
    # process group. Otherwise the judge inherits the driver/console group, the
    # recorded pgid names that shared group, and the FR-6 reaper's group-wide
    # SIGTERM/SIGKILL kills unrelated siblings (FR-6.3 violation). Drive the real
    # subprocess.Popen (keeping production kwargs intact) but swap the judge argv
    # for a harmless sleeper so no real judge service is needed.
    real_popen = judgeproc.subprocess.Popen
    spawned: list[subprocess.Popen] = []

    def spawn_benign(argv, env=None, **kwargs):
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            env=env,
            **kwargs,  # carries the production start_new_session through verbatim
        )
        spawned.append(proc)
        return proc

    monkeypatch.setattr(ManagedJudge, "_port_is_free", staticmethod(lambda h, p: True))
    monkeypatch.setattr(ManagedJudge, "_await_healthy", lambda self: None)
    monkeypatch.setattr(judgeproc.subprocess, "Popen", spawn_benign)
    mj = ManagedJudge(
        policy_path=Path("p.yaml"),
        audit_path=Path("a.jsonl"),
        run_id="run-2026-06-25T16-41-22",
        run_dir=tmp_path,
    )
    try:
        mj.start()
        rec = read_judge_record(tmp_path)
        assert rec is not None
        # The judge leads its own session: pgid == pid, and — crucially — NOT the
        # driver/test-process group the reaper would otherwise be steered to kill.
        assert rec.pgid == rec.pid
        assert rec.pgid != os.getpgid(os.getpid())
    finally:
        for proc in spawned:
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        mj._proc = None


def test_judge_record_mode_is_0600(monkeypatch, clean_managed_env, tmp_path):
    # FR-5.1: the per-run judge token at rest is 0600 (§7 token-at-rest).
    mj = _spawn_live(monkeypatch, tmp_path)
    mj.start()
    try:
        mode = (tmp_path / JUDGE_RECORD_NAME).stat().st_mode & 0o777
        assert mode == 0o600
    finally:
        mj._proc = None  # skip the fake-proc teardown signalling


def test_judge_record_write_failure_does_not_abort_run(
    monkeypatch, clean_managed_env, tmp_path
):
    # FR-5.2: a judge.json write failure is best-effort — the run proceeds and
    # the judge stays healthy (it is up in-process regardless). Point the run
    # dir at a *file* so opening run_dir/judge.json raises NotADirectoryError.
    not_a_dir = tmp_path / "notadir"
    not_a_dir.write_text("x")
    mj = _spawn_live(monkeypatch, not_a_dir)
    try:
        env = mj.start()  # must not raise
        assert mj._proc is not None  # judge is up
        assert env[RUN_ID_ENV_VAR] == "run-2026-06-25T16-41-22"
        assert not (not_a_dir / JUDGE_RECORD_NAME).exists()
    finally:
        mj._proc = None


def test_no_record_written_without_run_dir(monkeypatch, clean_managed_env, tmp_path):
    # A standalone judge (no run_dir) writes no sidecar and does not blow up.
    monkeypatch.setattr(ManagedJudge, "_port_is_free", staticmethod(lambda h, p: True))
    monkeypatch.setattr(ManagedJudge, "_await_healthy", lambda self: None)
    monkeypatch.setattr(
        judgeproc.subprocess, "Popen", lambda argv, env=None, **kwargs: _LiveProc()
    )
    mj = ManagedJudge(policy_path=Path("p.yaml"), audit_path=Path("a.jsonl"), run_id="r")
    try:
        mj.start()  # run_dir is None → no record path, no write
    finally:
        mj._proc = None


# --- operator-session env contract (§6.3) ----------------------------------------
def _record() -> JudgeRecord:
    return JudgeRecord(
        pid=123,
        pgid=123,
        proc_identity={"platform": "darwin", "value": 1, "unit": "epoch_seconds"},
        host="h",
        port=8787,
        url="http://127.0.0.1:8787",
        token="per-run-judge-token",
        run_id="run-x",
        started_at="2026-06-25T16-41-22",
    )


def test_operator_session_env_omits_step_id():
    # §6.3 / §1.3: the operator session sets EXACTLY RUN_ID + judge URL/TOKEN +
    # interactive MODE, and deliberately omits GAUNTLET_STEP_ID — its absence is
    # what marks the call as the operator's own session (the load-bearing
    # classification, FR-10). MODE is interactive so an unreachable (run-ended)
    # judge degrades to an ask-prompt instead of bricking the operator session
    # with an unattended fail-closed deny (review F-004).
    env = operator_session_env(_record())
    assert env == {
        RUN_ID_ENV_VAR: "run-x",
        URL_ENV_VAR: "http://127.0.0.1:8787",
        TOKEN_ENV_VAR: "per-run-judge-token",
        MODE_ENV_VAR: "interactive",
    }
    assert STEP_ID_ENV_VAR not in env


def test_judge_record_round_trips_and_rejects_malformed(tmp_path):
    rec = _record()
    assert JudgeRecord.from_json(rec.to_json()) == rec
    # A missing field, a non-dict proc_identity, and non-JSON each fail closed.
    assert JudgeRecord.from_json('{"pid": 1}') is None
    assert JudgeRecord.from_json("not json") is None
    assert read_judge_record(tmp_path) is None  # no file → None (degraded path)

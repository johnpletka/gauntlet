"""The unattended resume sweep (#134, rec. 1b).

The decision is a pure table over the composite state, the lock proof, the
schedule and the config; the I/O paths (lock proof over real lockfiles, the
under-lock stamp, the launch) are exercised against a RunManager on a temp
run root with a stub launcher, so no driver is ever spawned.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine import recovery_exec as RX
from gauntlet.engine import sweep as SW
from gauntlet.engine.config import RunConfig
from gauntlet.engine.locking import DRIVING_LOCK_NAME, LockRecord
from gauntlet.engine.manifest import Manifest, PipelineRef, ScheduledResume, StepRecord
from gauntlet.engine.run import RunManager

T0 = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
THIS_HOST = __import__("socket").gethostname()


# --- helpers -----------------------------------------------------------------
def _manifest(status: str = M.RUN_PARKED, steps: list[StepRecord] | None = None) -> Manifest:
    return Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
        status=status, steps=list(steps or []),
        current_step=steps[-1].id if steps else None,
    )


def _parked(reason: str, *, step_type: str = "agent_task", sched: ScheduledResume | None = None,
            step_status: str = M.PARKED) -> Manifest:
    return _manifest(M.RUN_PARKED, [StepRecord(
        id="implement", type=step_type, status=step_status, parked_reason=reason,
        scheduled_resume=sched,
    )])


def _running() -> Manifest:
    return _manifest(M.RUN_RUNNING, [StepRecord(id="implement", type="agent_task",
                                                status=M.RUNNING)])


def _due(attempts: int = 0, max_attempts: int = 3) -> ScheduledResume:
    return ScheduledResume(attempt_at=(T0 - timedelta(hours=1)).isoformat(),
                           attempts=attempts, max_attempts=max_attempts)


def _cfg(**kw) -> RunConfig:
    base = {"keep_awake": True, "run_root": "runs"}
    base.update(kw)
    return RunConfig.model_validate(base)


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()  # reaped → os.kill raises ProcessLookupError
    return proc.pid


def _write_lock(path: Path, *, pid: int, slug: str = "demo") -> None:
    rec = LockRecord(nonce="n", slug=slug, run_id="run-1", pid=pid, pgid=pid,
                     started_at="2026-09-03T11-00-00", host=THIS_HOST, proc_identity=None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rec.to_json())


AUTO = _cfg(resume_on_quota="auto")
NOTIFY = _cfg()


# --- the pure decision table --------------------------------------------------
@pytest.mark.parametrize(
    "man, liveness, lock, config, act, needle",
    [
        # (a) orphan reclaim needs a PROVEN-dead lock
        (_running(), op.LIVENESS_ORPHANED, SW.LOCK_DEAD, NOTIFY, True, SW.REASON_ORPHAN),
        (_running(), op.LIVENESS_NONE, SW.LOCK_ABSENT, NOTIFY, False, "no drive lock to prove"),
        (_running(), op.LIVENESS_ORPHANED, SW.LOCK_MALFORMED, NOTIFY, False, "malformed"),
        (_running(), op.LIVENESS_ORPHANED, SW.LOCK_LIVE, NOTIFY, False, "live lock holder"),
        (_running(), op.LIVENESS_ALIVE, SW.LOCK_LIVE, NOTIFY, False, "in_progress"),
        (_running(), op.LIVENESS_INDETERMINATE, SW.LOCK_LIVE, NOTIFY, False, "indeterminate"),
        # human-decision parks are never swept
        (_parked(M.PARKED_REASON_GATE, step_type="human_gate"), op.LIVENESS_NONE,
         SW.LOCK_ABSENT, AUTO, False, "awaiting a human"),
        (_parked(M.PARKED_REASON_RESPONSE), op.LIVENESS_NONE, SW.LOCK_ABSENT, AUTO,
         False, "awaiting a human"),
        (_parked(M.PARKED_REASON_ARTIFACT_INVALID), op.LIVENESS_NONE, SW.LOCK_ABSENT, AUTO,
         False, "awaiting a human"),
        # (b) a due schedule under its knob
        (_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()), op.LIVENESS_NONE,
         SW.LOCK_ABSENT, AUTO, True, SW.REASON_SCHEDULE),
        (_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()), op.LIVENESS_NONE,
         SW.LOCK_ABSENT, NOTIFY, False, "not `auto`"),
        (_parked(M.PARKED_REASON_USAGE_LIMIT), op.LIVENESS_NONE, SW.LOCK_ABSENT, AUTO,
         False, "no scheduled_resume"),
        (_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()), op.LIVENESS_ALIVE,
         SW.LOCK_LIVE, AUTO, False, "live driver is waiting"),
        (_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()), op.LIVENESS_INDETERMINATE,
         SW.LOCK_LIVE, AUTO, False, "cannot be proven"),
        (_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due(attempts=3)), op.LIVENESS_NONE,
         SW.LOCK_ABSENT, AUTO, False, "exhausted"),
        (_parked(M.PARKED_REASON_USAGE_LIMIT, sched=ScheduledResume(
            attempt_at=(T0 + timedelta(hours=2)).isoformat())), op.LIVENESS_NONE,
         SW.LOCK_ABSENT, AUTO, False, "not due"),
        # a provider park with no governing knob on this config → skip, never guess
        (_parked(M.PARKED_REASON_PROVIDER_UNAVAILABLE, sched=_due()), op.LIVENESS_NONE,
         SW.LOCK_ABSENT, SimpleNamespace(resume_on_quota="auto"), False,
         "resume_on_provider_unavailable"),
        (_parked(M.PARKED_REASON_PROVIDER_UNAVAILABLE, sched=_due()), op.LIVENESS_NONE,
         SW.LOCK_ABSENT, SimpleNamespace(resume_on_provider_unavailable="auto"), True,
         SW.REASON_SCHEDULE),
        # terminal / failure states
        (_manifest(M.RUN_DONE), op.LIVENESS_NONE, SW.LOCK_ABSENT, AUTO, False, "terminal"),
        (_manifest(M.RUN_FAILED, [StepRecord(id="tests", type="shell", status=M.FAILED)]),
         op.LIVENESS_NONE, SW.LOCK_ABSENT, AUTO, False, "failure state"),
        (_parked(M.PARKED_REASON_USAGE_LIMIT, step_status=M.HALTED), op.LIVENESS_NONE,
         SW.LOCK_ABSENT, AUTO, False, "failure state"),
    ],
)
def test_decision_table(man, liveness, lock, config, act, needle):
    d = SW.decide(man, liveness, lock=lock, config=config, now=T0)
    assert d.act is act, d
    assert needle in d.reason


def test_schedule_decision_names_step_and_reason():
    d = SW.decide(_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()), op.LIVENESS_NONE,
                  lock=SW.LOCK_ABSENT, config=AUTO, now=T0)
    assert d.step_id == "implement" and d.park_reason == M.PARKED_REASON_USAGE_LIMIT
    assert d.audit_reason == f"{SW.REASON_SCHEDULE}/{M.PARKED_REASON_USAGE_LIMIT}"


def test_auto_resume_knob_matrix():
    assert SW.auto_resume_knob(AUTO, M.PARKED_REASON_USAGE_LIMIT) == ("resume_on_quota", True)
    assert SW.auto_resume_knob(NOTIFY, M.PARKED_REASON_USAGE_LIMIT) == ("resume_on_quota", False)
    assert SW.auto_resume_knob(AUTO, M.PARKED_REASON_GATE) == (None, False)
    ns = SimpleNamespace()
    assert SW.auto_resume_knob(ns, M.PARKED_REASON_PROVIDER_UNAVAILABLE) == (
        "resume_on_provider_unavailable", False)


# --- lock proof over real lockfiles --------------------------------------------
def test_lock_proof_classes(tmp_path):
    run_root = tmp_path / "runs"
    run_dir = run_root / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    assert SW.lock_proof(run_root, run_dir, "demo") == SW.LOCK_ABSENT
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=_dead_pid())
    assert SW.lock_proof(run_root, run_dir, "demo") == SW.LOCK_DEAD
    # a dead FOREIGN record is not evidence about this run
    (run_dir / DRIVING_LOCK_NAME).unlink()
    _write_lock(run_root / DRIVING_LOCK_NAME, pid=_dead_pid(), slug="other")
    assert SW.lock_proof(run_root, run_dir, "demo") == SW.LOCK_ABSENT
    # a live (unverifiable-identity → live) holder anywhere in scope blocks
    _write_lock(run_dir.parent / DRIVING_LOCK_NAME, pid=os.getpid())
    assert SW.lock_proof(run_root, run_dir, "demo") == SW.LOCK_LIVE
    # malformed wins over everything (fail closed)
    (run_dir / DRIVING_LOCK_NAME).write_text("{not json")
    assert SW.lock_proof(run_root, run_dir, "demo") == SW.LOCK_MALFORMED


def test_enumerate_slugs_needs_a_run_instance(tmp_path):
    root = tmp_path / "runs"
    (root / "a" / "run-1").mkdir(parents=True)
    (root / "b").mkdir()  # no instance
    (root / ".hidden" / "run-1").mkdir(parents=True)
    assert SW.enumerate_slugs(root) == ["a"]
    assert SW.enumerate_slugs(tmp_path / "nope") == []


# --- I/O: stamp under lock + launch ------------------------------------------------
class _Harness:
    def __init__(self, tmp_path: Path, config: RunConfig):
        self.repo = tmp_path
        self.mgr = RunManager(tmp_path, config=config)
        self.run_root = tmp_path / "runs"
        self.run_dir = self.run_root / "demo" / "run-1"
        self.run_dir.mkdir(parents=True)
        (self.run_root / "demo" / "active-run.txt").write_text("run-1")
        self.launches: list[tuple[str, Path, str | None]] = []

    def save(self, man: Manifest) -> None:
        man.write_atomic(self.run_dir / "manifest.json")

    def load(self) -> Manifest:
        return Manifest.load(self.run_dir / "manifest.json")

    def launcher(self, mgr, slug, run_dir, run_id) -> str:
        self.launches.append((slug, run_dir, run_id))
        return "stub"


def test_due_schedule_is_fired_write_ahead_and_audited(tmp_path):
    h = _Harness(tmp_path, AUTO)
    h.save(_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()))
    out = SW.sweep_run(h.mgr, "demo", now=T0, launcher=h.launcher)
    assert out.action == SW.ACTION_RESUMED, out
    assert out.state == RX.STATE_PARKED_USAGE_LIMIT
    assert h.launches == [("demo", h.run_dir, "run-1")]
    man = h.load()
    assert man.record("implement").scheduled_resume.attempts == 1  # counted write-ahead
    assert any(w.startswith("unattended sweep resumed (scheduled_resume/usage_limit)")
               for w in man.warnings)
    # Each sweep is one attempt; the ceiling still holds across sweeps.
    SW.sweep_run(h.mgr, "demo", now=T0, launcher=h.launcher)
    SW.sweep_run(h.mgr, "demo", now=T0, launcher=h.launcher)
    out = SW.sweep_run(h.mgr, "demo", now=T0, launcher=h.launcher)
    assert out.action == SW.ACTION_SKIPPED and "exhausted" in out.reason
    assert len(h.launches) == 3
    assert h.load().record("implement").scheduled_resume.attempts == 3


def test_notify_knob_never_fires(tmp_path):
    h = _Harness(tmp_path, NOTIFY)
    h.save(_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()))
    out = SW.sweep_run(h.mgr, "demo", now=T0, launcher=h.launcher)
    assert out.action == SW.ACTION_SKIPPED and "not `auto`" in out.reason
    assert h.launches == [] and h.load().warnings == []


def test_orphan_with_dead_lock_is_reclaimed(tmp_path):
    h = _Harness(tmp_path, NOTIFY)
    h.save(_running())
    _write_lock(h.run_dir / DRIVING_LOCK_NAME, pid=_dead_pid())
    out = SW.sweep_run(h.mgr, "demo", now=T0, launcher=h.launcher)
    assert out.action == SW.ACTION_RESUMED and out.reason == SW.REASON_ORPHAN, out
    assert out.state == RX.STATE_ORPHANED
    assert h.launches == [("demo", h.run_dir, "run-1")]
    assert any("orphan_reclaim" in w for w in h.load().warnings)


def test_orphan_with_live_lock_is_left_alone(tmp_path):
    h = _Harness(tmp_path, NOTIFY)
    h.save(_running())
    _write_lock(h.run_dir / DRIVING_LOCK_NAME, pid=os.getpid())
    out = SW.sweep_run(h.mgr, "demo", now=T0, launcher=h.launcher)
    assert out.action == SW.ACTION_SKIPPED
    assert h.launches == []


def test_gate_park_is_skipped_untouched(tmp_path):
    h = _Harness(tmp_path, AUTO)
    h.save(_parked(M.PARKED_REASON_GATE, step_type="human_gate"))
    out = SW.sweep_run(h.mgr, "demo", now=T0, launcher=h.launcher)
    assert out.action == SW.ACTION_SKIPPED and "human" in out.reason
    assert h.launches == [] and h.load().warnings == []


def test_engine_refusal_is_reported_not_raised(tmp_path):
    h = _Harness(tmp_path, AUTO)
    h.save(_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()))

    def refusing(mgr, slug, run_dir, run_id):
        raise RuntimeError("lock lost to a concurrent driver")

    out = SW.sweep_run(h.mgr, "demo", now=T0, launcher=refusing)
    assert out.action == SW.ACTION_REFUSED
    assert "lock lost" in (out.detail or "")


def test_unresolvable_slug_is_a_skip(tmp_path):
    h = _Harness(tmp_path, AUTO)
    out = SW.sweep_run(h.mgr, "../etc", now=T0, launcher=h.launcher)
    assert out.action == SW.ACTION_SKIPPED and "unresolvable" in out.reason
    out = SW.sweep_run(h.mgr, "ghost", now=T0, launcher=h.launcher)
    assert out.action == SW.ACTION_SKIPPED


def test_sweep_slugs_never_short_circuits(tmp_path):
    h = _Harness(tmp_path, AUTO)
    h.save(_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()))
    outs = SW.sweep_slugs(h.mgr, ["ghost", "demo"], now=T0, launcher=h.launcher)
    assert [o.action for o in outs] == [SW.ACTION_SKIPPED, SW.ACTION_RESUMED]
    assert all(set(o.to_dict()) >= {"slug", "state", "action", "reason"} for o in outs)


# --- the console timer ----------------------------------------------------------------
def test_web_sweep_interval_config():
    from gauntlet.web.config import WebConfig

    assert WebConfig().sweep_interval_s == 120.0
    assert WebConfig.model_validate({"sweep_interval_s": 0}).sweep_interval_s == 0
    with pytest.raises(ValueError):
        WebConfig.model_validate({"sweep_interval_s": -1})


def test_console_sweep_launches_owned_driver(tmp_path):
    from gauntlet.web.service import console_sweep

    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text(
        "run_root: runs\nresume_on_quota: auto\nkeep_awake: true\n"
    )
    h = _Harness(tmp_path, AUTO)
    h.save(_parked(M.PARKED_REASON_USAGE_LIMIT, sched=_due()))
    resumed: list[str] = []

    class _Sup:
        def resume(self, slug):
            resumed.append(slug)
            return SimpleNamespace(pid=4242)

    store = SimpleNamespace(repo_root=tmp_path)
    outs = console_sweep(store, _Sup())
    assert [o.action for o in outs] == [SW.ACTION_RESUMED]
    assert resumed == ["demo"]
    assert "4242" in (outs[0].detail or "")


# --- the CLI verb ---------------------------------------------------------------------
def test_cli_sweep_requires_slug_xor_all(fixture_repo, monkeypatch):
    from typer.testing import CliRunner

    import gauntlet.cli as cli

    monkeypatch.chdir(fixture_repo)
    r = CliRunner().invoke(cli.app, ["sweep"])
    assert r.exit_code == 2
    r = CliRunner().invoke(cli.app, ["sweep", "demo", "--all"])
    assert r.exit_code == 2


def test_cli_sweep_json_reports_skip(fixture_repo, monkeypatch):
    from typer.testing import CliRunner

    import gauntlet.cli as cli
    from test_run_lifecycle import CONFIG_YAML

    (fixture_repo / ".gauntlet").mkdir()
    (fixture_repo / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    mgr = RunManager(fixture_repo)
    run_dir = fixture_repo / mgr.config.run_root / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir.parent / "active-run.txt").write_text("run-1")
    _parked(M.PARKED_REASON_GATE, step_type="human_gate").write_atomic(run_dir / "manifest.json")
    monkeypatch.chdir(fixture_repo)
    r = CliRunner().invoke(cli.app, ["sweep", "--all", "--json"])
    assert r.exit_code == 0, r.output
    data = json.loads(r.output)
    assert data[0]["slug"] == "demo" and data[0]["action"] == SW.ACTION_SKIPPED
    r = CliRunner().invoke(cli.app, ["sweep", "demo"])
    assert r.exit_code == 0
    assert "skipped" in r.output and "human" in r.output

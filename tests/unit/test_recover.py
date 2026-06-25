"""`gauntlet recover` — guarded, identity-checked termination (operator-aids P4).

The only mutating operator verb (FR-5). It is tested adversarially around its two
load-bearing properties:

* **Fail-closed identity gate (FR-5.1/FR-5.4):** it signals ONLY a fully-verified
  target — ownership + host equality + exact process-identity match + PID-in-PGID,
  all ANDed — and refuses with NO signal on any failed or unobtainable datum.
* **Crash-consistent, idempotent protocol (FR-5.6):** the nonce-/state-guarded
  sequence is safe to interrupt at every boundary and safe to re-run; a surviving
  intent is reconciled (finalized or discarded) on the next mutating entry point
  (`recover` or `resume`), never by read-only `status`.

Real processes (spawned in their own session) back the liveness/identity checks,
so PID-reuse safety is exercised honestly rather than against sentinel pids.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gauntlet.engine import execution, manifest as M
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.run import (
    DRIVING_LOCK_NAME,
    RECOVERY_INTENT_NAME,
    RecoverConcurrent,
    RecoverRefused,
    RecoverSignalError,
    RunManager,
    UnsafeRunSegment,
    WorktreeLockError,
    _LockRecord,
    _RecoveryIntent,
    _signal_process_group,
)
from gauntlet.procident import read_process_identity

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

MINI_PIPELINE = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: shell, run: "true"}
"""

DEAD_PID = 2_000_000_000  # never live; the kill -9'd / power-loss case
THIS_HOST = socket.gethostname()


@pytest.fixture(autouse=True)
def _supported_platform():
    if read_process_identity(os.getpid()) is None:
        pytest.skip("process identity unobtainable on this platform")


@pytest.fixture(autouse=True)
def _no_pipeline_ctx(monkeypatch):
    # `recover` refuses inside a pipeline-agent context; ensure the marker is
    # unset for every test except the one that asserts the refusal.
    monkeypatch.delenv("GAUNTLET_STEP_ID", raising=False)


@pytest.fixture
def procs():
    """Spawn session-leader subprocesses and reap them at teardown."""
    spawned: list[subprocess.Popen] = []

    def make() -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,  # own process group, pgid == pid
        )
        spawned.append(proc)
        return proc

    yield make
    for proc in spawned:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except OSError:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def _mgr(root: Path) -> RunManager:
    (root / ".gauntlet").mkdir(parents=True, exist_ok=True)
    (root / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    return RunManager(root)


def _setup_run(
    root: Path,
    *,
    slug: str = "demo",
    run_id: str = "run-1",
    step_status: str = M.RUNNING,
    run_status: str = M.RUN_RUNNING,
    step_id: str = "implement",
) -> Path:
    slug_dir = root / "runs" / slug
    run_dir = slug_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    man = Manifest(
        run_id=run_id,
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=run_status,
        current_step=step_id,
        steps=[StepRecord(id=step_id, type="agent_task", status=step_status)],
    )
    man.write_atomic(run_dir / "manifest.json")
    (slug_dir / "active-run.txt").write_text(run_id)
    return run_dir


def _ident(pid: int) -> dict | None:
    i = read_process_identity(pid)
    return i.to_dict() if i else None


def _write_lock(
    root: Path,
    *,
    pid: int,
    identity: dict | None,
    pgid: int | None = None,
    slug: str = "demo",
    run_id: str = "run-1",
    nonce: str = "nonce-1",
    host: str | None = None,
) -> _LockRecord:
    rec = _LockRecord(
        nonce=nonce,
        slug=slug,
        run_id=run_id,
        pid=pid,
        pgid=pid if pgid is None else pgid,
        started_at="2026-06-25T16-44-03",
        host=THIS_HOST if host is None else host,
        proc_identity=identity,
    )
    lp = root / "runs" / DRIVING_LOCK_NAME
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(rec.to_json())
    return rec


def _write_intent(run_dir: Path, *, pid: int, identity: dict | None, **kw) -> None:
    defaults = dict(
        ts="2026-06-25T16-44-03",
        actor="tester",
        actor_source="os_user",
        reason=None,
        lock_nonce="nonce-1",
        pgid=pid,
        host=THIS_HOST,
        step_id="implement",
        prior_step_status=M.RUNNING,
        prior_run_status=M.RUN_RUNNING,
    )
    defaults.update(kw)
    intent = _RecoveryIntent(pid=pid, proc_identity=identity, **defaults)
    (run_dir / RECOVERY_INTENT_NAME).write_text(intent.to_json())


def _lock_path(root: Path) -> Path:
    return root / "runs" / DRIVING_LOCK_NAME


# ---- FR-5.1 / FR-5.4: the identity gate refuses fail-closed, no signal -------


def test_recover_refuses_when_no_lock(tmp_path):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    with pytest.raises(RecoverRefused, match="no drive lock"):
        mgr.recover("demo")
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()
    assert Manifest.load(run_dir / "manifest.json").status == M.RUN_RUNNING


def test_recover_refuses_foreign_slug(tmp_path, procs):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid), slug="other")
    with pytest.raises(RecoverRefused, match="owned by 'other'"):
        mgr.recover("demo")
    assert proc.poll() is None  # not signalled
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


def test_recover_refuses_foreign_host(tmp_path, procs):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid), host="other-host")
    with pytest.raises(RecoverRefused, match="foreign-host"):
        mgr.recover("demo")
    assert proc.poll() is None
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


def test_recover_refuses_dead_pid(tmp_path):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_lock(tmp_path, pid=DEAD_PID, identity=_ident(os.getpid()))
    with pytest.raises(RecoverRefused, match="orphaned|resume"):
        mgr.recover("demo")
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


def test_recover_refuses_unverifiable_identity(tmp_path, procs):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    # Live pid but recorded proc_identity is null → liveness indeterminate.
    _write_lock(tmp_path, pid=proc.pid, identity=None)
    with pytest.raises(RecoverRefused, match="indeterminate|unverifiable"):
        mgr.recover("demo")
    assert proc.poll() is None
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


def test_recover_refuses_identity_mismatch(tmp_path, procs):
    mgr = _mgr(tmp_path)
    _setup_run(tmp_path)
    proc = procs()
    real = _ident(proc.pid)
    reused = {**real, "value": real["value"] + 1}  # PID reuse
    _write_lock(tmp_path, pid=proc.pid, identity=reused)
    with pytest.raises(RecoverRefused, match="orphaned|recycled"):
        mgr.recover("demo")
    assert proc.poll() is None


def test_recover_refuses_pgid_mismatch(tmp_path, procs):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    # Identity + host verify (liveness alive), but the recorded pgid is wrong, so
    # the PID-in-PGID gate refuses immediately before signalling (FR-5.1 #4).
    actual_pgid = os.getpgid(proc.pid)
    _write_lock(
        tmp_path, pid=proc.pid, identity=_ident(proc.pid), pgid=actual_pgid + 1
    )
    with pytest.raises(RecoverRefused, match="process group"):
        mgr.recover("demo")
    assert proc.poll() is None
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


# ---- FR-5.2 / FR-5.3: verified recovery terminates + marks INTERRUPTED -------


def _recover_with_reaper(mgr: RunManager, proc: subprocess.Popen, **kw) -> str:
    """Run `recover` while concurrently reaping ``proc``.

    pytest is the spawned child's parent, so a SIGTERM'd child lingers as a
    zombie (keeping ``killpg(pgid, 0)`` succeeding) until reaped — the reaper
    thread reaps it the instant it dies, so `recover`'s group-gone poll resolves
    promptly to a real `terminated_*` outcome instead of waiting out the grace.
    """
    reaper = threading.Thread(target=lambda: _safe_wait(proc), daemon=True)
    reaper.start()
    try:
        return mgr.recover("demo", **kw)
    finally:
        reaper.join(timeout=15)


def _safe_wait(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def test_recover_terminates_group_and_records(tmp_path, procs):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid), nonce="abc123")

    status = _recover_with_reaper(mgr, proc, reason="wedged on model timeout")

    assert status == M.RUN_FAILED
    assert proc.poll() is not None  # the group was terminated (FR-5.2)

    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_FAILED
    assert man.record("implement").status == M.INTERRUPTED
    assert len(man.recoveries) == 1
    rec = man.recoveries[0]
    assert rec.signal_outcome in (M.SIGNAL_TERMINATED_SIGTERM, M.SIGNAL_TERMINATED_SIGKILL)
    assert rec.lock_nonce == "abc123"
    assert rec.pid == proc.pid
    assert rec.pgid == proc.pid  # start_new_session ⇒ session leader, pgid == pid
    assert rec.actor_source == "os_user"
    assert rec.reason == "wedged on model timeout"
    assert rec.prior_step_status == M.RUNNING
    assert rec.prior_run_status == M.RUN_RUNNING
    assert rec.resulting_step_status == M.INTERRUPTED
    assert rec.resulting_run_status == M.RUN_FAILED

    assert not _lock_path(tmp_path).exists()  # lock released (FR-5.6 step 8)
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # intent cleared (step 7)


def test_recover_records_null_reason_when_omitted(tmp_path, procs):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid))
    _recover_with_reaper(mgr, proc)
    assert Manifest.load(run_dir / "manifest.json").recoveries[0].reason is None


def test_recover_leaves_resumable_and_second_recover_appends(tmp_path, procs):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid), nonce="nonce-1")
    _recover_with_reaper(mgr, proc)

    man = Manifest.load(run_dir / "manifest.json")
    # Resumable: a response-less `gauntlet resume` is accepted (no park error).
    assert mgr._plan_response_action(man, None).kind == "none"

    # Simulate the resume re-acquiring a fresh lock and a new step reaching
    # `running` (the only state in which a second recover is reachable, per
    # FR-5.3) — then a second recover APPENDS a second record, never replacing.
    proc2 = procs()
    rec_impl = man.record("implement")
    rec_impl.status = M.RUNNING
    man.status = M.RUN_RUNNING
    man.write_atomic(run_dir / "manifest.json")
    _write_lock(tmp_path, pid=proc2.pid, identity=_ident(proc2.pid), nonce="nonce-2")

    _recover_with_reaper(mgr, proc2)

    man2 = Manifest.load(run_dir / "manifest.json")
    assert len(man2.recoveries) == 2
    assert man2.recoveries[0].lock_nonce == "nonce-1"
    assert man2.recoveries[1].lock_nonce == "nonce-2"


# ---- FR-5.5: operator-only boundary -----------------------------------------


def test_recover_is_not_a_pipeline_step_type():
    # Mechanism 1: not registered, so no pipeline YAML can dispatch it.
    assert "recover" not in execution.step_specs()


def test_recover_refuses_inside_pipeline_context(tmp_path, procs, monkeypatch):
    # Mechanism 2: refuses fail-closed when GAUNTLET_STEP_ID is set, independent
    # of policy.yaml — an in-pipeline agent that shells out to it is refused.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid))
    monkeypatch.setenv("GAUNTLET_STEP_ID", "implement")
    with pytest.raises(RecoverRefused, match="operator-only"):
        mgr.recover("demo")
    assert proc.poll() is None  # no signal
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


# ---- FR-5.6: concurrency & crash-consistency --------------------------------


def test_recover_aborts_when_nonce_changes_before_signal(tmp_path, procs, monkeypatch):
    # (a) The lock's nonce changed between capture (step 1) and the pre-signal
    # re-read (step 3): the driver finished/relaunched → abort WITHOUT signalling.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    verified = _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid), nonce="nonce-1")
    changed = _LockRecord(
        nonce="nonce-2", slug=verified.slug, run_id=verified.run_id, pid=verified.pid,
        pgid=verified.pgid, started_at=verified.started_at, host=verified.host,
        proc_identity=verified.proc_identity,
    )
    reads = iter([verified, changed])  # step 1 capture, then step 3 re-read
    monkeypatch.setattr(mgr, "_read_lock", lambda: next(reads))

    with pytest.raises(RecoverConcurrent, match="completed or relaunched"):
        mgr.recover("demo")
    assert proc.poll() is None  # no signal
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()
    assert Manifest.load(run_dir / "manifest.json").status == M.RUN_RUNNING


def test_recover_aborts_when_step_not_running(tmp_path, procs):
    # (b) The target step is no longer `running` → no-mutation abort; never
    # overwrite a completed/terminal step status with INTERRUPTED.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path, step_status=M.DONE)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid))
    with pytest.raises(RecoverConcurrent, match="transitioned concurrently"):
        mgr.recover("demo")
    assert proc.poll() is None
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()
    man = Manifest.load(run_dir / "manifest.json")
    assert man.record("implement").status == M.DONE
    assert man.recoveries == []


def test_reconcile_finalizes_live_intent_absent_lock(tmp_path):
    # (e) Live disposition: lock ABSENT (verified target already killed, nothing
    # relaunched) → finalize. Idempotent re-run is a no-op (FR-5.6 step c).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-x")

    note = mgr._reconcile_recovery_intent(run_dir)
    assert "finalized" in note

    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_FAILED
    assert man.record("implement").status == M.INTERRUPTED
    assert len(man.recoveries) == 1
    assert man.recoveries[0].signal_outcome == M.SIGNAL_ALREADY_DEAD
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()

    # Idempotent: a second reconcile finds no intent and changes nothing.
    assert mgr._reconcile_recovery_intent(run_dir) is None
    assert len(Manifest.load(run_dir / "manifest.json").recoveries) == 1


def test_reconcile_discards_stale_intent_no_mutation(tmp_path):
    # (e) Stale disposition: lock PRESENT with a DIFFERENT nonce (a relaunched
    # driver holds a fresh lock) → discard, no signal, no manifest mutation.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-old")
    _write_lock(tmp_path, pid=DEAD_PID, identity=None, nonce="n-new")  # relaunched

    note = mgr._reconcile_recovery_intent(run_dir)
    assert "stale" in note and "discarded" in note

    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # discarded
    assert _lock_path(tmp_path).exists()  # the relaunched driver's lock survives
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_RUNNING  # no mutation
    assert man.record("implement").status == M.RUNNING
    assert man.recoveries == []


def test_recover_entrypoint_reconciles_live_intent(tmp_path):
    # (d1, live) Crash injected between step 5 (group dead) and step 6 (manifest
    # write): intent persisted, manifest still `running`, no record. A subsequent
    # `recover` reconciles it into the finalized state — then refuses the fresh
    # recovery (no lock to act on), but the reconciliation already landed.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-x")

    with pytest.raises(RecoverRefused, match="no drive lock"):
        mgr.recover("demo")

    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_FAILED
    assert man.record("implement").status == M.INTERRUPTED
    assert len(man.recoveries) == 1
    assert man.recoveries[0].signal_outcome == M.SIGNAL_ALREADY_DEAD
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


def test_recover_entrypoint_discards_stale_intent(tmp_path):
    # (d1, stale) Same crash window, but a relaunched driver holds a fresh
    # (different-nonce) lock: the next `recover` discards the stale intent without
    # mutating the manifest, then evaluates the present lock (a dead orphan here →
    # refuse). The stale intent is gone; the manifest is untouched.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-old")
    _write_lock(tmp_path, pid=DEAD_PID, identity=_ident(os.getpid()), nonce="n-new")

    with pytest.raises(RecoverRefused):
        mgr.recover("demo")

    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # discarded
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_RUNNING  # no mutation
    assert man.recoveries == []


def test_resume_entrypoint_reconciles_live_intent(tmp_path):
    # (d2, live) The same crash window is reconciled by the engine's resume path.
    # resume reconciles BEFORE acquiring the lock, then proceeds; here it fails the
    # pipeline-hash guard (a deterministic post-reconcile error), proving the
    # reconciliation ran on the resume entry point.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    (run_dir / "pipeline.yaml").write_text(MINI_PIPELINE)
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-x")

    with pytest.raises(RuntimeError, match="pipeline content hash"):
        mgr.resume("demo", use_judge=False)

    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_FAILED
    assert man.record("implement").status == M.INTERRUPTED
    assert len(man.recoveries) == 1
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()
    assert not _lock_path(tmp_path).exists()  # resume's fresh lock released too


def test_resume_entrypoint_discards_stale_intent(tmp_path, procs):
    # (d2, stale) resume reconciles (discards the stale intent) before acquiring
    # the lock; the relaunched driver's lock is LIVE, so the acquire then fails
    # closed (WorktreeLockError) — but the stale intent is already discarded and
    # the manifest is untouched.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    (run_dir / "pipeline.yaml").write_text(MINI_PIPELINE)
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-old")
    live = procs()
    _write_lock(tmp_path, pid=live.pid, identity=_ident(live.pid), nonce="n-new")

    with pytest.raises(WorktreeLockError):
        mgr.resume("demo", use_judge=False)

    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # discarded
    assert live.poll() is None  # the relaunched driver was not signalled
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_RUNNING  # no mutation
    assert man.recoveries == []


def test_reconcile_reused_pgid_sends_no_signal(tmp_path, procs):
    # (f) Reused-PGID injection: the verified target is gone and a DIFFERENT live
    # process now occupies the recorded pgid (its identity no longer matches the
    # frozen one). Finalization sends NO signal and records `already_dead`,
    # writing the INTERRUPTED transition + record + cleared intent — never killing
    # the innocent occupant, never leaving the manifest stranded `running`.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    occupant = procs()  # live, but NOT the original target
    real = _ident(occupant.pid)
    wrong_identity = {**real, "value": real["value"] + 1}  # frozen identity ≠ now
    _write_intent(
        run_dir, pid=occupant.pid, pgid=os.getpgid(occupant.pid),
        identity=wrong_identity, lock_nonce="n-x",
    )  # lock absent → live branch

    note = mgr._reconcile_recovery_intent(run_dir)
    assert "finalized" in note

    assert occupant.poll() is None  # innocent occupant NOT signalled
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_FAILED  # not stranded `running`
    assert man.record("implement").status == M.INTERRUPTED
    assert man.recoveries[0].signal_outcome == M.SIGNAL_ALREADY_DEAD
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


def test_reconcile_does_not_duplicate_an_already_recorded_recovery(tmp_path):
    # (c) Crash injected AFTER step 6 (record written, step INTERRUPTED) but
    # before step 7 (intent cleared): the surviving intent's record is already
    # present. Reconciliation must complete steps 7–8 WITHOUT appending a second
    # record or re-flipping state — the record is written exactly once.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path, step_status=M.INTERRUPTED, run_status=M.RUN_FAILED)
    man = Manifest.load(run_dir / "manifest.json")
    man.recoveries.append(
        M.RecoveryRecord(
            ts="t", actor="a", actor_source="os_user", reason=None,
            lock_nonce="n-x", pid=DEAD_PID, pgid=DEAD_PID, proc_identity=None,
            host=THIS_HOST, signal_outcome=M.SIGNAL_ALREADY_DEAD,
            prior_step_id="implement", prior_step_status=M.RUNNING,
            prior_run_status=M.RUN_RUNNING, resulting_step_status=M.INTERRUPTED,
            resulting_run_status=M.RUN_FAILED,
        )
    )
    man.write_atomic(run_dir / "manifest.json")
    _write_intent(run_dir, pid=DEAD_PID, identity=None, lock_nonce="n-x")

    mgr._reconcile_recovery_intent(run_dir)

    man2 = Manifest.load(run_dir / "manifest.json")
    assert len(man2.recoveries) == 1  # not duplicated
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # steps 7–8 still completed


def test_reason_preserved_through_reconcile(tmp_path):
    # The operator `--reason` is frozen in the intent, so a crash-reconciled
    # finalize (which builds the record from the intent alone) preserves it.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_intent(
        run_dir, pid=DEAD_PID, identity=_ident(os.getpid()),
        lock_nonce="n-x", reason="kept across the crash",
    )
    mgr._reconcile_recovery_intent(run_dir)
    assert Manifest.load(run_dir / "manifest.json").recoveries[0].reason == (
        "kept across the crash"
    )


# ---- F-002: reconciliation never overwrites a transitioned run --------------


def test_reconcile_refuses_when_target_step_no_longer_running(tmp_path):
    # (F-002) A surviving live-branch intent (lock ABSENT) whose target step has
    # since completed normally must NOT be finalized — doing so would overwrite the
    # completed step/run as INTERRUPTED/RUN_FAILED. Refuse without signalling,
    # mutating the manifest, or deleting the intent; leave it for the operator.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path, step_status=M.DONE, run_status=M.RUN_DONE)
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-x")

    note = mgr._reconcile_recovery_intent(run_dir)
    assert "no longer" in note and "left in place" in note

    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_DONE  # not overwritten
    assert man.record("implement").status == M.DONE
    assert man.recoveries == []
    assert (run_dir / RECOVERY_INTENT_NAME).exists()  # intent left for inspection


# ---- F-003: recover's mutable paths stay inside the run root -----------------


def test_active_run_dir_rejects_traversal_pointer(tmp_path):
    # (F-003) The active-run pointer flows straight into the mutable manifest/intent
    # paths recover reads AND writes; a traversal segment must be refused before any
    # path is built (so external bytes can never redirect a mutation out of tree).
    mgr = _mgr(tmp_path)
    layout = mgr.layout("demo")
    layout.slug_dir.mkdir(parents=True, exist_ok=True)
    layout.active_pointer.write_text("../../etc")
    with pytest.raises(UnsafeRunSegment):
        layout.active_run_dir()


def test_active_run_dir_rejects_absolute_pointer(tmp_path):
    # (F-003) An absolute path in the pointer (contains a separator) is refused too.
    mgr = _mgr(tmp_path)
    layout = mgr.layout("demo")
    layout.slug_dir.mkdir(parents=True, exist_ok=True)
    layout.active_pointer.write_text("/etc")
    with pytest.raises(UnsafeRunSegment):
        layout.active_run_dir()


def test_reconcile_refuses_symlinked_intent_no_read(tmp_path):
    # (F-003) A symlinked .recovery-intent.json could redirect the read that drives
    # signalling + manifest mutation outside the run tree. Refuse with NO read,
    # leave it in place, and do not touch the manifest — mirroring the read-only
    # parser's containment check on the mutating reconcile path.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    outside = tmp_path / "evil-intent.json"
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-x")
    (run_dir / RECOVERY_INTENT_NAME).rename(outside)  # move the real intent outside
    (run_dir / RECOVERY_INTENT_NAME).symlink_to(outside)  # ...and point at it

    note = mgr._reconcile_recovery_intent(run_dir)
    assert "symlink" in note.lower()

    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_RUNNING  # untouched
    assert man.record("implement").status == M.RUNNING
    assert man.recoveries == []


# ---- F-004: a normal completion during the intent-write window is not clobbered


def test_recover_aborts_if_driver_completes_during_intent_write(tmp_path, procs, monkeypatch):
    # (F-004) The driver can complete NORMALLY between the step-3 nonce re-read and
    # the durable intent write. recover must reload the lock + manifest immediately
    # after the write and abort WITHOUT signalling or overwriting the now-completed
    # manifest — and clean up the intent it just wrote.
    import gauntlet.engine.run as run_mod

    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid), nonce="nonce-1")

    real_write = run_mod._atomic_write_durable

    def write_then_complete(path, text):
        real_write(path, text)
        # Simulate the driver finishing normally inside the window: step DONE, run
        # RUN_DONE, and the lock released — exactly what a clean exit leaves behind.
        man = Manifest.load(run_dir / "manifest.json")
        man.record("implement").status = M.DONE
        man.status = M.RUN_DONE
        man.write_atomic(run_dir / "manifest.json")
        _lock_path(tmp_path).unlink()

    monkeypatch.setattr(run_mod, "_atomic_write_durable", write_then_complete)

    with pytest.raises(RecoverConcurrent, match="completed or relaunched"):
        mgr.recover("demo")

    assert proc.poll() is None  # never signalled
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_DONE  # the completed manifest is intact
    assert man.record("implement").status == M.DONE
    assert man.recoveries == []
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # the written intent cleaned up


# ---- F-005: a verified-but-unsignalable driver fails closed, no retry wedge --


def test_signal_process_group_raises_on_permission_denied(monkeypatch):
    # (F-005) When the OS refuses the SIGKILL (EPERM) the helper raises a typed
    # RecoverError instead of letting an unhandled OSError escape.
    import gauntlet.engine.run as run_mod

    monkeypatch.setattr(
        run_mod.os, "killpg",
        lambda pgid, sig: (_ for _ in ()).throw(PermissionError("not permitted")),
    )
    with pytest.raises(RecoverSignalError, match="permission denied"):
        _signal_process_group(2_000_000_001, grace_s=0.05)


def test_recover_cleans_up_intent_when_signal_permission_denied(tmp_path, procs, monkeypatch):
    # (F-005) recover converts the unsignalable case into a fail-closed refusal:
    # the still-alive driver is not marked INTERRUPTED, the manifest is untouched,
    # the lock is left in place — and the durable intent is removed so reconciliation
    # does not retry the un-killable signal forever.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    proc = procs()
    _write_lock(tmp_path, pid=proc.pid, identity=_ident(proc.pid), nonce="nonce-1")

    def boom(intent):
        raise RecoverSignalError("permission denied signalling the driver")

    monkeypatch.setattr(mgr, "_signal_recover_target", boom)

    with pytest.raises(RecoverSignalError, match="permission denied"):
        mgr.recover("demo")

    assert proc.poll() is None  # still alive — we could not kill it
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_RUNNING  # untouched
    assert man.record("implement").status == M.RUNNING
    assert man.recoveries == []
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # cleaned up, no retry wedge
    assert _lock_path(tmp_path).exists()  # lock not released (driver still holds it)


def test_reconcile_cleans_up_intent_when_signal_permission_denied(tmp_path, monkeypatch):
    # (F-005) Reconciliation hitting the same EPERM clears the intent (so it is not
    # retried on every later entry point) and leaves the manifest running for manual
    # intervention, rather than wedging or raising an unhandled error.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_intent(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()), lock_nonce="n-x")

    def boom(intent):
        raise RecoverSignalError("permission denied signalling the driver")

    monkeypatch.setattr(mgr, "_signal_recover_target", boom)

    note = mgr._reconcile_recovery_intent(run_dir)
    assert "could not finalize" in note

    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # cleared, no retry wedge
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_RUNNING  # not mutated
    assert man.recoveries == []

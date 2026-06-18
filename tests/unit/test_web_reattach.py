"""Re-attach & crash survival (P4, FR-7.1/7.2/7.3).

The P4 assumption: the console holds **no authoritative run state** (D2). A
*fresh* :class:`JobSupervisor` (no in-memory handles, modelling a restarted
server) re-discovers owned runs purely from ``.serve/job.json`` and classifies
each with the PID-reuse-safe liveness check — re-attaching a still-live run and
reclaiming an orphan onto the *same* ``resume`` path a ``kill -9``'d run already
has (FR-7.3).

The headline test mirrors ``test_resume_crash`` (Popen + SIGKILL): it launches a
real engine run that dies mid-step, overlays the owned-run sidecar the supervisor
would have written, then proves a fresh supervisor classifies it
``interrupted``, removes the stale sidecar, and that ``resume`` recovers it to
``done`` with exactly one set of effects. The classifier itself is table-tested
in isolation, and PID-reuse / unverifiable-identity orphans are asserted against
a real *live* pid so a reused or null identity is never a spurious re-attach.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine import gitops
from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import (
    RUN_ABORTED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PARKED,
    RUN_RUNNING,
    Manifest,
    PipelineRef,
)
from gauntlet.procident import ProcessIdentity, read_process_identity
from gauntlet.web.jobproc import JOB_FILENAME, SERVE_DIRNAME, JobRecord
from gauntlet.web.service import create_app
from gauntlet.web.store import RunStore
from gauntlet.web.supervisor import (
    COMPLETED,
    FAILED_LAUNCH,
    INTERRUPTED,
    REATTACHED,
    Job,
    JobSupervisor,
)

# Reuse the P3 supervisor + engine crash fixtures rather than re-deriving them.
from test_resume_crash import CHILD, RecoverAdapter
from test_resume_crash import _build_repo as _build_crash_repo
from test_web_supervisor import LONG_PIPELINE, SLEEP_PIPELINE, _build_repo

TOKEN = "reattach-test-token"
DEAD_PID = 2**30  # an unused pid → the recorded driver always reads as dead


# ---- helpers ----------------------------------------------------------------


def _bare_supervisor(tmp_path: Path) -> JobSupervisor:
    """A supervisor over a plain ``runs/`` tree (no engine/git needed)."""
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    return JobSupervisor(repo)


def _seed_owned_run(
    root: Path,
    slug: str,
    run_id: str,
    *,
    status: str | None,
    pid: int,
    identity: ProcessIdentity | None,
) -> Path:
    """Lay an owned run on disk: optional manifest + a ``.serve/job.json`` sidecar
    and a captured log, exactly as :class:`RunProcess` would have written them.

    ``status=None`` writes no manifest (the FR-6.1a crash-before-manifest case).
    """
    run_dir = root / slug / run_id
    serve = run_dir / SERVE_DIRNAME
    serve.mkdir(parents=True, exist_ok=True)
    if status is not None:
        man = Manifest(
            run_id=run_id,
            slug=slug,
            branch=f"gauntlet/{slug}",
            base_branch="main",
            pipeline=PipelineRef(name="p", version=1, hash="sha256:x"),
            status=status,
        )
        man.write_atomic(run_dir / "manifest.json")
    (serve / "run.log").write_text("captured run output\n")
    rec = JobRecord(
        pid=pid,
        pgid=pid,
        verb="run",
        slug=slug,
        run_id=run_id,
        started_at="t",
        log_path=str(serve / "run.log"),
        proc_identity=identity.to_dict() if identity is not None else None,
    )
    (serve / JOB_FILENAME).write_text(rec.to_json())
    return run_dir


def _own_existing_run(run_dir: Path, slug: str, pid: int) -> None:
    """Overlay the owned-run sidecar a console launch would have written onto a
    run an engine subprocess created — modelling "the console launched this run"
    when the supervisor itself cannot (an agent_task pipeline needs no creds only
    via an in-process test adapter, FR-6.1a). The recorded identity is the
    child's REAL live identity so re-attach liveness is exercised honestly."""
    serve = run_dir / SERVE_DIRNAME
    serve.mkdir(parents=True, exist_ok=True)
    (serve / "run.log").write_text("captured run output\n")
    identity = read_process_identity(pid)
    rec = JobRecord(
        pid=pid,
        pgid=pid,
        verb="run",
        slug=slug,
        run_id=run_dir.name,
        started_at="t",
        log_path=str(serve / "run.log"),
        proc_identity=identity.to_dict() if identity is not None else None,
    )
    (serve / JOB_FILENAME).write_text(rec.to_json())


def _wait_for_manifest(run_dir: Path, rp, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not (run_dir / "manifest.json").exists() and time.monotonic() < deadline:
        if rp.poll() is not None:
            raise RuntimeError("child exited before writing a manifest")
        time.sleep(0.05)
    assert (run_dir / "manifest.json").exists()


def _sidecar(run_dir: Path) -> Path:
    return run_dir / SERVE_DIRNAME / JOB_FILENAME


# ---- pure classifier (no disk, no spawn) ------------------------------------


@pytest.mark.parametrize(
    "live,status,expected",
    [
        (True, RUN_RUNNING, REATTACHED),
        (True, None, REATTACHED),  # a live process wins even with no manifest yet
        (False, None, FAILED_LAUNCH),
        (False, RUN_DONE, COMPLETED),
        (False, RUN_ABORTED, COMPLETED),
        (False, RUN_FAILED, COMPLETED),
        (False, RUN_RUNNING, INTERRUPTED),
        (False, RUN_PARKED, INTERRUPTED),
    ],
)
def test_recovery_disposition_table(live, status, expected):
    """The disposition is a pure function of (liveness, manifest status)."""
    rec = JobRecord(
        pid=1, pgid=1, verb="run", slug="s", run_id="run-1",
        started_at="t", log_path="x", proc_identity=None,
    )
    job = Job("s", "run-1", Path("/does/not/exist"), rec)
    man = None
    if status is not None:
        man = Manifest(
            run_id="run-1", slug="s", branch="gauntlet/s", base_branch="main",
            pipeline=PipelineRef(name="p", version=1, hash="sha256:x"), status=status,
        )
    assert job.recovery_disposition(man, live=live) == expected


# ---- reattach() side effects over seeded runs -------------------------------


def test_reattach_interrupted_removes_stale_sidecar(tmp_path):
    sup = _bare_supervisor(tmp_path)
    rd = _seed_owned_run(
        sup.run_root, "demo", "run-1", status=RUN_PARKED, pid=DEAD_PID, identity=None
    )
    (out,) = sup.reattach()
    assert out.disposition == INTERRUPTED
    assert out.resume_available is True
    # The dead sidecar is gone (orphan → resume path, FR-7.3) but the captured
    # log is kept so the run stays diagnosable.
    assert not _sidecar(rd).exists()
    assert (rd / SERVE_DIRNAME / "run.log").exists()


def test_reattach_completed_keeps_sidecar(tmp_path):
    sup = _bare_supervisor(tmp_path)
    rd = _seed_owned_run(
        sup.run_root, "done", "run-1", status=RUN_DONE, pid=DEAD_PID, identity=None
    )
    (out,) = sup.reattach()
    assert out.disposition == COMPLETED
    assert out.resume_available is False
    # A finished owned run keeps its sidecar → it stays owned for history (FR-1.4).
    assert _sidecar(rd).exists()


def test_reattach_failed_launch_no_manifest(tmp_path):
    sup = _bare_supervisor(tmp_path)
    rd = _seed_owned_run(
        sup.run_root, "boom", "run-1", status=None, pid=DEAD_PID, identity=None
    )
    (out,) = sup.reattach()
    assert out.disposition == FAILED_LAUNCH
    # The captured bootstrap log is still readable → the failure is diagnosable.
    assert (rd / SERVE_DIRNAME / "run.log").exists()


def test_reattach_live_identity_match_reattaches(tmp_path):
    """A live pid whose recorded identity matches is re-attached, not resumed."""
    sup = _bare_supervisor(tmp_path)
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        identity = read_process_identity(sleeper.pid)
        rd = _seed_owned_run(
            sup.run_root, "live", "run-1", status=RUN_RUNNING,
            pid=sleeper.pid, identity=identity,
        )
        (out,) = sup.reattach()
        if identity is not None:  # supported platform: identity matches → re-attach
            assert out.disposition == REATTACHED
            assert _sidecar(rd).exists()  # kept; the live run is still ours
        else:  # unsupported platform: identity unobtainable → fail-closed orphan
            assert out.disposition == INTERRUPTED
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


def test_reattach_pid_reuse_and_null_identity_are_orphans(tmp_path):
    """A *live* pid is never re-attached when its identity disagrees (PID reuse)
    or is unrecorded (unverifiable) — both fail closed to interrupted (FR-7.2)."""
    sup = _bare_supervisor(tmp_path)
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        real = read_process_identity(sleeper.pid)
        if real is not None:
            wrong = ProcessIdentity(
                platform=real.platform, value=real.value + 9999, unit=real.unit
            )
        else:  # unsupported platform: any non-None identity reads as unobtainable
            wrong = ProcessIdentity(platform="linux", value=1, unit="boot_ticks")
        rd_reuse = _seed_owned_run(
            sup.run_root, "reuse", "run-1", status=RUN_PARKED,
            pid=sleeper.pid, identity=wrong,
        )
        rd_null = _seed_owned_run(
            sup.run_root, "null", "run-2", status=RUN_PARKED,
            pid=sleeper.pid, identity=None,
        )
        by_slug = {o.slug: o for o in sup.reattach()}
        assert by_slug["reuse"].disposition == INTERRUPTED
        assert by_slug["null"].disposition == INTERRUPTED
        # Neither live-but-unverifiable run was re-attached; both reclaimed.
        assert not _sidecar(rd_reuse).exists()
        assert not _sidecar(rd_null).exists()
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=10)


# ---- reattach() over a real supervisor-launched run -------------------------


def test_reattach_live_launched_run(tmp_path):
    """A still-running owned run survives a server restart and re-attaches —
    state from the manifest, control via the recorded pgid (no resume needed)."""
    sup = _build_repo(tmp_path / "repo", pipelines={"long": LONG_PIPELINE})
    rp = sup.launch_run("demo", pipeline="long", no_judge=True)
    try:
        _wait_for_manifest(rp.run_dir, rp)
        fresh = JobSupervisor(sup.repo_root)  # restart: no in-memory handles
        out = next(o for o in fresh.reattach() if o.run_id == rp.run_id)
        assert out.disposition == REATTACHED
        assert _sidecar(rp.run_dir).exists()  # kept: the run is still live
        # The fresh supervisor can drive it (control rides job.json's pgid, P3).
        assert fresh.is_attached("demo", rp.run_id) is True
    finally:
        rp.stop()


def test_reattach_killed_launched_run_is_interrupted(tmp_path):
    """A supervisor-launched run that *dies* (not a clean stop) is reclaimed as
    an interrupted orphan by a fresh supervisor — the kill-9 recovery path."""
    sup = _build_repo(tmp_path / "repo", pipelines={"long": LONG_PIPELINE})
    rp = sup.launch_run("demo", pipeline="long", no_judge=True)
    _wait_for_manifest(rp.run_dir, rp)
    # The run crashes: kill its whole process group (it was sleeping mid-step).
    os.killpg(rp.pgid, signal.SIGKILL)
    rp.wait(timeout=10)

    fresh = JobSupervisor(sup.repo_root)
    out = next(o for o in fresh.reattach() if o.run_id == rp.run_id)
    assert out.disposition == INTERRUPTED
    assert out.resume_available is True
    assert not _sidecar(rp.run_dir).exists()  # stale sidecar reclaimed


# ---- headline: orphan → interrupted → resume to done, one effect ------------


@pytest.mark.parametrize("kill_delay", [0.0, 0.03, 0.08])
def test_orphaned_owned_run_resumes_to_done(tmp_path, kill_delay):
    """The P4 headline (FR-7.3): an owned run killed mid-edit is re-discovered by
    a fresh supervisor as interrupted, its stale sidecar reclaimed, and ``resume``
    recovers it to DONE with exactly one set of effects — no lost or duplicated
    work — the *same* recovery a ``kill -9``'d CLI run gets."""
    repo, mgr = _build_crash_repo(tmp_path / "repo", policy="reset_to_base")

    # Launch a real engine run that writes a partial edit then blocks mid-step.
    ready = repo / ".crash_ready"
    if ready.exists():
        ready.unlink()
    proc = subprocess.Popen([sys.executable, str(CHILD), str(repo), "demo"])
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"child exited early ({proc.returncode})")
        time.sleep(0.01)
    assert ready.exists(), "child never reached the mid-step sentinel"

    # Model console ownership: write the sidecar with the child's real identity.
    run_dir = mgr.layout("demo").active_run_dir()
    _own_existing_run(run_dir, "demo", proc.pid)

    time.sleep(kill_delay)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)

    # A fresh supervisor (no in-memory state) re-discovers and reclassifies.
    sup = JobSupervisor(repo)
    out = next(o for o in sup.reattach() if o.run_id == run_dir.name)
    assert out.disposition == INTERRUPTED
    assert out.resume_available is True
    assert not _sidecar(run_dir).exists()  # reclaimed → resume path (FR-7.3)
    assert (run_dir / SERVE_DIRNAME / "run.log").exists()  # still diagnosable

    # Recovery is `resume`, exactly like a kill -9'd run → DONE, one commit.
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: RecoverAdapter()
    )
    assert status == M.RUN_DONE
    final = mgr.status("demo")
    assert [c.phase for c in final.commits] == ["P1"]
    assert gitops.commit_subject(repo, "HEAD") == "P1: crash phase"
    assert (repo / "feature.py").read_text() == "RECOVERED — final content\n"
    assert gitops.is_clean(repo, exclude=["runs"])
    assert gitops._run(repo, "log", "--format=%s").count("P1: crash phase") == 1


# ---- server startup runs re-attach (lifespan wiring) ------------------------


def test_app_startup_reattaches_orphans(tmp_path):
    """`gauntlet serve` re-discovers and reconciles owned runs on boot (FR-7.1):
    the lifespan startup runs ``reattach`` so a stale orphan sidecar is reclaimed
    before the console starts observing."""
    sup = _build_repo(tmp_path / "repo", pipelines={"simple": SLEEP_PIPELINE})
    rd = _seed_owned_run(
        sup.run_root, "demo", "run-z", status=RUN_PARKED, pid=DEAD_PID, identity=None
    )
    store = RunStore.from_repo(sup.repo_root, supervisor=sup)
    app = create_app(store, token=TOKEN, supervisor=sup)
    assert _sidecar(rd).exists()  # present before boot
    with TestClient(app):  # lifespan startup triggers reattach()
        assert not _sidecar(rd).exists()  # reclaimed on startup

"""Orphaned-judge reaping on `abort` / `finish` / `clean` (P2, FR-6).

The cleanup verbs reap a per-run judge **iff all three** hold (§6.4):

* the judge's **own** identity verifies as ours on **this host** (procident:
  PID live + exact creation-time match), and
* the recorded ``pgid`` is still the verified PID's group — positive and equal
  (review F-001), and
* the **owning driver is gone** — ``driver_liveness`` is ``orphaned`` or
  ``none`` (never ``alive``/``indeterminate``).

Any other case sends **no signal**, leaves ``judge.json`` intact, and never a
foreign / PID-reused kill. The shared per-worktree console is never a target
(FR-6.3). Real session-leader subprocesses back the liveness/identity checks so
PID-reuse safety is exercised honestly rather than against sentinel pids.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from gauntlet.engine import manifest as M
from gauntlet.engine.judgeproc import JUDGE_RECORD_NAME, JudgeRecord
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.run import (
    DRIVING_LOCK_NAME,
    AbortGuardError,
    RunManager,
    _LockRecord,
)
from gauntlet.procident import read_process_identity
from gauntlet.web import registry as R

from conftest import FakeAdapter, git

DEAD_PID = 2_000_000_000  # never live; the kill -9'd / power-loss driver/judge
THIS_HOST = socket.gethostname()

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

LINEAR = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, message: "P1: implement\\n\\nthe body."}
"""


@pytest.fixture(autouse=True)
def _supported_platform():
    if read_process_identity(os.getpid()) is None:
        pytest.skip("process identity unobtainable on this platform")


@pytest.fixture
def procs():
    """Spawn session-leader subprocesses (own group, pgid == pid) and reap them."""
    spawned: list[subprocess.Popen] = []

    def make() -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            start_new_session=True,
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


def _safe_wait(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def _with_reaper(proc: subprocess.Popen, fn):
    """Run ``fn`` while concurrently reaping ``proc`` (mirrors test_recover).

    pytest is the spawned judge's parent, so a SIGTERM'd judge lingers as a
    zombie (keeping ``killpg(pgid, 0)`` succeeding) until reaped — the thread
    reaps it the instant it dies so the reaper's group-gone poll resolves
    promptly to a real ``terminated_*`` outcome instead of waiting out the
    grace. In production the cleanup verb runs in a *separate* process from the
    run that spawned the judge, so the judge is reparented to init and never a
    zombie of the reaper.
    """
    t = threading.Thread(target=lambda: _safe_wait(proc), daemon=True)
    t.start()
    try:
        return fn()
    finally:
        t.join(timeout=15)


def _mgr(root: Path) -> RunManager:
    (root / ".gauntlet").mkdir(parents=True, exist_ok=True)
    (root / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    return RunManager(root)


def _setup_run(
    root: Path,
    *,
    slug: str = "demo",
    run_id: str = "run-1",
    run_status: str = M.RUN_RUNNING,
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
        current_step="implement",
        steps=[StepRecord(id="implement", type="agent_task", status=M.RUNNING)],
    )
    man.write_atomic(run_dir / "manifest.json")
    (slug_dir / "active-run.txt").write_text(run_id)
    return run_dir


def _ident(pid: int) -> dict | None:
    i = read_process_identity(pid)
    return i.to_dict() if i else None


def _mismatched_identity(pid: int) -> dict:
    """A real, well-formed identity for ``pid`` with its value bumped — so the
    fresh re-read never matches (PID-reuse stand-in)."""
    real = dict(_ident(pid))
    real["value"] = int(real["value"]) + 1
    return real


def _write_judge(
    run_dir: Path,
    *,
    pid: int,
    identity: dict | None,
    pgid: int | None = None,
    host: str | None = None,
    run_id: str = "run-1",
) -> Path:
    rec = JudgeRecord(
        pid=pid,
        pgid=pid if pgid is None else pgid,
        proc_identity=identity,
        host=THIS_HOST if host is None else host,
        port=8787,
        url="http://127.0.0.1:8787",
        token="per-run-judge-token",
        run_id=run_id,
        started_at="2026-06-25T16-44-03",
    )
    path = run_dir / JUDGE_RECORD_NAME
    path.write_text(rec.to_json())
    return path


def _write_drive_lock(
    root: Path,
    *,
    pid: int,
    identity: dict | None,
    slug: str = "demo",
    host: str | None = None,
) -> None:
    """Make ``driver_liveness`` resolve to a chosen state by writing the lock.

    none → no lock (don't call this); orphaned → dead pid; alive → live pid +
    matching identity + this host; indeterminate → live pid + null identity.
    """
    rec = _LockRecord(
        nonce="nonce-1",
        slug=slug,
        run_id="run-1",
        pid=pid,
        pgid=pid,
        started_at="2026-06-25T16-44-03",
        host=THIS_HOST if host is None else host,
        proc_identity=identity,
    )
    lp = root / "runs" / DRIVING_LOCK_NAME
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(rec.to_json())


# ---- gone-driver / kill: the one case it SHOULD reap ------------------------


def test_abort_reaps_judge_when_driver_none(tmp_path, procs):
    # driver `none` (no drive lock) + a verified live judge → reap: the recorded
    # group is terminated and judge.json removed (FR-6.1).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    jpath = _write_judge(run_dir, pid=judge.pid, identity=_ident(judge.pid))

    assert _with_reaper(judge, lambda: mgr.abort("demo")) == M.RUN_ABORTED
    judge.wait(timeout=10)
    assert judge.poll() is not None  # the judge group was signalled
    assert not jpath.exists()  # judge.json removed after a successful reap


def test_abort_reaps_judge_when_driver_orphaned(tmp_path, procs):
    # driver `orphaned` (lock present but PID proven dead) + a verified live
    # judge → reap (FR-6.1).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    jpath = _write_judge(run_dir, pid=judge.pid, identity=_ident(judge.pid))
    _write_drive_lock(tmp_path, pid=DEAD_PID, identity=_ident(os.getpid()))

    assert _with_reaper(judge, lambda: mgr.abort("demo")) == M.RUN_ABORTED
    judge.wait(timeout=10)
    assert judge.poll() is not None
    assert not jpath.exists()


# ---- alive / indeterminate driver: never reap ------------------------------


def test_abort_does_not_reap_when_driver_alive(tmp_path, procs):
    # driver `alive` (live lock, matching identity, this host) → the judge is
    # NOT signalled and judge.json is left intact, even though the judge's own
    # identity verifies (FR-6.1 alive-driver/no-kill).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    driver = procs()
    jpath = _write_judge(run_dir, pid=judge.pid, identity=_ident(judge.pid))
    _write_drive_lock(tmp_path, pid=driver.pid, identity=_ident(driver.pid))

    mgr.abort("demo")
    assert judge.poll() is None  # not signalled — driver is running
    assert jpath.exists()


def test_abort_does_not_reap_when_driver_indeterminate(tmp_path, procs):
    # driver `indeterminate` (live pid but null recorded identity → liveness
    # unprovable) → fail closed, no signal, file intact (FR-6.1).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    driver = procs()
    jpath = _write_judge(run_dir, pid=judge.pid, identity=_ident(judge.pid))
    _write_drive_lock(tmp_path, pid=driver.pid, identity=None)

    mgr.abort("demo")
    assert judge.poll() is None
    assert jpath.exists()


# ---- fail-closed identity: dead / mismatched / null / foreign host ----------


def test_abort_no_signal_dead_judge_pid(tmp_path):
    # judge.json records a dead pid → no signal, file intact (nothing to kill).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    jpath = _write_judge(run_dir, pid=DEAD_PID, identity=_ident(os.getpid()))
    mgr.abort("demo")
    assert jpath.exists()


def test_abort_no_signal_mismatched_identity(tmp_path, procs):
    # live judge pid but the recorded identity does not match the fresh read
    # (the PID-reuse case) → fail closed, no signal, file intact (FR-6.2).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    jpath = _write_judge(
        run_dir, pid=judge.pid, identity=_mismatched_identity(judge.pid)
    )
    mgr.abort("demo")
    assert judge.poll() is None
    assert jpath.exists()


def test_abort_no_signal_null_identity(tmp_path, procs):
    # a `null` recorded identity (unsupported platform / unobtainable at write)
    # is unverifiable → never reaped (FR-6.2; procident fail-closed contract).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    jpath = _write_judge(run_dir, pid=judge.pid, identity=None)
    mgr.abort("demo")
    assert judge.poll() is None
    assert jpath.exists()


def test_abort_no_signal_foreign_host(tmp_path, procs):
    # a judge recorded on another host must never be signalled from here, even
    # if the pid happens to be live locally (FR-6.2).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    jpath = _write_judge(
        run_dir, pid=judge.pid, identity=_ident(judge.pid), host="other-host"
    )
    mgr.abort("demo")
    assert judge.poll() is None
    assert jpath.exists()


# ---- fail-closed pgid (review F-001) ---------------------------------------


def test_abort_no_signal_mismatched_pgid(tmp_path, procs):
    # the judge's identity verifies but the recorded pgid is not the verified
    # PID's group → no signal (the reaper never signals an unconfirmed group).
    # Driver forced to `none` so ONLY the pgid gate can hold the kill back.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    other = procs()  # a different, unrelated live group
    jpath = _write_judge(
        run_dir, pid=judge.pid, identity=_ident(judge.pid), pgid=other.pid
    )
    mgr.abort("demo")
    assert judge.poll() is None and other.poll() is None
    assert jpath.exists()


def test_abort_no_signal_nonpositive_pgid(tmp_path, procs):
    # a non-positive recorded pgid (corrupted record) → no signal, file intact.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    jpath = _write_judge(
        run_dir, pid=judge.pid, identity=_ident(judge.pid), pgid=0
    )
    mgr.abort("demo")
    assert judge.poll() is None
    assert jpath.exists()


# ---- no judge / malformed record -------------------------------------------


def test_abort_without_judge_record_is_a_noop(tmp_path):
    # No judge.json at all → abort still succeeds, nothing to reap.
    mgr = _mgr(tmp_path)
    _setup_run(tmp_path)
    assert mgr.abort("demo") == M.RUN_ABORTED


def test_abort_malformed_judge_record_no_signal(tmp_path):
    # A corrupt judge.json round-trips to None → treated as absent, no signal.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    (run_dir / JUDGE_RECORD_NAME).write_text("{ not json")
    assert mgr.abort("demo") == M.RUN_ABORTED
    assert (run_dir / JUDGE_RECORD_NAME).exists()  # left untouched


# ---- FR-6.3: the shared console is never a reap target ----------------------


def test_cleanup_never_kills_console(tmp_path, procs):
    # A registered live console must survive a reaping abort and keep its
    # registry entry intact — only the per-run judge is a target (FR-6.3).
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    judge = procs()
    console = procs()
    _write_judge(run_dir, pid=judge.pid, identity=_ident(judge.pid))
    run_root = tmp_path / "runs"
    record = R.ConsoleRecord(
        pid=console.pid,
        pgid=os.getpgid(console.pid),
        proc_identity=_ident(console.pid),
        host=THIS_HOST,
        port=8765,
        url="http://127.0.0.1:8765",
        token_fingerprint="fp",
        started_at="2026-06-25T16-44-03",
        log_path=str(run_root / R.CONSOLE_LOG_NAME),
    )
    R.write_registry(run_root, record)

    _with_reaper(judge, lambda: mgr.abort("demo"))
    judge.wait(timeout=10)
    assert judge.poll() is not None  # judge reaped
    assert console.poll() is None  # console untouched
    again = R.read_registry(run_root)
    assert again is not None and again.pid == console.pid  # registry intact


# ---- the helper itself: terminal abort is refused before any reap -----------


def test_abort_terminal_run_refused_no_reap(tmp_path, procs):
    # A terminal run cannot be aborted (review F-002); the guard fires before any
    # reap, so the judge.json is left for finish/clean to handle.
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path, run_status=M.RUN_DONE)
    judge = procs()
    jpath = _write_judge(run_dir, pid=judge.pid, identity=_ident(judge.pid))
    with pytest.raises(AbortGuardError):
        mgr.abort("demo")
    assert judge.poll() is None
    assert jpath.exists()


# ---- finish / clean wire the same reaper (git-backed, real done run) --------


def _prepare_repo(repo: Path) -> RunManager:
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _run_to_done(mgr: RunManager, repo: Path, slug: str = "demo") -> Path:
    mgr.new(slug)
    mgr.layout(slug).prd_path.write_text("# Real PRD\n\nA genuine PRD.\n")
    (repo / "pipelines").mkdir(exist_ok=True)
    ppath = repo / "pipelines" / "p.yaml"
    ppath.write_text(LINEAR)
    mgr.start(
        slug, ppath, use_judge=False,
        adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}),
    )
    run_dir = mgr.layout(slug).active_run_dir()
    assert Manifest.load(run_dir / "manifest.json").status == M.RUN_DONE
    return run_dir


def test_finish_reaps_orphaned_judge(fixture_repo, procs):
    # A completed run's driver is gone (the drive lock released on done →
    # liveness `none`); finish reaps its orphaned judge, then lands the branch.
    mgr = _prepare_repo(fixture_repo)
    run_dir = _run_to_done(mgr, fixture_repo)
    judge = procs()
    jpath = _write_judge(
        run_dir, pid=judge.pid, identity=_ident(judge.pid), run_id=run_dir.name
    )
    _with_reaper(judge, lambda: mgr.finish("demo"))
    judge.wait(timeout=10)
    assert judge.poll() is not None
    assert not jpath.exists()


def test_clean_reaps_orphaned_judge(fixture_repo, procs):
    # clean reaps before clearing the active-run pointer (which would otherwise
    # lose the run dir holding judge.json).
    mgr = _prepare_repo(fixture_repo)
    run_dir = _run_to_done(mgr, fixture_repo)
    judge = procs()
    jpath = _write_judge(
        run_dir, pid=judge.pid, identity=_ident(judge.pid), run_id=run_dir.name
    )
    _with_reaper(judge, lambda: mgr.clean("demo", force=True))
    judge.wait(timeout=10)
    assert judge.poll() is not None
    assert not jpath.exists()


def test_clean_without_active_pointer_is_safe(fixture_repo, procs):
    # clean after the pointer is already cleared has no run dir to read — it must
    # not fail trying to reap (the _safe wrapper swallows the missing pointer).
    mgr = _prepare_repo(fixture_repo)
    _run_to_done(mgr, fixture_repo)
    # First clean clears the pointer and deletes the branch.
    mgr.clean("demo", force=True)
    # A second clean has no active pointer; it must still succeed.
    mgr.clean("demo", force=True)

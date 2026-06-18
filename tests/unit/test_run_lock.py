"""Worktree-scoped active-run lock + run-id handshake (P3, FR-10.5 / FR-6.1a).

The one sanctioned engine phase's two approved modifications. The lock is the R1
mitigation — two orchestrators can never drive one worktree — so it is tested
adversarially: cross-slug fail-closed, single-holder under concurrency, stale +
PID-reuse reclaim, release on park/done/error, and the F-004 nonce-validated
release that survives a stale-reclaim race.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import Manifest
from gauntlet.engine.run import (
    DRIVING_LOCK_NAME,
    RESERVATION_FILENAME,
    SERVE_DIRNAME,
    ActiveRunError,
    RunManager,
    UnsafeRunSegment,
    WorktreeLockError,
    _LockHandle,
    _LockRecord,
)
from gauntlet.procident import read_process_identity

from conftest import FakeAdapter, git

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

GATED = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [prd.md]}
      - {id: after, type: shell, run: "true"}
"""

# Reaches DONE with no commit (so it never sweeps untracked fixture files onto
# the run branch), which keeps the two-run release test isolated.
SIMPLE_DONE = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: t, type: shell, run: "true"}
"""


def _prepare(repo: Path) -> RunManager:
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _author_prd(mgr: RunManager, slug: str) -> None:
    mgr.new(slug)
    mgr.layout(slug).prd_path.write_text(f"# PRD {slug}\n\nA genuine human PRD.\n")


def _pipeline(repo: Path, text: str, name: str = "p") -> Path:
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / f"{name}.yaml"
    path.write_text(text)
    return path


def _lock_path(repo: Path) -> Path:
    return repo / "runs" / DRIVING_LOCK_NAME


def _write_lock(
    repo: Path, *, pid: int, identity: dict | None, slug="other", run_id="run-x",
    nonce="nonce-foreign",
) -> None:
    repo.joinpath("runs").mkdir(parents=True, exist_ok=True)
    rec = _LockRecord(
        nonce=nonce, slug=slug, run_id=run_id, pid=pid, pgid=pid,
        started_at="t", host="h", proc_identity=identity,
    )
    _lock_path(repo).write_text(rec.to_json())


def _live_identity() -> dict | None:
    ident = read_process_identity(os.getpid())
    return ident.to_dict() if ident else None


# ---- run-id allocation handshake (FR-6.1a) ---------------------------------


def test_run_id_handshake_uses_provided_id(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    status = mgr.start(
        "demo", path, use_judge=False, run_id="run-preallocated-1",
        adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}),
    )
    assert status == M.RUN_DONE
    run_dir = mgr.layout("demo").run_dir("run-preallocated-1")
    assert (run_dir / "manifest.json").exists()
    assert Manifest.load(run_dir / "manifest.json").run_id == "run-preallocated-1"


def test_run_id_handshake_is_single_use(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    mgr.start("demo", path, use_judge=False, run_id="run-fixed")
    mgr.abort("demo")  # terminal, so the per-slug orphan guard would allow a start
    # ...but the run dir of that id already exists → single-use error (FR-6.1a).
    with pytest.raises(ActiveRunError, match="single-use"):
        mgr.start("demo", path, use_judge=False, run_id="run-fixed")


def test_run_id_handshake_adopts_matching_reservation(fixture_repo):
    # The supervisor pre-creates `run_dir/.serve/` and writes a single-use
    # reservation token before launch; a child carrying the matching
    # `--reservation-token` may adopt that pre-existing dir (FR-6.1a, F-005).
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    run_id = "run-reserved-1"
    serve = mgr.layout("demo").run_dir(run_id) / SERVE_DIRNAME
    serve.mkdir(parents=True)
    (serve / RESERVATION_FILENAME).write_text("tok-abc")
    status = mgr.start(
        "demo", path, use_judge=False, run_id=run_id, reservation_token="tok-abc",
        adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}),
    )
    assert status == M.RUN_DONE
    assert Manifest.load(
        mgr.layout("demo").run_dir(run_id) / "manifest.json"
    ).run_id == run_id


def test_run_id_handshake_refuses_dir_without_matching_reservation(fixture_repo):
    # A pre-existing run dir holding a prior launch's diagnostic state but no
    # matching fresh reservation token must NOT be reused/overwritten — that
    # would clobber a failed launch's sidecar/log (FR-6.1a single-use, F-005).
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    run_id = "run-leftover-1"
    serve = mgr.layout("demo").run_dir(run_id) / SERVE_DIRNAME
    serve.mkdir(parents=True)
    (serve / "run.log").write_text("prior crash output\n")
    # No token supplied → refused.
    with pytest.raises(ActiveRunError, match="single-use"):
        mgr.start("demo", path, use_judge=False, run_id=run_id)
    # A non-matching token is equally refused.
    (serve / RESERVATION_FILENAME).write_text("the-real-token")
    with pytest.raises(ActiveRunError, match="single-use"):
        mgr.start(
            "demo", path, use_judge=False, run_id=run_id,
            reservation_token="wrong-token",
        )


@pytest.mark.parametrize("bad", ["../escape", "a/b", "a\\b", "", ".", "..", "x\x00y"])
def test_start_rejects_unsafe_run_id(fixture_repo, bad):
    # `--run-id` flows straight into the run-root path; a traversal/separator/
    # NUL segment is refused before any path is built (FR-10.1, F-001).
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    with pytest.raises(UnsafeRunSegment):
        mgr.start("demo", path, use_judge=False, run_id=bad)


@pytest.mark.parametrize("bad", ["../evil", "a/b", "a\\b", ".", "..", "x\x00y"])
def test_start_rejects_unsafe_slug(fixture_repo, bad):
    # The slug is a run-root path segment too; refuse traversal before the
    # entry contract or any sidecar write (FR-10.1, F-001).
    mgr = _prepare(fixture_repo)
    path = _pipeline(fixture_repo, LINEAR)
    with pytest.raises(UnsafeRunSegment):
        mgr.start(bad, path, use_judge=False)


# ---- lock fail-closed (FR-10.5) --------------------------------------------


def test_different_slug_start_fails_closed_against_live_lock(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    # A live foreign driver holds the worktree lock for a DIFFERENT slug.
    _write_lock(fixture_repo, pid=os.getpid(), identity=_live_identity(), slug="other")
    with pytest.raises(WorktreeLockError, match="being driven by other"):
        mgr.start("demo", path, use_judge=False)


def test_resume_and_approve_fail_closed_against_live_lock(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    # Park demo first (releases the lock), then a foreign live driver grabs it.
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    assert not _lock_path(fixture_repo).exists()  # released at the gate
    _write_lock(fixture_repo, pid=os.getpid(), identity=_live_identity(), slug="other")
    with pytest.raises(WorktreeLockError):
        mgr.resume("demo", use_judge=False)
    with pytest.raises(WorktreeLockError):
        mgr.approve("demo", notes="ok", use_judge=False)


# ---- stale / PID-reuse reclaim (FR-7.2) ------------------------------------


def test_stale_dead_pid_lock_is_reclaimed(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    # A dead pid holds the lock (the kill -9 case) → reclaimable, start proceeds.
    _write_lock(fixture_repo, pid=2_000_000_000, identity=_live_identity())
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED


def test_pid_reused_lock_is_reclaimed(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    # Same (live) pid, but a mismatched proc identity == PID reuse → reclaimable.
    ident = read_process_identity(os.getpid())
    if ident is None:
        pytest.skip("process identity unobtainable on this platform")
    reused = {"platform": ident.platform, "value": ident.value + 1, "unit": ident.unit}
    _write_lock(fixture_repo, pid=os.getpid(), identity=reused)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED


def test_unverifiable_identity_lock_is_reclaimed(fixture_repo):
    # proc_identity == null (unrecordable at capture) → unverifiable → fail
    # closed to "not live" → reclaimable (FR-7.2).
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    _write_lock(fixture_repo, pid=os.getpid(), identity=None)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED


# ---- concurrency: exactly one holder ---------------------------------------


def test_concurrent_acquire_yields_exactly_one_holder(fixture_repo):
    _prepare(fixture_repo)
    (fixture_repo / "runs").mkdir(exist_ok=True)
    results: list[object] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        mgr = RunManager(fixture_repo)
        barrier.wait()
        try:
            handle = mgr._acquire_worktree_lock("demo", "run-x")
            with lock:
                results.append(handle)
        except WorktreeLockError:
            with lock:
                results.append("fail")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    holders = [r for r in results if isinstance(r, _LockHandle)]
    assert len(holders) == 1, results
    assert results.count("fail") == 7
    RunManager(fixture_repo)._release_worktree_lock(holders[0])  # tidy up


# ---- release semantics (F-004 + park/done/error) ---------------------------


def test_lock_released_on_done_and_park(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "done-slug")
    _author_prd(mgr, "park-slug")
    done_path = _pipeline(fixture_repo, SIMPLE_DONE, name="simple")
    park_path = _pipeline(fixture_repo, GATED, name="gated")
    assert mgr.start("done-slug", done_path, use_judge=False) == M.RUN_DONE
    assert not _lock_path(fixture_repo).exists()  # released on done
    assert mgr.start("park-slug", park_path, use_judge=False) == M.RUN_PARKED
    assert not _lock_path(fixture_repo).exists()  # released on park


def test_lock_released_on_error(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED  # parks, frees lock
    # A second start over the still-parked run acquires the lock, then the
    # per-slug orphan guard raises — the `finally` must still free the lock.
    with pytest.raises(ActiveRunError):
        mgr.start("demo", path, use_judge=False)
    assert not _lock_path(fixture_repo).exists()


def test_release_is_noop_when_nonce_differs(fixture_repo):
    # F-004: a stale holder A's late release must NOT unlink a *new* owner B's
    # lock. Simulate B holding the lock (its own nonce); A's release is a no-op.
    mgr = _prepare(fixture_repo)
    (fixture_repo / "runs").mkdir(exist_ok=True)
    _write_lock(fixture_repo, pid=os.getpid(), identity=_live_identity(),
                slug="B", nonce="nonce-B")
    stale_handle = _LockHandle(path=_lock_path(fixture_repo), nonce="nonce-A")
    mgr._release_worktree_lock(stale_handle)
    # B's lock survives intact.
    assert _lock_path(fixture_repo).exists()
    assert _LockRecord.from_json(_lock_path(fixture_repo).read_text()).nonce == "nonce-B"


def test_release_unlinks_when_nonce_matches(fixture_repo):
    mgr = _prepare(fixture_repo)
    (fixture_repo / "runs").mkdir(exist_ok=True)
    handle = mgr._acquire_worktree_lock("demo", "run-x")
    assert _lock_path(fixture_repo).exists()
    mgr._release_worktree_lock(handle)
    assert not _lock_path(fixture_repo).exists()


def test_stale_reclaim_then_late_release_keeps_new_owner(fixture_repo):
    # Full F-004 race: A goes stale, B reclaims (writes its own nonce), then A's
    # deferred release runs — assert A's release is a no-op and B's lock survives.
    mgr_b = _prepare(fixture_repo)
    (fixture_repo / "runs").mkdir(exist_ok=True)
    # A's stale lock (dead pid).
    _write_lock(fixture_repo, pid=2_000_000_000, identity=None,
                slug="A", nonce="nonce-A")
    # B reclaims it (live = this process) and now holds a fresh nonce.
    handle_b = mgr_b._acquire_worktree_lock("B", "run-b")
    assert _LockRecord.from_json(_lock_path(fixture_repo).read_text()).nonce == handle_b.nonce
    # A's late release (its own stale handle) must be a no-op.
    mgr_b._release_worktree_lock(_LockHandle(path=_lock_path(fixture_repo), nonce="nonce-A"))
    assert _lock_path(fixture_repo).exists()
    assert _LockRecord.from_json(_lock_path(fixture_repo).read_text()).nonce == handle_b.nonce


# ---- worktree stays clean (gitignore) --------------------------------------


def test_run_root_bookkeeping_does_not_dirty_worktree(fixture_repo):
    from gauntlet.engine import gitops

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    # Commit the pre-existing fixture files (prd.md, pipeline) so the only NEW
    # paths after the run are the engine's run/lock bookkeeping.
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "fixture prd + pipeline")
    mgr.start("demo", path, use_judge=False)
    gi = fixture_repo / "runs" / ".gitignore"
    assert gi.exists()
    body = gi.read_text()
    assert DRIVING_LOCK_NAME in body
    # The run-root bookkeeping (.gitignore, lock temp pattern) is self-ignored,
    # so the worktree is clean without excluding the run root.
    assert gitops.is_clean(fixture_repo)

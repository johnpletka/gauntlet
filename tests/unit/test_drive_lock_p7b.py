"""The P7b drive-lock split: per-run lock, retained tree guard, repo-global lock.

P7b moves the driving lock from the worktree-global path
``<run_root>/.driving.lock`` to the per-run path
``<run_root>/<slug>/<run-id>/.driving.lock`` — but the tree is still shared by
every run until P7c, so the worktree-global guard is **retained** rather than
replaced. That retention is the load-bearing decision of the phase, and it is
what the first two tests here exist to pin down: with the per-run lock alone,
two runs of two different slugs, and even two ``gauntlet run`` invocations of
the SAME slug (which mint different run ids, hence different lock paths), would
both drive one worktree.

The rest of the file covers the migration contract (a legacy lock is read, and
a live one refuses), the reclaim asymmetry at BOTH scopes, the R1 obligations
around a vanished or crash-abandoned lock, and the repo-global git lock that
serializes the shared worktree-administration dir.

Adversarial by construction: every exclusion test drives a REAL run and takes
the second verb from inside the live step, so the lock under test is one this
process genuinely holds — not a synthesized lockfile.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gauntlet.adapters.base import AdapterCapabilities, AgentResult
from gauntlet.engine import gitops, locking, manifest as M, operator as op, repolock
from gauntlet.engine.manifest import Manifest
from gauntlet.engine.recovery_exec import RecoveryLockError, WorktreeLockGuard
from gauntlet.engine.run import (
    DRIVING_LOCK_NAME,
    RunManager,
    WorktreeLockError,
)
from gauntlet.procident import read_process_identity

from conftest import git

CHILD = Path(__file__).parent / "_crash_child.py"

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

CRASH_PIPELINE = """
name: crash
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, message: "P1: crash phase\\n\\nthe body."}
"""


# --- fixtures / helpers ------------------------------------------------------
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
    git(repo, "add", "pipelines")
    git(repo, "commit", "-qm", f"add pipeline {name}")
    return path


def _tree_lock(repo: Path) -> Path:
    return repo / "runs" / DRIVING_LOCK_NAME


def _live_identity() -> dict | None:
    ident = read_process_identity(os.getpid())
    return ident.to_dict() if ident else None


def _write_lock(
    path: Path, *, pid: int, identity: dict | None, slug: str = "demo",
    run_id: str = "run-x", nonce: str = "nonce-foreign",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        locking.LockRecord(
            nonce=nonce, slug=slug, run_id=run_id, pid=pid, pgid=pid,
            started_at="2026-08-04T00-00-00", host=os.uname().nodename,
            proc_identity=identity,
        ).to_json()
    )


def _running_manifest() -> Manifest:
    """A minimal `running` manifest for the next-actions assertions."""
    from gauntlet.engine.manifest import PipelineRef, StepRecord

    return Manifest(
        run_id="run-1", slug="alpha", branch="gauntlet/alpha", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_RUNNING,
        steps=[StepRecord(id="s", type="agent_task", status=M.RUNNING)],
    )


class _MidStepAdapter:
    """A builder that runs ``hook()`` while the drive lock is genuinely held."""

    name = "midstep"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def __init__(self, hook, writes: dict[str, str] | None = None) -> None:
        self.hook = hook
        self.writes = writes or {"feature.py": "x\n"}

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.hook()
        for rel, body in self.writes.items():
            (Path(cwd) / rel).write_text(body)
        return AgentResult(text="done", exit_code=0)


@pytest.fixture(autouse=True)
def _isolate_repo_lock():
    """The repo-lock reentrancy table is process-global; reset it per test."""
    repolock._reset_for_tests()
    yield
    repolock._reset_for_tests()


# =============================================================================
# Decision A — the double-driving regression the retained tree guard prevents
# =============================================================================


def test_two_concurrent_drives_of_different_slugs_cannot_both_proceed(fixture_repo):
    """Decision A, the cross-slug half: one shared tree, one driver.

    The per-run lock cannot supply this — ``alpha`` and ``beta`` take different
    per-run paths by construction. Only the retained worktree-global guard
    stops the second verb, and it must keep doing so until P7c gives each run
    its own tree (spike §8.1's one-branch-one-worktree rule is what replaces it).
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    _author_prd(mgr, "beta")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "author both PRDs")
    linear = _pipeline(fixture_repo, LINEAR)
    gated = _pipeline(fixture_repo, GATED, name="gated")

    seen: list[object] = []

    def drive_beta() -> None:
        try:
            RunManager(fixture_repo).start("beta", gated, use_judge=False)
            seen.append("PROCEEDED")
        except Exception as exc:  # noqa: BLE001 - the type is the assertion
            seen.append(exc)

    assert mgr.start(
        "alpha", linear, use_judge=False,
        adapter_factory=lambda n: _MidStepAdapter(drive_beta),
    ) == M.RUN_DONE

    assert len(seen) == 1
    assert isinstance(seen[0], WorktreeLockError), seen
    assert "being driven by alpha" in str(seen[0])
    # ...and beta genuinely never started: no run instance, no per-run lock.
    assert not (fixture_repo / "runs" / "beta" / "active-run.txt").exists()


def test_two_concurrent_starts_of_the_same_slug_cannot_both_proceed(fixture_repo):
    """Decision A, the same-slug half — the case per-run locks make WORSE.

    Two concurrent ``gauntlet run <slug>`` invocations mint DIFFERENT run ids
    (``run-<utc>``), so a purely per-run lock puts them at two different paths
    and both proceed, with only the racy ``active-run.txt`` check between them.
    The refusal must be the LOCK's (``WorktreeLockError``), not the per-slug
    orphan guard's (``ActiveRunError``) — the orphan guard runs after the
    acquisition and is moot at a gate, so it cannot carry this guarantee.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "author PRD")
    linear = _pipeline(fixture_repo, LINEAR)

    seen: list[object] = []

    def second_start() -> None:
        try:
            RunManager(fixture_repo).start("alpha", linear, use_judge=False)
            seen.append("PROCEEDED")
        except Exception as exc:  # noqa: BLE001
            seen.append(exc)

    assert mgr.start(
        "alpha", linear, use_judge=False,
        adapter_factory=lambda n: _MidStepAdapter(second_start),
    ) == M.RUN_DONE

    assert isinstance(seen[0], WorktreeLockError), seen
    # Exactly one run instance exists — the second start minted nothing.
    instances = sorted(
        p.name for p in (fixture_repo / "runs" / "alpha").iterdir()
        if p.is_dir() and p.name.startswith("run-")
    )
    assert len(instances) == 1, instances


def test_both_scopes_are_held_during_a_drive_and_both_released_after(fixture_repo):
    """One acquisition, one nonce, two files — and a clean release of both."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "author PRD")
    linear = _pipeline(fixture_repo, LINEAR)
    observed: dict[str, object] = {}

    def inspect() -> None:
        run_dir = mgr.layout("alpha").active_run_dir()
        tree = locking.read_record(_tree_lock(fixture_repo))
        run = locking.read_record(run_dir / DRIVING_LOCK_NAME)
        observed["tree"] = tree
        observed["run"] = run
        observed["run_dir"] = run_dir

    assert mgr.start(
        "alpha", linear, use_judge=False,
        adapter_factory=lambda n: _MidStepAdapter(inspect),
    ) == M.RUN_DONE

    tree, run = observed["tree"], observed["run"]
    assert tree is not None and run is not None, observed
    assert tree.nonce == run.nonce  # one acquisition
    assert tree.pid == run.pid == os.getpid()
    assert run.slug == "alpha" and run.run_id is not None
    # Released in reverse order, both gone.
    assert not _tree_lock(fixture_repo).exists()
    assert not (observed["run_dir"] / DRIVING_LOCK_NAME).exists()


def test_per_run_lock_never_dirties_the_worktree(fixture_repo):
    """Problem B: the run dir is self-ignored BEFORE the lock lands in it.

    The per-run lock is the first file written into a run-instance dir, and it
    lands before the Orchestrator's ``_ignore_run_dir`` runs. Without the
    engine writing the ``*`` marker at acquisition time the lock would be
    briefly visible to ``git status`` and would dirty the tree ahead of the
    first clean-handoff guard (FR-9.3).
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "author PRD")
    linear = _pipeline(fixture_repo, LINEAR)
    seen: dict[str, object] = {}

    def inspect() -> None:
        run_dir = mgr.layout("alpha").active_run_dir()
        seen["gitignore"] = (run_dir / ".gitignore").read_text()
        seen["dirt"] = gitops.status_porcelain(fixture_repo, untracked_all=True)

    mgr.start(
        "alpha", linear, use_judge=False,
        adapter_factory=lambda n: _MidStepAdapter(inspect),
    )
    assert seen["gitignore"] == "*\n"
    assert DRIVING_LOCK_NAME not in str(seen["dirt"])


def test_a_refused_start_leaves_no_empty_run_instance(fixture_repo):
    """The per-run lock must not be bootstrapped by minting a run dir eagerly.

    ``start`` acquires before it can know whether it will proceed (a still-
    active run, a dirty worktree). If acquisition created ``run-<ts>/`` to hold
    the lock, a refusal would leave an empty instance behind — and with no
    ``active-run.txt``, ``resolve_run_instance`` picks the lexicographically
    greatest instance, i.e. exactly that manifest-less husk.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "author PRD")
    gated = _pipeline(fixture_repo, GATED)
    assert mgr.start("alpha", gated, use_judge=False) == M.RUN_PARKED
    before = sorted(p.name for p in (fixture_repo / "runs" / "alpha").iterdir())
    from gauntlet.engine.run import ActiveRunError

    with pytest.raises(ActiveRunError):  # the parked run blocks a second start
        mgr.start("alpha", gated, use_judge=False)
    assert sorted(p.name for p in (fixture_repo / "runs" / "alpha").iterdir()) == before


# =============================================================================
# Migration (spike §10) — a legacy lock is READ, and a live one refuses
# =============================================================================


def test_live_legacy_lock_refuses_every_driving_verb(fixture_repo):
    """A half-migrated machine cannot double-drive (spike §10).

    A pre-P7b engine writes ONLY ``<run_root>/.driving.lock``. This engine must
    treat a live one as a live driver and refuse — which is exactly what
    retaining the tree guard at that path buys, in both directions: the old
    engine also still sees this engine's hold.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    gated = _pipeline(fixture_repo, GATED)
    assert mgr.start("alpha", gated, use_judge=False) == M.RUN_PARKED
    run_dir = mgr.layout("alpha").active_run_dir()
    assert not (run_dir / DRIVING_LOCK_NAME).exists()  # released at the gate

    # A pre-P7b driver of THIS slug, live, with only the legacy file on disk.
    _write_lock(
        _tree_lock(fixture_repo), pid=os.getpid(), identity=_live_identity(),
        slug="alpha", run_id="run-legacy",
    )
    with pytest.raises(WorktreeLockError, match="being driven by alpha"):
        mgr.resume("alpha", use_judge=False)
    with pytest.raises(WorktreeLockError):
        mgr.approve("alpha", notes="ok", use_judge=False)
    with pytest.raises(WorktreeLockError):
        mgr.rollback("alpha", phase=1)
    # No per-run lock was minted while the legacy holder was live.
    assert not (run_dir / DRIVING_LOCK_NAME).exists()


def test_legacy_lock_is_read_by_driver_info_without_a_per_run_lock(fixture_repo):
    """A legacy run's liveness is answerable with no migration at all."""
    run_root = fixture_repo / "runs"
    run_dir = run_root / "alpha" / "run-legacy"
    run_dir.mkdir(parents=True)
    _write_lock(
        _tree_lock(fixture_repo), pid=os.getpid(), identity=_live_identity(),
        slug="alpha", run_id="run-legacy",
    )
    info = op.driver_info(run_root, "alpha", run_instance_dir=run_dir)
    assert info.state == op.LIVENESS_ALIVE
    assert info.pid == os.getpid()


def test_per_run_lock_wins_over_a_stale_legacy_lock(fixture_repo):
    """The per-run path is authoritative when it answers."""
    run_root = fixture_repo / "runs"
    run_dir = run_root / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    # A stale legacy lock (dead pid) and a live per-run lock for the same run.
    _write_lock(_tree_lock(fixture_repo), pid=2_000_000_000, identity=None,
                slug="alpha", nonce="legacy")
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=os.getpid(),
                identity=_live_identity(), slug="alpha", nonce="fresh")
    assert op.driver_liveness(
        run_root, "alpha", run_instance_dir=run_dir
    ) == op.LIVENESS_ALIVE


def test_malformed_per_run_lock_does_not_fall_through_to_the_legacy_path(
    fixture_repo,
):
    """Row g must not be silently upgraded by consulting a different file.

    An unreadable lock for exactly this run is ``indeterminate`` (fail closed).
    Falling back to a healthy legacy lock would answer confidently about a
    driver we cannot actually see.
    """
    run_root = fixture_repo / "runs"
    run_dir = run_root / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / DRIVING_LOCK_NAME).write_text("{ not json")
    _write_lock(_tree_lock(fixture_repo), pid=2_000_000_000, identity=None,
                slug="alpha")
    assert op.driver_liveness(
        run_root, "alpha", run_instance_dir=run_dir
    ) == op.LIVENESS_INDETERMINATE


def test_fr24_row_b_stays_reachable_for_a_foreign_tree_hold(fixture_repo):
    """Row b (foreign lock → none) is NOT legacy-only in P7b.

    While the tree is shared, another slug driving it holds the worktree-global
    lock under ITS name, and this slug has no per-run lock — which is the row's
    live, non-legacy trigger. "No driver for alpha" is the true answer: beta's
    hold is transient contention on the tree, not evidence about alpha's run.
    """
    run_root = fixture_repo / "runs"
    run_dir = run_root / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    _write_lock(_tree_lock(fixture_repo), pid=os.getpid(),
                identity=_live_identity(), slug="beta", run_id="run-b")
    info = op.driver_info(run_root, "alpha", run_instance_dir=run_dir)
    assert info.state == op.LIVENESS_NONE
    assert (info.pid, info.host, info.since) == (None, None, None)


def test_foreign_record_at_the_per_run_path_is_indeterminate_not_none(fixture_repo):
    """Review F-003: inconsistent evidence at a run's OWN path must fail closed.

    An earlier revision classified this as row b (``none``) "defensively". That
    was the wrong direction and this test previously pinned it: ``none`` means
    "no driver — resume is safe", while the mutating acquisition refuses the
    very same file as a live foreign holder. That is the R4 disagreement the
    plan forbids ("the read-only view cannot ... recommend a resume that the
    mutating path will reject"). The per-run path is this run's authoritative
    evidence; a record naming someone else there is misplaced or corrupt, and
    the only non-misleading answer is ``indeterminate``.
    """
    mgr = _prepare(fixture_repo)
    run_root = fixture_repo / "runs"
    run_dir = run_root / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=os.getpid(),
                identity=_live_identity(), slug="beta", run_id="run-b")

    state = op.driver_liveness(run_root, "alpha", run_instance_dir=run_dir)
    assert state == op.LIVENESS_INDETERMINATE
    # Indeterminate offers read-only actions only — so status no longer proposes
    # an action the mutating path rejects.
    man = _running_manifest()
    actions = op.next_actions(man, state)
    assert {a.kind for a in actions} == {"observe"}, actions
    # ...and the mutating path does refuse, which is what it must agree with.
    with pytest.raises(WorktreeLockError):
        mgr._acquire_worktree_lock("alpha", "run-1", run_dir=run_dir)


def test_driver_info_without_an_instance_reads_the_worktree_global_lock(
    fixture_repo,
):
    """The pre-P7b two-argument call is unchanged — no caller was forced to move."""
    run_root = fixture_repo / "runs"
    _write_lock(_tree_lock(fixture_repo), pid=os.getpid(),
                identity=_live_identity(), slug="alpha")
    assert op.driver_liveness(run_root, "alpha") == op.LIVENESS_ALIVE


# =============================================================================
# Reclaim semantics, unchanged, at BOTH scopes
# =============================================================================


def test_stale_per_run_lock_is_reclaimed(fixture_repo):
    """A dead holder's per-run lock never wedges the run (R1)."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    gated = _pipeline(fixture_repo, GATED)
    assert mgr.start("alpha", gated, use_judge=False) == M.RUN_PARKED
    run_dir = mgr.layout("alpha").active_run_dir()
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=2_000_000_000, identity=None,
                slug="alpha")
    assert mgr.approve("alpha", notes="ok", use_judge=False) == M.RUN_DONE
    assert not (run_dir / DRIVING_LOCK_NAME).exists()


def test_unverifiable_live_per_run_lock_is_not_reclaimed(fixture_repo):
    """The `_lock_is_live` asymmetry holds at the new scope too.

    A LIVE pid whose identity is unverifiable blocks; it is never stolen. This
    is the deliberate opposite of ``procident.process_is_alive``.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    gated = _pipeline(fixture_repo, GATED)
    assert mgr.start("alpha", gated, use_judge=False) == M.RUN_PARKED
    run_dir = mgr.layout("alpha").active_run_dir()
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=os.getpid(), identity=None,
                slug="alpha")
    with pytest.raises(WorktreeLockError, match="being driven by alpha"):
        mgr.approve("alpha", notes="ok", use_judge=False)
    # ...and the tree guard was not left behind by the refused acquisition.
    assert not _tree_lock(fixture_repo).exists()


# --- review F-002: a lock we cannot read is never a lock we may delete -------


@pytest.mark.parametrize("scope", ["tree", "run"])
@pytest.mark.parametrize("how", ["unparseable", "unreadable"])
def test_malformed_lock_is_never_reclaimed_at_either_scope(fixture_repo, scope, how):
    """A lockfile that EXISTS but cannot be read must fail the verb closed.

    The reclaim path used to collapse absent/unreadable/unparseable into one
    ``None`` and then unlink "the corrupt lock". That is a fail-closed
    violation with teeth: an ``OSError`` on read (permissions, I/O) is exactly
    what a LIVE driver's lock looks like when the file is momentarily
    unreadable, and stealing it re-opens double-driving. It was also a direct
    R4 disagreement — ``driver_info`` reports these as ``indeterminate`` while
    the mutating path deleted the evidence and proceeded.
    """
    mgr = _prepare(fixture_repo)
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    path = _tree_lock(fixture_repo) if scope == "tree" else run_dir / DRIVING_LOCK_NAME

    if how == "unparseable":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not valid json ")
        expected = "{ not valid json "
    else:
        # A LIVE, identity-verifiable holder whose file we cannot read.
        rec = locking.new_record("beta", "run-b")
        _write_lock(path, pid=os.getpid(), identity=_live_identity(),
                    slug="beta", nonce=rec.nonce)
        expected = path.read_text()
        os.chmod(path, 0o000)

    try:
        # The read-only view already fails closed here (FR-2.4 row g)...
        assert op.driver_liveness(
            fixture_repo / "runs", "alpha", run_instance_dir=run_dir
        ) == op.LIVENESS_INDETERMINATE
        # ...and now so does the mutating path, instead of unlinking it.
        with pytest.raises(WorktreeLockError, match="cannot be read or parsed"):
            mgr._acquire_worktree_lock("alpha", "run-1", run_dir=run_dir)
    finally:
        if how == "unreadable":
            os.chmod(path, 0o600)
    assert path.exists(), "the unreadable lock was deleted"
    assert path.read_text() == expected, "the unreadable lock was overwritten"
    # No partial hold survived the refusal.
    assert mgr._held_lock is None
    if scope == "run":
        assert not _tree_lock(fixture_repo).exists()


def test_malformed_per_run_lock_does_not_fall_through_in_the_engine_read(
    fixture_repo,
):
    """`RunManager._read_lock` must not answer from a different file (F-002).

    `recover` keys its FR-5.1 gate on this read. Falling through from an
    unreadable per-run lock to the tree guard would let it verify (and signal)
    against a driver whose own evidence it could not read.
    """
    mgr = _prepare(fixture_repo)
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / DRIVING_LOCK_NAME).write_text("{ not valid json ")
    _write_lock(_tree_lock(fixture_repo), pid=os.getpid(),
                identity=_live_identity(), slug="alpha", nonce="tree-nonce")
    assert mgr._read_lock(run_dir) is None  # not the tree guard's record
    # An ABSENT per-run lock still falls back (the legacy-run read path).
    (run_dir / DRIVING_LOCK_NAME).unlink()
    rec = mgr._read_lock(run_dir)
    assert rec is not None and rec.nonce == "tree-nonce"


def _seed_intent_run(repo: Path, *, lock_body: str | None) -> tuple[RunManager, Path]:
    """A run with a surviving recovery intent whose target step is `running`."""
    from gauntlet.engine.manifest import PipelineRef, StepRecord
    from gauntlet.engine.run import RECOVERY_INTENT_NAME

    mgr = _prepare(repo)
    run_dir = repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    Manifest(
        run_id="run-1", slug="alpha", branch="gauntlet/alpha", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"), status=M.RUN_RUNNING,
        steps=[StepRecord(id="s", type="agent_task", status=M.RUNNING)],
    ).write_atomic(run_dir / "manifest.json")
    (run_dir / RECOVERY_INTENT_NAME).write_text(json.dumps({
        "ts": "t", "actor": "a", "actor_source": "os_user", "reason": None,
        "lock_nonce": "N", "pid": 2_000_000_000, "pgid": 2_000_000_000,
        "proc_identity": None, "host": os.uname().nodename, "step_id": "s",
        "prior_step_status": "running", "prior_run_status": "running",
    }))
    if lock_body is not None:
        (run_dir / DRIVING_LOCK_NAME).write_text(lock_body)
    return mgr, run_dir


def test_malformed_lock_blocks_recovery_intent_finalization(fixture_repo):
    """F-002 confirm pass: 'cannot read the lock' is not 'the lock is absent'.

    `_reconcile_recovery_intent` runs on BOTH `resume` and `recover`, before any
    lock is acquired, and its live branch signals a process and rewrites the
    manifest. It keyed on `_read_lock`, which collapsed malformed into `None` —
    so an unreadable lock authorized finalization as though the driver were
    provably gone. `operator.read_recovery_intent` (the read-only detector)
    already failed closed here, making this the same R4 disagreement as the
    original finding.
    """
    from gauntlet.engine.run import RECOVERY_INTENT_NAME

    mgr, run_dir = _seed_intent_run(fixture_repo, lock_body="{ not valid json ")
    note = mgr._reconcile_recovery_intent(run_dir)

    assert note is not None and "cannot be read or parsed" in note
    assert (run_dir / RECOVERY_INTENT_NAME).exists(), "the intent was consumed"
    man = Manifest.load(run_dir / "manifest.json")
    assert man.steps[0].status == M.RUNNING, "the manifest was mutated"
    assert man.recoveries == []
    # The refusal names the file the operator has to look at.
    assert str(run_dir / DRIVING_LOCK_NAME) in note


def test_absent_lock_still_finalizes_a_recovery_intent(fixture_repo):
    """The counterfactual that proves the branch above genuinely diverged.

    An ABSENT lock is a fact — the verified target was killed and nothing
    relaunched — and must still finalize. Without this, the fix could be a
    blanket refusal that quietly disables FR-5.6 reconciliation.
    """
    mgr, run_dir = _seed_intent_run(fixture_repo, lock_body=None)
    mgr._reconcile_recovery_intent(run_dir)
    man = Manifest.load(run_dir / "manifest.json")
    assert man.steps[0].status != M.RUNNING
    assert len(man.recoveries) == 1


def test_vanished_lock_is_still_acquirable(fixture_repo):
    """Fail-closed on unreadable must not break the genuine `absent` race.

    `absent` is the one kind that may be treated as free; the tri-state split
    exists precisely so tightening `malformed` does not wedge this path.
    """
    mgr = _prepare(fixture_repo)
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    handle = mgr._acquire_worktree_lock("alpha", "run-1", run_dir=run_dir)
    mgr._release_worktree_lock(handle)
    again = mgr._acquire_worktree_lock("alpha", "run-1", run_dir=run_dir)
    assert again.run_path == run_dir / DRIVING_LOCK_NAME
    mgr._release_worktree_lock(again)


def test_repo_lock_never_reclaims_an_unreadable_holder(fixture_repo):
    """The same rule at the third scope — one policy, not three (F-002)."""
    path = repolock.repo_lock_path(fixture_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not valid json ")
    with pytest.raises(repolock.RepoLockError, match="unreadable"):
        with repolock.repo_lock(fixture_repo, reason="thief", timeout_s=0.2,
                                sleep=lambda _s: None):
            pass
    assert path.read_text() == "{ not valid json "


def test_one_tri_state_lock_read_is_shared_by_every_scope():
    """The reclaim decision has exactly one reader (F-002).

    Two readers with different notions of "unreadable" is how the operator view
    and the mutating verbs disagreed in the first place.
    """
    import inspect

    assert "locking.read_lock_state" in inspect.getsource(op._lock_file_state)
    assert "read_lock_state" in inspect.getsource(repolock._acquire)
    assert "read_lock_state" in inspect.getsource(RunManager._acquire_one)
    assert "read_lock_state" in inspect.getsource(RunManager._try_reclaim)


def test_a_failed_per_run_acquisition_releases_the_tree_guard(fixture_repo):
    """No partial hold survives: acquisition is tree-then-run, all or nothing."""
    mgr = _prepare(fixture_repo)
    run_root = fixture_repo / "runs"
    run_dir = run_root / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=os.getpid(), identity=None,
                slug="alpha")
    with pytest.raises(WorktreeLockError):
        mgr._acquire_worktree_lock("alpha", "run-1", run_dir=run_dir)
    assert not _tree_lock(fixture_repo).exists()
    assert mgr._held_lock is None


def test_concurrent_acquire_of_one_run_yields_exactly_one_holder(fixture_repo):
    mgr_seed = _prepare(fixture_repo)
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    results: list[object] = []
    guard = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        mgr = RunManager(fixture_repo)
        barrier.wait()
        try:
            handle = mgr._acquire_worktree_lock("alpha", "run-1", run_dir=run_dir)
            with guard:
                results.append((mgr, handle))
        except WorktreeLockError:
            with guard:
                results.append("fail")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    holders = [r for r in results if not isinstance(r, str)]
    assert len(holders) == 1, results
    assert results.count("fail") == 7
    mgr, handle = holders[0]
    assert handle.run_path == run_dir / DRIVING_LOCK_NAME
    mgr._release_worktree_lock(handle)
    assert not (run_dir / DRIVING_LOCK_NAME).exists()
    assert not _tree_lock(fixture_repo).exists()
    del mgr_seed


def test_release_by_nonce_clears_both_scopes(fixture_repo):
    """`recover`'s step-8 release must not leave the wedged driver's tree guard.

    Releasing only the per-run lock would leave a worktree-global hold behind
    with a dead owner, wedging every driving verb on the tree — the opposite of
    what recovery is for (R1).
    """
    mgr = _prepare(fixture_repo)
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    _write_lock(_tree_lock(fixture_repo), pid=2_000_000_000, identity=None,
                slug="alpha", nonce="wedged")
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=2_000_000_000, identity=None,
                slug="alpha", nonce="wedged")
    mgr._release_lock_if_nonce("wedged", run_dir)
    assert not _tree_lock(fixture_repo).exists()
    assert not (run_dir / DRIVING_LOCK_NAME).exists()


def test_release_by_nonce_spares_a_new_owner_at_either_scope(fixture_repo):
    """F-004 at both scopes: never unlink a fresh owner's lock."""
    mgr = _prepare(fixture_repo)
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    _write_lock(_tree_lock(fixture_repo), pid=os.getpid(),
                identity=_live_identity(), slug="alpha", nonce="fresh")
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=2_000_000_000, identity=None,
                slug="alpha", nonce="wedged")
    mgr._release_lock_if_nonce("wedged", run_dir)
    assert _tree_lock(fixture_repo).exists()  # fresh owner untouched
    assert not (run_dir / DRIVING_LOCK_NAME).exists()


# =============================================================================
# R1 — a lock that vanishes or is abandoned mid-drive
# =============================================================================


def test_lock_deleted_mid_drive_leaves_a_resumable_run(fixture_repo):
    """R1: a vanished lock must not crash the drive nor wedge the run.

    An operator (or a `clean -xdff` on an adopter that ignores the run root)
    can remove the lock files under a live driver. The release is then a
    nonce-guarded no-op, the drive completes, and the next verb still works —
    a safe executable action always exists.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "alpha")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "author PRD")
    gated = _pipeline(fixture_repo, GATED)

    # Park first so the run dir exists, then re-drive through approve with a
    # step that deletes both lock files while they are genuinely held.
    assert mgr.start("alpha", gated, use_judge=False) == M.RUN_PARKED
    run_dir = mgr.layout("alpha").active_run_dir()

    held: dict[str, bool] = {}

    class _Vandal:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self) -> None:
            held["tree_before"] = _tree_lock(fixture_repo).exists()
            held["run_before"] = (run_dir / DRIVING_LOCK_NAME).exists()
            _tree_lock(fixture_repo).unlink()
            (run_dir / DRIVING_LOCK_NAME).unlink()

    # `approve` re-drives; the `after` shell step runs under the held lock, so
    # remove the files from a manifest-write hook instead of an agent step.
    vandal = _Vandal()
    real_write = Manifest.write_atomic
    done = {"fired": False}

    def hooked(self, path, *a, **kw):
        if not done["fired"] and path.name == "manifest.json":
            done["fired"] = True
            vandal()
        return real_write(self, path, *a, **kw)

    Manifest.write_atomic = hooked
    try:
        status = mgr.approve("alpha", notes="ok", use_judge=False)
    finally:
        Manifest.write_atomic = real_write

    assert held == {"tree_before": True, "run_before": True}
    assert status == M.RUN_DONE  # the drive survived its lock vanishing
    # And the run is not wedged: a further verb acquires cleanly.
    handle = mgr._acquire_worktree_lock("alpha", "run-x", run_dir=run_dir)
    mgr._release_worktree_lock(handle)


# =============================================================================
# `_crash_child` boundaries around lock acquire / release
# =============================================================================


def _crash_repo(tmp: Path) -> tuple[Path, RunManager]:
    repo = tmp
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@gauntlet.local")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("crash fixture\n")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    (repo / "pipelines").mkdir()
    (repo / "pipelines" / "crash.yaml").write_text(CRASH_PIPELINE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "-M", "main")
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# PRD\n\nReal human-authored PRD.\n")
    return repo, mgr


class _RecoverAdapter:
    name = "recover"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        (Path(cwd) / "feature.py").write_text("RECOVERED — final content\n")
        return AgentResult(text="recovered", session_id="r", exit_code=0)


@pytest.mark.parametrize("sig", ["kill", "term"])
@pytest.mark.parametrize(
    "point", ["after_tree", "after_run", "before_release"]
)
def test_kill_at_every_lock_boundary_never_wedges_the_tree(tmp_path, sig, point):
    """R1 at the three boundaries the split lock introduces.

    A real process death holding one or both lock files must leave a state a
    later verb can reclaim — the fail-closed reclaim rule proves the holder is
    dead — never a permanently-held tree.
    """
    repo, mgr = _crash_repo(tmp_path / f"{point}-{sig}")
    proc = subprocess.run(
        [sys.executable, str(CHILD), str(repo), "demo", f"lock:{point}:{sig}"],
        timeout=120, capture_output=True,
    )
    assert proc.returncode != 0, "the self-signal did not kill the child"

    # Whatever landed on disk, the holder is provably dead, so a driving verb
    # reclaims rather than fails closed forever.
    stale = [p for p in (
        repo / "runs" / DRIVING_LOCK_NAME,
        *(repo / "runs" / "demo").glob(f"run-*/{DRIVING_LOCK_NAME}"),
    ) if p.exists()]
    assert stale, f"boundary {point} left no lock to reclaim — nothing to prove"

    if (repo / "runs" / "demo" / "active-run.txt").exists():
        status = mgr.resume(
            "demo", use_judge=False, adapter_factory=lambda n: _RecoverAdapter()
        )
    else:
        # Killed before the run was durably pointed at: a fresh start must be
        # able to reclaim the abandoned tree guard.
        status = mgr.start(
            "demo", repo / "pipelines" / "crash.yaml", use_judge=False,
            adapter_factory=lambda n: _RecoverAdapter(),
        )
    assert status == M.RUN_DONE
    assert not (repo / "runs" / DRIVING_LOCK_NAME).exists()


# =============================================================================
# The recovery executor's lock guard follows the lock
# =============================================================================


def test_lock_guard_follows_the_lock_to_the_per_run_path(fixture_repo):
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    guard = WorktreeLockGuard(fixture_repo, "runs", run_dir=run_dir)
    assert guard.lock_path == run_dir / DRIVING_LOCK_NAME
    assert guard.tree_lock_path == fixture_repo / "runs" / DRIVING_LOCK_NAME


def test_lock_guard_verifies_a_lock_this_process_holds(fixture_repo):
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    _write_lock(_tree_lock(fixture_repo), pid=os.getpid(),
                identity=_live_identity(), slug="alpha")
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=os.getpid(),
                identity=_live_identity(), slug="alpha")
    with WorktreeLockGuard(fixture_repo, "runs", run_dir=run_dir).hold():
        pass  # verification path: nothing created, nothing removed
    assert (run_dir / DRIVING_LOCK_NAME).exists()
    assert _tree_lock(fixture_repo).exists()


def test_lock_guard_refuses_a_live_foreign_holder_at_either_scope(fixture_repo):
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # (a) foreign live holder of the per-run lock.
        _write_lock(run_dir / DRIVING_LOCK_NAME, pid=other.pid, identity=None,
                    slug="alpha")
        with pytest.raises(RecoveryLockError, match="held by live pid"):
            with WorktreeLockGuard(fixture_repo, "runs", run_dir=run_dir).hold():
                pass
        (run_dir / DRIVING_LOCK_NAME).unlink()
        # (b) foreign live holder of the TREE guard — another slug owns this
        # shared tree, so a rewind of it must refuse even though this run's own
        # lock is absent. This gate is new in P7b; it cannot be weaker.
        _write_lock(_tree_lock(fixture_repo), pid=other.pid, identity=None,
                    slug="beta")
        with pytest.raises(RecoveryLockError, match="held by live pid"):
            with WorktreeLockGuard(fixture_repo, "runs", run_dir=run_dir).hold():
                pass
    finally:
        other.kill()
        other.wait(timeout=10)


def test_lock_guard_refuses_a_stale_foreign_lock_without_reclaiming(fixture_repo):
    """Reclaim policy stays in RunManager; the guard only refuses."""
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    _write_lock(run_dir / DRIVING_LOCK_NAME, pid=2_000_000_000, identity=None,
                slug="alpha", nonce="stale")
    with pytest.raises(RecoveryLockError, match="not verifiably this process"):
        with WorktreeLockGuard(fixture_repo, "runs", run_dir=run_dir).hold():
            pass
    rec = locking.read_record(run_dir / DRIVING_LOCK_NAME)
    assert rec is not None and rec.nonce == "stale"  # untouched


def test_lock_guard_ephemeral_hold_takes_both_scopes(fixture_repo):
    """An embedded caller with no verb around it must not hold LESS than before.

    Pre-P7b the ephemeral lock was the worktree-global one. Taking only the
    per-run lock would let a concurrent RunManager verb drive a different run
    against this same tree mid-rewind.
    """
    mgr = _prepare(fixture_repo)
    run_dir = fixture_repo / "runs" / "alpha" / "run-1"
    run_dir.mkdir(parents=True)
    with WorktreeLockGuard(fixture_repo, "runs", run_dir=run_dir).hold():
        assert (run_dir / DRIVING_LOCK_NAME).exists()
        assert _tree_lock(fixture_repo).exists()
        assert mgr._read_lock(run_dir) is not None
    assert not (run_dir / DRIVING_LOCK_NAME).exists()
    assert not _tree_lock(fixture_repo).exists()


def test_lock_guard_without_a_run_dir_keeps_the_pre_p7b_path(fixture_repo):
    guard = WorktreeLockGuard(fixture_repo, "runs")
    assert guard.lock_path == fixture_repo / "runs" / DRIVING_LOCK_NAME
    with guard.hold():
        assert guard.lock_path.exists()
    assert not guard.lock_path.exists()


# =============================================================================
# The repo-global git lock (spike §8.3 layer 2)
# =============================================================================


def test_run_paths_common_dir_survives_a_missing_work_tree(fixture_repo, tmp_path):
    """Review F-001: the common dir must resolve when work_root is GONE.

    That is the P7 A3 incident — recreate a missing run worktree — and it is
    the one moment a lookup routed through ``work_root`` cannot answer. Routing
    it through ``repo_root`` (the operator's surviving checkout) makes the
    answer available exactly when it is needed.
    """
    from gauntlet.engine.execution import RunPaths

    missing = tmp_path / "run-worktree-that-was-swept-away"
    paths = RunPaths(
        repo_root=fixture_repo,
        work_root=missing,
        state_root=fixture_repo / "runs" / "alpha" / "run-1",
        artifact_root=fixture_repo / "runs" / "alpha",
    )
    assert not missing.exists()
    assert paths.dedicated_worktree
    assert paths.git_common_dir() == gitops.git_common_dir(fixture_repo)
    # ...and the repo-global lock path, which is derived from it, is reachable.
    assert repolock.repo_lock_path(fixture_repo).parent.parent == paths.git_common_dir()


def test_repo_lock_path_is_under_the_git_common_dir(fixture_repo):
    path = repolock.repo_lock_path(fixture_repo)
    assert path == gitops.git_common_dir(fixture_repo) / "gauntlet" / ".repo.lock"
    # Inside `.git/`, so it is invisible to status/clean in EVERY worktree —
    # no gitignore rule can be forgotten.
    with repolock.repo_lock(fixture_repo, reason="t"):
        assert path.exists()
        assert gitops.is_clean(fixture_repo)
    assert not path.exists()


def test_two_linked_worktrees_share_the_repo_lock_but_not_the_drive_lock(
    fixture_repo, tmp_path
):
    """Why layer 2 is not redundant with the drive lock (spike §8.3 / E8-C).

    The drive lock lives under ``<checkout>/runs``, so two linked worktrees of
    ONE repository have two independent drive locks and can drive concurrently
    — while sharing one object DB, one ref store, and one worktree
    administration dir. `git worktree prune` is repository-wide, so run A's
    verifier teardown can remove run B's admin entry. Only a lock under the
    git COMMON dir excludes them.
    """
    linked = tmp_path / "linked"
    git(fixture_repo, "worktree", "add", "-q", "-b", "side", str(linked))
    try:
        assert gitops.git_common_dir(linked) == gitops.git_common_dir(fixture_repo)
        assert repolock.repo_lock_path(linked) == repolock.repo_lock_path(fixture_repo)
        # ...but the drive locks are two different files.
        assert _tree_lock(linked) != _tree_lock(fixture_repo)
        # A hold from "the other worktree" therefore excludes this one.
        other = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"]
        )
        try:
            _write_lock(repolock.repo_lock_path(linked), pid=other.pid,
                        identity=None, slug="verify:discard-copy")
            with pytest.raises(repolock.RepoLockError):
                with repolock.repo_lock(fixture_repo, reason="b", timeout_s=0.0):
                    pass
        finally:
            other.kill()
            other.wait(timeout=10)
            repolock.repo_lock_path(linked).unlink(missing_ok=True)
    finally:
        git(fixture_repo, "worktree", "remove", "--force", str(linked))


def test_repo_lock_excludes_a_second_holder(fixture_repo):
    """The live critical section: two runs must not `worktree prune` at once."""
    with repolock.repo_lock(fixture_repo, reason="a"):
        # A *different* process's hold is what a second run looks like; model it
        # by a live foreign record at the same path.
        pass
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(repolock.repo_lock_path(fixture_repo), pid=other.pid,
                    identity=None, slug="verify:add-disposable-copy")
        with pytest.raises(repolock.RepoLockError, match="within 0s"):
            with repolock.repo_lock(fixture_repo, reason="b", timeout_s=0.0):
                pass
    finally:
        other.kill()
        other.wait(timeout=10)


def test_repo_lock_waits_then_succeeds_when_the_holder_releases(fixture_repo):
    path = repolock.repo_lock_path(fixture_repo)
    _write_lock(path, pid=os.getpid(), identity=_live_identity(), slug="holder")
    calls = {"n": 0}

    def sleeper(_interval: float) -> None:
        calls["n"] += 1
        if calls["n"] == 3:
            path.unlink()  # the holder finished its section

    with repolock.repo_lock(fixture_repo, reason="waiter", timeout_s=10.0,
                            sleep=sleeper):
        assert path.exists()
    assert calls["n"] >= 3
    assert not path.exists()


def test_repo_lock_reclaims_a_proven_dead_holder(fixture_repo):
    path = repolock.repo_lock_path(fixture_repo)
    _write_lock(path, pid=2_000_000_000, identity=None, slug="crashed")
    with repolock.repo_lock(fixture_repo, reason="fresh", timeout_s=5.0):
        rec = locking.read_record(path)
        assert rec is not None and rec.slug == "fresh"
    assert not path.exists()


def test_repo_lock_never_steals_an_unverifiable_live_holder(fixture_repo):
    """The same fail-closed asymmetry as the drive lock, not a second rule."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        path = repolock.repo_lock_path(fixture_repo)
        _write_lock(path, pid=other.pid, identity=None, slug="live-unverifiable")
        with pytest.raises(repolock.RepoLockError):
            with repolock.repo_lock(fixture_repo, reason="thief", timeout_s=0.2,
                                    sleep=lambda _s: None):
                pass
        rec = locking.read_record(path)
        assert rec is not None and rec.slug == "live-unverifiable"
    finally:
        other.kill()
        other.wait(timeout=10)


def test_repo_lock_is_reentrant_within_one_process(fixture_repo):
    path = repolock.repo_lock_path(fixture_repo)
    with repolock.repo_lock(fixture_repo, reason="outer"):
        first = locking.read_record(path)
        with repolock.repo_lock(fixture_repo, reason="inner"):
            again = locking.read_record(path)
            assert again is not None and first is not None
            assert again.nonce == first.nonce  # not re-acquired
        assert path.exists()  # the inner exit must not release the outer hold
    assert not path.exists()


def test_snapshot_ref_publication_is_race_safe_without_the_repo_lock(fixture_repo):
    """Why §8.3's snapshot-ref section is NOT under the repo-global lock (F-004b).

    The confirm pass asked for it to be locked narrowly around
    `create_ref_exclusive`. That would wrap an operation git already makes
    atomic: `git update-ref --stdin` with the `create` verb is a ref-store
    transaction that fails when the ref exists (P2 review F-003 chose it for
    exactly this reason). An advisory lock around it adds contention and buys
    nothing — so the decline is recorded as evidence here rather than as prose
    in a docstring.
    """
    import concurrent.futures as cf

    sha = gitops.head_sha(fixture_repo)
    ref = "refs/gauntlet/recovery/race"

    def create(_i: int) -> str:
        try:
            gitops.create_ref_exclusive(fixture_repo, ref, sha)
            return "won"
        except gitops.GitError:
            return "lost"

    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(create, range(16)))

    assert results.count("won") == 1, results
    assert results.count("lost") == 15, results
    assert gitops.rev_parse(fixture_repo, ref) == sha  # the winner's ref stands


def test_run_branch_is_protected_across_worktrees_by_git_itself(fixture_repo, tmp_path):
    """The other half of the F-004b decline: branch mutation (spike E2-B/D/E).

    Branch create/delete/force-update against a branch checked out in another
    worktree hard-refuse in git. That is a stronger guarantee than an advisory
    lockfile, and it is what P7c relies on once each run has its own tree.
    """
    linked = tmp_path / "linked"
    git(fixture_repo, "worktree", "add", "-q", "-b", "gauntlet/held", str(linked))
    try:
        for args in (
            ("checkout", "gauntlet/held"),      # E2-B
            ("branch", "-D", "gauntlet/held"),  # E2-D
            ("branch", "-f", "gauntlet/held", "HEAD"),  # E2-E
        ):
            proc = subprocess.run(
                ["git", "-C", str(fixture_repo), *args],
                capture_output=True, text=True,
            )
            assert proc.returncode != 0, f"git allowed {args!r}"
            assert "used by worktree" in (proc.stderr or "")
    finally:
        git(fixture_repo, "worktree", "remove", "--force", str(linked))
        git(fixture_repo, "branch", "-D", "gauntlet/held")


def test_repo_lock_reentrancy_does_not_leak_across_threads(fixture_repo):
    """Reentrancy is a property of one call stack, not of the process.

    A second thread is a genuine second acquirer; if it inherited the first
    thread's hold it would run the critical section unserialized.
    """
    entered = threading.Event()
    release = threading.Event()
    outcome: list[object] = []

    def holder():
        with repolock.repo_lock(fixture_repo, reason="holder"):
            entered.set()
            release.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert entered.wait(10)
        try:
            with repolock.repo_lock(fixture_repo, reason="other-thread",
                                    timeout_s=0.0):
                outcome.append("PROCEEDED")
        except repolock.RepoLockError as exc:
            outcome.append(exc)
    finally:
        release.set()
        t.join(timeout=10)
    assert isinstance(outcome[0], repolock.RepoLockError), outcome
    assert not repolock.repo_lock_path(fixture_repo).exists()


def test_repo_lock_best_effort_degrades_instead_of_raising(fixture_repo):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(repolock.repo_lock_path(fixture_repo), pid=other.pid,
                    identity=None, slug="holder")
        with repolock.repo_lock_best_effort(
            fixture_repo, reason="teardown", timeout_s=0.0
        ) as acquired:
            assert acquired is False  # the caller decides what is safe to skip
    finally:
        other.kill()
        other.wait(timeout=10)


def test_teardown_skips_shared_git_mutations_when_the_repo_lock_is_unavailable(
    fixture_repo,
):
    """Review F-004: a failed acquire must not run the mutations anyway.

    An earlier revision degraded to "prune anyway", which reproduces spike
    E8-C at exactly the moment the lock exists to prevent it — a contended lock
    means another run is inside its own add/remove/prune section right now, and
    a repository-wide prune there can drop that run's admin entry. The drive
    lock cannot substitute: two linked worktrees have two different drive-lock
    paths. Skipping leaves a `prunable` entry, which the next LOCKED teardown
    cleans up.
    """
    from gauntlet.engine import verify

    copy = verify.make_disposable_copy(fixture_repo)
    called: list[str] = []
    real_remove, real_prune = gitops.remove_worktree, gitops.prune_worktrees
    gitops.remove_worktree = lambda *a, **k: called.append("remove")
    gitops.prune_worktrees = lambda *a, **k: called.append("prune")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    real_best = repolock.repo_lock_best_effort
    repolock.repo_lock_best_effort = lambda w, *, reason, **kw: real_best(
        w, reason=reason, timeout_s=0.0
    )
    try:
        _write_lock(repolock.repo_lock_path(fixture_repo), pid=other.pid,
                    identity=None, slug="another-run")
        verify.discard_disposable_copy(fixture_repo, copy)  # must not raise
    finally:
        repolock.repo_lock_best_effort = real_best
        gitops.remove_worktree, gitops.prune_worktrees = real_remove, real_prune
        other.kill()
        other.wait(timeout=10)
        repolock.repo_lock_path(fixture_repo).unlink(missing_ok=True)

    assert called == [], f"ran shared-git mutations unlocked: {called}"
    # Our own temp root is not shared state, so it is still cleaned up...
    assert not copy.root.exists()
    # ...and the leftover entry is `prunable`, i.e. self-healing, not corrupt.
    listing = gitops._run(fixture_repo, "worktree", "list", "--porcelain")
    assert "prunable" in listing
    gitops.prune_worktrees(fixture_repo)  # a later LOCKED teardown clears it
    assert "prunable" not in gitops._run(
        fixture_repo, "worktree", "list", "--porcelain"
    )


def test_repo_lock_best_effort_propagates_body_errors(fixture_repo):
    """Only the ACQUISITION is best-effort — a body failure is never swallowed.

    Raises an ``OSError`` — one of the very types the acquisition guard catches
    — so the test proves the guard is scoped to ``__enter__`` and cannot swallow
    a teardown failure as "lock unavailable".
    """
    with pytest.raises(OSError, match="boom"):
        with repolock.repo_lock_best_effort(fixture_repo, reason="t") as acquired:
            assert acquired is True
            raise OSError("boom")
    assert not repolock.repo_lock_path(fixture_repo).exists()


def test_disposable_copy_lifecycle_takes_the_repo_lock(fixture_repo):
    """The lock is wired to a REAL section, not left as dead code.

    `make_disposable_copy` / `discard_disposable_copy` run `worktree add`,
    `worktree remove` and — the load-bearing one — repository-wide
    `worktree prune` (spike E8-C).
    """
    from gauntlet.engine import verify

    seen: list[str] = []
    real = repolock.repo_lock
    real_best = repolock.repo_lock_best_effort

    def spy(work_root, *, reason, **kw):
        seen.append(reason)
        return real(work_root, reason=reason, **kw)

    def spy_best(work_root, *, reason, **kw):
        seen.append(reason)
        return real_best(work_root, reason=reason, **kw)

    repolock.repo_lock, repolock.repo_lock_best_effort = spy, spy_best
    try:
        copy = verify.make_disposable_copy(fixture_repo)
        verify.discard_disposable_copy(fixture_repo, copy)
    finally:
        repolock.repo_lock = real
        repolock.repo_lock_best_effort = real_best
    # `repo_lock_best_effort` delegates to `repo_lock`, so the spy sees the
    # teardown reason twice; collapse consecutive repeats.
    distinct = [r for i, r in enumerate(seen) if i == 0 or seen[i - 1] != r]
    assert distinct == ["verify:add-disposable-copy", "verify:discard-copy"]
    assert not repolock.repo_lock_path(fixture_repo).exists()


def test_disposable_copy_creation_fails_closed_on_an_unobtainable_repo_lock(
    fixture_repo,
):
    """FR-2.3: no copy → the sub-step parks. Never "verify skipped, proceed"."""
    from gauntlet.engine import verify

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        _write_lock(repolock.repo_lock_path(fixture_repo), pid=other.pid,
                    identity=None, slug="holder")
        real = repolock.repo_lock
        repolock.repo_lock = lambda w, *, reason, **kw: real(
            w, reason=reason, timeout_s=0.0
        )
        try:
            with pytest.raises(verify.CopyCreationError, match="fail closed"):
                verify.make_disposable_copy(fixture_repo)
        finally:
            repolock.repo_lock = real
    finally:
        other.kill()
        other.wait(timeout=10)


# =============================================================================
# Structural: one reclaim rule, not three
# =============================================================================


def test_the_reclaim_rule_has_exactly_one_implementation():
    """Three lock layers, one `record_is_live` — drift here is a double-drive."""
    assert RunManager._lock_is_live is locking.record_is_live
    assert RunManager._link_into_place is locking.link_into_place
    source = (Path(repolock.__file__)).read_text()
    assert "def record_is_live" not in source
    assert "locking.record_is_live" in source


def test_lock_record_json_shape_is_unchanged():
    """The on-disk contract did not move with the code (mixed-version reads)."""
    rec = locking.new_record("demo", "run-1")
    data = json.loads(rec.to_json())
    assert set(data) == {
        "nonce", "slug", "run_id", "pid", "pgid", "started_at", "host",
        "proc_identity",
    }
    assert time.strptime(data["started_at"], "%Y-%m-%dT%H-%M-%S")
    assert locking.LockRecord.from_json(rec.to_json()) == rec


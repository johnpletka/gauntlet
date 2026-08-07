"""P3 — every rewind behind the recovery executor (plan §4.2/§4.3, §6 P3).

Deterministic tests against real throwaway Git repositories, in two layers:

* **Executor transaction** — the canonical ordering (lock → re-observe
  fingerprint → validate → durable snapshot → intent → apply → persist →
  clear), fault injection at each transaction boundary, idempotent intent
  replay, and the fail-closed refusals (fingerprint mismatch, foreign lock,
  unrecognized replay state). Asserted ONCE, table-driven, at the executor.
* **Site routing** — each of the five converted rewind sites (rollback,
  interrupted ``reset_to_base``, conflict-park cleanup, fix-resume reset,
  reviewer-mutation revert) drives the SAME transaction: a shared order
  probe records the executor's phases while each site's own driver triggers
  its rewind, and a shared snapshot-failure table proves every site aborts
  before any destructive verb.

The post-`177d721` PR #77 review regressions are encoded structurally:

* **F-002** — staged B plus worktree C survive a rewind as separate planes
  (the real-index-mutating single-tree backup is gone);
* **F-003** — a protected symlink is preserved and restored as a symlink,
  never read through or written through (the byte-overlay is gone);
* **F-004** — rollback prevalidation failures leave the checkout untouched
  (asserted in test_run_lifecycle: the P1 strict xfail now passes).
"""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path

import pytest
import yaml

from conftest import FakeAdapter, git, run_work_tree

from gauntlet.engine import cycle as cycle_mod
from gauntlet.engine import git_snapshot, gitops, manifest as M
from gauntlet.engine import recovery_exec as RX
from gauntlet.engine import run as run_mod
from gauntlet.engine.config import RunConfig
from gauntlet.engine.execution import (
    PARKED,
    StepContext,
    StepResult,
    get_spec,
    run_bookkeeping_excludes,
)
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline
from gauntlet.engine.recovery import RecoveryCause
from gauntlet.engine.run import RunManager
from gauntlet.logging.redact import RedactingWriter

_ids = itertools.count(1)


# --- executor-level harness -----------------------------------------------------


def _env(repo: Path):
    """A run-instance dir + manifest on the fixture repo's ``main`` branch."""
    run_dir = repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".gitignore").write_text("*\n")  # real layout: self-ignoring
    man = Manifest(
        run_id="run-1", slug="demo", branch="main", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
    )
    man.write_atomic(run_dir / "manifest.json")
    excludes = [run_dir.relative_to(repo).as_posix(), "runs/*/PR.md"]
    return run_dir, man, excludes


def _plan(
    repo: Path,
    run_dir: Path,
    man: Manifest,
    excludes: list[str],
    *,
    target: str,
    recorded: str | None = None,
    rec: StepRecord | None = None,
    mode: str = RX.RESET_PLAIN,
    bookkeeping: tuple[str, ...] = (),
    message: str | None = None,
    clean: bool = True,
    checkout: str | None = None,
    protected: list[str] | None = None,
):
    """Build (executor, assessment, action, spec, snapshot request, fp)."""
    git_obs = RX.observe_git(
        repo, run_branch=man.branch, recorded_sha=recorded or target,
        excludes=excludes,
    )
    state_obs = RX.observe_state(man, rec, liveness=RX.DriverLiveness.NONE)

    def fp():
        return RX.build_progress_fingerprint(
            repo, manifest=man, record=rec, excludes=excludes
        )

    action = RX.SnapshotAndRestartAction(
        description="test rewind",
        target_ref=f"refs/heads/{man.branch}",
        target_sha=target,
        reason="test rewind",
    )
    assessment = RX.RecoveryPlanner(repo).assess_rewind(
        git_obs=git_obs, state_obs=state_obs, fingerprint=fp(), action=action,
        cause=RecoveryCause.WORKTREE_PARTIAL,
    )
    spec = RX.RewindSpec(
        site="test.rewind",
        checkout_branch=checkout,
        target_sha=target,
        reset_mode=mode,
        bookkeeping_paths=bookkeeping,
        rewind_message=message,
        clean=clean,
        clean_excludes=("runs",),
    )
    request = RX.SnapshotRequest(
        snapshot_id=f"t{next(_ids)}",
        reason="test rewind",
        run_branch=man.branch,
        exclude=list(excludes),
        protected=protected if protected is not None else ["PR.md", "runs/*/PR.md"],
    )
    executor = RX.RecoveryExecutor(
        repo, run_dir, run_id=man.run_id, run_root="runs", excludes=excludes,
    )
    return executor, assessment, action, spec, request, fp


def _recovery_refs(repo: Path) -> list[str]:
    return gitops._run(
        repo, "for-each-ref", "--format=%(refname)", "refs/gauntlet/recovery/",
    ).splitlines()


# --- the shared order probe -----------------------------------------------------


@pytest.fixture
def order_probe(monkeypatch):
    """Record the executor's transaction phases in execution order.

    Wraps the REAL functions (behavior unchanged) so a single fixture asserts
    the canonical precondition → snapshot → intent → apply → clear ordering
    for the executor and for every converted site.
    """
    events: list[str] = []

    def wrap(module, name, label):
        real = getattr(module, name)

        def wrapped(*args, **kwargs):
            events.append(label)
            return real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapped)

    wrap(RX, "build_progress_fingerprint", "observe")
    wrap(git_snapshot, "create_snapshot", "snapshot")
    wrap(RX, "_write_intent", "intent")
    wrap(gitops, "reset_hard", "apply")
    wrap(gitops, "rewind_impl_preserving_bookkeeping", "apply")
    wrap(gitops, "checkout_branch", "apply")
    wrap(RX, "_clear_intent", "clear")
    return events


def _assert_canonical_order(events: list[str]) -> None:
    """precondition (re-observe) → snapshot → intent → apply → clear."""
    assert "snapshot" in events, f"no snapshot recorded: {events}"
    i_snap = events.index("snapshot")
    i_int = events.index("intent")
    i_apply = min(i for i, e in enumerate(events) if e == "apply")
    i_clear = events.index("clear")
    observed_before = [i for i, e in enumerate(events) if e == "observe" and i < i_snap]
    # At least two observations precede the snapshot: the assessment's and the
    # executor's re-observation under the lock (step 2).
    assert len(observed_before) >= 2, f"missing re-observation: {events}"
    assert i_snap < i_int < i_apply < i_clear, f"order violated: {events}"


# --- canonical ordering + end state, table-driven over spec shapes --------------


def _shape_plain(repo, run_dir, man, excludes):
    (repo / "tracked.txt").write_text("committed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed tracked")
    target = gitops.head_sha(repo)
    (repo / "tracked.txt").write_text("dirty edit\n")
    (repo / "junk.txt").write_text("untracked partial\n")
    kwargs = dict(target=target, mode=RX.RESET_PLAIN)

    def verify():
        assert (repo / "tracked.txt").read_text() == "committed\n"
        assert not (repo / "junk.txt").exists()

    return kwargs, verify


def _shape_bookkeeping(repo, run_dir, man, excludes):
    target = gitops.head_sha(repo)
    (repo / "impl.py").write_text("work above target\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "operator work above the target")
    bk_rel = (run_dir / "manifest.json").relative_to(repo).as_posix()
    kwargs = dict(
        target=target,
        recorded=target,
        mode=RX.RESET_BOOKKEEPING_PRESERVING,
        bookkeeping=(bk_rel,),
        message="gauntlet: rewind implementation for re-run (test)",
    )

    def verify():
        head = gitops.head_sha(repo)
        assert head != target  # the rewind commit sits on the target...
        assert gitops.commit_parent(repo, head) == target
        assert not (repo / "impl.py").exists()  # ...implementation rewound
        assert (run_dir / "manifest.json").exists()  # bookkeeping preserved

    return kwargs, verify


def _shape_checkout(repo, run_dir, man, excludes):
    (repo / "phase.py").write_text("run branch work\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "run branch tip")
    tip = gitops.head_sha(repo)
    target = gitops.commit_parent(repo, tip)
    git(repo, "checkout", "-qb", "elsewhere")  # operator on another branch
    kwargs = dict(
        target=target, recorded=tip, checkout="main", clean=False,
    )

    def verify():
        assert gitops.current_branch(repo) == "main"
        assert gitops.head_sha(repo) == target
        assert gitops.rev_parse(repo, "refs/heads/elsewhere") == tip

    return kwargs, verify


_SHAPES = {
    "plain_reset": _shape_plain,
    "bookkeeping_preserving": _shape_bookkeeping,
    "checkout_then_reset": _shape_checkout,
}


@pytest.mark.parametrize("shape", sorted(_SHAPES), ids=sorted(_SHAPES))
def test_transaction_order_and_end_state(fixture_repo, order_probe, shape):
    run_dir, man, excludes = _env(fixture_repo)
    kwargs, verify = _SHAPES[shape](fixture_repo, run_dir, man, excludes)
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, **kwargs
    )
    result = executor.apply(
        assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
    )
    _assert_canonical_order(order_probe)
    verify()
    assert result.applied
    assert RX.load_intent(run_dir) is None  # cleared only after durability
    assert _recovery_refs(fixture_repo)  # the snapshot ref is durable


# --- fail-closed refusals -------------------------------------------------------


def test_fingerprint_mismatch_between_assess_and_apply_aborts(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "dirty.txt").write_text("partial\n")
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    # The repository moves between assessment and apply: a concurrent edit.
    (fixture_repo / "dirty.txt").write_text("changed since assessment\n")
    with pytest.raises(RX.RecoveryPreconditionError, match="fingerprint changed"):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
        )
    # Zero mutation: the moved state is untouched, no snapshot, no intent.
    assert (fixture_repo / "dirty.txt").read_text() == "changed since assessment\n"
    assert _recovery_refs(fixture_repo) == []
    assert RX.load_intent(run_dir) is None


def test_validation_failure_aborts_before_snapshot(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "dirty.txt").write_text("partial\n")
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    bad_spec = spec.model_copy(update={"checkout_branch": "no-such-branch"})
    with pytest.raises(RX.RecoveryPreconditionError, match="missing"):
        executor.apply(
            assessment, action, spec=bad_spec, snapshot_request=request,
            fingerprint=fp,
        )
    assert (fixture_repo / "dirty.txt").read_text() == "partial\n"
    assert _recovery_refs(fixture_repo) == []
    assert RX.load_intent(run_dir) is None


def test_planner_refuses_a_target_outside_observed_history(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    head = gitops.head_sha(fixture_repo)
    git_obs = RX.observe_git(
        fixture_repo, run_branch="main", recorded_sha=head, excludes=excludes
    )
    state_obs = RX.observe_state(man, None, liveness=RX.DriverLiveness.NONE)
    fp = RX.build_progress_fingerprint(fixture_repo, manifest=man, excludes=excludes)
    action = RX.SnapshotAndRestartAction(
        description="rewind to unproven history",
        target_ref="refs/heads/main",
        target_sha="deadbeef" * 5,
        reason="test",
    )
    with pytest.raises(RX.RecoveryPreconditionError, match="unproven history"):
        RX.RecoveryPlanner(fixture_repo).assess_rewind(
            git_obs=git_obs, state_obs=state_obs, fingerprint=fp, action=action,
            cause=RecoveryCause.WORKTREE_PARTIAL,
        )


# --- worktree lock (transaction step 1) -----------------------------------------


def _write_lock(repo: Path, pid: int) -> Path:
    lock = repo / "runs" / RX.DRIVING_LOCK_NAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({
        "nonce": "abc", "slug": "demo", "run_id": "run-1", "pid": pid,
        "pgid": pid, "started_at": "t", "host": "h", "proc_identity": None,
    }))
    return lock


def test_foreign_live_lock_fails_closed(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "dirty.txt").write_text("partial\n")
    _write_lock(fixture_repo, os.getppid())  # a live pid that is not us
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    with pytest.raises(RX.RecoveryLockError, match="held by live pid"):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
        )
    assert (fixture_repo / "dirty.txt").read_text() == "partial\n"
    assert _recovery_refs(fixture_repo) == []


def test_own_process_lock_is_verified_held_and_left_in_place(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "dirty.txt").write_text("partial\n")
    lock = _write_lock(fixture_repo, os.getpid())  # the RunManager-held case
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    executor.apply(
        assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
    )
    assert lock.exists()  # verification never releases the verb's own lock


def test_ephemeral_lock_is_taken_and_released(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "dirty.txt").write_text("partial\n")
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    executor.apply(
        assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
    )
    assert not (fixture_repo / "runs" / RX.DRIVING_LOCK_NAME).exists()


def test_lock_name_matches_run_manager() -> None:
    assert RX.DRIVING_LOCK_NAME == run_mod.DRIVING_LOCK_NAME


# --- fault injection at each transaction boundary -------------------------------

# Each row kills the transaction at one boundary by making one call explode.
# The invariant (plan §6 P3 acceptance): the repository is either untouched
# or carries a durable snapshot plus a replayable intent; replay converges to
# the intended end state and is idempotent.
_BOUNDARIES = [
    # (boundary, module attr to break, expectation)
    ("before_snapshot", (RX.RecoveryExecutor, "_validate"), "untouched_nothing"),
    ("during_snapshot", (git_snapshot, "create_snapshot"), "untouched_nothing"),
    ("before_intent_persist", (RX, "_write_intent"), "untouched_snapshot_only"),
    ("after_intent_before_apply", (gitops, "reset_hard"), "replayable"),
    ("mid_apply_after_reset", (gitops, "clean_untracked"), "replayable"),
    ("after_apply_before_persist", ("persist", None), "replayable"),
    ("before_intent_clear", (RX, "_clear_intent"), "replayable"),
]


class _Boom(RuntimeError):
    pass


@pytest.mark.parametrize(
    "boundary,breakpoint,expectation",
    _BOUNDARIES,
    ids=[row[0] for row in _BOUNDARIES],
)
def test_fault_injection_at_each_boundary(
    fixture_repo, monkeypatch, boundary, breakpoint, expectation
):
    run_dir, man, excludes = _env(fixture_repo)
    (fixture_repo / "tracked.txt").write_text("committed\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "seed tracked")
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "tracked.txt").write_text("dirty edit\n")
    (fixture_repo / "junk.txt").write_text("untracked partial\n")

    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )

    persist = None
    holder, name = breakpoint
    if holder == "persist":
        def persist(_result):
            raise _Boom("killed before the state transition persisted")
    else:
        real = getattr(holder, name)

        def exploding(*args, **kwargs):
            if name in ("_validate",):
                real(*args, **kwargs)  # the boundary is AFTER this step ran
            raise _Boom(f"killed at {boundary}")

        monkeypatch.setattr(holder, name, exploding)

    with pytest.raises(_Boom):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request,
            fingerprint=fp, persist=persist,
        )
    monkeypatch.undo()  # the "next process" runs the real code

    intent = RX.load_intent(run_dir)
    if expectation == "untouched_nothing":
        assert (fixture_repo / "tracked.txt").read_text() == "dirty edit\n"
        assert (fixture_repo / "junk.txt").exists()
        assert intent is None
        if boundary == "before_snapshot":
            assert _recovery_refs(fixture_repo) == []
        assert RX.replay_pending_intent(fixture_repo, run_dir) is None
        return
    if expectation == "untouched_snapshot_only":
        # The snapshot ref is durable garbage — harmless — but with no intent
        # there is nothing to replay and nothing was mutated.
        assert (fixture_repo / "tracked.txt").read_text() == "dirty edit\n"
        assert (fixture_repo / "junk.txt").exists()
        assert intent is None
        assert _recovery_refs(fixture_repo)
        assert RX.replay_pending_intent(fixture_repo, run_dir) is None
        return

    # replayable: a durable snapshot AND a replayable intent survive.
    assert intent is not None
    assert _recovery_refs(fixture_repo)
    snapshot = git_snapshot.load_snapshot(fixture_repo, intent.snapshot_ref)
    tree = gitops._run(
        fixture_repo, "ls-tree", "-r", "--name-only", snapshot.worktree_tree
    )
    assert "junk.txt" in tree  # the partial work is preserved, not lost

    # The next mutating command replays the intent idempotently...
    note = RX.replay_pending_intent(fixture_repo, run_dir)
    assert note is not None
    assert gitops.head_sha(fixture_repo) == target
    assert (fixture_repo / "tracked.txt").read_text() == "committed\n"
    assert not (fixture_repo / "junk.txt").exists()
    assert RX.load_intent(run_dir) is None  # ...exactly once: then cleared
    # ...and a cleared intent is never replayed again.
    assert RX.replay_pending_intent(fixture_repo, run_dir) is None
    assert gitops.head_sha(fixture_repo) == target


def test_replay_refuses_an_unrecognized_repository_state(fixture_repo, monkeypatch):
    run_dir, man, excludes = _env(fixture_repo)
    (fixture_repo / "tracked.txt").write_text("committed\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "seed tracked")
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "junk.txt").write_text("partial\n")
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    monkeypatch.setattr(
        gitops, "reset_hard",
        lambda *a, **k: (_ for _ in ()).throw(_Boom("killed")),
    )
    with pytest.raises(_Boom):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
        )
    monkeypatch.undo()
    # Someone (or something) moves the repository past both the pre-state and
    # the target before the replay runs.
    (fixture_repo / "other.txt").write_text("new work\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "unrelated progress")
    moved_head = gitops.head_sha(fixture_repo)
    with pytest.raises(RX.RecoveryIntentError, match="refusing to replay"):
        RX.replay_pending_intent(fixture_repo, run_dir)
    # Fail closed: nothing mutated, and the intent stays in place as evidence.
    assert gitops.head_sha(fixture_repo) == moved_head
    assert RX.load_intent(run_dir) is not None


def test_cleared_intent_is_never_replayed(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "junk.txt").write_text("partial\n")
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    executor.apply(
        assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
    )
    assert RX.load_intent(run_dir) is None
    # New work lands after the completed transaction; a (non-existent) replay
    # must never touch it.
    (fixture_repo / "after.txt").write_text("post-transaction work\n")
    assert RX.replay_pending_intent(fixture_repo, run_dir) is None
    assert (fixture_repo / "after.txt").read_text() == "post-transaction work\n"


# --- fault injection across the protected-restore boundary (plan §7) ------------

# Plan §7's "before and after worktree/index restoration" points, inside the
# executor transaction: the apply's final step is
# ``git_snapshot.restore_protected`` — the scoped restoration that re-applies
# protected deletions and re-materializes protected files after reset/clean.
# A kill at any of its internal boundaries must leave the standard replayable
# pair (durable snapshot + surviving intent), and the replay must converge —
# including re-applying the protected deletion the interrupted reset just
# resurrected, or finishing one the kill left half-applied.

_PROTECTED_RESTORE_BOUNDARIES = [
    # (boundary, git_snapshot attr to break)
    ("before_protected_restore", "restore_protected"),
    ("during_protected_deletion", "_safe_unlink"),
    ("during_protected_materialize", "_prepare_destination"),
]


def _protected_env(repo: Path):
    """The shared fault harness plus BOTH protected planes: a protected
    deletion (PR.md committed, then deleted by a human) and an
    excluded-but-protected operator file (runs/demo/PR.md)."""
    run_dir, man, excludes = _env(repo)
    (repo / "tracked.txt").write_text("committed\n")
    (repo / "PR.md").write_text("committed notes\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed tracked + PR.md")
    target = gitops.head_sha(repo)
    (repo / "tracked.txt").write_text("dirty edit\n")
    (repo / "junk.txt").write_text("untracked partial\n")
    (repo / "PR.md").unlink()  # protected deletion vs HEAD
    (repo / "runs" / "demo" / "PR.md").write_text("operator notes\n")
    return run_dir, man, excludes, target


def _assert_protected_convergence(repo: Path, run_dir: Path, target: str):
    assert gitops.head_sha(repo) == target
    assert (repo / "tracked.txt").read_text() == "committed\n"
    assert not (repo / "junk.txt").exists()
    # The protected deletion the reset resurrected is re-applied, and the
    # excluded-but-protected operator file is back byte-exact.
    assert not (repo / "PR.md").exists()
    assert (repo / "runs" / "demo" / "PR.md").read_text() == "operator notes\n"
    assert RX.load_intent(run_dir) is None  # cleared exactly once
    # A cleared intent never replays again, and repeating changes nothing.
    assert RX.replay_pending_intent(repo, run_dir) is None
    assert not (repo / "PR.md").exists()
    assert (repo / "runs" / "demo" / "PR.md").read_text() == "operator notes\n"


@pytest.mark.parametrize(
    "boundary,attr",
    _PROTECTED_RESTORE_BOUNDARIES,
    ids=[row[0] for row in _PROTECTED_RESTORE_BOUNDARIES],
)
def test_fault_injection_across_protected_restore(
    fixture_repo, monkeypatch, boundary, attr
):
    run_dir, man, excludes, target = _protected_env(fixture_repo)
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )

    def exploding(*args, **kwargs):
        raise _Boom(f"killed at {boundary}")

    monkeypatch.setattr(git_snapshot, attr, exploding)
    with pytest.raises(_Boom):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
        )
    monkeypatch.undo()  # the "next process" runs the real code

    # The §7 replayable pair survives: a durable snapshot that captured BOTH
    # protected planes, plus an intent to converge them.
    intent = RX.load_intent(run_dir)
    assert intent is not None
    snapshot = git_snapshot.load_snapshot(fixture_repo, intent.snapshot_ref)
    assert "PR.md" in snapshot.protected_deletions
    assert "runs/demo/PR.md" in snapshot.protected_paths

    assert RX.replay_pending_intent(fixture_repo, run_dir) is not None
    _assert_protected_convergence(fixture_repo, run_dir, target)


def test_double_kill_across_protected_restore_converges_on_second_replay(
    fixture_repo, monkeypatch
):
    """A second kill during the FIRST replay's own protected restore still
    converges: the intent survives as evidence and the next replay finishes
    the restoration — the §7 property holds under repeated crashes."""
    run_dir, man, excludes, target = _protected_env(fixture_repo)
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )

    def _boom(*args, **kwargs):
        raise _Boom("killed inside protected restore")

    monkeypatch.setattr(git_snapshot, "restore_protected", _boom)
    with pytest.raises(_Boom):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
        )
    monkeypatch.undo()

    # Kill 2: the first replay dies inside the SAME restoration step.
    monkeypatch.setattr(git_snapshot, "_safe_unlink", _boom)
    with pytest.raises(_Boom):
        RX.replay_pending_intent(fixture_repo, run_dir)
    monkeypatch.undo()
    assert RX.load_intent(run_dir) is not None  # evidence retained, not cleared

    assert RX.replay_pending_intent(fixture_repo, run_dir) is not None
    _assert_protected_convergence(fixture_repo, run_dir, target)


# --- post-177d721 F-002: staged B + worktree C are separate recoverable planes --


def test_f002_staged_and_worktree_planes_survive_a_rewind(fixture_repo):
    """Structurally impossible to lose: the executor's snapshot goes through
    the P2 temporary-index machinery, so staged B and worktree C are captured
    as distinct planes (the PR #77 ``git add -A``-on-the-real-index backup
    that collapsed them no longer exists) and both restore exactly."""
    run_dir, man, excludes = _env(fixture_repo)
    (fixture_repo / "dual.txt").write_text("A committed\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "track A")
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "dual.txt").write_text("B staged\n")
    git(fixture_repo, "add", "--", "dual.txt")
    (fixture_repo / "dual.txt").write_text("C unstaged\n")

    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    executor.apply(
        assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
    )
    ref = _recovery_refs(fixture_repo)[0]
    snapshot = git_snapshot.load_snapshot(fixture_repo, ref)
    assert snapshot.index_tree is not None
    assert gitops._run(
        fixture_repo, "show", f"{snapshot.index_tree}:dual.txt"
    ) == "B staged\n"
    assert gitops._run(
        fixture_repo, "show", f"{snapshot.worktree_tree}:dual.txt"
    ) == "C unstaged\n"
    # And the full restoration reproduces the divergence exactly.
    git_snapshot.restore_snapshot(fixture_repo, snapshot)
    assert gitops._run(fixture_repo, "show", ":dual.txt") == "B staged\n"
    assert (fixture_repo / "dual.txt").read_text() == "C unstaged\n"


# --- post-177d721 F-003: protected symlinks are never followed ------------------


def test_f003_protected_symlink_survives_rewind_without_being_followed(
    fixture_repo, tmp_path
):
    """Structurally impossible to escape: the byte-overlay that read through
    (and wrote through) symlinks is gone; the executor preserves the symlink
    ENTRY in the snapshot and Git rematerializes it on restore. Neither
    outside target is ever read or written."""
    run_dir, man, excludes = _env(fixture_repo)
    original = tmp_path / "outside-original.txt"
    changed = tmp_path / "outside-changed.txt"
    original.write_text("outside original\n")
    changed.write_text("outside changed\n")
    pr = fixture_repo / "PR.md"
    pr.symlink_to(original)
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "track PR symlink")
    target = gitops.head_sha(fixture_repo)
    pr.unlink()
    pr.symlink_to(changed)  # the human's uncommitted retarget

    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target, protected=["PR.md"]
    )
    executor.apply(
        assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
    )
    # The protected restore brought back the retargeted symlink AS A SYMLINK.
    assert pr.is_symlink()
    assert os.readlink(pr) == str(changed)
    # Neither outside file was read into the snapshot or written through.
    assert original.read_text() == "outside original\n"
    assert changed.read_text() == "outside changed\n"
    ref = _recovery_refs(fixture_repo)[0]
    snapshot = git_snapshot.load_snapshot(fixture_repo, ref)
    entry = gitops._run(
        fixture_repo, "ls-tree", snapshot.worktree_tree, "--", "PR.md"
    )
    assert entry.split()[0] == "120000"  # captured as a symlink entry


# --- the five converted sites route through the one transaction -----------------

_BUILDER_CFG = {"agents": {"builder": {"adapter": "claude-code"}}}

_SITE_PIPELINE = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: commit, type: commit, message: "P1: implement\\n\\nthe body."}
"""


def _orchestrator(repo, *, manifest, interrupted="park", adapters=None):
    cfg = RunConfig.model_validate({**_BUILDER_CFG, "interrupted_step": interrupted})
    pipeline = Pipeline.model_validate(yaml.safe_load(_SITE_PIPELINE))
    artifact_root = repo / "runs" / "demo"
    run_dir = artifact_root / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    adapters = adapters or {}
    return Orchestrator(
        repo_root=repo, run_dir=run_dir, artifact_root=artifact_root,
        config=cfg, pipeline=pipeline, manifest=manifest,
        adapter_factory=(lambda name: adapters[name]) if adapters else None,
    )


def _seed_running(repo, base_sha) -> Manifest:
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )
    man.upsert(StepRecord(
        id="implement", type="agent_task", agent="builder", status=M.RUNNING,
        base_sha=base_sha, attempts=1, started="t0",
    ))
    return man


def _drive_reset_to_base(repo) -> None:
    base = gitops.head_sha(repo)
    (repo / "partial.py").write_text("half written")
    man = _seed_running(repo, base)
    orch = _orchestrator(
        repo, manifest=man, interrupted="reset_to_base",
        adapters={"builder": FakeAdapter(writes={"clean.py": "out\n"})},
    )
    assert orch.drive() == M.RUN_DONE
    assert not (repo / "partial.py").exists()


def _drive_conflict_park(repo) -> None:
    base = gitops.head_sha(repo)
    man = _seed_running(repo, base)
    orch = _orchestrator(repo, manifest=man)
    rec = man.record("implement")
    step = orch.pipeline.stages[0].steps[0]
    (repo / "leak.py").write_text("builder edit before conflict")
    result = orch._restore_clean_after_conflict_park(
        step, get_spec("agent_task"), rec,
        StepResult(status=PARKED, parked_reason=M.PARKED_REASON_RESPONSE),
    )
    assert "preserved as recovery snapshot" in (result.notes or "")
    assert not (repo / "leak.py").exists()


_ROLLBACK_CONFIG = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

_ROLLBACK_PIPELINE = """
name: p
version: 1
stages:
  - id: p1
    steps:
      - {id: impl1, type: agent_task, agent: builder, prompt_text: a}
      - {id: c1, type: commit, message: "P1: phase one\\n\\nbody one."}
  - id: p2
    steps:
      - {id: impl2, type: agent_task, agent: builder, prompt_text: b}
      - {id: c2, type: commit, message: "P2: phase two\\n\\nbody two."}
"""


def _rollback_manager(repo) -> RunManager:
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(_ROLLBACK_CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# Real PRD\n\nA human-authored PRD.\n")
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / "p.yaml"
    path.write_text(_ROLLBACK_PIPELINE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add pipeline + prd")
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    return mgr


def _drive_rollback(repo) -> None:
    mgr = _rollback_manager(repo)
    target = mgr.rollback("demo", phase=1)
    # P7g: rollback rewinds the RUN's tree; the operator's HEAD never moves.
    assert gitops.head_sha(run_work_tree(repo)) == target


def _cycle_ctx(repo):
    run_dir = repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".gitignore").write_text("*\n")
    (run_dir / "manifest.json").write_text('{"run_id": "r"}\n')
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    rec = StepRecord(id="cycle", type="adversarial_cycle")
    man.upsert(rec)
    pipeline = Pipeline.model_validate({
        "name": "demo", "version": 1,
        "stages": [{"id": "s", "steps": [
            {"id": "cycle", "type": "adversarial_cycle", "mode": "artifact",
             "artifact": "prd.md", "phase": "P5", "reviewer": "r",
             "triager": "t", "fixer": "f"},
        ]}],
    })
    return StepContext(
        repo_root=repo, run_dir=run_dir, artifact_root=repo,
        config=RunConfig.model_validate({"agents": {}}), pipeline=pipeline,
        manifest=man, record=rec, writer=RedactingWriter(),
        excludes=run_bookkeeping_excludes(repo, run_dir, repo),
    )


def _drive_fix_resume(repo) -> None:
    handoff = gitops.head_sha(repo)
    (repo / "work.py").write_text("partial fixer edit")
    ctx = _cycle_ctx(repo)
    note = cycle_mod._reset_dirty_to_handoff(ctx, handoff, 1)
    assert note is not None and "recovery snapshot" in note
    assert not (repo / "work.py").exists()


def _drive_mutation_revert(repo) -> None:
    handoff = gitops.head_sha(repo)
    ctx = _cycle_ctx(repo)
    (repo / "sneaky.py").write_text("reviewer mutation")
    guard = cycle_mod._MutationGuard(
        None, ctx, "revert", "P5", 1, handoff, "reviewer", []
    )
    guard.check()
    assert not (repo / "sneaky.py").exists()
    assert guard.synthetic_findings
    assert "refs/gauntlet/recovery/" in guard.synthetic_findings[0]["claim"]


_SITES = {
    "rollback": _drive_rollback,
    "reset_to_base": _drive_reset_to_base,
    "conflict_park": _drive_conflict_park,
    "fix_resume": _drive_fix_resume,
    "mutation_revert": _drive_mutation_revert,
}


@pytest.mark.parametrize("site", sorted(_SITES), ids=sorted(_SITES))
def test_every_site_produces_the_canonical_ordering(fixture_repo, order_probe, site):
    """One shared harness (not five copies): each converted site's own driver
    triggers its rewind while the order probe records the executor phases;
    every site must produce precondition → snapshot → intent → apply →
    clear, with the durable ref present and the intent cleared."""
    _SITES[site](fixture_repo)
    _assert_canonical_order(order_probe)
    assert _recovery_refs(fixture_repo)


@pytest.mark.parametrize("site", sorted(_SITES), ids=sorted(_SITES))
def test_snapshot_failure_aborts_every_site_before_mutation(
    fixture_repo, monkeypatch, site
):
    """R2 at every site: when snapshot creation fails, no checkout, reset, or
    clean runs — the rewind fails closed with the dirty state untouched."""
    resets: list = []
    monkeypatch.setattr(
        git_snapshot, "create_snapshot",
        lambda *a, **k: (_ for _ in ()).throw(
            git_snapshot.SnapshotError("injected snapshot failure")
        ),
    )
    monkeypatch.setattr(gitops, "reset_hard", lambda *a, **k: resets.append(a))
    monkeypatch.setattr(
        gitops, "rewind_impl_preserving_bookkeeping",
        lambda *a, **k: resets.append(a),
    )
    monkeypatch.setattr(gitops, "clean_untracked", lambda *a, **k: resets.append(a))
    with pytest.raises(git_snapshot.SnapshotError):
        _SITES[site](fixture_repo)
    assert resets == []  # no destructive verb ran after the failure


# --- a killed rollback is replayed by the next mutating command -----------------


def test_killed_rollback_replays_via_resume_and_converges(fixture_repo, monkeypatch):
    """End-to-end intent replay through a real verb: rollback dies between the
    Git apply and its manifest persist; the next mutating command (resume)
    replays the intent — re-running the manifest rewind through the registered
    site finisher — exactly once, then drives normally."""
    mgr = _rollback_manager(fixture_repo)
    man = mgr.status("demo")
    p1_target = next(c.sha for c in man.commits if c.phase == "P1")

    monkeypatch.setattr(
        run_mod, "_apply_rollback_manifest_transition",
        lambda *a, **k: (_ for _ in ()).throw(_Boom("killed before persist")),
    )
    with pytest.raises(_Boom):
        mgr.rollback("demo", phase=1)
    monkeypatch.undo()

    run_dir = mgr.layout("demo").active_run_dir()
    # The Git apply completed; the manifest transition did not; the intent is
    # durable and replayable. P7g: the apply landed in the RUN's tree — the
    # operator's HEAD is untouched, which is acceptance A1 and is asserted for
    # this test by the autouse property.
    assert gitops.head_sha(run_work_tree(fixture_repo)) == p1_target
    assert RX.load_intent(run_dir) is not None
    stale = mgr.status("demo")
    assert [c.phase for c in stale.commits] == ["P1", "P2"]  # not yet rewound

    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"g{calls['n']}.py": "y\n"})

    status = mgr.resume("demo", use_judge=False, adapter_factory=factory)
    assert status == M.RUN_DONE
    man = mgr.status("demo")
    # The replayed finisher rewound the manifest; the resume then re-ran P2.
    assert RX.load_intent(run_dir) is None
    assert any("replayed after a process death" in w for w in man.warnings)
    assert [c.phase for c in man.commits] == ["P1", "P2"]
    assert gitops.commit_subject(fixture_repo, "gauntlet/demo") == "P2: phase two"
    # A second resume finds nothing to replay and nothing to do.
    assert RX.replay_pending_intent(fixture_repo, run_dir) is None


# --- reset --hard / checkout / clean stay out of the converted callers ----------


def test_no_direct_destructive_git_calls_remain_in_converted_rewind_paths():
    """Plan §9: after P3, no reset/clean path outside the recovery executor.

    Static check over the exact converted call sites: the rewind functions in
    orchestrator.py, cycle.py, and run.py's rollback must not invoke
    reset_hard / clean_untracked / rewind_impl_preserving_bookkeeping
    directly — those verbs belong to recovery_exec (and the narrowly
    documented non-recovery users that remain: the checkpoint squash's
    reset_soft, branch lifecycle, and the verifier's disposable worktrees).
    """
    import inspect

    from gauntlet.engine import orchestrator as orch_mod

    for func in (
        orch_mod.Orchestrator._resume_disposition,
        orch_mod.Orchestrator._restore_clean_after_conflict_park,
        cycle_mod._reset_dirty_to_handoff,
        cycle_mod._MutationGuard._revert,
        run_mod.RunManager._rollback_locked,
    ):
        source = inspect.getsource(func)
        for verb in (
            "gitops.reset_hard(",
            "gitops.clean_untracked(",
            "gitops.rewind_impl_preserving_bookkeeping(",
            "gitops.checkout_branch(",
        ):
            assert verb not in source, (
                f"{func.__qualname__} still calls {verb} directly; every "
                "rewind mutation must route through RecoveryExecutor (P3)"
            )

    # And the deprecated lossy helpers are gone from gitops entirely.
    for name in ("backup_dirty_worktree", "worktree_overlay", "restore_overlay"):
        assert not hasattr(gitops, name)


# =============================================================================
# P3.1 — post-review regressions (F-001..F-005)
# =============================================================================


# --- F-001: same-HEAD work created after a kill is never replayed away ----------


@pytest.mark.parametrize(
    "contaminate", ["tracked_edit", "staged_new_file", "untracked_file"]
)
def test_replay_refuses_same_head_work_created_after_the_kill(
    fixture_repo, monkeypatch, contaminate
):
    """F-001: the intent's independently re-derivable index/worktree plane
    witnesses mean a same-HEAD repository that gained tracked, staged, or
    untracked work after the kill is provably NOT the assessed pre-state —
    replay fails closed with the new work intact, instead of resetting it
    away under a snapshot that never captured it."""
    run_dir, man, excludes = _env(fixture_repo)
    (fixture_repo / "tracked.txt").write_text("committed\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "seed tracked")
    target = gitops.head_sha(fixture_repo)
    (fixture_repo / "tracked.txt").write_text("dirty edit\n")
    (fixture_repo / "junk.txt").write_text("untracked partial\n")
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    monkeypatch.setattr(
        gitops, "reset_hard",
        lambda *a, **k: (_ for _ in ()).throw(_Boom("killed before apply")),
    )
    with pytest.raises(_Boom):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
        )
    monkeypatch.undo()
    assert RX.load_intent(run_dir) is not None

    # New work lands WITHOUT moving HEAD — the exact case a bare-HEAD replay
    # check cannot see.
    if contaminate == "tracked_edit":
        (fixture_repo / "tracked.txt").write_text("NEW work after the kill\n")
        marker = fixture_repo / "tracked.txt"
        expected = "NEW work after the kill\n"
    elif contaminate == "staged_new_file":
        (fixture_repo / "newfile.txt").write_text("NEW staged work\n")
        git(fixture_repo, "add", "--", "newfile.txt")
        marker = fixture_repo / "newfile.txt"
        expected = "NEW staged work\n"
    else:
        (fixture_repo / "brand-new.txt").write_text("NEW untracked work\n")
        marker = fixture_repo / "brand-new.txt"
        expected = "NEW untracked work\n"

    with pytest.raises(RX.RecoveryIntentError, match="never captured"):
        RX.replay_pending_intent(fixture_repo, run_dir)
    # Fail closed: the new work is untouched and the intent stays as evidence.
    assert marker.read_text() == expected
    assert gitops.head_sha(fixture_repo) == target
    assert RX.load_intent(run_dir) is not None


def test_replay_refuses_new_staging_of_snapshot_captured_work(
    fixture_repo, monkeypatch
):
    """F-001 confirm: matching worktree bytes are not enough to call dirt
    snapshot residue. Staging the already-captured work after the kill creates
    a new INDEX plane that the pre-kill snapshot did not contain; replay must
    retain the intent and leave that staging untouched."""
    run_dir, man, excludes = _env(fixture_repo)
    tracked = fixture_repo / "tracked.txt"
    tracked.write_text("A committed\n")
    git(fixture_repo, "add", "--", "tracked.txt")
    git(fixture_repo, "commit", "-qm", "seed tracked")
    target = gitops.head_sha(fixture_repo)
    tracked.write_text("B captured only in worktree\n")
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo, run_dir, man, excludes, target=target
    )
    monkeypatch.setattr(
        gitops, "reset_hard",
        lambda *a, **k: (_ for _ in ()).throw(_Boom("killed before apply")),
    )
    with pytest.raises(_Boom):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
        )
    monkeypatch.undo()

    git(fixture_repo, "add", "--", "tracked.txt")  # NEW staging after kill
    with pytest.raises(RX.RecoveryIntentError, match="never captured"):
        RX.replay_pending_intent(fixture_repo, run_dir)
    assert gitops._run(fixture_repo, "show", ":tracked.txt") == (
        "B captured only in worktree\n"
    )
    assert tracked.read_text() == "B captured only in worktree\n"
    assert RX.load_intent(run_dir) is not None


@pytest.mark.parametrize("boundary", ["after_read_tree", "after_bookkeeping_add"])
def test_bookkeeping_index_sub_boundaries_remain_replayable(
    fixture_repo, monkeypatch, boundary
):
    """F-001: strict index proof must not create a wedge inside the sanctioned
    bookkeeping rewind. Both deterministic pre-reset index states are recognized
    and replay converges without accepting arbitrary staging."""
    run_dir, man, excludes = _env(fixture_repo)
    target = gitops.head_sha(fixture_repo)
    bk_rel = (run_dir / "manifest.json").relative_to(fixture_repo).as_posix()
    git(fixture_repo, "add", "-f", "--", bk_rel)
    git(fixture_repo, "commit", "-qm", "gauntlet: response test pending")
    pre_head = gitops.head_sha(fixture_repo)
    (fixture_repo / "partial.py").write_text("captured partial work\n")
    executor, assessment, action, spec, request, fp = _plan(
        fixture_repo,
        run_dir,
        man,
        excludes,
        target=target,
        recorded=target,
        mode=RX.RESET_BOOKKEEPING_PRESERVING,
        bookkeeping=(bk_rel,),
        message="gauntlet: response test pending",
    )

    def interrupted_rewind(repo, target_sha, bookkeeping, message, *, identity):
        gitops._run(repo, "read-tree", target_sha)
        if boundary == "after_bookkeeping_add":
            gitops._run(repo, "add", "-f", "--", *bookkeeping)
        raise _Boom(f"killed {boundary}")

    monkeypatch.setattr(
        gitops, "rewind_impl_preserving_bookkeeping", interrupted_rewind
    )
    with pytest.raises(_Boom):
        executor.apply(
            assessment, action, spec=spec, snapshot_request=request, fingerprint=fp
        )
    monkeypatch.undo()
    assert gitops.head_sha(fixture_repo) == pre_head
    assert RX.load_intent(run_dir) is not None

    assert RX.replay_pending_intent(fixture_repo, run_dir) is not None
    assert RX.load_intent(run_dir) is None
    assert gitops.commit_parent(fixture_repo, "HEAD") == target
    assert (run_dir / "manifest.json").exists()
    assert not (fixture_repo / "partial.py").exists()


# --- F-002: every mutating verb reconciles a surviving intent -------------------


def test_killed_rollback_converges_when_retried_via_rollback(
    fixture_repo, monkeypatch
):
    """F-002: a rollback killed between its Git apply and its manifest persist
    leaves the branch reset but the manifest un-rewound. Retrying ROLLBACK
    itself (not resume) must converge: the entry replay re-runs the manifest
    transition through the site finisher BEFORE the tier-2 agreement guard —
    which would otherwise refuse 'behind' forever."""
    mgr = _rollback_manager(fixture_repo)
    man = mgr.status("demo")
    p1_target = next(c.sha for c in man.commits if c.phase == "P1")
    monkeypatch.setattr(
        run_mod, "_apply_rollback_manifest_transition",
        lambda *a, **k: (_ for _ in ()).throw(_Boom("killed before persist")),
    )
    with pytest.raises(_Boom):
        mgr.rollback("demo", phase=1)
    monkeypatch.undo()
    run_dir = mgr.layout("demo").active_run_dir()
    assert gitops.head_sha(run_work_tree(fixture_repo)) == p1_target  # branch already reset
    assert RX.load_intent(run_dir) is not None
    stale = mgr.status("demo")
    assert [c.phase for c in stale.commits] == ["P1", "P2"]  # not yet rewound

    target = mgr.rollback("demo", phase=1)  # the retried verb converges
    assert target == p1_target
    man = mgr.status("demo")
    assert [c.phase for c in man.commits] == ["P1"]
    assert man.record("impl2").status == M.PENDING
    assert RX.load_intent(run_dir) is None
    assert any("replayed after a process death" in w for w in man.warnings)


_GATED_PIPELINE = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [prd.md]}
      - {id: after, type: shell, run: "true"}
"""


def _gated_manager(repo) -> RunManager:
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(_ROLLBACK_CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# Real PRD\n\nA human-authored PRD.\n")
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / "p.yaml"
    path.write_text(_GATED_PIPELINE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add pipeline + prd")
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    return mgr


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_gate_verbs_reconcile_surviving_intents_first(
    fixture_repo, monkeypatch, verb
):
    """F-002: approve and reject are driving verbs — each must call the
    intent-reconciliation hook (under the lock, before any drive) exactly
    like resume and rollback."""
    mgr = _gated_manager(fixture_repo)
    calls: list[str] = []
    real = RX.replay_pending_intent

    def spying(repo, run_dir):
        calls.append(verb)
        return real(repo, run_dir)

    monkeypatch.setattr(run_mod.RX, "replay_pending_intent", spying)
    if verb == "approve":
        assert mgr.approve("demo", notes="ok", use_judge=False) == M.RUN_DONE
    else:
        mgr.reject("demo", "not yet", use_judge=False)
    assert calls == [verb]


@pytest.mark.parametrize("verb", ["approve", "reject"])
@pytest.mark.parametrize("initial_gate", [True, False])
def test_gate_verbs_resolve_the_gate_only_after_replay(
    fixture_repo, monkeypatch, verb, initial_gate
):
    """F-002 confirm: the pre-replay manifest cannot select or reject a gate.
    Replay runs first, the manifest is reloaded, and only the post-replay
    ``current_step`` is eligible to drive."""
    mgr = _gated_manager(fixture_repo)
    run_dir = mgr.layout("demo").active_run_dir()
    if not initial_gate:
        man = M.Manifest.load(run_dir / "manifest.json")
        man.current_step = None
        man.write_atomic(run_dir / "manifest.json")
    calls: list[str] = []

    def replay_and_clear_gate(repo, replay_run_dir):
        calls.append(verb)
        man = M.Manifest.load(replay_run_dir / "manifest.json")
        man.current_step = None
        man.write_atomic(replay_run_dir / "manifest.json")
        return "replayed"

    monkeypatch.setattr(
        run_mod.RX, "replay_pending_intent", replay_and_clear_gate
    )
    with pytest.raises(ValueError, match="no gate"):
        if verb == "approve":
            mgr.approve("demo", use_judge=False)
        else:
            mgr.reject("demo", "not yet", use_judge=False)
    assert calls == [verb]


# --- F-003: the action's target_ref is resolved and validated -------------------


def test_planner_refuses_an_unresolvable_target_ref(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    head = gitops.head_sha(fixture_repo)
    git_obs = RX.observe_git(
        fixture_repo, run_branch="main", recorded_sha=head, excludes=excludes
    )
    state_obs = RX.observe_state(man, None, liveness=RX.DriverLiveness.NONE)
    fp = RX.build_progress_fingerprint(fixture_repo, manifest=man, excludes=excludes)
    action = RX.SnapshotAndRestartAction(
        description="rewind under a phantom ref",
        target_ref="refs/heads/does-not-exist",
        target_sha=head,
        reason="test",
    )
    with pytest.raises(RX.RecoveryPreconditionError, match="does not resolve"):
        RX.RecoveryPlanner(fixture_repo).assess_rewind(
            git_obs=git_obs, state_obs=state_obs, fingerprint=fp, action=action,
            cause=RecoveryCause.WORKTREE_PARTIAL,
        )


def test_executor_refuses_a_target_ref_naming_a_different_ref(fixture_repo):
    """F-003: the advertised action and the mutation must name the same ref.
    The ref resolves to the observed tip at assessment time, then moves; the
    executor re-resolves it under the lock and refuses before any snapshot."""
    run_dir, man, excludes = _env(fixture_repo)
    (fixture_repo / "work.txt").write_text("committed\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "second commit")
    head = gitops.head_sha(fixture_repo)
    parent = gitops.commit_parent(fixture_repo, head)
    git(fixture_repo, "branch", "other")  # == head, so the planner accepts it
    (fixture_repo / "dirty.txt").write_text("partial\n")

    git_obs = RX.observe_git(
        fixture_repo, run_branch="main", recorded_sha=head, excludes=excludes
    )
    state_obs = RX.observe_state(man, None, liveness=RX.DriverLiveness.NONE)

    def fp():
        return RX.build_progress_fingerprint(
            fixture_repo, manifest=man, excludes=excludes
        )

    action = RX.SnapshotAndRestartAction(
        description="rewind advertised under the WRONG ref",
        target_ref="refs/heads/other",
        target_sha=head,
        reason="test",
    )
    assessment = RX.RecoveryPlanner(fixture_repo).assess_rewind(
        git_obs=git_obs, state_obs=state_obs, fingerprint=fp(), action=action,
        cause=RecoveryCause.WORKTREE_PARTIAL,
    )
    spec = RX.RewindSpec(
        site="test.rewind", target_sha=head, reset_mode=RX.RESET_PLAIN,
        clean=True, clean_excludes=("runs",),
    )
    executor = RX.RecoveryExecutor(
        fixture_repo, run_dir, run_id=man.run_id, run_root="runs",
        excludes=excludes,
    )
    # The advertised ref moves between assessment and apply: HEAD (the actual
    # rewind tip) and the action's ref now disagree.
    git(fixture_repo, "branch", "-f", "other", parent)
    with pytest.raises(RX.RecoveryPreconditionError, match="disagree"):
        executor.apply(
            assessment, action, spec=spec,
            snapshot_request=RX.SnapshotRequest(
                snapshot_id=f"t{next(_ids)}", reason="test", run_branch="main",
                exclude=list(excludes), protected=[],
            ),
            fingerprint=fp,
        )
    assert _recovery_refs(fixture_repo) == []  # refused before the snapshot
    assert (fixture_repo / "dirty.txt").read_text() == "partial\n"


# --- F-004: governed-artifact discards are loud, never refused ------------------


def test_internal_rewind_surfaces_governed_operator_discard_loudly(fixture_repo):
    """F-004 (scoped per operator direction): a hand-committed prd.md/plan.md
    edit is a SANCTIONED workflow, so an operator-invoked rewind that
    discards one proceeds — with the discard promoted to a manifest warning
    and the commit preserved through the snapshot's parent chain."""
    base = gitops.head_sha(fixture_repo)
    plan = fixture_repo / "runs" / "demo" / "plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("manually amended plan\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "-c", "user.name=Human", "-c", "user.email=h@h.local",
        "commit", "-qm", "PLAN.1: amend the plan by hand")
    ahead = gitops.head_sha(fixture_repo)

    man = _seed_running(fixture_repo, base)
    orch = _orchestrator(
        fixture_repo, manifest=man, interrupted="reset_to_base",
        adapters={"builder": FakeAdapter(writes={"clean.py": "out\n"})},
    )
    assert orch.drive() == M.RUN_DONE  # proceeds — never refused
    governed = [
        w for w in orch.manifest.warnings
        if RX.GOVERNED_DISCARD_EVIDENCE_PREFIX in w and "plan.md" in w
    ]
    assert governed, orch.manifest.warnings
    # The discarded commit stays reachable through the snapshot.
    refs = _recovery_refs(fixture_repo)
    snapshot = git_snapshot.load_snapshot(fixture_repo, refs[0])
    assert gitops.is_ancestor(fixture_repo, ahead, snapshot.snapshot_commit)


def _commit_governed_edit_on_the_run_branch(
    repo: Path, mgr, text: str, *,
    subject: str = "tweak the plan wording by hand",
) -> None:
    """A human's governed-artifact edit, committed where the rollback will see it.

    R9's governance path watches the COMMIT RANGE a rewind discards, so the
    edit has to be a commit on the run branch. P7g splits where the two happen:
    the human authors in their own checkout (§14.2 option A — the authoring
    surface, and what the engine reads and hashes), and the bytes reach the run
    branch through the engine's publish-into-the-work-tree step. Doing both
    here, in that order, is what a `same_tree` run does in one move — and what
    a `dedicated` run does across two trees.
    """
    from conftest import run_work_tree as _work

    authority = mgr.layout("demo").slug_dir / "plan.md"
    authority.write_text(text)
    work = _work(repo)
    published = work / "runs" / "demo" / "plan.md"
    published.parent.mkdir(parents=True, exist_ok=True)
    published.write_text(text)
    git(work, "add", "--", "runs/demo/plan.md")
    git(work, "-c", "user.name=Human", "-c", "user.email=h@h.local",
        "commit", "-qm", subject)


def test_rollback_surfaces_governed_operator_discard_loudly(fixture_repo):
    mgr = _rollback_manager(fixture_repo)
    _commit_governed_edit_on_the_run_branch(
        fixture_repo, mgr, "manually amended plan\n"
    )

    mgr.rollback("demo", phase=1)
    man = mgr.status("demo")
    assert any(
        RX.GOVERNED_DISCARD_EVIDENCE_PREFIX in w and "plan.md" in w
        for w in man.warnings
    ), man.warnings


def test_manual_governed_edit_then_approve_is_untouched(fixture_repo):
    """The sanctioned operator workflow end-to-end (operator direction on
    F-004): hand-edit a governed artifact while parked at a human gate,
    commit it, approve — nothing refuses, nothing rewinds, the edit stays."""
    mgr = _gated_manager(fixture_repo)
    prd = mgr.layout("demo").prd_path
    prd.write_text("# Real PRD\n\nManually revised after review.\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "-c", "user.name=Human", "-c", "user.email=h@h.local",
        "commit", "-qm", "revise the PRD by hand before approving")
    edited = gitops.head_sha(fixture_repo)

    assert mgr.approve("demo", notes="ok", use_judge=False) == M.RUN_DONE
    assert prd.read_text() == "# Real PRD\n\nManually revised after review.\n"
    assert gitops.is_ancestor(fixture_repo, edited, gitops.head_sha(fixture_repo))


def test_governed_discard_evidence_survives_kill_before_manifest_persist(
    fixture_repo, monkeypatch
):
    """F-004: the assessment evidence rides the durable intent, so a kill after
    Git apply cannot turn a governed discard into a silent one. Replay persists
    the warning before clearing the intent."""
    mgr = _rollback_manager(fixture_repo)
    # The subject deliberately LOOKS engine-shaped (`PLAN.1: …`), so the
    # classifier must reject it on identity rather than on the message.
    _commit_governed_edit_on_the_run_branch(
        fixture_repo, mgr, "manual conventional-subject amendment\n",
        subject="PLAN.1: revise the plan before approval",
    )
    monkeypatch.setattr(
        run_mod,
        "_apply_rollback_manifest_transition",
        lambda *a, **k: (_ for _ in ()).throw(_Boom("killed before persist")),
    )
    with pytest.raises(_Boom):
        mgr.rollback("demo", phase=1)
    monkeypatch.undo()
    run_dir = mgr.layout("demo").active_run_dir()
    assert RX.load_intent(run_dir) is not None

    mgr.rollback("demo", phase=1)
    man = mgr.status("demo")
    assert RX.load_intent(run_dir) is None
    assert any(
        RX.GOVERNED_DISCARD_EVIDENCE_PREFIX in warning
        and "plan.md" in warning
        for warning in man.warnings
    ), man.warnings


# --- F-005: an unreadable intent fails closed -----------------------------------


def test_unreadable_intent_fails_closed(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    path = RX.intent_path(run_dir)
    path.write_text("{}")
    if os.geteuid() == 0:  # pragma: no cover - CI-as-root cannot drop perms
        pytest.skip("root bypasses file permissions")
    os.chmod(path, 0)
    try:
        with pytest.raises(RX.RecoveryIntentError, match="could not be read"):
            RX.load_intent(run_dir)
        with pytest.raises(RX.RecoveryIntentError, match="could not be read"):
            RX.replay_pending_intent(fixture_repo, run_dir)
    finally:
        os.chmod(path, 0o644)


def test_malformed_intent_fails_closed(fixture_repo):
    run_dir, man, excludes = _env(fixture_repo)
    RX.intent_path(run_dir).write_text("not json at all")
    with pytest.raises(RX.RecoveryIntentError, match="malformed"):
        RX.load_intent(run_dir)

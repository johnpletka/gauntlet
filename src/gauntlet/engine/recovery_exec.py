"""Recovery planner + executor: the single rewind gateway (P3, plan §4.2/§4.3).

Every production rewind — rollback, the interrupted ``reset_to_base``
disposition, the conflict-park clean restore, the cycle's fix-resume reset,
and the reviewer-mutation revert — routes through :class:`RecoveryExecutor`,
which owns the mutation ordering:

1. acquire (or verify) the run/worktree lock;
2. re-observe and confirm the assessment's progress fingerprint still matches;
3. resolve and validate refs, targets, ancestry, and paths;
4. create the durable :mod:`git_snapshot` recovery snapshot — a snapshot
   failure aborts before any destructive verb (R2);
5. persist a recovery intent (action, snapshot ref, preconditions,
   idempotency key) under the ignored run-instance state dir;
6. apply the checkout/reset/clean/protected-restore operations;
7. persist the resulting state transition (caller callback / site finisher);
8. clear the intent only after the result is durable.

A surviving intent (a process killed between 5 and 8) is replayed
idempotently by the next mutating command (:func:`replay_pending_intent`),
converging to the intended end state or failing closed with named evidence —
never silently proceeding over an unrecognized repository state.

The planner (:class:`RecoveryPlanner`) is read-only: it consumes the P1
observation models plus liveness/pipeline metadata and emits a
:class:`~gauntlet.engine.recovery.RecoveryAssessment` whose ``safe_actions``
are the P1 discriminated-union actions with complete execution payloads.
P3 deliberately builds only what the executor and the converted rewind
callers need; the full four-workflow unification (status/resume rendering
from the same assessment) is P4, and nothing here precludes it — the
observation/assessment vocabulary is exactly the P1 contract P4 consumes.

The journal-anchored intent form (``refs/gauntlet/state/...``) is P6; P3
intents live as one atomic JSON file in the run-instance dir, which the
reset/clean paths never touch (it is excluded engine state).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import socket
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gauntlet.engine import git_snapshot, gitops, journal as J, manifest as M
from gauntlet.engine.gitops import ENGINE_IDENTITY, GitError
from gauntlet.engine.recovery import (
    AbortAction,
    AdoptCommitsAction,
    RebuildProjectionAction,
    BranchRelation,
    CommitKind,
    ContinueOnRecoveryBranchAction,
    DriverLiveness,
    GitCommitObservation,
    GitCommitPathChange,
    GitObservation,
    HumanDecisionAction,
    PathChangeKind,
    ProgressFingerprint,
    RecoveryAction,
    RecoveryActionKind,
    RecoveryAssessment,
    RecoveryCause,
    RecoveryDisposition,
    ResponseState,
    RestartFromCheckpointAction,
    RetryAction,
    RunStatus,
    SnapshotAndRestartAction,
    StateObservation,
    StepStatus,
    fingerprint_data,
)

# The executor's durable transaction intent, distinct from `recover`'s
# pre-signal `.recovery-intent.json` (FR-5.6): that one authorizes a kill;
# this one makes a planned REWIND replayable across a process death.
EXECUTOR_INTENT_NAME = ".recovery-transaction.json"
INTENT_SCHEMA_VERSION = 1

# The worktree-scoped active-run lock file name. Canonically this value lives
# in engine.run (DRIVING_LOCK_NAME); it is re-declared here because run.py
# imports the orchestrator (which imports this module), so importing run from
# here would be circular. test_recovery_executor pins the two equal.
DRIVING_LOCK_NAME = ".driving.lock"

# Commit-subject conventions, matched at fixed field position (the subject
# line), never against free prose. The stage-label alternation MIRRORS the
# enforced commit format (commit_format._HEADER_RE): artifact-mode cycles
# legitimately land `PRD.1:`/`PLAN.1:` fix commits, and classifying those as
# operator work would make governance checks refuse the engine's own
# review-loop history (post-P3 review F-004).
_STAGE_LABEL = r"(?:P\d+|PRD|PLAN|REVIEW)"
_WIP_RE = re.compile(r"^(P\d+) wip:")
_PHASE_RE = re.compile(rf"^({_STAGE_LABEL}):")
_FIX_RE = re.compile(rf"^({_STAGE_LABEL})\.(?:\d+|r\d+):")
_ENGINE_RE = re.compile(r"^gauntlet: ")

RESET_PLAIN = "plain"
RESET_BOOKKEEPING_PRESERVING = "bookkeeping_preserving"

# Assessment-evidence marker for a rewind that discards an operator commit
# modifying a governed artifact (prd.md/plan.md). The converted sites promote
# these lines to manifest warnings so the discard is loud (R9/FR-10.4) —
# manual PRD/plan edits are a sanctioned operator workflow and are never
# refused, only surfaced and snapshot-preserved.
GOVERNED_DISCARD_EVIDENCE_PREFIX = (
    "rewind discards governed-artifact commit "
)
GOVERNED_EVIDENCE_PAYLOAD_KEY = "governed_discard_evidence"

# Site-specific replay finishers: after a replayed apply converges the Git
# state, the intent's site may need its own state transition re-persisted
# (rollback's manifest rewind). run.py registers its finisher at import time;
# a replay with no registered finisher records a manifest warning instead of
# guessing (fail closed, data over inference).
REPLAY_FINISHERS: dict[str, Callable[[Path, Path, "RecoveryIntent"], None]] = {}


class RecoveryExecError(RuntimeError):
    """Base for recovery-transaction failures (all fail closed)."""


class RecoveryLockError(RecoveryExecError):
    """The run/worktree lock is held by another live process."""


class RecoveryPreconditionError(RecoveryExecError):
    """A precondition failed between assess and apply; nothing was mutated."""


class StateInvariantError(RecoveryExecError):
    """A verb tried to persist a run state the classifier calls `unknown`.

    #100: an `unknown` composite state forbids every mutating verb but has no
    counterpart repair affordance — so a verb that WRITES such a state wedges
    the run permanently. The invariant fails the verb (loudly, before any
    durable write), never the run.
    """


class RecoveryObservationError(RecoveryExecError):
    """The repository state cannot be represented by the P1 contracts.

    E.g. a merge commit inside a recorded..tip range: the P1 commit-inventory
    contract requires a contiguous single-parent chain, so the observer
    refuses rather than mislabel the range (fail closed).
    """


class RecoveryIntentError(RecoveryExecError):
    """A surviving intent could not be replayed safely; it is left in place."""


# --- observation ----------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def index_fingerprint(repo: Path, *, exclude: list[str] | None = None) -> str:
    """Stable content fingerprint of the real index (paths, modes, oids, stages).

    ``exclude`` drops the engine's own bookkeeping paths (#90): a verb-own
    bookkeeping commit re-stages those paths, and without the exclusion every
    resume's checkpoint commit moved this plane and read as progress.
    """
    out = gitops._run(
        repo, "ls-files", "--stage", "-z", *gitops._exclude_pathspec(exclude)
    )
    return _sha256(out.encode("utf-8", "surrogateescape"))


def _dirty_paths(repo: Path, *, exclude: list[str] | None) -> list[str]:
    """Worktree paths with any index/worktree state vs HEAD, rename-resolved.

    ``-z`` porcelain so special characters never arrive quoted; the second
    NUL-separated field of a rename entry (the source) is skipped — the live
    path is what gets content-fingerprinted.
    """
    out = gitops._run(
        repo, "status", "--porcelain", "-z", "--untracked-files=all",
        *gitops._exclude_pathspec(exclude),
    )
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        # The worktree-driving lock (and its transient acquire temp files) is
        # engine control state, not work: the executor itself may hold an
        # ephemeral lock while fingerprinting, and a run root without the
        # ignore rule would otherwise read the lock as progress.
        name = PurePosixPath(rel).name
        if name == DRIVING_LOCK_NAME or name.startswith(DRIVING_LOCK_NAME + "."):
            if "R" in status or "C" in status:
                i += 1
            continue
        paths.append(rel)
        if "R" in status or "C" in status:
            i += 1  # skip the rename/copy source field
    return sorted(set(paths))


def worktree_fingerprint(
    repo: Path, *, exclude: list[str] | None = None, anchor: str | None = None
) -> str:
    """Content-true fingerprint of the worktree plane relative to HEAD.

    Porcelain alone is lossy (two different byte contents of one modified path
    fingerprint identically), so every dirty path's live content identity is
    folded in: symlinks by target (never followed), files by byte hash,
    missing paths as deletions. HEAD's tree id anchors the committed plane —
    unless ``anchor`` names a different commit to anchor on (the R5 guard
    passes the last substantive commit so verb-own bookkeeping commits do not
    read as progress, #90).
    """
    entries: list[tuple[str, str, str]] = []
    for rel in _dirty_paths(repo, exclude=exclude):
        path = repo / rel
        try:
            info = path.lstat()
        except FileNotFoundError:
            entries.append((rel, "absent", ""))
            continue
        if stat.S_ISLNK(info.st_mode):
            entries.append((rel, "symlink", os.readlink(path)))
        elif stat.S_ISDIR(info.st_mode):
            entries.append((rel, "directory", ""))
        elif stat.S_ISREG(info.st_mode):
            mode = "100755" if info.st_mode & stat.S_IXUSR else "100644"
            entries.append((rel, f"file:{mode}", _sha256(path.read_bytes())))
        else:
            entries.append((rel, f"other:{info.st_mode:o}", ""))
    head_tree = gitops._run(repo, "rev-parse", f"{anchor or 'HEAD'}^{{tree}}").strip()
    payload = json.dumps([head_tree, entries], sort_keys=True).encode()
    return _sha256(payload)


def _classify_commit(
    sha: str,
    author_name: str,
    author_email: str,
    subject: str,
    changed: tuple[GitCommitPathChange, ...],
    *,
    bookkeeping_candidates: frozenset[str],
) -> tuple[CommitKind, str | None, str | None, tuple[str, ...]]:
    """Evidence-backed (kind, phase_id, checkpoint_id, evidence) for one commit.

    Engine bookkeeping requires all three legs (identity AND subject prefix AND
    every changed path in the allowlist) — the same fail-closed rule as
    ``gitops.advance_is_engine_bookkeeping``. Checkpoint/phase/fix key on the
    fixed subject conventions; anything else is operator work (plan §5.4:
    unknown commits are operator work, never engine bookkeeping).
    """
    is_engine_identity = (
        author_name == ENGINE_IDENTITY.name and author_email == ENGINE_IDENTITY.email
    )
    if is_engine_identity and _ENGINE_RE.match(subject):
        paths = {c.path for c in changed}
        if paths <= bookkeeping_candidates:
            return (
                CommitKind.ENGINE_BOOKKEEPING,
                None,
                None,
                (
                    "engine identity + `gauntlet: ` subject + every changed "
                    "path in the bookkeeping allowlist",
                ),
            )
        return (
            CommitKind.OPERATOR,
            None,
            None,
            (
                "engine-labelled commit touches paths outside the bookkeeping "
                f"allowlist ({sorted(paths - bookkeeping_candidates)!r}); "
                "markers alone are forgeable — classified as operator work",
            ),
        )
    wip = _WIP_RE.match(subject)
    if wip:
        return (
            CommitKind.CHECKPOINT,
            wip.group(1),
            sha,
            (f"subject matches the `{wip.group(1)} wip:` checkpoint convention",),
        )
    fix = _FIX_RE.match(subject)
    if fix:
        return (
            CommitKind.FIX,
            fix.group(1),
            None,
            ("subject matches the `P<N>.<x>:` fix/review-round convention",),
        )
    phase = _PHASE_RE.match(subject)
    if phase:
        return (
            CommitKind.PHASE,
            phase.group(1),
            None,
            ("subject matches the `P<N>:` phase-commit convention",),
        )
    return (
        CommitKind.OPERATOR,
        None,
        None,
        (
            f"authored by {author_name} <{author_email}> with no engine/phase "
            "subject convention — treated as operator work (plan §5.4)",
        ),
    )


_PATH_STATUS_KINDS = {
    "A": PathChangeKind.ADDED,
    "M": PathChangeKind.MODIFIED,
    "D": PathChangeKind.DELETED,
    "T": PathChangeKind.TYPE_CHANGED,
    "U": PathChangeKind.UNMERGED,
}


def _commit_path_changes(
    repo: Path, sha: str, *, approved_artifacts: frozenset[str]
) -> tuple[GitCommitPathChange, ...]:
    out = gitops._run(
        repo, "diff-tree", "-r", "-z", "-M", "--name-status", "--no-commit-id", sha
    )
    fields = [f for f in out.split("\0")]
    changes: list[GitCommitPathChange] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        i += 1
        if not status:
            continue
        code = status[0]
        if code in ("R", "C"):
            previous, current = fields[i], fields[i + 1]
            i += 2
            kind = PathChangeKind.RENAMED if code == "R" else PathChangeKind.COPIED
            changes.append(
                GitCommitPathChange(
                    kind=kind,
                    path=current,
                    previous_path=previous,
                    approved_artifact=current in approved_artifacts,
                )
            )
        else:
            current = fields[i]
            i += 1
            changes.append(
                GitCommitPathChange(
                    kind=_PATH_STATUS_KINDS.get(code, PathChangeKind.MODIFIED),
                    path=current,
                    approved_artifact=current in approved_artifacts,
                )
            )
    return tuple(changes)


def _inventory_range(
    repo: Path,
    boundary: str,
    tip: str,
    *,
    bookkeeping_candidates: frozenset[str],
    approved_artifacts: frozenset[str],
) -> tuple[GitCommitObservation, ...]:
    """Oldest→newest commit inventory for ``boundary..tip`` (P1 contract).

    Refuses a non-linear range: the P1 model requires a contiguous
    single-parent chain, and every rewind path in this engine operates on a
    linear run branch — a merge inside the range is evidence of manual
    surgery that must be reconciled by a human, not silently classified.
    """
    out = gitops._run(
        repo, "log", "--reverse", "--format=%H%x00%P%x00%an%x00%ae%x00%s",
        f"{boundary}..{tip}",
    )
    commits: list[GitCommitObservation] = []
    for line in out.splitlines():
        sha, parents_raw, name, email, subject = line.split("\x00", 4)
        parents = tuple(parents_raw.split()) if parents_raw else ()
        if len(parents) != 1:
            raise RecoveryObservationError(
                f"commit {sha[:10]} in {boundary[:10]}..{tip[:10]} has "
                f"{len(parents)} parents; the recovery contracts require a "
                "linear run-branch range (reconcile the merge manually first)"
            )
        changed = _commit_path_changes(repo, sha, approved_artifacts=approved_artifacts)
        kind, phase_id, checkpoint_id, evidence = _classify_commit(
            sha, name, email, subject, changed,
            bookkeeping_candidates=bookkeeping_candidates,
        )
        commits.append(
            GitCommitObservation(
                sha=sha,
                parents=parents,
                author_name=name,
                author_email=email,
                subject=subject,
                changed_paths=changed,
                kind=kind,
                phase_id=phase_id,
                checkpoint_id=checkpoint_id,
                classification_evidence=evidence,
            )
        )
    return tuple(commits)


def _relation_from_kinds(kinds: set[CommitKind]) -> BranchRelation:
    """The ahead-relation label the inventoried commit roles prove (P1 rules)."""
    if kinds == {CommitKind.ENGINE_BOOKKEEPING}:
        return BranchRelation.ENGINE_BOOKKEEPING_AHEAD
    if CommitKind.CHECKPOINT in kinds and kinds <= {
        CommitKind.CHECKPOINT, CommitKind.ENGINE_BOOKKEEPING
    }:
        return BranchRelation.CHECKPOINT_AHEAD
    implementation = {
        CommitKind.PHASE, CommitKind.FIX,
        CommitKind.CHECKPOINT, CommitKind.ENGINE_BOOKKEEPING,
    }
    if kinds & {CommitKind.PHASE, CommitKind.FIX} and kinds <= implementation:
        return BranchRelation.IMPLEMENTATION_AHEAD
    if kinds == {CommitKind.OPERATOR}:
        return BranchRelation.OPERATOR_AHEAD
    if kinds == {CommitKind.UNKNOWN}:
        return BranchRelation.UNCLASSIFIED_AHEAD
    return BranchRelation.MIXED_AHEAD


def observe_git(
    work_root: Path,
    *,
    run_branch: str,
    recorded_sha: str | None,
    excludes: list[str] | None = None,
    bookkeeping_candidates: list[str] | None = None,
    approved_artifacts: list[str] | None = None,
) -> GitObservation:
    """Build the P1 :class:`GitObservation` for a rewind decision. Read-only.

    ``recorded_sha`` is the site's recorded boundary (a step's ``base_sha``,
    a cycle round's handoff, rollback's last recorded commit). An ahead or
    forked branch is fully inventoried commit-by-commit so the relation label
    is proven by auditable data, exactly as the P1 model validators demand.
    """
    branch = gitops.current_branch(work_root)
    checked_out = None if branch == "HEAD" else branch
    head = gitops.head_sha(work_root)
    run_branch_sha: str | None = None
    if gitops.branch_exists(work_root, run_branch):
        run_branch_sha = gitops.rev_parse(work_root, f"refs/heads/{run_branch}")

    bookkeeping = frozenset(bookkeeping_candidates or [])
    approved = frozenset(approved_artifacts or [])
    merge_base: str | None = None
    commits: tuple[GitCommitObservation, ...] = ()

    if run_branch_sha is None:
        relation = BranchRelation.MISSING
    elif recorded_sha is None:
        relation = BranchRelation.UNRECORDED
    elif run_branch_sha == recorded_sha:
        relation = BranchRelation.EQUAL
    elif gitops.is_ancestor(work_root, recorded_sha, run_branch_sha):
        commits = _inventory_range(
            work_root, recorded_sha, run_branch_sha,
            bookkeeping_candidates=bookkeeping, approved_artifacts=approved,
        )
        relation = _relation_from_kinds({c.kind for c in commits})
    elif gitops.is_ancestor(work_root, run_branch_sha, recorded_sha):
        relation = BranchRelation.BEHIND
        merge_base = run_branch_sha
    else:
        relation = BranchRelation.FORKED
        merge_base = gitops.merge_base(work_root, run_branch_sha, recorded_sha)
        if merge_base is None:
            raise RecoveryObservationError(
                f"run branch {run_branch!r} shares no history with the recorded "
                f"boundary {recorded_sha[:10]}; unrelated histories cannot be "
                "assessed for recovery"
            )
        commits = _inventory_range(
            work_root, merge_base, run_branch_sha,
            bookkeeping_candidates=bookkeeping, approved_artifacts=approved,
        )

    return GitObservation(
        checked_out_branch=checked_out,
        head_sha=head,
        run_branch=run_branch,
        run_branch_sha=run_branch_sha,
        recorded_sha=recorded_sha,
        branch_relation=relation,
        merge_base_sha=merge_base,
        run_branch_commits=commits,
        index_fingerprint=index_fingerprint(work_root),
        worktree_fingerprint=worktree_fingerprint(work_root, exclude=excludes),
    )


def observe_state(
    manifest: "M.Manifest",
    record: "M.StepRecord | None",
    *,
    liveness: DriverLiveness,
) -> StateObservation:
    """Build the P1 :class:`StateObservation` from persisted state. Read-only."""
    pending_id = None
    pending_state = None
    if record is not None and record.human_responses:
        entry = record.human_responses[-1]
        pending_id = entry.response_id
        pending_state = ResponseState(entry.state)
    return StateObservation(
        run_status=RunStatus(manifest.status),
        step_status=StepStatus(record.status) if record is not None else None,
        attempt_id=(
            f"{record.id}#{record.attempts}" if record is not None else None
        ),
        liveness=liveness,
        pending_response_id=pending_id,
        pending_response_state=pending_state,
    )


# Engine-authored commit subjects all carry this prefix (response checkpoints,
# bookkeeping flushes, rewind markers). They are created BY the very verbs the
# R5 guard wraps, so treating them as branch movement lets every resume mint a
# "fresh" fingerprint and the no-progress guard never fires (#90).
_ENGINE_COMMIT_SUBJECT_PREFIX = "gauntlet: "
_ENGINE_COMMIT_WALK_LIMIT = 100


def _skip_engine_bookkeeping_commits(repo: Path, sha: str) -> str:
    """First ancestor of ``sha`` that is not an engine bookkeeping commit.

    A rewind marker's parent IS the rewind target, so skipping it still
    registers the rewind (the substantive ancestor changed); a response
    checkpoint's parent is whatever the branch already held, so skipping it
    correctly reads "no substantive movement". The walk is bounded and any
    git failure returns the sha reached — fail toward the raw tip, never
    toward silence.
    """
    for _ in range(_ENGINE_COMMIT_WALK_LIMIT):
        try:
            subject = gitops.commit_subject(repo, sha)
            if not subject.startswith(_ENGINE_COMMIT_SUBJECT_PREFIX):
                return sha
            sha = gitops.commit_parent(repo, sha)
        except gitops.GitError:
            return sha  # root commit or unreadable history: stop here
    return sha


def build_progress_fingerprint(
    repo: Path,
    *,
    manifest: "M.Manifest",
    record: "M.StepRecord | None" = None,
    excludes: list[str] | None = None,
    latest_cycle_substep: str | None = None,
) -> ProgressFingerprint:
    """The plan §4.5 fingerprint over run/step/branch/index/worktree state.

    ``artifact_fingerprint`` and ``latest_cycle_substep`` are derived from the
    step record's own durable state (post-review F-005): the artifact input is
    the FR-2.2 revalidation record (whose content hashes the validator updates
    against the live artifact bytes on every resume, so a hand-edit that still
    fails validation registers as progress), and the cycle input is the newest
    persisted sub-step checkpoint — a cycle that completed one more durable
    sub-step without landing a commit is progress, never a no-op. An explicit
    ``latest_cycle_substep`` argument still wins when a caller has richer
    in-flight knowledge.
    """
    run_branch_sha = None
    if gitops.branch_exists(repo, manifest.branch):
        run_branch_sha = _skip_engine_bookkeeping_commits(
            repo, gitops.rev_parse(repo, f"refs/heads/{manifest.branch}")
        )
    pending_id = None
    pending_state = None
    artifact_fp = None
    if record is not None and record.human_responses:
        entry = record.human_responses[-1]
        pending_id = entry.response_id
        pending_state = ResponseState(entry.state)
    if record is not None and record.revalidation is not None:
        artifact_fp = fingerprint_data(record.revalidation.model_dump(mode="json"))
    if latest_cycle_substep is None and record is not None and record.checkpoints:
        last = record.checkpoints[-1]
        latest_cycle_substep = f"r{last.round}-{last.sub_step}"
    # The FAILED retry counter is bookkeeping, not progress (P5.1 review
    # F-002): every re-failure increments ``attempts``, so folding it into the
    # attempt identity would mint a fresh fingerprint per identical failure
    # and let a deterministic re-runnable failure bypass the R5 no-progress
    # guard forever. A FAILED record's attempt identity is therefore the step
    # alone; every other status keeps the attempt-boundary counter.
    attempt_id = None
    if record is not None:
        counter = "" if record.status == M.FAILED else f"#{record.attempts}"
        attempt_id = f"{record.id}{counter}"
    notes_fp = None
    if record is not None and record.notes:
        notes_fp = _sha256(record.notes.encode("utf-8", "surrogateescape"))
    return ProgressFingerprint(
        run_id=manifest.run_id,
        current_step=manifest.current_step,
        iteration=record.iteration if record is not None else None,
        attempt_id=attempt_id,
        run_status=RunStatus(manifest.status),
        step_status=StepStatus(record.status) if record is not None else None,
        run_branch_sha=run_branch_sha,
        index_fingerprint=index_fingerprint(repo, exclude=excludes),
        worktree_fingerprint=worktree_fingerprint(
            repo, exclude=excludes, anchor=run_branch_sha
        ),
        artifact_fingerprint=artifact_fp,
        pending_response_id=pending_id,
        pending_response_state=pending_state,
        latest_cycle_substep=latest_cycle_substep,
        step_notes_fingerprint=notes_fp,
    )


# --- the planner ----------------------------------------------------------------


class RecoveryPlanner:
    """Read-only rewind assessment (plan §4.2) — P3 scope: Git mutation paths.

    Consumes the P1 observation models plus liveness metadata and emits an
    assessment whose ``safe_actions`` carry complete execution payloads. It
    performs no checkout, staging, reset, cleanup, or file writes; its only
    repository access is read-only ancestry validation of the proposed target.
    """

    def __init__(self, repo: Path) -> None:
        self.repo = repo

    def assess(
        self,
        *,
        manifest: "M.Manifest",
        liveness: str,
        git_obs: GitObservation | None,
        fingerprint: ProgressFingerprint,
    ) -> RecoveryAssessment:
        """The one general recovery assessment (P4, plan §4.2 / R4).

        Consumed by all four public workflows: ``status`` renders it,
        ``resume`` chooses/apply its default action from it, ``recover``
        derives its finalization reconciliation from the same observation, and
        ``rollback`` (already) plans its rewind on the same machinery. Pure
        over its inputs: no checkout, staging, reset, cleanup, or file writes.

        The composite state and the mutating-action set come from the SAME
        table operator.py renders (:func:`classify_composite` /
        :func:`mutating_action_kinds`), and the branch-relation refinement
        (adoption / checkpoint continuation / recovery-ref workflow, plan
        §5.4) comes from the same inventory :func:`reconcile_branch_ahead`
        applies — so the read-only view and the mutating path cannot disagree.
        """
        state, parked, failure = classify_composite(manifest, liveness)
        failure_kind = failure.failure_kind if failure is not None else None
        failure_type = failure.type if failure is not None else None
        rec = _attempt_record(manifest)
        attempt_id = f"{rec.id}#{rec.attempts}" if rec is not None else None
        slug = manifest.slug

        cause = _STATE_CAUSE[state]
        disposition = _STATE_DISPOSITION[state]
        if state == STATE_FAILED and failure_kind in M.RERUNNABLE_FAILURE_KINDS:
            cause = RecoveryCause.PRECONDITION_UNSATISFIED
            disposition = RecoveryDisposition.RETRY
        # P5 refinement (plan §6 P5): a record that carries the orthogonal
        # cause/disposition pair stamped at outcome time is EVIDENCE — it
        # refines the coarse state→cause map above. Values are validated
        # against the closed enums; an unrecognized persisted value is ignored
        # (fail closed to the coarse map, never a guessed refinement). Pre-P5
        # manifests carry None and classify exactly as before (plan §8).
        refined_note: str | None = None
        refine_rec = parked or failure
        if refine_rec is not None and (
            refine_rec.recovery_cause or refine_rec.recovery_disposition
        ):
            try:
                refined_cause = (
                    RecoveryCause(refine_rec.recovery_cause)
                    if refine_rec.recovery_cause else cause
                )
                refined_disposition = (
                    RecoveryDisposition(refine_rec.recovery_disposition)
                    if refine_rec.recovery_disposition else disposition
                )
            except ValueError:
                refined_note = (
                    "recorded recovery classification "
                    f"({refine_rec.recovery_cause!r}/"
                    f"{refine_rec.recovery_disposition!r}) is not a known "
                    "cause/disposition; ignored (fail closed to the state map)"
                )
            else:
                cause, disposition = refined_cause, refined_disposition
                refined_note = (
                    f"recorded outcome classification: cause={cause.value}, "
                    f"disposition={disposition.value} (stamped at finalize, "
                    "plan §6 P5)"
                )

        actions: list[RecoveryAction] = []
        for kind in mutating_action_kinds(
            state, failure_kind=failure_kind, step_type=failure_type
        ):
            if kind is RecoveryActionKind.RETRY:
                actions.append(
                    RetryAction(
                        description=(
                            f"plain `gauntlet resume {slug}` re-drives the run "
                            "from its persisted state"
                        ),
                        operation="resume",
                        attempt_id=attempt_id,
                    )
                )
            elif kind is RecoveryActionKind.HUMAN_DECISION:
                step = parked or failure
                step_id = step.id if step is not None else slug
                if state == STATE_PARKED_GATE:
                    actions.append(
                        HumanDecisionAction(
                            description=(
                                f"decide the parked gate: `gauntlet approve "
                                f"{slug}` or `gauntlet reject {slug} --notes`"
                            ),
                            decision_id=f"gate:{step_id}",
                            prompt="approve or reject the parked human gate",
                        )
                    )
                else:
                    actions.append(
                        HumanDecisionAction(
                            description=(
                                f"`gauntlet resume {slug} --response "
                                '"<decision>"` injects a human decision'
                            ),
                            decision_id=f"response:{step_id}",
                            prompt="supply the decision that unblocks the step",
                        )
                    )
            elif kind is RecoveryActionKind.SNAPSHOT_AND_RESTART:
                target_sha = None
                if rec is not None and rec.base_sha:
                    target_sha = rec.base_sha
                elif git_obs is not None and git_obs.run_branch_sha:
                    target_sha = git_obs.run_branch_sha
                if target_sha is None:
                    continue  # no executable payload → cannot advertise it
                branch = (
                    git_obs.run_branch if git_obs is not None else manifest.branch
                )
                actions.append(
                    SnapshotAndRestartAction(
                        description=(
                            f"`gauntlet resume {slug} --reset-interrupted` "
                            "snapshots the partial work and re-runs from the "
                            "latest committed checkpoint"
                        ),
                        target_ref=f"refs/heads/{branch}",
                        target_sha=target_sha,
                        reason="discard the interrupted attempt after snapshot",
                    )
                )
            elif kind is RecoveryActionKind.ABORT:
                # F-007: a terminal failure of a non-respondable step — its
                # only executable exit that resume's validators will accept.
                actions.append(
                    AbortAction(
                        description=(
                            f"`gauntlet abort {slug}` aborts the run, retaining "
                            "every snapshot and all evidence"
                        ),
                        reason=(
                            f"step type {failure_type!r} accepts neither a plain "
                            "re-run nor `--response`; abort is the executable exit"
                        ),
                    )
                )
                disposition = RecoveryDisposition.ABORT_ONLY

        evidence = [f"composite_state={state}", f"liveness={liveness}"]
        if refined_note is not None:
            evidence.append(refined_note)
        # Branch-relation refinement applies ONLY to a proven-dead driver in a
        # nonterminal state (post-review F-002): while a driver is verifiably
        # alive — or unprovably so — a branch legitimately runs ahead of the
        # manifest mid-step, and advertising an adoption resume or a raw ref
        # restore would race (or bypass the lock of) the active process. Live
        # and indeterminate rows keep their observe-only base actions; the
        # branch evidence is still recorded, as evidence.
        driver_gone = str(liveness) in (_LIVENESS_ORPHANED, _LIVENESS_NONE)
        refinable = driver_gone and state in _NONTERMINAL_DEAD_DRIVER_STATES
        if git_obs is not None:
            relation = git_obs.branch_relation
            evidence.append(f"branch_relation={relation.value}")
            if not refinable and relation not in (
                BranchRelation.EQUAL,
                BranchRelation.UNRECORDED,
                BranchRelation.ENGINE_BOOKKEEPING_AHEAD,
            ):
                evidence.append(
                    "branch evidence is advisory only: no proven-dead driver "
                    "in a nonterminal state, so no reconciliation action is "
                    "advertised (F-002)"
                )
            adoption_applies = rec is None or rec.type == "agent_task"
            if (
                refinable
                and relation in _ADOPTABLE_AHEAD
                and git_obs.run_branch_commits
                and adoption_applies  # step-owned recovery defers, like resume
            ):
                boundary = git_obs.recorded_sha
                tip = git_obs.run_branch_sha
                evidence.append(
                    f"run branch ahead of the recorded boundary by "
                    f"{boundary[:10]}..{tip[:10]} "
                    f"({len(git_obs.run_branch_commits)} commit(s))"
                )
                for commit, paths in _governed_range_edits(git_obs):
                    evidence.append(
                        f"commit {commit.sha[:10]} ({commit.subject!r}) "
                        f"modifies governed artifact(s) {', '.join(paths)} — "
                        "sanctioned; ratified through the artifact's own "
                        "gate/response loop (R9/FR-10.4)"
                    )
                relation_action: RecoveryAction
                if relation is BranchRelation.CHECKPOINT_AHEAD:
                    checkpoint = _latest_checkpoint(git_obs)
                    assert checkpoint is not None
                    relation_action = RestartFromCheckpointAction(
                        description=(
                            f"plain `gauntlet resume {slug}` adopts checkpoint "
                            f"{checkpoint.sha[:10]} ({checkpoint.subject!r}) as "
                            "the attempt boundary and continues from it"
                        ),
                        checkpoint_sha=checkpoint.sha,
                        step_id=rec.id if rec is not None else slug,
                    )
                    cause = RecoveryCause.BRANCH_AHEAD
                    disposition = RecoveryDisposition.RESTART_FROM_CHECKPOINT
                else:
                    relation_action = AdoptCommitsAction(
                        description=(
                            f"plain `gauntlet resume {slug}` adopts "
                            f"{boundary[:10]}..{tip[:10]} into the manifest "
                            "and continues (loud manifest audit; nothing "
                            "rewound or discarded)"
                        ),
                        base_sha=boundary,
                        tip_sha=tip,
                        commit_shas=tuple(
                            c.sha for c in git_obs.run_branch_commits
                        ),
                    )
                    cause = RecoveryCause.BRANCH_AHEAD
                    disposition = RecoveryDisposition.ADOPT_COMMITS
                actions = [
                    a for a in actions if a.kind is not RecoveryActionKind.RETRY
                ]
                actions.insert(0, relation_action)
            elif refinable and relation in _DIVERGED_RELATIONS:
                evidence.append(
                    "the run branch and the recorded boundary disagree "
                    f"({relation.value}); a plain resume refuses until the "
                    "branch is restored"
                )
                cause = RecoveryCause.BRANCH_DIVERGED
                disposition = RecoveryDisposition.CONTINUE_ON_RECOVERY_BRANCH
                actions = list(relation_recovery_actions(git_obs, manifest))

        if state in _NONTERMINAL_DEAD_DRIVER_STATES and not actions:
            # R1: every persisted nonterminal state with a dead driver exposes
            # at least one safe executable action — abort is the floor.
            actions.append(
                AbortAction(
                    description=(
                        f"`gauntlet abort {slug}` aborts the run, retaining "
                        "every snapshot and all evidence"
                    ),
                    reason="no more specific safe action is executable",
                )
            )
        kinds = [a.kind for a in actions]
        recommended = kinds[0] if kinds else None
        if not actions and disposition is not RecoveryDisposition.CONTINUE:
            disposition = RecoveryDisposition.CONTINUE  # nothing executable
        return RecoveryAssessment(
            cause=cause,
            disposition=disposition,
            evidence=tuple(evidence),
            safe_actions=tuple(actions),
            recommended_action=recommended,
            progress_fingerprint=fingerprint.digest,
        )

    def assess_rewind(
        self,
        *,
        git_obs: GitObservation,
        state_obs: StateObservation,
        fingerprint: ProgressFingerprint,
        action: SnapshotAndRestartAction,
        cause: RecoveryCause,
        evidence: tuple[str, ...] = (),
    ) -> RecoveryAssessment:
        """Assess one planned snapshot-and-restart rewind.

        Validates that the action's target is provably reachable from the
        observed state: the current HEAD itself, the recorded boundary, an
        inventoried range commit, or an ancestor of the observed tips. The
        action's ``target_ref`` must resolve to an observed tip — an action
        naming one ref while the mutation would rewind another can never be
        advertised as safe (post-P3 review F-003).

        Artifact governance (R9/FR-10.4, post-P3 review F-004): commits the
        rewind would discard are checked against the observation's
        governed-artifact flags, and every operator commit modifying a
        governed artifact (prd.md/plan.md) in the discard range is recorded
        as explicit assessment evidence — the converted sites surface it as
        a manifest warning. Deliberately observation-and-audit, NEVER a
        refusal: manually editing and committing the PRD/plan is a sanctioned
        operator workflow (that is what the human gates ratify), so a rewind
        the operator invokes must proceed — loudly, with the discarded state
        preserved in the durable snapshot — rather than wedge their own
        edit behind a guard. Engine review-loop commits (phase/fix/
        checkpoint/bookkeeping shapes) are not governance events.
        Approval-state routing landed with the P5 taxonomy (plan §5.1): a
        PRE-approval artifact defect parks back into the artifact's own
        author/fix loop at its site (the FR-2.2 validators / phase_lint),
        while a POST-approval edit stays on this loud, never-refused
        governance path.
        """
        target = action.target_sha
        known = {git_obs.head_sha, git_obs.recorded_sha, git_obs.run_branch_sha}
        known |= {c.sha for c in git_obs.run_branch_commits}
        if target not in known:
            tip = git_obs.run_branch_sha or git_obs.head_sha
            if not gitops.is_ancestor(self.repo, target, tip):
                raise RecoveryPreconditionError(
                    f"rewind target {target[:10]} is neither an observed "
                    f"state nor an ancestor of the observed tip {tip[:10]}; "
                    "refusing to plan a rewind onto unproven history"
                )
        try:
            resolved_ref = gitops.rev_parse(self.repo, action.target_ref)
        except GitError as exc:
            raise RecoveryPreconditionError(
                f"action target_ref {action.target_ref!r} does not resolve; "
                "an action without a real executable ref cannot be safe"
            ) from exc
        if resolved_ref not in {git_obs.head_sha, git_obs.run_branch_sha}:
            raise RecoveryPreconditionError(
                f"action target_ref {action.target_ref!r} resolves to "
                f"{resolved_ref[:10]}, which is neither the observed HEAD "
                f"({git_obs.head_sha[:10]}) nor the observed run-branch tip; "
                "the advertised action and the observation disagree"
            )
        governed = _governed_discards(git_obs, target)
        if governed:
            evidence = evidence + tuple(
                f"{GOVERNED_DISCARD_EVIDENCE_PREFIX}{c.sha[:10]} "
                f"({c.subject!r}) modifying governed artifact(s) "
                f"{', '.join(paths)} — preserved in the recovery snapshot, "
                "surfaced loudly, never silently (R9/FR-10.4)"
                for c, paths in governed
            )
        return RecoveryAssessment(
            cause=cause,
            disposition=RecoveryDisposition.SNAPSHOT_AND_RESTART,
            evidence=(
                *evidence,
                f"branch_relation={git_obs.branch_relation}",
                f"liveness={state_obs.liveness}",
            ),
            safe_actions=(
                action,
                AbortAction(
                    description="abort, retaining every snapshot and all evidence",
                    reason="operator abort while recovery evidence is preserved",
                ),
            ),
            recommended_action=RecoveryActionKind.SNAPSHOT_AND_RESTART,
            progress_fingerprint=fingerprint.digest,
        )


def projection_rebuild_assessment(
    repo_root: Path, run_dir: Path, *, slug: str | None = None
) -> tuple[RecoveryAssessment, RebuildProjectionAction] | None:
    """The one assessment for a missing/corrupt manifest projection (plan §5.5).

    Consumed by BOTH surfaces (R4): read-only ``status`` renders the returned
    action (``operator.load_projection_view``), and the mutating verbs apply
    the SAME action through :meth:`RecoveryExecutor.apply_rebuild`
    (``RunManager._reconcile_projection``) — one construction point, zero
    drift. Returns ``None`` when no rebuild is pending (healthy projection,
    or a pre-P6 run with no journal to rebuild from — old runs keep their
    exact pre-P6 behavior, plan §8).

    The action carries the plan §4.6 execution payload: the journal and
    projection paths (repo-relative) and the evidence fingerprint of the
    on-disk projection bytes (``"absent"`` when deleted), which the executor
    re-verifies under the lock before mutating anything.
    """
    status = J.projection_status(
        run_dir, mutate=False, validate=M.validate_projection_text
    )
    if status.health not in (J.HEALTH_CORRUPT, J.HEALTH_MISSING):
        return None
    try:
        run_rel = run_dir.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return None  # a run dir outside the repo cannot carry a contained path
    action = RebuildProjectionAction(
        description=(
            "rebuild manifest.json from the authoritative journal head "
            f"(seq {status.head_seq}); the malformed original is preserved "
            "as recovery evidence first — a plain resume applies this "
            "(plan §5.5; never hand-edit manifest.json)"
        ),
        journal_path=(run_rel / J.JOURNAL_DIRNAME).as_posix(),
        projection_path=(run_rel / "manifest.json").as_posix(),
        evidence_fingerprint=status.evidence_fingerprint,
    )
    label = slug or status.run_id or "<run>"
    assessment = RecoveryAssessment(
        cause=RecoveryCause.STATE_INCONSISTENT,
        disposition=RecoveryDisposition.REBUILD_PROJECTION,
        evidence=(
            f"manifest projection is {status.health} "
            f"(fingerprint {status.evidence_fingerprint})",
            f"journal head seq {status.head_seq} for run "
            f"{status.run_id!r} is the authoritative state (plan §4.6/R8)",
            *status.notes,
        ),
        safe_actions=(
            action,
            AbortAction(
                description=(
                    f"`gauntlet abort {label}` aborts the run, retaining "
                    "every snapshot and all evidence"
                ),
                reason="operator abort instead of rebuilding the projection",
            ),
        ),
        recommended_action=RecoveryActionKind.REBUILD_PROJECTION,
        progress_fingerprint=status.evidence_fingerprint,
    )
    return assessment, action


def _governed_discards(
    git_obs: GitObservation, target: str
) -> list[tuple[GitCommitObservation, list[str]]]:
    """Commits the rewind would discard that modify governed artifacts.

    Commit-subject conventions classify phase/fix/checkpoint roles, not
    ownership. A human or assistant may legitimately use ``PRD.1:`` or
    ``PLAN.1:`` while manually revising an artifact, so governance auditing
    must key on the changed path itself rather than ``CommitKind.OPERATOR``.
    The result is warning-only: manual edits remain sanctioned and rewinds
    still proceed after the complete snapshot preserves the discarded tip.
    """
    shas = [c.sha for c in git_obs.run_branch_commits]
    if target in shas:
        discarded = git_obs.run_branch_commits[shas.index(target) + 1:]
    elif target == git_obs.run_branch_sha:
        discarded = ()
    else:
        discarded = git_obs.run_branch_commits
    out: list[tuple[GitCommitObservation, list[str]]] = []
    for commit in discarded:
        paths = [ch.path for ch in commit.changed_paths if ch.approved_artifact]
        if paths:
            out.append((commit, paths))
    return out


# --- the one recovery assessment (P4, plan §4.2 / R4) ---------------------------
#
# The composite run-state classification and the state → mutating-action table
# live HERE — not in operator.py — so the read-only status surface and the
# mutating resume path consume one decision core and can never drift (R4).
# operator.py renders these (adding its observe-only rows); resume chooses its
# default action from the same table; recover's finalization derives its
# branch↔manifest reconciliation notes from the same observation machinery.

# Composite run-state classes (PRD operator-aids §6.3). operator.py re-exports
# these as its STATE_* names; the values are the §6.1 `state` enum verbatim.
STATE_IN_PROGRESS = "in_progress"
STATE_ORPHANED = "orphaned"
STATE_INDETERMINATE = "indeterminate"
STATE_PARKED_GATE = "parked_gate"
STATE_PARKED_FOR_RESPONSE = "parked_for_response"
STATE_PARKED_USAGE_LIMIT = "parked_usage_limit"
STATE_PARKED_USAGE_WINDOW = "parked_usage_window"
STATE_PARKED_ARTIFACT_INVALID = "parked_artifact_invalid"
# P5 (plan §5.2): a transport/dependency park — provider outage / timeout /
# connection failure after the bounded persisted retries. Plain resume retries;
# never `--response` (R7). Appended additively (schema_version stays 1).
STATE_PARKED_PROVIDER_UNAVAILABLE = "parked_provider_unavailable"
STATE_FAILED = "failed"
STATE_HALTED = "halted"
STATE_INTERRUPTED = "interrupted"
STATE_DONE = "done"
STATE_ABORTED = "aborted"
STATE_UNKNOWN = "unknown"

# Step statuses that mean "a terminal failure of this step" (§6.3a).
_FAILURE_STATUSES = (M.FAILED, M.HALTED, M.INTERRUPTED)

_LIVENESS_ALIVE = DriverLiveness.ALIVE.value
_LIVENESS_ORPHANED = DriverLiveness.ORPHANED.value
_LIVENESS_NONE = DriverLiveness.NONE.value


def classify_composite(
    man: "M.Manifest", liveness: str
) -> "tuple[str, M.StepRecord | None, M.StepRecord | None]":
    """The total ``(run_status, liveness, descriptor) -> state`` function (§6.3).

    Returns ``(state, parked_record, failure_record)`` — raw step records, so
    this core stays free of operator.py's rendering machinery (operator wraps
    them into its descriptors). Any unrecognized ``run_status`` or an
    internally contradictory manifest maps to ``unknown`` → read-only
    inspection only.

    P4 (plan §5.3, historical-shape recognition): ``RUN_RUNNING`` plus exactly
    one ENDED interrupted/halted/failed step is the kill-window shape a
    pre-P4 engine could persist (step terminal state flushed, run status not
    yet) — and remains reachable for one write even post-P4 (the resume
    write-ahead precedes a terminal re-persist). It maps to the corresponding
    RECOVERABLE state by liveness instead of ``unknown``:
    alive → ``in_progress`` (the driver is mid-transition); orphaned/none →
    the step's own failure class; indeterminate → ``indeterminate``.
    """
    status = man.status
    parked_steps = [s for s in man.steps if s.status == M.PARKED]
    failure_steps = [s for s in man.steps if s.status in _FAILURE_STATUSES]

    # `running` is untrustworthy from the manifest, so liveness governs.
    if status == M.RUN_RUNNING:
        if parked_steps or failure_steps:
            # Plan §5.3: the recognized historical kill-window shape.
            if (
                len(failure_steps) == 1
                and not parked_steps
                and failure_steps[0].ended
            ):
                fs = failure_steps[0]
                if liveness == _LIVENESS_ALIVE:
                    return STATE_IN_PROGRESS, None, None
                if liveness in (_LIVENESS_ORPHANED, _LIVENESS_NONE):
                    return fs.status, None, fs
                return STATE_INDETERMINATE, None, None
            return STATE_UNKNOWN, None, None  # descriptor under a `—` status
        if liveness == _LIVENESS_ALIVE:
            return STATE_IN_PROGRESS, None, None
        if liveness in (_LIVENESS_ORPHANED, _LIVENESS_NONE):
            return STATE_ORPHANED, None, None
        return STATE_INDETERMINATE, None, None  # indeterminate → read-only

    # done/aborted are engine-written and authoritative; a parked/failure
    # descriptor under them is contradictory.
    if status in (M.RUN_DONE, M.RUN_ABORTED):
        if parked_steps or failure_steps:
            return STATE_UNKNOWN, None, None
        return (STATE_DONE if status == M.RUN_DONE else STATE_ABORTED), None, None

    # parked — a genuine human/response park OR a budget/timeout halt / a
    # mid-step interruption (the engine parks the RUN while the STEP keeps its
    # HALTED/INTERRUPTED status — orchestrator._set_run_status, FR-3.3).
    if status == M.RUN_PARKED:
        halt_steps = [s for s in man.steps if s.status in (M.HALTED, M.INTERRUPTED)]
        if len(halt_steps) == 1 and not parked_steps:
            return halt_steps[0].status, None, halt_steps[0]
        if len(parked_steps) != 1 or halt_steps:
            return STATE_UNKNOWN, None, None  # zero/multiple/mixed → contradiction
        ps = parked_steps[0]
        reason = M.normalize_parked_reason(ps.parked_reason, ps.type, ps.status)
        if reason == M.PARKED_REASON_RESPONSE:
            return STATE_PARKED_FOR_RESPONSE, ps, None
        if reason == M.PARKED_REASON_USAGE_LIMIT:
            return STATE_PARKED_USAGE_LIMIT, ps, None
        if reason == M.PARKED_REASON_ARTIFACT_INVALID:
            return STATE_PARKED_ARTIFACT_INVALID, ps, None
        if reason == M.PARKED_REASON_USAGE_WINDOW:
            return STATE_PARKED_USAGE_WINDOW, ps, None
        if reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE:
            return STATE_PARKED_PROVIDER_UNAVAILABLE, ps, None
        if reason == M.PARKED_REASON_GATE and ps.type == "human_gate":
            return STATE_PARKED_GATE, ps, None
        return STATE_UNKNOWN, None, None

    # failed — the last failure step in manifest order is authoritative (§6.3a).
    if status == M.RUN_FAILED:
        if not failure_steps:
            return STATE_UNKNOWN, None, None
        fs = failure_steps[-1]
        return fs.status, None, fs

    return STATE_UNKNOWN, None, None  # any unrecognized run_status


def require_classifiable(man: "M.Manifest", *, verb: str) -> None:
    """Refuse a state that classifies `unknown` for EVERY liveness (#100).

    The invariant check at every recovering state-writing boundary (rollback,
    reject, recover): a composite `unknown` forbids all mutating verbs and has
    no native repair path, so persisting one converts a recoverable run into a
    permanent wedge. Call this immediately before the boundary's durable write
    — a raise fails the VERB with the manifest on disk untouched, leaving the
    run exactly as drivable as it was before the verb started.

    Only shapes that are `unknown` regardless of liveness are refused; the
    ``running`` rows are liveness-sensitive by design and a live driver must
    never be failed by its own write-ahead.
    """
    verdicts = {
        lv: classify_composite(man, lv)[0]
        for lv in (_LIVENESS_ALIVE, _LIVENESS_NONE)
    }
    if all(v == STATE_UNKNOWN for v in verdicts.values()):
        parked = [s.id for s in man.steps if s.status == M.PARKED]
        failures = [
            f"{s.id}={s.status}"
            for s in man.steps
            if s.status in (*_FAILURE_STATUSES,)
        ]
        raise StateInvariantError(
            f"{verb}: refusing to persist a run state the composite "
            f"classifier cannot recognize (would be `unknown`, forbidding "
            f"every mutating verb): run_status={man.status!r}, "
            f"current_step={man.current_step!r}, parked_steps={parked!r}, "
            f"failure_steps={failures!r}. The manifest on disk is unchanged; "
            f"the run remains in its pre-{verb} state. This is an engine "
            f"invariant violation — report it with the run's journal."
        )


# The state → mutating-action-kind table (§6.3 next-action column, mutating
# rows only). Consumed by BOTH operator._actions_for (rendered as CLI rows,
# after its observe-only prefix) and RecoveryPlanner.assess (as payload-complete
# P1 actions) — one table, zero drift (R4). `failed` is resolved by
# `mutating_action_kinds` because it depends on the failure kind.
_MUTATING_KINDS: dict[str, tuple[RecoveryActionKind, ...]] = {
    STATE_IN_PROGRESS: (),
    STATE_ORPHANED: (RecoveryActionKind.RETRY,),
    STATE_INDETERMINATE: (),
    STATE_PARKED_GATE: (RecoveryActionKind.HUMAN_DECISION,),
    STATE_PARKED_FOR_RESPONSE: (RecoveryActionKind.HUMAN_DECISION,),
    STATE_PARKED_USAGE_LIMIT: (RecoveryActionKind.RETRY,),
    STATE_PARKED_USAGE_WINDOW: (RecoveryActionKind.RETRY,),
    STATE_PARKED_ARTIFACT_INVALID: (RecoveryActionKind.RETRY,),
    STATE_PARKED_PROVIDER_UNAVAILABLE: (RecoveryActionKind.RETRY,),
    STATE_HALTED: (RecoveryActionKind.RETRY,),
    STATE_INTERRUPTED: (
        RecoveryActionKind.RETRY,
        RecoveryActionKind.SNAPSHOT_AND_RESTART,
    ),
    STATE_DONE: (),
    STATE_ABORTED: (),
    STATE_UNKNOWN: (),
}


def mutating_action_kinds(
    state: str, *, failure_kind: str | None = None, step_type: str | None = None
) -> tuple[RecoveryActionKind, ...]:
    """The mutating action kinds for a composite state (the shared table).

    ``step_type`` is the failed step's type (post-review F-007): a terminal
    failure of a step that is not ``--response``-respondable (a shell step, a
    terminally rejected human_gate) must never advertise ``resume --response``
    — the resume validator rejects it — so its executable safe action is an
    abort that retains all evidence (R1). ``None`` (type unknown to a pure
    caller) keeps the human-decision rendering.
    """
    if state == STATE_FAILED:
        if failure_kind in M.RERUNNABLE_FAILURE_KINDS:
            return (RecoveryActionKind.RETRY,)
        if step_type is not None and step_type not in M.RESPONDABLE_STEP_TYPES:
            return (RecoveryActionKind.ABORT,)
        return (RecoveryActionKind.HUMAN_DECISION,)
    return _MUTATING_KINDS.get(state, ())


# States whose composite class means "the driver is finished/absent and the
# state is nonterminal" — the R1 rows every assessment must arm with at least
# one safe mutating action.
_NONTERMINAL_DEAD_DRIVER_STATES = frozenset(
    {
        STATE_ORPHANED,
        STATE_PARKED_GATE,
        STATE_PARKED_FOR_RESPONSE,
        STATE_PARKED_USAGE_LIMIT,
        STATE_PARKED_USAGE_WINDOW,
        STATE_PARKED_ARTIFACT_INVALID,
        STATE_PARKED_PROVIDER_UNAVAILABLE,
        STATE_FAILED,
        STATE_HALTED,
        STATE_INTERRUPTED,
    }
)

_STATE_CAUSE: dict[str, RecoveryCause] = {
    STATE_IN_PROGRESS: RecoveryCause.NONE,
    STATE_ORPHANED: RecoveryCause.PROCESS_LOST,
    STATE_INDETERMINATE: RecoveryCause.NONE,
    STATE_PARKED_GATE: RecoveryCause.NONE,
    STATE_PARKED_FOR_RESPONSE: RecoveryCause.NONE,
    STATE_PARKED_USAGE_LIMIT: RecoveryCause.QUOTA_EXHAUSTED,
    STATE_PARKED_USAGE_WINDOW: RecoveryCause.QUOTA_EXHAUSTED,
    STATE_PARKED_ARTIFACT_INVALID: RecoveryCause.ARTIFACT_INVALID,
    STATE_PARKED_PROVIDER_UNAVAILABLE: RecoveryCause.PROVIDER_UNAVAILABLE,
    STATE_FAILED: RecoveryCause.INTERNAL_ERROR,
    STATE_HALTED: RecoveryCause.INTERNAL_ERROR,
    STATE_INTERRUPTED: RecoveryCause.PROCESS_LOST,
    STATE_DONE: RecoveryCause.NONE,
    STATE_ABORTED: RecoveryCause.NONE,
    STATE_UNKNOWN: RecoveryCause.STATE_INCONSISTENT,
}

_STATE_DISPOSITION: dict[str, RecoveryDisposition] = {
    STATE_IN_PROGRESS: RecoveryDisposition.CONTINUE,
    STATE_ORPHANED: RecoveryDisposition.RETRY,
    STATE_INDETERMINATE: RecoveryDisposition.CONTINUE,
    STATE_PARKED_GATE: RecoveryDisposition.HUMAN_DECISION,
    STATE_PARKED_FOR_RESPONSE: RecoveryDisposition.HUMAN_DECISION,
    STATE_PARKED_USAGE_LIMIT: RecoveryDisposition.RETRY,
    STATE_PARKED_USAGE_WINDOW: RecoveryDisposition.RETRY,
    STATE_PARKED_ARTIFACT_INVALID: RecoveryDisposition.EDIT_THEN_RETRY,
    STATE_PARKED_PROVIDER_UNAVAILABLE: RecoveryDisposition.RETRY,
    STATE_FAILED: RecoveryDisposition.HUMAN_DECISION,  # RETRY when re-runnable
    STATE_HALTED: RecoveryDisposition.RETRY,
    STATE_INTERRUPTED: RecoveryDisposition.RETRY,
    STATE_DONE: RecoveryDisposition.CONTINUE,
    STATE_ABORTED: RecoveryDisposition.CONTINUE,
    STATE_UNKNOWN: RecoveryDisposition.CONTINUE,  # read-only inspection only
}

# --- P5: the orthogonal outcome taxonomy (plan §4.1 / §6 P5) -----------------
# The (status, parked_reason, halt_reason, failure_kind) → (cause, disposition)
# map ``_finalize`` stamps onto every terminal/parked StepRecord it writes.
# This is the EVIDENCE the coarse ``_STATE_CAUSE`` state map is refined by in
# :meth:`RecoveryPlanner.assess` — recorded at outcome time, when the engine
# knows exactly what happened, instead of re-derived from the composite state
# alone. Pure and total: any unrecognized shape maps to (None, None) — nothing
# is stamped, and classification falls back to the coarse map (fail closed,
# never a guessed refinement).

_PARK_OUTCOME: dict[str, tuple[RecoveryCause, RecoveryDisposition]] = {
    M.PARKED_REASON_USAGE_LIMIT: (
        RecoveryCause.QUOTA_EXHAUSTED, RecoveryDisposition.RETRY),
    M.PARKED_REASON_USAGE_WINDOW: (
        RecoveryCause.QUOTA_EXHAUSTED, RecoveryDisposition.RETRY),
    M.PARKED_REASON_PROVIDER_UNAVAILABLE: (
        RecoveryCause.PROVIDER_UNAVAILABLE, RecoveryDisposition.RETRY),
    M.PARKED_REASON_ARTIFACT_INVALID: (
        RecoveryCause.ARTIFACT_INVALID, RecoveryDisposition.EDIT_THEN_RETRY),
    M.PARKED_REASON_RESPONSE: (
        RecoveryCause.NONE, RecoveryDisposition.HUMAN_DECISION),
    M.PARKED_REASON_GATE: (
        RecoveryCause.NONE, RecoveryDisposition.HUMAN_DECISION),
}

_HALT_OUTCOME: dict[str, tuple[RecoveryCause, RecoveryDisposition]] = {
    # The adapter/step deadline tripped: the dependency did not answer in time
    # (plan §5.2's timeout class); a plain resume re-runs the step.
    M.HALT_REASON_TIMEOUT: (
        RecoveryCause.PROVIDER_UNAVAILABLE, RecoveryDisposition.RETRY),
    # Engine policy guards (budget cap, judge deny): the run's own policy, not
    # an infrastructure or artifact fault.
    M.HALT_REASON_BUDGET: (
        RecoveryCause.POLICY_DENIED, RecoveryDisposition.RETRY),
    M.HALT_REASON_JUDGE_DENY: (
        RecoveryCause.POLICY_DENIED, RecoveryDisposition.RETRY),
    M.HALT_REASON_SIGNAL_KILL: (
        RecoveryCause.PROCESS_LOST, RecoveryDisposition.RETRY),
    M.HALT_REASON_ADAPTER_ERROR: (
        RecoveryCause.INTERNAL_ERROR, RecoveryDisposition.RETRY),
    M.HALT_REASON_PRECONDITION: (
        RecoveryCause.PRECONDITION_UNSATISFIED, RecoveryDisposition.RETRY),
    M.HALT_REASON_OPERATOR_RECOVER: (
        RecoveryCause.PROCESS_LOST, RecoveryDisposition.RETRY),
}


def outcome_classification(
    status: str,
    *,
    parked_reason: str | None = None,
    halt_reason: str | None = None,
    failure_kind: str | None = None,
) -> tuple[str | None, str | None]:
    """The recorded (cause, disposition) for one finalized step outcome (P5).

    Returns enum VALUES (plain strings, the manifest's storage form) or
    ``(None, None)`` for a shape with nothing to record (DONE/SKIPPED, or an
    unrecognized reason — fail closed to the coarse map, never a guess).
    """
    if status == M.PARKED:
        pair = _PARK_OUTCOME.get(parked_reason or "")
        return (pair[0].value, pair[1].value) if pair else (None, None)
    if status == M.FAILED:
        if failure_kind == M.FAILURE_KIND_CLEAN_HANDOFF:
            return (
                RecoveryCause.PRECONDITION_UNSATISFIED.value,
                RecoveryDisposition.RETRY.value,
            )
        if failure_kind == M.FAILURE_KIND_SIDE_EFFECT_FREE:
            # An unknown adapter failure whose attempt provably left no
            # Git/worktree side effects (plan §5.2): retry is safe.
            return (
                RecoveryCause.INTERNAL_ERROR.value,
                RecoveryDisposition.RETRY.value,
            )
        if halt_reason == M.HALT_REASON_JUDGE_DENY:
            return (
                RecoveryCause.POLICY_DENIED.value,
                RecoveryDisposition.HUMAN_DECISION.value,
            )
        if halt_reason == M.HALT_REASON_PRECONDITION:
            return (
                RecoveryCause.PRECONDITION_UNSATISFIED.value,
                RecoveryDisposition.HUMAN_DECISION.value,
            )
        return (
            RecoveryCause.INTERNAL_ERROR.value,
            RecoveryDisposition.HUMAN_DECISION.value,
        )
    if status == M.HALTED:
        pair = _HALT_OUTCOME.get(halt_reason or "")
        return (pair[0].value, pair[1].value) if pair else (None, None)
    if status == M.INTERRUPTED:
        return (RecoveryCause.PROCESS_LOST.value, RecoveryDisposition.RETRY.value)
    return (None, None)


# Ahead relations resume reconciles by adoption (plan §5.4 / R6); behind /
# forked / missing instead get an explicit recovery-ref workflow.
_ADOPTABLE_AHEAD = frozenset(
    {
        BranchRelation.CHECKPOINT_AHEAD,
        BranchRelation.IMPLEMENTATION_AHEAD,
        BranchRelation.OPERATOR_AHEAD,
        BranchRelation.MIXED_AHEAD,
        BranchRelation.UNCLASSIFIED_AHEAD,
    }
)
_DIVERGED_RELATIONS = frozenset(
    {BranchRelation.BEHIND, BranchRelation.FORKED, BranchRelation.MISSING}
)
# Public names for the verb layer (resume/recover consume these classes).
ADOPTABLE_AHEAD_RELATIONS = _ADOPTABLE_AHEAD
DIVERGED_RELATIONS = _DIVERGED_RELATIONS


def _attempt_record(man: "M.Manifest") -> "M.StepRecord | None":
    """The step record whose ``base_sha`` anchors the in-flight attempt.

    The last record with a stamped attempt boundary that is still in a
    non-terminal-for-the-run state (running / interrupted): that is the record
    whose boundary a plain resume's disposition will diff against, so it is
    the one an adoption re-anchors.
    """
    target = None
    for rec in man.steps:
        if rec.base_sha and rec.status in (M.RUNNING, M.INTERRUPTED):
            target = rec
    return target


def reconciliation_boundary(man: "M.Manifest") -> str | None:
    """The recorded Git boundary a resume reconciles the run branch against.

    The in-flight attempt's ``base_sha`` when a step is mid-attempt (that is
    what the resume disposition diffs against), else the manifest's last
    recorded commit (the rollback/branch-guard boundary), else ``None``
    (nothing recorded — an unrecorded relation, never reconciled).
    """
    rec = _attempt_record(man)
    if rec is not None and rec.base_sha:
        return rec.base_sha
    if man.commits:
        return man.commits[-1].sha
    return None


def relation_recovery_actions(
    git_obs: GitObservation, man: "M.Manifest"
) -> tuple[RecoveryAction, ...]:
    """Executable recovery actions for a behind/forked/missing run branch.

    Plan §5.4: these relations must offer a recovery-ref/restore/continue
    workflow, "not only reconcile manually". Every action is non-destructive:
    it creates or fast-forwards a ref onto history that provably exists, or
    aborts retaining all evidence.

    "Non-destructive" is a claim about the ref *as observed here*, and this
    function runs at assessment time while the command it produces runs
    whenever the operator gets to it (post-review F-003). Each action therefore
    carries ``expected_sha`` — the observed value, or ``None`` for a ref that
    did not exist — so the rendered command is a compare-and-swap and a ref
    that moved in that gap refuses the stale update instead of applying it.
    """
    relation = git_obs.branch_relation
    actions: list[RecoveryAction] = []
    recorded = git_obs.recorded_sha
    on_run_branch = git_obs.checked_out_branch == git_obs.run_branch
    if relation is BranchRelation.MISSING and recorded is not None:
        actions.append(
            ContinueOnRecoveryBranchAction(
                description=(
                    f"recreate the missing run branch {git_obs.run_branch!r} at "
                    f"the last recorded commit {recorded[:10]} (the commit "
                    "object still exists; creating the ref discards nothing)"
                ),
                branch_name=git_obs.run_branch,
                start_sha=recorded,
                via="branch_force",
                # MISSING: the ref must still be absent when the command runs.
                # A ref recreated in the gap (a resume, another operator) is
                # NOT this action's subject, and overwriting it would discard
                # whatever it points at.
                expected_sha=None,
            )
        )
    elif relation is BranchRelation.BEHIND and recorded is not None:
        # F-003: `git branch -f` is invalid for the CHECKED-OUT branch, so the
        # advertised command is checkout-aware — a pure fast-forward merge when
        # the operator is standing on the run branch (git refuses a non-ff, so
        # it can never discard anything), a forced ref move otherwise.
        actions.append(
            ContinueOnRecoveryBranchAction(
                description=(
                    f"fast-forward {git_obs.run_branch!r} back to the recorded "
                    f"tip {recorded[:10]} (a pure fast-forward — every recorded "
                    "commit is preserved in git and re-anchored)"
                ),
                branch_name=git_obs.run_branch,
                start_sha=recorded,
                via="ff_merge" if on_run_branch else "branch_force",
                # BEHIND: the branch is at `run_branch_sha` and `recorded` is
                # ahead of it. If a driver advances the branch in the gap, the
                # forced move back to `recorded` would REWIND the new tip and
                # orphan those commits; the guard makes git refuse it instead.
                # (The `ff_merge` form is already refused by git in that case,
                # but it carries the value too so both forms audit alike.)
                expected_sha=git_obs.run_branch_sha,
            )
        )
    elif relation is BranchRelation.FORKED and git_obs.run_branch_sha is not None:
        tip = git_obs.run_branch_sha
        # F-003: the payload PRESERVES the forked tip only — the description
        # promises exactly that. Restoring the run branch onto the recorded
        # boundary afterwards is a rewind (it discards the fork from the run
        # branch), which stays behind the snapshot-backed verbs
        # (`gauntlet rollback`, `resume --reset-interrupted`) — never a bare
        # advertised git command.
        actions.append(
            ContinueOnRecoveryBranchAction(
                description=(
                    f"preserve the forked tip {tip[:10]} on recovery branch "
                    f"{git_obs.run_branch}-fork-{tip[:10]} (creates a ref only; "
                    "discards nothing). The run branch itself stays forked — "
                    "reconcile it afterwards through a snapshot-backed verb "
                    "(`gauntlet rollback`), never a bare reset"
                ),
                branch_name=f"{git_obs.run_branch}-fork-{tip[:10]}",
                start_sha=tip,
                via="branch_force",
                # The fork-preservation ref is a NEW name derived from the tip
                # it preserves. It must not already exist: one that does was
                # created by something else, and this action has no claim on it.
                expected_sha=None,
            )
        )
    actions.append(
        AbortAction(
            description="abort the run, retaining every snapshot and all evidence",
            reason=f"run branch relation {relation.value} left unreconciled",
        )
    )
    return tuple(actions)


def _latest_checkpoint(
    git_obs: GitObservation,
) -> GitCommitObservation | None:
    """The newest checkpoint commit in the inventoried ahead range."""
    found = None
    for commit in git_obs.run_branch_commits:
        if commit.kind is CommitKind.CHECKPOINT:
            found = commit
    return found


def _governed_range_edits(
    git_obs: GitObservation,
) -> list[tuple[GitCommitObservation, list[str]]]:
    """Commits in the inventoried range that modify governed artifacts."""
    out: list[tuple[GitCommitObservation, list[str]]] = []
    for commit in git_obs.run_branch_commits:
        paths = [c.path for c in commit.changed_paths if c.approved_artifact]
        if paths:
            out.append((commit, paths))
    return out


def _phase_ordinal(label: str | None) -> int | None:
    """``P<N>`` → ``N``; every other stage label (PRD/PLAN/REVIEW/None) → None."""
    if label and re.fullmatch(r"P\d+", label):
        return int(label[1:])
    return None


def reconcile_branch_ahead(
    man: "M.Manifest",
    git_obs: GitObservation,
    *,
    verb: str = "resume",
) -> list[str]:
    """Adopt a provably-linear ahead range into the manifest (plan §5.4 / R6).

    Manifest-only — never a Git mutation, so no executor transaction is
    required (every Git-mutating rewind stays behind :class:`RecoveryExecutor`).
    Returns the audit notes appended to ``man.warnings`` (every adoption is
    loud); the caller owns the atomic persist. The reconciliation classes:

    * ``engine_bookkeeping_ahead`` — tolerated, unchanged (not adopted; the
      dirty checks already treat pure bookkeeping advance as clean);
    * ``checkpoint_ahead`` — continue from the newest ``P<N> wip:`` checkpoint:
      it becomes the in-flight attempt's boundary instead of re-parking;
    * ``implementation_ahead`` — recognized phase/fix commits are adopted into
      ``manifest.commits`` (the builder committed, the flush never landed) and
      the attempt boundary moves to the tip; a range whose phase ordinal
      precedes the last recorded phase is NOT treated as fresh implementation
      (that shape is manual history surgery) — it falls through to the
      operator-adoption path, loudly;
    * ``operator_ahead`` / ``mixed_ahead`` / ``unclassified_ahead`` — adopted
      as the next attempt's base. A commit modifying a governed artifact
      (prd.md/plan.md) is surfaced LOUDLY as the sanctioned upstream path —
      the artifact's own review loop and human gate ratify it (R9/FR-10.4) —
      and is NEVER refused or silently discarded (operator direction on the
      post-P3 F-004 review: hand-editing and committing governed artifacts is
      a normal workflow).
    """
    relation = git_obs.branch_relation
    if relation not in _ADOPTABLE_AHEAD or not git_obs.run_branch_commits:
        return []
    if man.status in (M.RUN_DONE, M.RUN_ABORTED):
        return []  # a finished run has no next attempt to reconcile toward
    rec = _attempt_record(man)
    if rec is not None and rec.type != "agent_task":
        # Step types that OWN their recovery reconcile the range themselves:
        # a killed `commit` step adopts its already-landed `P<N>:` commit from
        # the git log on re-entry, and an `adversarial_cycle` re-enters
        # through its own checkpoint-aware recovery. Re-anchoring their
        # attempt boundary here would erase exactly the evidence those
        # mechanisms key on (head-moved-off-base), so the verb-level adoption
        # defers — fail toward the narrower, step-owned reconciliation.
        return []
    tip = git_obs.run_branch_sha
    boundary = git_obs.recorded_sha
    assert tip is not None and boundary is not None  # proven by the relation
    notes: list[str] = []
    known_shas = {c.sha for c in man.commits}

    def note(text: str) -> None:
        if text not in man.warnings:
            man.warnings.append(text)
        notes.append(text)

    range_label = f"{boundary[:10]}..{tip[:10]}"
    governed = _governed_range_edits(git_obs)

    adopt_as_implementation = relation is BranchRelation.IMPLEMENTATION_AHEAD
    if adopt_as_implementation:
        last_phase = None
        for commit in man.commits:
            ordinal = _phase_ordinal(commit.phase.split(".")[0])
            if ordinal is not None:
                last_phase = ordinal if last_phase is None else max(last_phase, ordinal)
        for commit in git_obs.run_branch_commits:
            ordinal = _phase_ordinal(commit.phase_id)
            if (
                commit.kind in (CommitKind.PHASE, CommitKind.FIX)
                and ordinal is not None
                and last_phase is not None
                and ordinal < last_phase
            ):
                adopt_as_implementation = False
                note(
                    f"{verb}: commit {commit.sha[:10]} ({commit.subject!r}) in "
                    f"{range_label} carries phase P{ordinal}, which precedes "
                    f"the last recorded phase P{last_phase} — not fresh "
                    "implementation work; the range is adopted as operator "
                    "work instead (plan §5.4)"
                )

    if relation is BranchRelation.CHECKPOINT_AHEAD:
        checkpoint = _latest_checkpoint(git_obs)
        assert checkpoint is not None  # proven by the relation label
        if rec is not None:
            rec.base_sha = checkpoint.sha
        note(
            f"{verb}: adopted checkpoint {checkpoint.sha[:10]} "
            f"({checkpoint.subject!r}) as the attempt boundary for "
            f"{rec.id if rec is not None else 'the next attempt'} — the run "
            f"branch was ahead of the manifest by {range_label} "
            "(committed checkpoint work continues instead of re-parking; "
            "plan §5.4/R6)"
        )
        return notes

    if adopt_as_implementation:
        adopted: list[str] = []
        for commit in git_obs.run_branch_commits:
            if commit.kind not in (CommitKind.PHASE, CommitKind.FIX):
                continue
            if commit.sha in known_shas:
                continue
            phase_label = commit.subject.split(":", 1)[0].strip()
            man.commits.append(
                M.CommitRecord(
                    step_id=(rec.id if rec is not None else verb),
                    phase=phase_label,
                    sha=commit.sha,
                )
            )
            adopted.append(f"{commit.sha[:10]} ({commit.subject!r})")
        if rec is not None:
            rec.base_sha = tip
        note(
            f"{verb}: adopted {len(adopted)} implementation commit(s) in "
            f"{range_label} into the manifest — the builder committed but the "
            "manifest flush never landed (issue #72, plan §5.4/R6): "
            + "; ".join(adopted)
        )
        for commit, paths in governed:
            note(
                f"{verb}: adopted commit {commit.sha[:10]} modifies governed "
                f"artifact(s) {', '.join(paths)} — surfaced through the "
                "artifact's own review loop and human gate (R9/FR-10.4), "
                "never refused or silently discarded"
            )
        return notes

    # operator / mixed / unclassified (or demoted implementation) adoption.
    for commit, paths in governed:
        note(
            f"{verb}: operator commit {commit.sha[:10]} ({commit.subject!r}) "
            f"modifies governed artifact(s) {', '.join(paths)}; the edit is "
            "SANCTIONED and preserved — it reaches ratification through the "
            "artifact's own gate/response loop (R9/FR-10.4), never refused, "
            "never silently discarded"
        )
    if rec is not None:
        rec.base_sha = tip
    note(
        f"{verb}: adopted operator work {range_label} as the next attempt's "
        f"base (relation {relation.value}); nothing was rewound or discarded "
        "(plan §5.4/R6)"
    )
    return notes


# --- the transaction ------------------------------------------------------------


def _validate_rel_path(value: str, *, field: str) -> None:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or value == "."
        or path.parts[0] == ".git"
    ):
        raise RecoveryPreconditionError(
            f"{field} must be a contained repository-relative path: {value!r}"
        )


class RewindSpec(BaseModel):
    """Serializable description of one site's apply mechanics.

    The executor owns WHEN each operation runs (the transaction ordering);
    the spec preserves WHAT each site's rewind means — its target selection,
    bookkeeping-preserving mechanics, and clean scope — so a replay by a
    fresh process reconstructs the exact operation from the intent alone.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    site: str = Field(min_length=1)
    checkout_branch: str | None = None
    target_sha: str = Field(min_length=40)
    reset_mode: str = RESET_PLAIN
    bookkeeping_paths: tuple[str, ...] = ()
    rewind_message: str | None = None
    clean: bool = False
    clean_excludes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _contract(self) -> "RewindSpec":
        if self.reset_mode not in (RESET_PLAIN, RESET_BOOKKEEPING_PRESERVING):
            raise ValueError(f"unknown reset_mode {self.reset_mode!r}")
        if self.reset_mode == RESET_BOOKKEEPING_PRESERVING:
            if not self.bookkeeping_paths or not self.rewind_message:
                raise ValueError(
                    "bookkeeping-preserving rewinds require bookkeeping_paths "
                    "and a rewind_message"
                )
        for rel in self.bookkeeping_paths:
            _validate_rel_path(rel, field="bookkeeping path")
        return self


class RecoveryIntent(BaseModel):
    """The durable transaction intent (transaction step 5).

    Written atomically before any destructive verb and cleared only after the
    resulting state is durable. Its preconditions freeze what the executor
    validated so a replaying process can prove the repository is still in the
    pre-apply state (apply from scratch), a recognized mid-apply state
    (finish), or neither (fail closed).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = INTENT_SCHEMA_VERSION
    intent_id: str = Field(min_length=1)  # the idempotency key
    run_id: str = Field(min_length=1)
    site: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    action_kind: str = Field(min_length=1)
    snapshot_ref: str = Field(min_length=1)
    pre_head: str = Field(min_length=40)
    pre_run_branch_sha: str | None = None
    pre_fingerprint_digest: str = Field(min_length=1)
    # Independently comparable pre-state witnesses for each durable Git plane
    # (post-P3 review F-001): a replaying process cannot reconstruct the full
    # run-state fingerprint, but it CAN re-derive these — so a same-HEAD
    # repository whose index or worktree gained new work after the kill is
    # provably NOT the assessed pre-state and the replay fails closed instead
    # of resetting the new work away.
    pre_index_fingerprint: str = Field(min_length=1)
    pre_worktree_fingerprint: str = Field(min_length=1)
    # The exclusion set the fingerprints were computed under, so the replay
    # observes the identical worktree plane.
    excludes: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    spec: RewindSpec
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True)


@dataclass
class RecoveryResult:
    """What one applied transaction produced (returned to the caller)."""

    snapshot: git_snapshot.GitRecoverySnapshot
    intent_id: str
    applied: bool
    notes: str


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def intent_path(run_dir: Path) -> Path:
    return run_dir / EXECUTOR_INTENT_NAME


def load_intent(run_dir: Path) -> RecoveryIntent | None:
    """The surviving intent, or ``None``. Unreadable/malformed intents fail
    closed: only a provably ABSENT intent returns ``None`` — a permission or
    I/O failure is indistinguishable from a surviving transaction, so it must
    block further mutation, never be silently treated as "no intent"
    (post-P3 review F-005)."""
    path = intent_path(run_dir)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RecoveryIntentError(
            f"recovery intent {path} exists but could not be read ({exc}); "
            "refusing every further mutation until it is inspected"
        ) from exc
    try:
        return RecoveryIntent(**json.loads(text))
    except (ValueError, TypeError) as exc:
        raise RecoveryIntentError(
            f"surviving recovery intent {path} is malformed ({exc}); refusing "
            "every further mutation until it is inspected and removed"
        ) from exc


def _write_intent(run_dir: Path, intent: RecoveryIntent) -> None:
    path = intent_path(run_dir)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(intent.to_json())
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _clear_intent(run_dir: Path, intent_id: str) -> None:
    """Remove the intent iff it still carries ``intent_id`` (never a newer one)."""
    current = load_intent(run_dir)
    if current is not None and current.intent_id == intent_id:
        try:
            os.unlink(intent_path(run_dir))
        except FileNotFoundError:
            pass
        _fsync_dir(run_dir)


class WorktreeLockGuard:
    """Transaction step 1: hold, or prove we already hold, the drive lock.

    Every production caller runs inside a verb that already holds the
    ``.driving.lock`` (resume/rollback acquire it in RunManager before any
    rewind can occur), so the common case is verification: the lock file
    exists and names THIS process. A foreign live holder fails closed. When
    no lock exists (direct engine embedding, tests), an ephemeral lock is
    taken for the transaction and released after — atomically created, never
    reclaimed from a live pid.

    P7b: the drive lock moved to the run-instance dir, so the guard follows it
    there (``lock_path``) — that is the file a P7b verb holds for this run. The
    worktree-global path is still consulted as a **second** gate
    (``tree_lock_path``): a live holder there is either another slug driving
    this shared tree or a pre-P7b engine's driver, and either must block a
    rewind of this tree. Both checks must pass, so this is strictly stronger
    than the single check it replaces — never weaker.

    Reclaim policy (identity verification) still belongs to RunManager: a
    stale foreign lock at either path is a fail-closed refusal here, never a
    steal.
    """

    def __init__(
        self, repo_root: Path, run_root: str, *, run_dir: Path | None = None
    ) -> None:
        self.tree_lock_path = repo_root / run_root / DRIVING_LOCK_NAME
        # No run dir (a caller embedding the executor directly) → the pre-P7b
        # behaviour: the worktree-global path IS the lock.
        self.lock_path = (
            (run_dir / DRIVING_LOCK_NAME) if run_dir is not None else self.tree_lock_path
        )
        # #86: the per-slug minting lock. A live holder there is a `start`
        # minting a new run of THIS slug; checked read-only in hold() so the
        # guard stays strictly stronger than the pre-#86 tree-guard check.
        self.slug_lock_path = (
            (run_dir.parent / DRIVING_LOCK_NAME) if run_dir is not None else None
        )

    def _read(self, path: Path | None = None) -> dict[str, Any] | None:
        try:
            data = json.loads((path or self.lock_path).read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True  # cannot prove dead → fail closed (treat as live)
        return True

    def _check(self, path: Path) -> str:
        """``ours`` / ``absent`` / raise. One lockfile, the unchanged rule."""
        record = self._read(path)
        if record is None:
            return "absent"
        try:
            pid = int(record.get("pid"))
        except (TypeError, ValueError):
            pid = -1
        if pid == os.getpid():
            return "ours"
        if pid > 0 and self._pid_alive(pid):
            raise RecoveryLockError(
                f"worktree lock {path} is held by live pid "
                f"{pid}; refusing a concurrent recovery mutation (FR-10.5)"
            )
        # A stale/corrupt foreign lock: do NOT reclaim here — reclaim
        # policy (identity verification) belongs to RunManager. Fail
        # closed with the remedy.
        raise RecoveryLockError(
            f"worktree lock {path} exists but its holder is not "
            "verifiably this process; run the recovery through a locked "
            "verb (resume/rollback) or remove the stale lock first"
        )

    def _ephemeral(self, path: Path, nonce: str) -> None:
        """Atomically create a transaction-scoped lock at ``path`` (never reclaim)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "nonce": nonce,
                "slug": "recovery-executor",
                "run_id": None,
                "pid": os.getpid(),
                "pgid": os.getpid(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "host": socket.gethostname(),
                "proc_identity": None,
            },
            indent=2,
        )
        tmp = path.with_name(f"{path.name}.{nonce}.tmp")
        tmp.write_text(payload)
        try:
            try:
                os.link(tmp, path)
            except FileExistsError:
                raise RecoveryLockError(
                    f"worktree lock {path} appeared during "
                    "acquisition; refusing a concurrent recovery mutation"
                ) from None
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass

    def _drop_ephemeral(self, path: Path, nonce: str) -> None:
        current = self._read(path)
        if current is not None and current.get("nonce") == nonce:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    @contextmanager
    def hold(self) -> Iterator[None]:
        # The worktree-global guard first: a live foreign holder there owns this
        # tree, whichever run it belongs to, and no rewind may proceed under it.
        # Absent is fine — an embedded caller has no RunManager verb around it.
        split = self.tree_lock_path != self.lock_path
        tree_state = self._check(self.tree_lock_path) if split else "ours"
        if split and self.slug_lock_path is not None:
            self._check(self.slug_lock_path)  # live foreign mint → raises (#86)
        if self._check(self.lock_path) == "ours":
            yield  # already held by this process (the normal verb path)
            return
        # Ephemeral acquisition. When the two scopes are split, take BOTH — the
        # tree guard is what stops a concurrent RunManager verb from driving a
        # DIFFERENT run against this same tree while the rewind is in flight, so
        # holding only the per-run lock would be weaker than the pre-P7b guard.
        nonce = secrets.token_hex(16)
        held_tree = False
        if split and tree_state == "absent":
            self._ephemeral(self.tree_lock_path, nonce)
            held_tree = True
        try:
            self._ephemeral(self.lock_path, nonce)
        except BaseException:
            if held_tree:
                self._drop_ephemeral(self.tree_lock_path, nonce)
            raise
        try:
            yield
        finally:
            # Reverse of acquisition: the per-run lock first, then the tree
            # guard, so no window exists where the tree looks free while this
            # rewind's own lock is still held.
            self._drop_ephemeral(self.lock_path, nonce)
            if held_tree:
                self._drop_ephemeral(self.tree_lock_path, nonce)


@dataclass
class SnapshotRequest:
    """What the durable pre-mutation snapshot must capture for one site."""

    snapshot_id: str
    reason: str
    attempt_id: str | None = None
    run_branch: str | None = None
    exclude: list[str] | None = None
    protected: list[str] | None = None


class RecoveryExecutor:
    """The single rewind gateway (plan §4.3). All mutation ordering lives here."""

    def __init__(
        self,
        repo_root: Path,
        run_dir: Path,
        *,
        run_id: str,
        run_root: str,
        excludes: list[str] | None = None,
        clock: Callable[[], str] | None = None,
        lock_guard: WorktreeLockGuard | None = None,
        work_root: Path | None = None,
    ) -> None:
        self.repo_root = repo_root
        # The tree this executor rewinds (P7a). Every rewind here mutates a
        # WORKING tree — checkout, reset, clean, index restore — and P7
        # acceptance A1 is exactly "that tree is never the operator's". `None`
        # is the pre-P7 same-tree layout; P7c passes the run's worktree.
        self.work_root = work_root or repo_root
        self.run_dir = run_dir
        self.run_id = run_id
        self.excludes = excludes
        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        # P7b: `run_dir` IS the run-instance dir, which is where the drive lock
        # lives now — so the guard follows the lock without a new parameter.
        self.lock_guard = lock_guard or WorktreeLockGuard(
            repo_root, run_root, run_dir=run_dir
        )

    # -- transaction steps, in order ------------------------------------------

    def apply(
        self,
        assessment: RecoveryAssessment,
        action: RecoveryAction,
        *,
        spec: RewindSpec,
        snapshot_request: SnapshotRequest,
        fingerprint: Callable[[], ProgressFingerprint],
        persist: Callable[[RecoveryResult], None] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RecoveryResult:
        """Apply one planned rewind under the full transaction ordering.

        ``fingerprint`` re-observes under the lock (step 2) using the same
        inputs the assessment fingerprinted; a digest mismatch aborts with
        zero mutation. ``persist`` is the caller's step-7 state transition —
        it runs after apply and before the intent clears, so a crash between
        apply and persist leaves a replayable intent whose site finisher can
        re-persist the transition.
        """
        if action.kind is not RecoveryActionKind.SNAPSHOT_AND_RESTART:
            raise RecoveryPreconditionError(
                f"the P3 executor applies snapshot_and_restart actions only; "
                f"got {action.kind}"
            )
        if action not in assessment.safe_actions:
            raise RecoveryPreconditionError(
                "the action to apply must be one of the assessment's safe_actions"
            )
        if action.target_sha != spec.target_sha:
            raise RecoveryPreconditionError(
                f"action target {action.target_sha[:10]} and spec target "
                f"{spec.target_sha[:10]} disagree"
            )
        with self.lock_guard.hold():  # step 1
            # A surviving intent from a previous killed transaction must be
            # replayed (converged) before any NEW transaction may proceed.
            survivor = load_intent(self.run_dir)
            if survivor is not None:
                self._replay(survivor)
            observed = fingerprint()  # step 2: re-observe under the lock
            if observed.digest != assessment.progress_fingerprint:
                raise RecoveryPreconditionError(
                    "progress fingerprint changed between assessment and "
                    f"apply ({assessment.progress_fingerprint} -> "
                    f"{observed.digest}); the repository or run state moved — "
                    "re-assess before mutating (fail closed)"
                )
            self._validate(action, spec)  # step 3
            snapshot = self._create_snapshot(snapshot_request)  # step 4 (R2)
            # P6 journal audit (plan §4.6): recovery evidence, deduplicated by
            # idempotency key and best-effort by contract — the durable
            # authority for this transaction is the intent file + snapshot
            # ref, never these events (plan §9: optional evidence gathering
            # must not prevent finalization).
            J.append_audit(
                self.run_dir,
                "RecoverySnapshotCreated",
                {"snapshot_ref": snapshot.ref, "site": spec.site,
                 "reason": snapshot_request.reason},
                run_id=self.run_id,
                idempotency_key=f"snapshot:{snapshot.ref}",
            )
            intent_payload = dict(payload or {})
            governed_evidence = [
                item for item in assessment.evidence
                if item.startswith(GOVERNED_DISCARD_EVIDENCE_PREFIX)
            ]
            if governed_evidence:
                intent_payload[GOVERNED_EVIDENCE_PAYLOAD_KEY] = governed_evidence
            intent = RecoveryIntent(  # step 5
                intent_id=secrets.token_hex(16),
                run_id=self.run_id,
                site=spec.site,
                created_at=self.clock(),
                action_kind=action.kind.value,
                snapshot_ref=snapshot.ref,
                pre_head=snapshot.head_sha,
                pre_run_branch_sha=snapshot.run_branch_sha,
                pre_fingerprint_digest=observed.digest,
                pre_index_fingerprint=observed.index_fingerprint,
                pre_worktree_fingerprint=observed.worktree_fingerprint,
                excludes=tuple(self.excludes or ()),
                protected=tuple(snapshot_request.protected or ()),
                spec=spec,
                payload=intent_payload,
            )
            _write_intent(self.run_dir, intent)
            J.append_audit(
                self.run_dir,
                "RecoveryActionPlanned",
                {"intent_id": intent.intent_id, "site": spec.site,
                 "action_kind": intent.action_kind,
                 "target_sha": spec.target_sha,
                 "snapshot_ref": snapshot.ref},
                run_id=self.run_id,
                idempotency_key=f"planned:{intent.intent_id}",
            )
            self._apply_spec(spec, snapshot)  # step 6
            result = RecoveryResult(
                snapshot=snapshot,
                intent_id=intent.intent_id,
                applied=True,
                notes=(
                    f"recovery rewind applied ({spec.site}): worktree preserved "
                    f"as {snapshot.ref}, reset to {spec.target_sha[:10]}"
                ),
            )
            if persist is not None:  # step 7
                persist(result)
            J.append_audit(
                self.run_dir,
                "RecoveryActionApplied",
                {"intent_id": intent.intent_id, "site": spec.site,
                 "target_sha": spec.target_sha,
                 "snapshot_ref": snapshot.ref},
                run_id=self.run_id,
                idempotency_key=f"applied:{intent.intent_id}",
            )
            _clear_intent(self.run_dir, intent.intent_id)  # step 8
            return result

    def apply_rebuild(
        self,
        assessment: RecoveryAssessment,
        action: RebuildProjectionAction,
    ) -> str:
        """Rebuild the manifest projection from the journal head (plan §5.5).

        The same transaction ordering as :meth:`apply`, degenerated to the
        file plane this action mutates: (1) hold the lock; (2) converge any
        surviving Git-transaction intent first; (3) re-verify the evidence
        fingerprint under the lock — the projection bytes must still be
        exactly what the assessment observed; (4) preserve the malformed
        original as durable recovery evidence (the R2 preservation for this
        action — no Git plane is touched, so no Git snapshot applies); (5)
        atomically write the journal head bytes; (6) record the applied
        action as a journal audit event and a loud manifest warning (which
        itself lands as a fresh journaled transition, so the progress
        fingerprint provably moves — R5). Steps 5/8 of the Git transaction
        (intent persist/clear) degenerate away: the rewrite is one atomic
        replace and re-running it converges on the same bytes, so a kill at
        any point leaves either the old or the new projection — both
        recognized states, replayed by the next contact.
        """
        if action.kind is not RecoveryActionKind.REBUILD_PROJECTION:
            raise RecoveryPreconditionError(
                f"apply_rebuild applies rebuild_projection actions only; "
                f"got {action.kind}"
            )
        if action not in assessment.safe_actions:
            raise RecoveryPreconditionError(
                "the action to apply must be one of the assessment's safe_actions"
            )
        projection = self.repo_root / action.projection_path
        with self.lock_guard.hold():  # step 1
            survivor = load_intent(self.run_dir)
            if survivor is not None:  # step 2: converge the Git plane first
                self._replay(survivor)
            observed = J.evidence_fingerprint(projection)  # step 3
            if observed != action.evidence_fingerprint:
                raise RecoveryPreconditionError(
                    "projection evidence fingerprint changed between "
                    f"assessment and apply ({action.evidence_fingerprint} -> "
                    f"{observed}); the projection moved — re-assess before "
                    "mutating (fail closed)"
                )
            preserved: Path | None = None
            if projection.exists():  # step 4 (R2): preserve, never hand-edit
                stamp = self.clock().replace(":", "-").replace("+", "-")
                preserved = projection.with_name(
                    f"manifest.corrupt-{stamp}.json"
                )
                data = projection.read_bytes()
                preserved.write_bytes(data)
                _fsync_dir(preserved.parent)
            seq, event_id = J.write_projection_from_head(self.run_dir)  # step 5
            note = (
                f"rebuilt manifest.json from the authoritative journal head "
                f"(seq {seq}, event {event_id}); the "
                + (
                    f"malformed original is preserved as {preserved.name}"
                    if preserved is not None
                    else "projection was missing (nothing to preserve)"
                )
                + " — plan §5.5, R8"
            )
            J.append_audit(  # step 6: applied-action evidence, deduplicated
                self.run_dir,
                "RecoveryActionApplied",
                {
                    "action_kind": RecoveryActionKind.REBUILD_PROJECTION.value,
                    "journal_seq": seq,
                    "head_event_id": event_id,
                    "evidence_fingerprint": action.evidence_fingerprint,
                    "preserved_as": preserved.name if preserved else None,
                },
                run_id=self.run_id,
                idempotency_key=(
                    f"rebuild:{action.evidence_fingerprint}:{event_id}"
                ),
            )
            # Loud, durable audit (plan §5.5): the warning is appended through
            # the normal journaled persist, so the rebuild provably advances
            # the progress fingerprint (R5) and the audit survives crashes.
            _append_manifest_warning(self.run_dir, note)
            return note

    # -- step 4: the durable pre-mutation snapshot -----------------------------

    def _create_snapshot(
        self, request: SnapshotRequest
    ) -> git_snapshot.GitRecoverySnapshot:
        """Create the R2 snapshot, de-colliding the id when a same-stamp
        snapshot already exists (stubbed clocks / rapid repeats). Every other
        snapshot failure propagates — it must abort the transaction before
        any destructive verb runs.
        """
        base_id = request.snapshot_id
        for attempt in range(20):
            snapshot_id = base_id if attempt == 0 else f"{base_id}-{attempt + 1}"
            try:
                # The snapshot hashes a WORKING TREE (its index and its file
                # contents), so it must be taken of the tree this executor is
                # about to rewind — never the operator's checkout (P7c). The
                # static ROOT_SCOPE audit cannot see this: `create_snapshot` is
                # not a `gitops.*` call, which is precisely why the roots have
                # to be carried rather than re-chosen per site.
                return git_snapshot.create_snapshot(
                    self.work_root,
                    run_id=self.run_id,
                    snapshot_id=snapshot_id,
                    attempt_id=request.attempt_id,
                    reason=request.reason,
                    run_branch=request.run_branch,
                    exclude=request.exclude,
                    protected=request.protected,
                    created_at=self.clock(),
                )
            except git_snapshot.SnapshotError as exc:
                if "already exists" not in str(exc):
                    raise
        raise git_snapshot.SnapshotError(
            f"could not allocate a unique snapshot id from {base_id!r}"
        )

    # -- step 3: validation ----------------------------------------------------

    def _validate(self, action: RecoveryAction, spec: RewindSpec) -> None:
        repo = self.work_root
        if not gitops.ref_is_valid_commit(repo, spec.target_sha):
            raise RecoveryPreconditionError(
                f"rewind target {spec.target_sha[:10]} does not resolve to a "
                "commit; refusing"
            )
        if spec.checkout_branch is not None:
            if not gitops.branch_exists(repo, spec.checkout_branch):
                raise RecoveryPreconditionError(
                    f"checkout target branch {spec.checkout_branch!r} is missing"
                )
            tip = gitops.rev_parse(repo, f"refs/heads/{spec.checkout_branch}")
        else:
            tip = gitops.head_sha(repo)
        # The action's target_ref must resolve, under the lock, to exactly the
        # tip this transaction is about to rewind — the advertised action and
        # the mutation can never name different refs (post-P3 review F-003).
        target_ref = getattr(action, "target_ref", None)
        if target_ref:
            try:
                resolved_ref = gitops.rev_parse(repo, target_ref)
            except GitError as exc:
                raise RecoveryPreconditionError(
                    f"action target_ref {target_ref!r} does not resolve; "
                    "refusing to mutate under an unexecutable action"
                ) from exc
            if resolved_ref != tip:
                raise RecoveryPreconditionError(
                    f"action target_ref {target_ref!r} resolves to "
                    f"{resolved_ref[:10]} but this transaction rewinds tip "
                    f"{tip[:10]}; the action and the mutation disagree on "
                    "which ref is being rewound"
                )
        if spec.target_sha != tip and not gitops.is_ancestor(
            repo, spec.target_sha, tip
        ):
            raise RecoveryPreconditionError(
                f"rewind target {spec.target_sha[:10]} is not an ancestor of "
                f"the tip {tip[:10]} being rewound; refusing a rewind onto "
                "unrelated history"
            )
        if spec.reset_mode == RESET_BOOKKEEPING_PRESERVING:
            for rel in spec.bookkeeping_paths:
                _validate_rel_path(rel, field="bookkeeping path")

    # -- step 6: apply ---------------------------------------------------------

    def _apply_spec(
        self, spec: RewindSpec, snapshot: git_snapshot.GitRecoverySnapshot
    ) -> None:
        repo = self.work_root
        if (
            spec.checkout_branch is not None
            and gitops.current_branch(repo) != spec.checkout_branch
        ):
            gitops.checkout_branch(repo, spec.checkout_branch)
        head = gitops.head_sha(repo)
        if spec.reset_mode == RESET_BOOKKEEPING_PRESERVING and head != spec.target_sha:
            gitops.rewind_impl_preserving_bookkeeping(
                repo,
                spec.target_sha,
                list(spec.bookkeeping_paths),
                spec.rewind_message,
                identity=ENGINE_IDENTITY,
            )
        else:
            # A plain reset also runs when head == target: that is the
            # uncommitted-dirt discard (the conflict-park shape).
            gitops.reset_hard(repo, spec.target_sha)
        if spec.clean:
            gitops.clean_untracked(repo, exclude=list(spec.clean_excludes))
        git_snapshot.restore_protected(repo, snapshot)

    # -- replay (idempotent convergence of a surviving intent) -----------------

    def _replay(self, intent: RecoveryIntent) -> str:
        # `replay_intent` checks out, resets and cleans — every one of them a
        # working-tree mutation, so it takes the WORK root (P7c).
        return replay_intent(self.work_root, self.run_dir, intent)


def _tree_entry(repo: Path, tree: str, rel: str) -> tuple[str, str] | None:
    """``(mode, oid)`` of ``rel`` in ``tree``, or ``None`` when absent."""
    out = gitops._run(repo, "ls-tree", "-z", tree, "--", rel)
    if not out:
        return None
    meta = out.rstrip("\0").split("\t", 1)[0]
    mode, _kind, oid = meta.split()
    return mode, oid


def _blob_oid(repo: Path, data: bytes) -> str:
    """The git blob id of ``data`` WITHOUT writing it (read-only witness)."""
    return gitops._run_bytes(
        repo, "hash-object", "--stdin", stdin=data
    ).decode().strip()


def _index_fingerprint_for_tree(
    work_root: Path, treeish: str, *, bookkeeping: tuple[str, ...] = ()
) -> str:
    """Index fingerprint produced by ``read-tree`` plus an optional overlay.

    Uses a temporary index, so deriving the legitimate intermediate states of
    ``rewind_impl_preserving_bookkeeping`` is observational. Its output uses
    the same ``ls-files --stage -z`` representation as :func:`index_fingerprint`.
    """
    with tempfile.TemporaryDirectory(prefix="gauntlet-replay-index-") as tmp:
        index_path = Path(tmp) / "index"
        gitops.run_with_temp_index(work_root, index_path, "read-tree", treeish)
        if bookkeeping:
            gitops.run_with_temp_index(
                work_root, index_path, "add", "-f", "--", *bookkeeping
            )
        out = gitops.run_with_temp_index(
            work_root, index_path, "ls-files", "--stage", "-z"
        )
    return _sha256(out.encode("utf-8", "surrogateescape"))


def _worktree_matches_snapshot(
    work_root: Path,
    snapshot: git_snapshot.GitRecoverySnapshot,
    *,
    excludes: list[str],
    protected: list[str],
) -> bool:
    """Whether the live filesystem plane still equals the captured snapshot.

    Unlike :func:`worktree_fingerprint`, this witness is independent of the
    real index, which a bookkeeping-preserving rewind deliberately scratches
    before moving HEAD. A temporary index seeded from the snapshot worktree
    tree stages the live filesystem with the same exclusions/protected carveout
    used at snapshot creation; no real Git state is changed.
    """
    def remove_control_locks(index_path: Path) -> None:
        entries = gitops.run_with_temp_index(
            work_root, index_path, "ls-files", "-z"
        ).split("\0")
        locks = [
            rel for rel in entries if rel and (
                PurePosixPath(rel).name == DRIVING_LOCK_NAME
                or PurePosixPath(rel).name.startswith(DRIVING_LOCK_NAME + ".")
            )
        ]
        if locks:
            gitops.run_with_temp_index(
                work_root,
                index_path,
                "update-index",
                "--force-remove",
                "-z",
                "--stdin",
                stdin="".join(f"{rel}\0" for rel in locks),
            )

    with tempfile.TemporaryDirectory(prefix="gauntlet-replay-worktree-") as tmp:
        index_path = Path(tmp) / "index"
        gitops.run_with_temp_index(
            work_root, index_path, "read-tree", snapshot.worktree_tree
        )
        # The executor's own ephemeral lock can exist while the snapshot is
        # built and disappear before replay (or carry a new nonce during a
        # locked verb). It is control state, never work; normalize it out just
        # as ``worktree_fingerprint`` / ``_dirty_paths`` already do.
        remove_control_locks(index_path)
        expected_tree = gitops.run_with_temp_index(
            work_root, index_path, "write-tree"
        ).strip()
        gitops.run_with_temp_index(
            work_root,
            index_path,
            "add",
            "-A",
            *gitops._exclude_pathspec(excludes),
        )
        if protected:
            # Force-add only the patterns that still match something in the
            # temp index's view: `git add` is fatal on any unmatched pathspec,
            # and a protected deletion already reflected in the seed tree
            # leaves its pattern with nothing to act on (mixed protected
            # states in the §7 restoration fault matrix).
            addable = git_snapshot._patterns_matching_temp_index(
                work_root, index_path, protected
            )
            if addable:
                gitops.run_with_temp_index(
                    work_root, index_path, "add", "-A", "-f", "--", *addable
                )
        remove_control_locks(index_path)
        live_tree = gitops.run_with_temp_index(
            work_root, index_path, "write-tree"
        ).strip()
    return live_tree == expected_tree


def _dirt_is_snapshot_residue(
    repo: Path,
    snapshot: git_snapshot.GitRecoverySnapshot,
    excludes: list[str],
    *,
    allowed_index_fingerprints: set[str],
) -> bool:
    """True iff every dirty path is provably leftover mid-apply state.

    A killed apply can leave the worktree between reset and clean (untracked
    files the snapshot captured) or mid protected-restore. Every such path's
    live content is byte-identical to the snapshot's worktree tree — Git
    materialized it from there, or it survives from the captured pre-state —
    and a captured protected deletion may already be re-applied. Anything
    else (a path absent from the snapshot, or content the snapshot never
    held) is NEW work created after the kill: replaying reset/clean over it
    would destroy state no snapshot covers, so the caller must fail closed
    (post-P3 review F-001).
    """
    if index_fingerprint(repo) not in allowed_index_fingerprints:
        return False
    for rel in _dirty_paths(repo, exclude=excludes):
        entry = _tree_entry(repo, snapshot.worktree_tree, rel)
        path = repo / rel
        try:
            info = path.lstat()
        except FileNotFoundError:
            if rel in snapshot.protected_deletions:
                continue  # the protected-deletion restore already ran
            return False
        if entry is None:
            return False  # a path the snapshot never captured: new work
        mode, oid = entry
        if stat.S_ISLNK(info.st_mode):
            if mode != "120000":
                return False
            if _blob_oid(repo, os.readlink(path).encode()) != oid:
                return False
        elif stat.S_ISREG(info.st_mode):
            if mode not in ("100644", "100755"):
                return False
            if _blob_oid(repo, path.read_bytes()) != oid:
                return False
        else:
            return False
    return True


def replay_intent(work_root: Path, run_dir: Path, intent: RecoveryIntent) -> str:
    """Idempotently converge a surviving intent to its intended end state.

    Decision table, keyed on the intent's independently re-derivable plane
    witnesses (post-P3 review F-001) — fail closed on anything unrecognized:

    * repository provably in the pre-apply state (HEAD **and** index **and**
      worktree fingerprints match the intent) → run the full apply;
    * proven mid-apply state — HEAD at the target (or the bookkeeping-
      preserving rewind commit) with a tree that is clean or carries ONLY
      dirt byte-identical to the snapshot's captured planes → finish the
      apply (re-reset, clean, protected restore);
    * anything else — including a same-HEAD repository holding tracked,
      staged, or untracked work created after the kill → raise
      :class:`RecoveryIntentError`; the intent stays in place as evidence
      and every further mutation keeps failing closed.

    After convergence the intent's site finisher (if registered) re-persists
    the site's state transition; the intent clears only after that returns.
    """
    spec = intent.spec
    snapshot = git_snapshot.load_snapshot(work_root, intent.snapshot_ref)
    excludes = list(intent.excludes)
    current_branch = gitops.current_branch(work_root)
    head = gitops.head_sha(work_root)

    def _planes_match_pre() -> bool:
        """The repository is byte-provably the assessed pre-apply state."""
        if head != intent.pre_head:
            return False
        if not _worktree_matches_snapshot(
            work_root,
            snapshot,
            excludes=excludes,
            protected=list(intent.protected),
        ):
            return False
        return index_fingerprint(work_root) == intent.pre_index_fingerprint

    def _refuse(why: str) -> "RecoveryIntentError":
        return RecoveryIntentError(
            f"surviving intent {intent.intent_id} ({intent.site}): {why}; "
            "refusing to replay over an unrecognized state — inspect "
            f"{intent_path(run_dir)} and snapshot {intent.snapshot_ref}"
        )

    def _full_apply() -> None:
        if spec.reset_mode == RESET_BOOKKEEPING_PRESERVING:
            gitops.rewind_impl_preserving_bookkeeping(
                work_root, spec.target_sha, list(spec.bookkeeping_paths),
                spec.rewind_message, identity=ENGINE_IDENTITY,
            )
        else:
            gitops.reset_hard(work_root, spec.target_sha)
        _finish()

    def _finish() -> None:
        if spec.clean:
            gitops.clean_untracked(work_root, exclude=list(spec.clean_excludes))
        git_snapshot.restore_protected(work_root, snapshot)

    head_index_fingerprint = _index_fingerprint_for_tree(work_root, head)
    target_index_fingerprint = _index_fingerprint_for_tree(work_root, spec.target_sha)
    bookkeeping_index_fingerprint = None
    if spec.reset_mode == RESET_BOOKKEEPING_PRESERVING:
        bookkeeping_index_fingerprint = _index_fingerprint_for_tree(
            work_root, spec.target_sha, bookkeeping=spec.bookkeeping_paths
        )

    def _bookkeeping_pre_reset_state() -> bool:
        """A recognized pre-reset sub-boundary of the bookkeeping rewind.

        The helper mutates the real index in three deterministic steps before
        its final reset: original pre-index, target ``read-tree``, then target
        plus the bookkeeping overlay. Recognize exactly those fingerprints,
        together with the unchanged pre-HEAD/worktree, so a kill between any
        two Git calls remains replayable without accepting arbitrary staging.
        """
        if spec.reset_mode != RESET_BOOKKEEPING_PRESERVING:
            return False
        if head != intent.pre_head:
            return False
        if not _worktree_matches_snapshot(
            work_root,
            snapshot,
            excludes=excludes,
            protected=list(intent.protected),
        ):
            return False
        assert bookkeeping_index_fingerprint is not None
        return index_fingerprint(work_root) in {
            intent.pre_index_fingerprint,
            target_index_fingerprint,
            bookkeeping_index_fingerprint,
        }

    tree_is_clean = not _dirty_paths(work_root, exclude=excludes)

    applied_note: str
    if spec.checkout_branch is not None and current_branch != spec.checkout_branch:
        # Crash before the checkout: only proceed when every plane is
        # provably still the assessed pre-apply state.
        if not _planes_match_pre():
            raise _refuse(
                f"the repository is on {current_branch!r} and its HEAD/"
                "index/worktree planes do not match the recorded pre-state"
            )
        gitops.checkout_branch(work_root, spec.checkout_branch)
        gitops.reset_hard(work_root, spec.target_sha)
        _finish()
        applied_note = "replayed the full apply from the recorded pre-state"
    elif head == spec.target_sha:
        if _planes_match_pre() or _bookkeeping_pre_reset_state():
            # target == pre-HEAD and nothing moved: the killed apply never
            # ran — run it in full (the reset is the captured-dirt discard).
            _full_apply()
            applied_note = "replayed the full apply from the recorded pre-state"
        elif tree_is_clean:
            _finish()
            applied_note = "finished a mid-apply intent (apply already effected)"
        elif _dirt_is_snapshot_residue(
            work_root,
            snapshot,
            excludes,
            allowed_index_fingerprints={head_index_fingerprint},
        ):
            gitops.reset_hard(work_root, spec.target_sha)
            _finish()
            applied_note = (
                "finished a mid-apply intent (residual captured dirt discarded)"
            )
        else:
            raise _refuse(
                "HEAD is at the target but the tree holds work the snapshot "
                "never captured (created after the killed transaction)"
            )
    elif spec.reset_mode == RESET_BOOKKEEPING_PRESERVING and (
        gitops.advance_is_engine_bookkeeping(
            work_root, spec.target_sha, bookkeeping=list(spec.bookkeeping_paths),
            tip=head,
        )
    ):
        # The bookkeeping-preserving rewind commit is already in place.
        if tree_is_clean:
            _finish()
        elif _dirt_is_snapshot_residue(
            work_root,
            snapshot,
            excludes,
            allowed_index_fingerprints={head_index_fingerprint},
        ):
            gitops.reset_hard(work_root, head)
            _finish()
        else:
            raise _refuse(
                "the bookkeeping-preserving rewind is in place but the tree "
                "holds work the snapshot never captured"
            )
        applied_note = (
            "finished a mid-apply intent (bookkeeping-preserving rewind "
            "already effected)"
        )
    elif head == intent.pre_run_branch_sha and spec.checkout_branch is not None:
        # Crash between the checkout and the reset: the pre-state was fully
        # verified before the checkout ran, so a clean/residue-only tree at
        # the run-branch tip is the proven mid-apply state.
        if not (
            tree_is_clean
            or _dirt_is_snapshot_residue(
                work_root,
                snapshot,
                excludes,
                allowed_index_fingerprints={head_index_fingerprint},
            )
        ):
            raise _refuse(
                "the checkout completed but the tree holds work the snapshot "
                "never captured"
            )
        gitops.reset_hard(work_root, spec.target_sha)
        _finish()
        applied_note = "finished a mid-apply intent (checkout already effected)"
    elif head == intent.pre_head:
        if not (_planes_match_pre() or _bookkeeping_pre_reset_state()):
            raise _refuse(
                "HEAD matches the pre-state but the index/worktree planes "
                "hold work created after the killed transaction"
            )
        _full_apply()
        applied_note = "replayed the full apply from the recorded pre-state"
    else:
        raise _refuse(
            f"HEAD {head[:10]} is neither the pre-state "
            f"({intent.pre_head[:10]}) nor the target "
            f"({spec.target_sha[:10]}); the repository moved since the "
            "killed transaction"
        )

    finisher = REPLAY_FINISHERS.get(intent.site)
    if finisher is not None:
        finisher(work_root, run_dir, intent)
    else:
        _append_manifest_warning(
            run_dir,
            f"recovery intent {intent.intent_id} ({intent.site}) was replayed "
            f"after a process death: {applied_note}; snapshot retained at "
            f"{intent.snapshot_ref}",
        )
    _persist_governed_replay_evidence(run_dir, intent)
    # P6 journal audit: the applied-action event shares the original apply's
    # idempotency key, so a transaction killed after its own event appends
    # nothing here (deduplicated — exactly once), while one killed before it
    # gains the missing evidence. Best-effort, never a gate.
    J.append_audit(
        run_dir,
        "RecoveryActionApplied",
        {"intent_id": intent.intent_id, "site": intent.site,
         "target_sha": intent.spec.target_sha,
         "snapshot_ref": intent.snapshot_ref, "replayed": True,
         "note": applied_note},
        run_id=intent.run_id,
        idempotency_key=f"applied:{intent.intent_id}",
    )
    _clear_intent(run_dir, intent.intent_id)
    return applied_note


def _persist_governed_replay_evidence(
    run_dir: Path, intent: RecoveryIntent
) -> None:
    """Persist intent-carried governance evidence before the intent clears.

    The caller may have died after the Git apply but before its in-memory
    warning reached the manifest. The intent is the durable bridge across that
    window. Failure to persist leaves the intent in place and fails closed.
    """
    raw = intent.payload.get(GOVERNED_EVIDENCE_PAYLOAD_KEY, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise RecoveryIntentError(
            f"surviving recovery intent {intent.intent_id} carries malformed "
            "governed-discard evidence; refusing to clear it"
        )
    if not raw:
        return
    path = run_dir / "manifest.json"
    try:
        man = M.Manifest.load(path)
        changed = False
        for note in raw:
            if any(note in warning for warning in man.warnings):
                continue
            man.warnings.append(
                f"replayed recovery intent {intent.intent_id}: {note}; "
                f"snapshot retained at {intent.snapshot_ref}"
            )
            changed = True
        if changed:
            man.write_atomic(path)
    except (OSError, ValueError) as exc:
        raise RecoveryIntentError(
            f"surviving recovery intent {intent.intent_id} could not persist "
            f"its governed-discard evidence to {path} ({exc}); intent retained"
        ) from exc


def _append_manifest_warning(run_dir: Path, note: str) -> None:
    """Best-effort durable audit note for a finisher-less replay."""
    path = run_dir / "manifest.json"
    try:
        man = M.Manifest.load(path)
    except (OSError, ValueError):
        return
    if note not in man.warnings:
        man.warnings.append(note)
        man.write_atomic(path)


def replay_pending_intent(work_root: Path, run_dir: Path) -> str | None:
    """Replay a surviving intent if one exists; ``None`` when there is none.

    The hook every mutating command calls after taking the lock: a killed
    transaction is converged (or fails closed with named evidence) before any
    new work runs against the repository.

    Takes the WORK root (P7c): the replay checks out, resets and cleans, so it
    converges the tree the run drives. ``run_dir`` stays the operator-checkout
    run-instance dir — a surviving intent must outlive the tree its replay
    repairs (spike §9.5), which is the whole reason §4.4 leaves it there.
    """
    intent = load_intent(run_dir)
    if intent is None:
        return None
    return replay_intent(work_root, run_dir, intent)


__all__ = [
    "DRIVING_LOCK_NAME",
    "EXECUTOR_INTENT_NAME",
    "STATE_ABORTED",
    "STATE_DONE",
    "STATE_FAILED",
    "STATE_HALTED",
    "STATE_INDETERMINATE",
    "STATE_INTERRUPTED",
    "STATE_IN_PROGRESS",
    "STATE_ORPHANED",
    "STATE_PARKED_ARTIFACT_INVALID",
    "STATE_PARKED_FOR_RESPONSE",
    "STATE_PARKED_GATE",
    "STATE_PARKED_PROVIDER_UNAVAILABLE",
    "STATE_PARKED_USAGE_LIMIT",
    "STATE_PARKED_USAGE_WINDOW",
    "STATE_UNKNOWN",
    "classify_composite",
    "mutating_action_kinds",
    "outcome_classification",
    "reconcile_branch_ahead",
    "reconciliation_boundary",
    "relation_recovery_actions",
    "GOVERNED_DISCARD_EVIDENCE_PREFIX",
    "GOVERNED_EVIDENCE_PAYLOAD_KEY",
    "REPLAY_FINISHERS",
    "RESET_BOOKKEEPING_PRESERVING",
    "RESET_PLAIN",
    "RecoveryExecError",
    "RecoveryExecutor",
    "RecoveryIntent",
    "RecoveryIntentError",
    "RecoveryLockError",
    "RecoveryObservationError",
    "RecoveryPlanner",
    "RecoveryPreconditionError",
    "RecoveryResult",
    "RewindSpec",
    "SnapshotRequest",
    "WorktreeLockGuard",
    "build_progress_fingerprint",
    "index_fingerprint",
    "intent_path",
    "load_intent",
    "observe_git",
    "observe_state",
    "projection_rebuild_assessment",
    "replay_intent",
    "replay_pending_intent",
    "worktree_fingerprint",
]

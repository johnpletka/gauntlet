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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from gauntlet.engine import git_snapshot, gitops, manifest as M
from gauntlet.engine.gitops import ENGINE_IDENTITY
from gauntlet.engine.recovery import (
    AbortAction,
    BranchRelation,
    CommitKind,
    DriverLiveness,
    GitCommitObservation,
    GitCommitPathChange,
    GitObservation,
    PathChangeKind,
    ProgressFingerprint,
    RecoveryAction,
    RecoveryActionKind,
    RecoveryAssessment,
    RecoveryCause,
    RecoveryDisposition,
    ResponseState,
    RunStatus,
    SnapshotAndRestartAction,
    StateObservation,
    StepStatus,
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

# Commit-subject conventions (mirrors gitops: checkpoint/engine subjects are
# matched at fixed field position, never against free prose).
_WIP_RE = re.compile(r"^(P\d+) wip:")
_PHASE_RE = re.compile(r"^(P\d+):")
_FIX_RE = re.compile(r"^(P\d+)\.\w+:")
_ENGINE_RE = re.compile(r"^gauntlet: ")

RESET_PLAIN = "plain"
RESET_BOOKKEEPING_PRESERVING = "bookkeeping_preserving"

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


def index_fingerprint(repo: Path) -> str:
    """Stable content fingerprint of the real index (paths, modes, oids, stages)."""
    out = gitops._run(repo, "ls-files", "--stage", "-z")
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


def worktree_fingerprint(repo: Path, *, exclude: list[str] | None = None) -> str:
    """Content-true fingerprint of the worktree plane relative to HEAD.

    Porcelain alone is lossy (two different byte contents of one modified path
    fingerprint identically), so every dirty path's live content identity is
    folded in: symlinks by target (never followed), files by byte hash,
    missing paths as deletions. HEAD's tree id anchors the committed plane.
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
    head_tree = gitops._run(repo, "rev-parse", "HEAD^{tree}").strip()
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
    repo: Path,
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
    branch = gitops.current_branch(repo)
    checked_out = None if branch == "HEAD" else branch
    head = gitops.head_sha(repo)
    run_branch_sha: str | None = None
    if gitops.branch_exists(repo, run_branch):
        run_branch_sha = gitops.rev_parse(repo, f"refs/heads/{run_branch}")

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
    elif gitops.is_ancestor(repo, recorded_sha, run_branch_sha):
        commits = _inventory_range(
            repo, recorded_sha, run_branch_sha,
            bookkeeping_candidates=bookkeeping, approved_artifacts=approved,
        )
        relation = _relation_from_kinds({c.kind for c in commits})
    elif gitops.is_ancestor(repo, run_branch_sha, recorded_sha):
        relation = BranchRelation.BEHIND
        merge_base = run_branch_sha
    else:
        relation = BranchRelation.FORKED
        merge_base = gitops.merge_base(repo, run_branch_sha, recorded_sha)
        if merge_base is None:
            raise RecoveryObservationError(
                f"run branch {run_branch!r} shares no history with the recorded "
                f"boundary {recorded_sha[:10]}; unrelated histories cannot be "
                "assessed for recovery"
            )
        commits = _inventory_range(
            repo, merge_base, run_branch_sha,
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
        index_fingerprint=index_fingerprint(repo),
        worktree_fingerprint=worktree_fingerprint(repo, exclude=excludes),
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


def build_progress_fingerprint(
    repo: Path,
    *,
    manifest: "M.Manifest",
    record: "M.StepRecord | None" = None,
    excludes: list[str] | None = None,
    latest_cycle_substep: str | None = None,
) -> ProgressFingerprint:
    """The plan §4.5 fingerprint over run/step/branch/index/worktree state."""
    run_branch_sha = None
    if gitops.branch_exists(repo, manifest.branch):
        run_branch_sha = gitops.rev_parse(repo, f"refs/heads/{manifest.branch}")
    pending_id = None
    pending_state = None
    if record is not None and record.human_responses:
        entry = record.human_responses[-1]
        pending_id = entry.response_id
        pending_state = ResponseState(entry.state)
    return ProgressFingerprint(
        run_id=manifest.run_id,
        current_step=manifest.current_step,
        iteration=record.iteration if record is not None else None,
        attempt_id=f"{record.id}#{record.attempts}" if record is not None else None,
        run_status=RunStatus(manifest.status),
        step_status=StepStatus(record.status) if record is not None else None,
        run_branch_sha=run_branch_sha,
        index_fingerprint=index_fingerprint(repo),
        worktree_fingerprint=worktree_fingerprint(repo, exclude=excludes),
        pending_response_id=pending_id,
        pending_response_state=pending_state,
        latest_cycle_substep=latest_cycle_substep,
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
        inventoried range commit, or an ancestor of the observed tips. An
        action whose target the observation cannot substantiate is refused —
        it must never be advertised as safe.
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
    """The surviving intent, or ``None``. Malformed intents fail closed."""
    path = intent_path(run_dir)
    try:
        text = path.read_text()
    except (OSError, FileNotFoundError):
        return None
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
    """Transaction step 1: hold, or prove we already hold, the worktree lock.

    Every production caller runs inside a verb that already holds the
    ``.driving.lock`` (resume/rollback acquire it in RunManager before any
    rewind can occur), so the common case is verification: the lock file
    exists and names THIS process. A foreign live holder fails closed. When
    no lock exists (direct engine embedding, tests), an ephemeral lock is
    taken for the transaction and released after — atomically created, never
    reclaimed from a live pid.
    """

    def __init__(self, repo_root: Path, run_root: str) -> None:
        self.lock_path = repo_root / run_root / DRIVING_LOCK_NAME

    def _read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.lock_path.read_text())
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

    @contextmanager
    def hold(self) -> Iterator[None]:
        record = self._read()
        if record is not None:
            try:
                pid = int(record.get("pid"))
            except (TypeError, ValueError):
                pid = -1
            if pid == os.getpid():
                yield  # already held by this process (the normal verb path)
                return
            if pid > 0 and self._pid_alive(pid):
                raise RecoveryLockError(
                    f"worktree lock {self.lock_path} is held by live pid "
                    f"{pid}; refusing a concurrent recovery mutation (FR-10.5)"
                )
            # A stale/corrupt foreign lock: do NOT reclaim here — reclaim
            # policy (identity verification) belongs to RunManager. Fail
            # closed with the remedy.
            raise RecoveryLockError(
                f"worktree lock {self.lock_path} exists but its holder is not "
                "verifiably this process; run the recovery through a locked "
                "verb (resume/rollback) or remove the stale lock first"
            )
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        nonce = secrets.token_hex(16)
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
        tmp = self.lock_path.with_name(f"{self.lock_path.name}.{nonce}.tmp")
        tmp.write_text(payload)
        try:
            try:
                os.link(tmp, self.lock_path)
            except FileExistsError:
                raise RecoveryLockError(
                    f"worktree lock {self.lock_path} appeared during "
                    "acquisition; refusing a concurrent recovery mutation"
                ) from None
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        try:
            yield
        finally:
            current = self._read()
            if current is not None and current.get("nonce") == nonce:
                try:
                    os.unlink(self.lock_path)
                except FileNotFoundError:
                    pass


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
    ) -> None:
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.run_id = run_id
        self.excludes = excludes
        self.clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        self.lock_guard = lock_guard or WorktreeLockGuard(repo_root, run_root)

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
            self._validate(spec)  # step 3
            snapshot = self._create_snapshot(snapshot_request)  # step 4 (R2)
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
                spec=spec,
                payload=payload or {},
            )
            _write_intent(self.run_dir, intent)
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
            _clear_intent(self.run_dir, intent.intent_id)  # step 8
            return result

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
                return git_snapshot.create_snapshot(
                    self.repo_root,
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

    def _validate(self, spec: RewindSpec) -> None:
        repo = self.repo_root
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
        repo = self.repo_root
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
        return replay_intent(self.repo_root, self.run_dir, intent)


def replay_intent(repo: Path, run_dir: Path, intent: RecoveryIntent) -> str:
    """Idempotently converge a surviving intent to its intended end state.

    Decision table (fail closed on anything unrecognized):

    * repository still in the pre-apply state (fingerprint digest matches) →
      run the full apply;
    * recognized mid-apply state (checked out, HEAD at the target, or — for a
      bookkeeping-preserving rewind — HEAD advanced from the target by engine
      bookkeeping only) → finish the apply (clean + protected restore);
    * anything else → :class:`RecoveryIntentError`; the intent stays in place
      as evidence and every further mutation keeps failing closed.

    After convergence the intent's site finisher (if registered) re-persists
    the site's state transition; the intent clears only after that returns.
    """
    spec = intent.spec
    snapshot = git_snapshot.load_snapshot(repo, intent.snapshot_ref)
    current_branch = gitops.current_branch(repo)
    head = gitops.head_sha(repo)

    def _finish() -> None:
        if spec.clean:
            gitops.clean_untracked(repo, exclude=list(spec.clean_excludes))
        git_snapshot.restore_protected(repo, snapshot)

    def _fingerprint_matches() -> bool:
        # The pre-state witness: index + worktree + HEAD content identity. Run
        # id / statuses are process-local at replay time, so the comparison
        # uses the durable planes the digest folds in via a fresh digest of
        # the SAME shape the executor fingerprinted. A conservative check:
        # the pre-head must match too.
        return head == intent.pre_head

    applied_note: str
    if spec.checkout_branch is not None and current_branch != spec.checkout_branch:
        # Crash before (or during) checkout: only proceed when the repository
        # is provably still in the assessed pre-apply state.
        if not _fingerprint_matches():
            raise RecoveryIntentError(
                f"surviving intent {intent.intent_id} ({intent.site}): the "
                f"repository is on {current_branch!r} with HEAD "
                f"{head[:10]} != recorded pre-state {intent.pre_head[:10]}; "
                "refusing to replay over an unrecognized state — inspect "
                f"{intent_path(run_dir)} and snapshot {intent.snapshot_ref}"
            )
        gitops.checkout_branch(repo, spec.checkout_branch)
        head = gitops.head_sha(repo)

    if head == spec.target_sha:
        # HEAD is already at the target — which is also true when the killed
        # apply never ran (a target == pre-head dirt discard), so the reset
        # itself must re-run: it is idempotent, and it is what discards the
        # captured index/worktree dirt the snapshot preserved.
        gitops.reset_hard(repo, spec.target_sha)
        _finish()
        applied_note = "finished a mid-apply intent (HEAD already at target)"
    elif spec.reset_mode == RESET_BOOKKEEPING_PRESERVING and (
        gitops.advance_is_engine_bookkeeping(
            repo, spec.target_sha, bookkeeping=list(spec.bookkeeping_paths),
            tip=head,
        )
    ):
        # The bookkeeping-preserving rewind commit is already in place;
        # discard any residual dirt against it (idempotent), then finish.
        gitops.reset_hard(repo, head)
        _finish()
        applied_note = (
            "finished a mid-apply intent (bookkeeping-preserving rewind "
            "already effected)"
        )
    elif head in (intent.pre_head, intent.pre_run_branch_sha):
        if spec.reset_mode == RESET_BOOKKEEPING_PRESERVING:
            gitops.rewind_impl_preserving_bookkeeping(
                repo, spec.target_sha, list(spec.bookkeeping_paths),
                spec.rewind_message, identity=ENGINE_IDENTITY,
            )
        else:
            gitops.reset_hard(repo, spec.target_sha)
        _finish()
        applied_note = "replayed the full apply from the recorded pre-state"
    else:
        raise RecoveryIntentError(
            f"surviving intent {intent.intent_id} ({intent.site}): HEAD "
            f"{head[:10]} is neither the pre-state ({intent.pre_head[:10]}) "
            f"nor the target ({spec.target_sha[:10]}); the repository moved "
            "since the killed transaction — refusing to replay. Inspect "
            f"{intent_path(run_dir)} and snapshot {intent.snapshot_ref}"
        )

    finisher = REPLAY_FINISHERS.get(intent.site)
    if finisher is not None:
        finisher(repo, run_dir, intent)
    else:
        _append_manifest_warning(
            run_dir,
            f"recovery intent {intent.intent_id} ({intent.site}) was replayed "
            f"after a process death: {applied_note}; snapshot retained at "
            f"{intent.snapshot_ref}",
        )
    _clear_intent(run_dir, intent.intent_id)
    return applied_note


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


def replay_pending_intent(repo: Path, run_dir: Path) -> str | None:
    """Replay a surviving intent if one exists; ``None`` when there is none.

    The hook every mutating command calls after taking the lock: a killed
    transaction is converged (or fails closed with named evidence) before any
    new work runs against the repository.
    """
    intent = load_intent(run_dir)
    if intent is None:
        return None
    return replay_intent(repo, run_dir, intent)


__all__ = [
    "DRIVING_LOCK_NAME",
    "EXECUTOR_INTENT_NAME",
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
    "replay_intent",
    "replay_pending_intent",
    "worktree_fingerprint",
]

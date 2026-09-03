"""Fail-closed recovery observations and executable action contracts.

P1 defines the complete, immutable evidence vocabulary used by later recovery
phases.  The models are deliberately side-effect free: observers must prove
that Git planes, commit ranges, lifecycle states, and actions are internally
consistent before a future planner can consume them.  P2-P4 will add snapshot,
planner, executor, and public-command behaviour without weakening these
contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ClosedStringEnum(str, Enum):
    """A JSON-friendly closed enum with readable ``str()`` output."""

    def __str__(self) -> str:
        return self.value


class BranchRelation(_ClosedStringEnum):
    """Graph relationship between the run ref and its recorded boundary."""

    EQUAL = "equal"
    ENGINE_BOOKKEEPING_AHEAD = "engine_bookkeeping_ahead"
    CHECKPOINT_AHEAD = "checkpoint_ahead"
    IMPLEMENTATION_AHEAD = "implementation_ahead"
    OPERATOR_AHEAD = "operator_ahead"
    UNCLASSIFIED_AHEAD = "unclassified_ahead"
    MIXED_AHEAD = "mixed_ahead"
    BEHIND = "behind"
    FORKED = "forked"
    MISSING = "missing"
    UNRECORDED = "unrecorded"


class DriverLiveness(_ClosedStringEnum):
    """Observed driver state; never inferred from manifest status alone."""

    ALIVE = "alive"
    ORPHANED = "orphaned"
    INDETERMINATE = "indeterminate"
    NONE = "none"


class RunStatus(_ClosedStringEnum):
    """Persistable run lifecycle values."""

    RUNNING = "running"
    PARKED = "parked"
    DONE = "done"
    ABORTED = "aborted"
    FAILED = "failed"


class StepStatus(_ClosedStringEnum):
    """Persistable step lifecycle values."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    PARKED = "parked"
    HALTED = "halted"
    SKIPPED = "skipped"


class ResponseState(_ClosedStringEnum):
    PENDING = "pending"
    CONSUMED = "consumed"


class GitEntryKind(_ClosedStringEnum):
    """Kinds Git or the filesystem can expose for an observed path."""

    ABSENT = "absent"
    REGULAR = "regular"
    SYMLINK = "symlink"
    GITLINK = "gitlink"
    DIRECTORY = "directory"
    UNKNOWN = "unknown"


class GitDelta(_ClosedStringEnum):
    """Change in one state plane relative to the preceding plane."""

    UNCHANGED = "unchanged"
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    TYPE_CHANGED = "type_changed"
    MODE_CHANGED = "mode_changed"
    UNMERGED = "unmerged"
    CONFLICTED = "conflicted"
    UNTRACKED = "untracked"
    IGNORED = "ignored"
    UNKNOWN = "unknown"


class RenamePlane(_ClosedStringEnum):
    """The Git state transition in which a rename was observed."""

    INDEX = "index"
    WORKTREE = "worktree"


class CommitKind(_ClosedStringEnum):
    """Evidence-backed ownership/role of a commit in a recovery range."""

    ENGINE_BOOKKEEPING = "engine_bookkeeping"
    CHECKPOINT = "checkpoint"
    PHASE = "phase"
    FIX = "fix"
    OPERATOR = "operator"
    UNKNOWN = "unknown"


class PathChangeKind(_ClosedStringEnum):
    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"
    COPIED = "copied"
    TYPE_CHANGED = "type_changed"
    UNMERGED = "unmerged"


class RecoveryCause(_ClosedStringEnum):
    """Why a run needs recovery, independent of what should happen next."""

    NONE = "none"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    QUOTA_EXHAUSTED = "quota_exhausted"
    PROCESS_LOST = "process_lost"
    ARTIFACT_INVALID = "artifact_invalid"
    PRECONDITION_UNSATISFIED = "precondition_unsatisfied"
    WORKTREE_PARTIAL = "worktree_partial"
    BRANCH_AHEAD = "branch_ahead"
    BRANCH_DIVERGED = "branch_diverged"
    STATE_INCONSISTENT = "state_inconsistent"
    POLICY_DENIED = "policy_denied"
    INTERNAL_ERROR = "internal_error"


class RecoveryDisposition(_ClosedStringEnum):
    """The recovery strategy selected for a cause."""

    CONTINUE = "continue"
    RETRY = "retry"
    RESUME_SESSION = "resume_session"
    EDIT_THEN_RETRY = "edit_then_retry"
    RESTART_FROM_CHECKPOINT = "restart_from_checkpoint"
    ADOPT_COMMITS = "adopt_commits"
    SNAPSHOT_AND_RESTART = "snapshot_and_restart"
    RESTORE_SNAPSHOT = "restore_snapshot"
    CONTINUE_ON_RECOVERY_BRANCH = "continue_on_recovery_branch"
    REBUILD_PROJECTION = "rebuild_projection"
    HUMAN_DECISION = "human_decision"
    ABORT_ONLY = "abort_only"


class RecoveryActionKind(_ClosedStringEnum):
    """Executable actions a planner may expose to an operator or command."""

    RETRY = "retry"
    RESUME_SESSION = "resume_session"
    EDIT_THEN_RETRY = "edit_then_retry"
    RESTART_FROM_CHECKPOINT = "restart_from_checkpoint"
    ADOPT_COMMITS = "adopt_commits"
    SNAPSHOT_AND_RESTART = "snapshot_and_restart"
    RESTORE_SNAPSHOT = "restore_snapshot"
    CONTINUE_ON_RECOVERY_BRANCH = "continue_on_recovery_branch"
    REBUILD_PROJECTION = "rebuild_projection"
    HUMAN_DECISION = "human_decision"
    ABORT = "abort"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GitEntryVersion(_FrozenModel):
    """One path as represented in HEAD, the index, or the working tree.

    Every present, known entry carries both a mode and a content/object identity.
    Symlink targets are recorded as data and are never dereferenced.  ``UNKNOWN``
    is explicit evidence degradation and must explain why identification failed.
    """

    kind: GitEntryKind
    mode: str | None = None
    object_id: str | None = None
    symlink_target: str | None = None
    unknown_reason: str | None = None

    @model_validator(mode="after")
    def _metadata_matches_kind(self) -> "GitEntryVersion":
        if self.kind is GitEntryKind.ABSENT:
            if any((self.mode, self.object_id, self.symlink_target, self.unknown_reason)):
                raise ValueError("an absent Git entry cannot carry metadata")
            return self
        if self.kind is GitEntryKind.UNKNOWN:
            if not self.unknown_reason:
                raise ValueError("an unknown Git entry must explain missing evidence")
            if self.symlink_target is not None:
                raise ValueError("an unknown Git entry cannot claim a symlink target")
            return self
        if not self.mode or not self.object_id:
            raise ValueError("a present Git entry requires mode and object_id")
        if self.unknown_reason is not None:
            raise ValueError("a known Git entry cannot carry unknown_reason")
        expected_mode = {
            GitEntryKind.SYMLINK: "120000",
            GitEntryKind.GITLINK: "160000",
            GitEntryKind.DIRECTORY: "040000",
        }.get(self.kind)
        if expected_mode is not None and self.mode != expected_mode:
            raise ValueError(f"{self.kind.value} entries require mode {expected_mode}")
        if self.kind is GitEntryKind.REGULAR and self.mode not in {"100644", "100755"}:
            raise ValueError("regular entries require mode 100644 or 100755")
        if self.kind is GitEntryKind.SYMLINK:
            if self.symlink_target is None:
                raise ValueError("a symlink observation must record its target")
        elif self.symlink_target is not None:
            raise ValueError("only a symlink observation may carry a target")
        return self


ABSENT_GIT_ENTRY = GitEntryVersion(kind=GitEntryKind.ABSENT)


def derive_git_delta(
    left: GitEntryVersion,
    right: GitEntryVersion,
    *,
    untracked: bool = False,
) -> GitDelta:
    """Derive a plane delta from versions; callers do not supply trusted labels."""

    if left == right:
        return GitDelta.UNCHANGED
    if GitEntryKind.UNKNOWN in {left.kind, right.kind}:
        return GitDelta.UNKNOWN
    if left.kind is GitEntryKind.ABSENT:
        return GitDelta.UNTRACKED if untracked else GitDelta.ADDED
    if right.kind is GitEntryKind.ABSENT:
        return GitDelta.DELETED
    if left.kind is not right.kind:
        return GitDelta.TYPE_CHANGED
    if left.mode != right.mode:
        return GitDelta.MODE_CHANGED
    if left.object_id != right.object_id or left.symlink_target != right.symlink_target:
        return GitDelta.MODIFIED
    return GitDelta.UNCHANGED


class GitRenameObservation(_FrozenModel):
    """Paired source evidence for one destination-path rename.

    A rename label is not trusted by itself.  It identifies the plane in which
    the source disappeared, records both source versions for that transition,
    and retains Git's similarity score.  The destination transition is held by
    the containing :class:`GitEntryObservation` and validated against this
    evidence there.
    """

    source_path: str
    plane: RenamePlane
    source_before: GitEntryVersion
    source_after: GitEntryVersion
    similarity: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _source_transition_is_a_deletion(self) -> "GitRenameObservation":
        _validate_repo_relative_path(self.source_path, field="rename source_path")
        if self.source_before.kind in {GitEntryKind.ABSENT, GitEntryKind.UNKNOWN}:
            raise ValueError("a rename source must be identified and present before")
        if self.source_after.kind is not GitEntryKind.ABSENT:
            raise ValueError("a rename source must be absent after its transition")
        return self


class GitIndexStage(_FrozenModel):
    """One conflict-stage entry from an unmerged index."""

    stage: int = Field(ge=1, le=3)
    version: GitEntryVersion

    @model_validator(mode="after")
    def _stage_is_present(self) -> "GitIndexStage":
        if self.version.kind in {GitEntryKind.ABSENT, GitEntryKind.UNKNOWN}:
            raise ValueError("an index conflict stage must be identified and present")
        return self


class GitEntryObservation(_FrozenModel):
    """Independent, self-consistent HEAD/index/worktree state for one path."""

    path: str
    head: GitEntryVersion = ABSENT_GIT_ENTRY
    index: GitEntryVersion = ABSENT_GIT_ENTRY
    worktree: GitEntryVersion = ABSENT_GIT_ENTRY
    index_delta: GitDelta = GitDelta.UNCHANGED
    worktree_delta: GitDelta = GitDelta.UNCHANGED
    index_stages: tuple[GitIndexStage, ...] = ()
    rename: GitRenameObservation | None = None
    protected: bool = False

    @model_validator(mode="after")
    def _entry_contract(self) -> "GitEntryObservation":
        _validate_repo_relative_path(self.path, field="path")
        if self.rename is not None and self.rename.source_path == self.path:
            raise ValueError("rename source_path must differ from destination path")
        if self.index_stages:
            stages = tuple(stage.stage for stage in self.index_stages)
            if len(set(stages)) != len(stages):
                raise ValueError("index conflict stages must be unique")
            if self.index.kind is not GitEntryKind.ABSENT:
                raise ValueError("an unmerged index has no stage-0 entry")
            if self.index_delta is not GitDelta.UNMERGED:
                raise ValueError("conflict stages require index_delta='unmerged'")
            if self.worktree_delta is not GitDelta.CONFLICTED:
                raise ValueError("an unmerged path requires worktree_delta='conflicted'")
            if self.rename is not None:
                raise ValueError("an unmerged path cannot also be declared renamed")
            return self
        if self.index_delta in {GitDelta.UNMERGED, GitDelta.CONFLICTED}:
            raise ValueError("unmerged index deltas require conflict stages")
        if self.worktree_delta in {GitDelta.UNMERGED, GitDelta.CONFLICTED}:
            raise ValueError("conflicted worktree deltas require conflict stages")

        expected_index = derive_git_delta(self.head, self.index)
        expected_worktree = derive_git_delta(self.index, self.worktree, untracked=True)
        renamed_deltas = sum(
            delta is GitDelta.RENAMED
            for delta in (self.index_delta, self.worktree_delta)
        )
        if self.rename is not None:
            if renamed_deltas != 1:
                raise ValueError("paired rename evidence requires exactly one renamed delta")
            if self.rename.plane is RenamePlane.INDEX:
                if self.index_delta is not GitDelta.RENAMED:
                    raise ValueError("index-plane rename requires index_delta='renamed'")
                if expected_index is not GitDelta.ADDED:
                    raise ValueError("index rename destination must be added in the index")
                if self.worktree_delta is not expected_worktree:
                    raise ValueError("worktree_delta contradicts index/worktree evidence")
                destination_after = self.index
            else:
                if self.worktree_delta is not GitDelta.RENAMED:
                    raise ValueError("worktree-plane rename requires worktree_delta='renamed'")
                if expected_worktree is not GitDelta.UNTRACKED:
                    raise ValueError("worktree rename destination must be newly present")
                if self.index_delta is not expected_index:
                    raise ValueError("index_delta contradicts HEAD/index evidence")
                destination_after = self.worktree
            if self.rename.source_before.kind is not destination_after.kind:
                raise ValueError("rename source and destination kinds must agree")
            if (
                self.rename.similarity == 100
                and self.rename.source_before.object_id != destination_after.object_id
            ):
                raise ValueError("100% rename evidence requires identical object content")
            return self
        if renamed_deltas:
            raise ValueError("a renamed delta requires paired source evidence")
        if self.index_delta is not expected_index:
            raise ValueError("index_delta contradicts HEAD/index evidence")
        if self.worktree_delta is GitDelta.IGNORED:
            if not (
                self.index.kind is GitEntryKind.ABSENT
                and self.worktree.kind is not GitEntryKind.ABSENT
            ):
                raise ValueError("ignored worktree evidence must be untracked")
        elif self.worktree_delta is not expected_worktree:
            raise ValueError("worktree_delta contradicts index/worktree evidence")
        return self


class GitCommitPathChange(_FrozenModel):
    """One path-level change made by a commit in a recovery range."""

    kind: PathChangeKind
    path: str
    previous_path: str | None = None
    protected: bool = False
    approved_artifact: bool = False

    @model_validator(mode="after")
    def _path_contract(self) -> "GitCommitPathChange":
        _validate_repo_relative_path(self.path, field="path")
        if self.kind in {PathChangeKind.RENAMED, PathChangeKind.COPIED}:
            if self.previous_path is None:
                raise ValueError("renamed/copied changes require previous_path")
            _validate_repo_relative_path(self.previous_path, field="previous_path")
        elif self.previous_path is not None:
            raise ValueError("previous_path is valid only for renamed/copied changes")
        return self


class GitCommitObservation(_FrozenModel):
    """Auditable identity and change evidence for one observed commit."""

    sha: str
    parents: tuple[str, ...]
    author_name: str = Field(min_length=1)
    author_email: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    changed_paths: tuple[GitCommitPathChange, ...] = ()
    kind: CommitKind
    attempt_id: str | None = None
    phase_id: str | None = None
    checkpoint_id: str | None = None
    classification_evidence: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _commit_contract(self) -> "GitCommitObservation":
        _validate_sha(self.sha, field="sha")
        for parent in self.parents:
            _validate_sha(parent, field="parent")
        if len(set(self.parents)) != len(self.parents):
            raise ValueError("commit parents must be unique")
        paths = tuple(change.path for change in self.changed_paths)
        if len(set(paths)) != len(paths):
            raise ValueError("commit changed paths must be unique")
        if self.kind is CommitKind.CHECKPOINT:
            if not self.phase_id or not self.checkpoint_id:
                raise ValueError("checkpoint commits require phase_id and checkpoint_id")
        elif self.checkpoint_id is not None:
            raise ValueError("checkpoint_id is valid only for checkpoint commits")
        if self.kind in {CommitKind.PHASE, CommitKind.FIX} and not self.phase_id:
            raise ValueError("phase/fix commits require phase_id")
        return self


_AHEAD_RELATIONS = frozenset(
    {
        BranchRelation.ENGINE_BOOKKEEPING_AHEAD,
        BranchRelation.CHECKPOINT_AHEAD,
        BranchRelation.IMPLEMENTATION_AHEAD,
        BranchRelation.OPERATOR_AHEAD,
        BranchRelation.UNCLASSIFIED_AHEAD,
        BranchRelation.MIXED_AHEAD,
    }
)


class GitObservation(_FrozenModel):
    """Repository evidence consumed by the future recovery planner."""

    checked_out_branch: str | None
    head_sha: str
    run_branch: str
    run_branch_sha: str | None
    recorded_sha: str | None
    branch_relation: BranchRelation
    merge_base_sha: str | None = None
    run_branch_commits: tuple[GitCommitObservation, ...] = ()
    index_fingerprint: str
    worktree_fingerprint: str
    dirty_entries: tuple[GitEntryObservation, ...] = ()

    @model_validator(mode="after")
    def _branch_contract(self) -> "GitObservation":
        _validate_sha(self.head_sha, field="head_sha")
        if self.run_branch_sha is not None:
            _validate_sha(self.run_branch_sha, field="run_branch_sha")
        if self.recorded_sha is not None:
            _validate_sha(self.recorded_sha, field="recorded_sha")
        if self.merge_base_sha is not None:
            _validate_sha(self.merge_base_sha, field="merge_base_sha")

        relation = self.branch_relation
        if relation is BranchRelation.MISSING:
            if self.run_branch_sha is not None or self.run_branch_commits:
                raise ValueError("a missing run branch cannot have a tip or commits")
        elif relation is BranchRelation.UNRECORDED:
            if self.run_branch_sha is None or self.recorded_sha is not None:
                raise ValueError("unrecorded requires a run tip and no recorded SHA")
            if self.run_branch_commits:
                raise ValueError("unrecorded history has no recorded range boundary")
        else:
            if self.run_branch_sha is None or self.recorded_sha is None:
                raise ValueError("a recorded branch relation requires both SHAs")

        if relation is BranchRelation.EQUAL:
            if self.run_branch_sha != self.recorded_sha:
                raise ValueError("equal relation requires identical SHAs")
            if self.run_branch_commits:
                raise ValueError("equal relation cannot carry range commits")
        elif relation in _AHEAD_RELATIONS:
            if self.run_branch_sha == self.recorded_sha:
                raise ValueError("ahead relation requires different SHAs")
            self._validate_linear_range(self.recorded_sha)
            kinds = {commit.kind for commit in self.run_branch_commits}
            if relation is BranchRelation.ENGINE_BOOKKEEPING_AHEAD and kinds != {
                CommitKind.ENGINE_BOOKKEEPING
            }:
                raise ValueError("engine-bookkeeping relation contradicts commit roles")
            if relation is BranchRelation.CHECKPOINT_AHEAD and not (
                CommitKind.CHECKPOINT in kinds
                and kinds <= {CommitKind.CHECKPOINT, CommitKind.ENGINE_BOOKKEEPING}
            ):
                raise ValueError("checkpoint relation contradicts commit roles")
            implementation_kinds = {
                CommitKind.PHASE,
                CommitKind.FIX,
                CommitKind.CHECKPOINT,
                CommitKind.ENGINE_BOOKKEEPING,
            }
            has_implementation = bool(kinds & {CommitKind.PHASE, CommitKind.FIX})
            if relation is BranchRelation.IMPLEMENTATION_AHEAD and not (
                has_implementation and kinds <= implementation_kinds
            ):
                raise ValueError("implementation relation contradicts commit roles")
            if relation is BranchRelation.OPERATOR_AHEAD and kinds != {CommitKind.OPERATOR}:
                raise ValueError("operator relation contradicts commit roles")
            if relation is BranchRelation.UNCLASSIFIED_AHEAD and kinds != {
                CommitKind.UNKNOWN
            }:
                raise ValueError("unclassified relation contradicts commit roles")
            if relation is BranchRelation.MIXED_AHEAD:
                specialized = (
                    kinds == {CommitKind.ENGINE_BOOKKEEPING}
                    or (
                        CommitKind.CHECKPOINT in kinds
                        and kinds
                        <= {CommitKind.CHECKPOINT, CommitKind.ENGINE_BOOKKEEPING}
                    )
                    or (has_implementation and kinds <= implementation_kinds)
                    or kinds == {CommitKind.OPERATOR}
                    or kinds == {CommitKind.UNKNOWN}
                )
                if len(kinds) < 2 or specialized:
                    raise ValueError("mixed relation contradicts commit roles")
        elif relation is BranchRelation.BEHIND:
            if self.run_branch_sha == self.recorded_sha:
                raise ValueError("behind relation requires different SHAs")
            if self.merge_base_sha != self.run_branch_sha:
                raise ValueError("behind relation requires merge base at run tip")
            if self.run_branch_commits:
                raise ValueError("behind relation has no run-only commits")
        elif relation is BranchRelation.FORKED:
            if self.run_branch_sha == self.recorded_sha:
                raise ValueError("forked relation requires different SHAs")
            if self.merge_base_sha in {None, self.run_branch_sha, self.recorded_sha}:
                raise ValueError("forked relation requires a distinct merge base")
            self._validate_linear_range(self.merge_base_sha)

        paths = tuple(entry.path for entry in self.dirty_entries)
        if len(set(paths)) != len(paths):
            raise ValueError("dirty entry paths must be unique")
        rename_sources = tuple(
            (entry.rename.plane, entry.rename.source_path)
            for entry in self.dirty_entries
            if entry.rename is not None
        )
        if len(set(rename_sources)) != len(rename_sources):
            raise ValueError("a rename source can have only one destination per plane")
        if any(
            entry.index_delta is GitDelta.UNCHANGED
            and entry.worktree_delta is GitDelta.UNCHANGED
            for entry in self.dirty_entries
        ):
            raise ValueError("dirty_entries cannot contain a clean path")
        return self

    def _validate_linear_range(self, boundary_sha: str | None) -> None:
        if boundary_sha is None or not self.run_branch_commits:
            raise ValueError("branch range requires inventoried commits")
        previous = boundary_sha
        seen: set[str] = set()
        for commit in self.run_branch_commits:
            if commit.sha in seen:
                raise ValueError("branch range commits must be unique")
            if len(commit.parents) != 1:
                raise ValueError("linear branch range cannot contain merge commits")
            if commit.parents != (previous,):
                raise ValueError("branch range is not a contiguous parent chain")
            seen.add(commit.sha)
            previous = commit.sha
        if previous != self.run_branch_sha:
            raise ValueError("branch range must end at the run-branch tip")


class StateObservation(_FrozenModel):
    """Persisted state-machine and process evidence, without policy inference."""

    run_status: RunStatus
    step_status: StepStatus | None = None
    attempt_id: str | None = None
    liveness: DriverLiveness
    pending_response_id: str | None = None
    pending_response_state: ResponseState | None = None
    last_snapshot_id: str | None = None
    artifact_fingerprint: str | None = None

    @model_validator(mode="after")
    def _response_contract(self) -> "StateObservation":
        if (self.pending_response_id is None) != (self.pending_response_state is None):
            raise ValueError("pending response id and state must appear together")
        return self


class _ActionBase(_FrozenModel):
    description: str = Field(min_length=1)
    requires_snapshot: bool
    requires_human_decision: bool


class RetryAction(_ActionBase):
    kind: Literal[RecoveryActionKind.RETRY] = RecoveryActionKind.RETRY
    operation: str = Field(min_length=1)
    attempt_id: str | None = None
    requires_snapshot: Literal[False] = False
    requires_human_decision: Literal[False] = False


class ResumeSessionAction(_ActionBase):
    kind: Literal[RecoveryActionKind.RESUME_SESSION] = RecoveryActionKind.RESUME_SESSION
    session_id: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    requires_snapshot: Literal[False] = False
    requires_human_decision: Literal[False] = False


class EditThenRetryAction(_ActionBase):
    kind: Literal[RecoveryActionKind.EDIT_THEN_RETRY] = RecoveryActionKind.EDIT_THEN_RETRY
    artifact_path: str
    expected_fingerprint: str = Field(min_length=1)
    requires_snapshot: Literal[False] = False
    requires_human_decision: Literal[False] = False

    @model_validator(mode="after")
    def _contained_artifact(self) -> "EditThenRetryAction":
        _validate_repo_relative_path(self.artifact_path, field="artifact_path")
        return self


class RestartFromCheckpointAction(_ActionBase):
    kind: Literal[RecoveryActionKind.RESTART_FROM_CHECKPOINT] = (
        RecoveryActionKind.RESTART_FROM_CHECKPOINT
    )
    checkpoint_sha: str
    step_id: str = Field(min_length=1)
    requires_snapshot: Literal[True] = True
    requires_human_decision: Literal[False] = False

    @model_validator(mode="after")
    def _valid_checkpoint(self) -> "RestartFromCheckpointAction":
        _validate_sha(self.checkpoint_sha, field="checkpoint_sha")
        return self


class AdoptCommitsAction(_ActionBase):
    kind: Literal[RecoveryActionKind.ADOPT_COMMITS] = RecoveryActionKind.ADOPT_COMMITS
    base_sha: str
    tip_sha: str
    commit_shas: tuple[str, ...] = Field(min_length=1)
    requires_snapshot: Literal[False] = False
    requires_human_decision: Literal[False] = False

    @model_validator(mode="after")
    def _valid_range(self) -> "AdoptCommitsAction":
        _validate_sha(self.base_sha, field="base_sha")
        _validate_sha(self.tip_sha, field="tip_sha")
        for sha in self.commit_shas:
            _validate_sha(sha, field="commit_sha")
        if len(set(self.commit_shas)) != len(self.commit_shas):
            raise ValueError("adopt commit SHAs must be unique")
        if self.commit_shas[-1] != self.tip_sha or self.base_sha in self.commit_shas:
            raise ValueError("adopt range must exclude base and end at tip")
        return self


class SnapshotAndRestartAction(_ActionBase):
    kind: Literal[RecoveryActionKind.SNAPSHOT_AND_RESTART] = (
        RecoveryActionKind.SNAPSHOT_AND_RESTART
    )
    target_ref: str = Field(min_length=1)
    target_sha: str
    reason: str = Field(min_length=1)
    requires_snapshot: Literal[True] = True
    requires_human_decision: Literal[False] = False

    @model_validator(mode="after")
    def _valid_target(self) -> "SnapshotAndRestartAction":
        _validate_sha(self.target_sha, field="target_sha")
        return self


class RestoreSnapshotAction(_ActionBase):
    kind: Literal[RecoveryActionKind.RESTORE_SNAPSHOT] = RecoveryActionKind.RESTORE_SNAPSHOT
    snapshot_id: str = Field(min_length=1)
    requires_snapshot: Literal[True] = True
    requires_human_decision: Literal[False] = False


class ContinueOnRecoveryBranchAction(_ActionBase):
    kind: Literal[RecoveryActionKind.CONTINUE_ON_RECOVERY_BRANCH] = (
        RecoveryActionKind.CONTINUE_ON_RECOVERY_BRANCH
    )
    branch_name: str = Field(min_length=1)
    start_sha: str
    # How the ref restoration executes (P4.1, post-review F-003): a plain
    # forced branch move is invalid for the CHECKED-OUT branch (git refuses),
    # so a behind run branch the operator is standing on restores via a pure
    # fast-forward merge instead. Additive with a back-compatible default.
    via: Literal["branch_force", "ff_merge"] = "branch_force"
    # The ref value this action was ASSESSED against, and the compare-and-swap
    # guard the rendered command carries (post-review F-003). ``None`` means
    # "the ref did not exist" — the missing-branch and fork-preservation forms,
    # which must create rather than overwrite.
    #
    # Why it is required rather than advisory: an assessment is a *photograph*.
    # The operator reads it, and runs the command it advertises some time
    # later; a resume, another operator or a driver may advance the ref in
    # between. Without the guard the advertised `git branch -f` applies a stale
    # decision to a moved ref — rewinding a tip that had advanced, or replacing
    # a ref recreated in the gap — and orphans those commits silently, which is
    # the one outcome plan §5.4's "every action is non-destructive" forbids.
    # Carrying the expected value makes git's own ref transaction refuse the
    # stale update atomically, so the failure mode becomes a loud refusal.
    expected_sha: str | None = None
    # With the guard in place this action cannot discard history: it either
    # creates a ref that did not exist, or fast-forwards one that is provably
    # still where it was observed. There is nothing for a pre-mutation snapshot
    # to preserve, and declaring one that no renderer takes was the contradiction
    # post-review F-003 named. Preservation here is *structural*, not archival.
    requires_snapshot: Literal[False] = False
    requires_human_decision: Literal[False] = False

    @model_validator(mode="after")
    def _valid_start(self) -> "ContinueOnRecoveryBranchAction":
        _validate_sha(self.start_sha, field="start_sha")
        if self.expected_sha is not None:
            _validate_sha(self.expected_sha, field="expected_sha")
            if self.expected_sha == self.start_sha:
                raise ValueError(
                    "a recovery ref restoration whose expected value already "
                    "equals its start commit would be a no-op"
                )
        if self.via == "ff_merge" and self.expected_sha is None:
            raise ValueError(
                "a fast-forward merge restoration requires the ref to exist"
            )
        return self


class RebuildProjectionAction(_ActionBase):
    kind: Literal[RecoveryActionKind.REBUILD_PROJECTION] = (
        RecoveryActionKind.REBUILD_PROJECTION
    )
    journal_path: str
    projection_path: str
    evidence_fingerprint: str = Field(min_length=1)
    requires_snapshot: Literal[True] = True
    requires_human_decision: Literal[False] = False

    @model_validator(mode="after")
    def _contained_paths(self) -> "RebuildProjectionAction":
        _validate_repo_relative_path(self.journal_path, field="journal_path")
        _validate_repo_relative_path(self.projection_path, field="projection_path")
        return self


class HumanDecisionAction(_ActionBase):
    kind: Literal[RecoveryActionKind.HUMAN_DECISION] = RecoveryActionKind.HUMAN_DECISION
    decision_id: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    requires_snapshot: Literal[False] = False
    requires_human_decision: Literal[True] = True


class AbortAction(_ActionBase):
    kind: Literal[RecoveryActionKind.ABORT] = RecoveryActionKind.ABORT
    reason: str = Field(min_length=1)
    requires_snapshot: Literal[False] = False
    requires_human_decision: Literal[False] = False


RecoveryAction: TypeAlias = Annotated[
    Union[
        RetryAction,
        ResumeSessionAction,
        EditThenRetryAction,
        RestartFromCheckpointAction,
        AdoptCommitsAction,
        SnapshotAndRestartAction,
        RestoreSnapshotAction,
        ContinueOnRecoveryBranchAction,
        RebuildProjectionAction,
        HumanDecisionAction,
        AbortAction,
    ],
    Field(discriminator="kind"),
]


class RecoveryAssessment(_FrozenModel):
    """A cause, strategy, and auditable set of executable actions."""

    cause: RecoveryCause
    disposition: RecoveryDisposition
    evidence: tuple[str, ...] = ()
    safe_actions: tuple[RecoveryAction, ...] = ()
    recommended_action: RecoveryActionKind | None = None
    progress_fingerprint: str

    @model_validator(mode="after")
    def _assessment_contract(self) -> "RecoveryAssessment":
        kinds = tuple(action.kind for action in self.safe_actions)
        if len(set(kinds)) != len(kinds):
            raise ValueError("safe recovery action kinds must be unique")
        if self.disposition is not RecoveryDisposition.CONTINUE and not kinds:
            raise ValueError("a non-continuing assessment needs a safe action")
        if self.recommended_action is not None and self.recommended_action not in kinds:
            raise ValueError("recommended_action must be one of safe_actions")
        return self


class ProgressFingerprint(_FrozenModel):
    """Stable inputs used to detect a successful no-progress recovery loop."""

    run_id: str
    current_step: str | None = None
    iteration: str | int | None = None
    attempt_id: str | None = None
    run_status: RunStatus
    step_status: StepStatus | None = None
    run_branch_sha: str | None = None
    index_fingerprint: str
    worktree_fingerprint: str
    artifact_fingerprint: str | None = None
    pending_response_id: str | None = None
    pending_response_state: ResponseState | None = None
    latest_cycle_substep: str | None = None
    # Hash of the anchor step's durable notes (#90). A verb whose only durable
    # output is a recorded verdict — e.g. the first resume of a killed dirty
    # step parking with the `--reset-interrupted` guidance — IS progress the
    # operator can act on; before this field that progress was invisible and
    # only registered by accident via the verb's own bookkeeping commit
    # (which the git plane now rightly skips). Identical re-parks re-stamp
    # byte-identical notes, so the repeat still reads unchanged.
    step_notes_fingerprint: str | None = None

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class NoProgressError(RuntimeError):
    """A mutating recovery command returned to an identical state."""

    def __init__(
        self,
        before: ProgressFingerprint,
        after: ProgressFingerprint,
        safe_actions: tuple[RecoveryAction, ...],
    ) -> None:
        if before.digest != after.digest:
            raise ValueError("NoProgressError requires identical fingerprints")
        if not safe_actions:
            raise ValueError("NoProgressError requires an executable next action")
        self.before = before
        self.after = after
        self.safe_actions = safe_actions
        self.unchanged_fields = tuple(type(before).model_fields)
        # Name each action's executable form, not only its kind (#134): the
        # kind alone ("retry, abort") sent operators to git surgery when the
        # description already said which verb re-arms the step.
        actions = "; ".join(
            f"{action.kind.value} — {action.description}" for action in safe_actions
        )
        super().__init__(
            "recovery made no progress: run/step, branch, index, worktree, "
            f"artifact, response, and cycle state are unchanged ({before.digest}); "
            f"safe next actions: {actions}"
        )


def require_progress(
    before: ProgressFingerprint,
    after: ProgressFingerprint,
    *,
    safe_actions: tuple[RecoveryAction, ...],
    legitimate_wait: bool = False,
) -> None:
    """Raise when a mutation neither progresses nor enters a legitimate wait."""

    if not legitimate_wait and before.digest == after.digest:
        raise NoProgressError(before, after, safe_actions)


def _validate_repo_relative_path(value: str, *, field: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or value == ".":
        raise ValueError(f"{field} must be a contained repository-relative path")


_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def _validate_sha(value: str, *, field: str) -> None:
    if not _SHA_RE.fullmatch(value):
        raise ValueError(f"{field} must be a full 40- or 64-hex object id")


def fingerprint_data(value: Any) -> str:
    """Return a stable SHA-256 for JSON-compatible observation data."""

    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "ABSENT_GIT_ENTRY",
    "AbortAction",
    "AdoptCommitsAction",
    "BranchRelation",
    "CommitKind",
    "ContinueOnRecoveryBranchAction",
    "DriverLiveness",
    "EditThenRetryAction",
    "GitCommitObservation",
    "GitCommitPathChange",
    "GitDelta",
    "GitEntryKind",
    "GitEntryObservation",
    "GitEntryVersion",
    "GitIndexStage",
    "GitObservation",
    "GitRenameObservation",
    "HumanDecisionAction",
    "NoProgressError",
    "PathChangeKind",
    "ProgressFingerprint",
    "RebuildProjectionAction",
    "RecoveryAction",
    "RecoveryActionKind",
    "RecoveryAssessment",
    "RecoveryCause",
    "RecoveryDisposition",
    "RenamePlane",
    "ResponseState",
    "RestartFromCheckpointAction",
    "RestoreSnapshotAction",
    "ResumeSessionAction",
    "RetryAction",
    "RunStatus",
    "SnapshotAndRestartAction",
    "StateObservation",
    "StepStatus",
    "derive_git_delta",
    "fingerprint_data",
    "require_progress",
]

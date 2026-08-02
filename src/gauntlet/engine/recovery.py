"""Recovery observations and contracts shared by future recovery workflows.

This module is intentionally side-effect free.  P1 establishes the vocabulary
used to describe repository and state-machine evidence; later phases will make
``status``, ``resume``, ``recover``, and ``rollback`` consume it through a
single planner/executor.  Keeping these models independent of the existing
rewind implementation lets the migration proceed without changing production
recovery behaviour before lossless snapshots exist.
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _ClosedStringEnum(str, Enum):
    """A JSON-friendly closed enum with readable ``str()`` output."""

    def __str__(self) -> str:
        return self.value


class BranchRelation(_ClosedStringEnum):
    """Relationship between the run branch and its recorded boundary."""

    EQUAL = "equal"
    ENGINE_BOOKKEEPING_AHEAD = "engine_bookkeeping_ahead"
    CHECKPOINT_AHEAD = "checkpoint_ahead"
    OPERATOR_AHEAD = "operator_ahead"
    MIXED_AHEAD = "mixed_ahead"
    BEHIND = "behind"
    FORKED = "forked"
    MISSING = "missing"


class DriverLiveness(_ClosedStringEnum):
    """Observed driver state; never inferred from manifest status alone."""

    ALIVE = "alive"
    ORPHANED = "orphaned"
    INDETERMINATE = "indeterminate"
    NONE = "none"


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
    UNTRACKED = "untracked"
    IGNORED = "ignored"
    UNKNOWN = "unknown"


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
    HUMAN_DECISION = "human_decision"
    ABORT = "abort"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class GitEntryVersion(_FrozenModel):
    """One path as represented in HEAD, the index, or the working tree.

    ``object_id`` is normally the Git object ID.  A future observer may use a
    content fingerprint when an object has not yet been written; callers must
    compare only values produced by the same observer.  Symlink targets are
    data and are never dereferenced.
    """

    kind: GitEntryKind
    mode: str | None = None
    object_id: str | None = None
    symlink_target: str | None = None

    @model_validator(mode="after")
    def _metadata_matches_kind(self) -> "GitEntryVersion":
        if self.kind is GitEntryKind.ABSENT:
            if any((self.mode, self.object_id, self.symlink_target)):
                raise ValueError("an absent Git entry cannot carry metadata")
            return self
        if self.kind is GitEntryKind.SYMLINK:
            if self.symlink_target is None:
                raise ValueError("a symlink observation must record its target")
        elif self.symlink_target is not None:
            raise ValueError("only a symlink observation may carry a target")
        return self


ABSENT_GIT_ENTRY = GitEntryVersion(kind=GitEntryKind.ABSENT)


class GitIndexStage(_FrozenModel):
    """One conflict-stage entry from an unmerged index."""

    stage: int = Field(ge=1, le=3)
    version: GitEntryVersion

    @model_validator(mode="after")
    def _stage_is_present(self) -> "GitIndexStage":
        if self.version.kind is GitEntryKind.ABSENT:
            raise ValueError("an index conflict stage cannot be absent")
        return self


class GitEntryObservation(_FrozenModel):
    """Independent HEAD/index/worktree state for one repository-relative path."""

    path: str
    head: GitEntryVersion = ABSENT_GIT_ENTRY
    index: GitEntryVersion = ABSENT_GIT_ENTRY
    worktree: GitEntryVersion = ABSENT_GIT_ENTRY
    index_delta: GitDelta = GitDelta.UNCHANGED
    worktree_delta: GitDelta = GitDelta.UNCHANGED
    index_stages: tuple[GitIndexStage, ...] = ()
    renamed_from: str | None = None
    protected: bool = False

    @model_validator(mode="after")
    def _entry_contract(self) -> "GitEntryObservation":
        _validate_repo_relative_path(self.path, field="path")
        if self.renamed_from is not None:
            _validate_repo_relative_path(self.renamed_from, field="renamed_from")
        if self.index_stages:
            stages = tuple(stage.stage for stage in self.index_stages)
            if len(set(stages)) != len(stages):
                raise ValueError("index conflict stages must be unique")
            if self.index_delta is not GitDelta.UNMERGED:
                raise ValueError("conflict stages require index_delta='unmerged'")
        if self.index_delta is GitDelta.UNMERGED and not self.index_stages:
            raise ValueError("index_delta='unmerged' requires conflict stages")
        if self.renamed_from is not None and not (
            self.index_delta is GitDelta.RENAMED
            or self.worktree_delta is GitDelta.RENAMED
        ):
            raise ValueError("renamed_from requires a renamed delta")
        return self


class GitObservation(_FrozenModel):
    """Repository evidence consumed by the future recovery planner."""

    checked_out_branch: str | None
    head_sha: str
    run_branch: str
    run_branch_sha: str | None
    recorded_sha: str | None
    branch_relation: BranchRelation
    index_fingerprint: str
    worktree_fingerprint: str
    dirty_entries: tuple[GitEntryObservation, ...] = ()

    @model_validator(mode="after")
    def _branch_contract(self) -> "GitObservation":
        if self.branch_relation is BranchRelation.MISSING:
            if self.run_branch_sha is not None:
                raise ValueError("a missing run branch cannot have a tip SHA")
        elif self.run_branch_sha is None:
            raise ValueError("a non-missing run branch must have a tip SHA")
        paths = tuple(entry.path for entry in self.dirty_entries)
        if len(set(paths)) != len(paths):
            raise ValueError("dirty entry paths must be unique")
        return self


class StateObservation(_FrozenModel):
    """Persisted state-machine and process evidence, without policy inference."""

    run_status: str
    step_status: str | None = None
    attempt_id: str | None = None
    liveness: DriverLiveness
    pending_response_id: str | None = None
    last_snapshot_id: str | None = None
    artifact_fingerprint: str | None = None


class RecoveryAction(_FrozenModel):
    """One concrete action that can be selected from an assessment."""

    kind: RecoveryActionKind
    description: str = Field(min_length=1)
    requires_snapshot: bool = False
    requires_human_decision: bool = False
    parameters: tuple[tuple[str, str], ...] = ()

    @model_validator(mode="after")
    def _unique_parameters(self) -> "RecoveryAction":
        names = tuple(name for name, _value in self.parameters)
        if len(set(names)) != len(names):
            raise ValueError("recovery action parameter names must be unique")
        if (
            self.kind is RecoveryActionKind.HUMAN_DECISION
            and not self.requires_human_decision
        ):
            raise ValueError("human_decision actions must require a human decision")
        return self


class RecoveryAssessment(_FrozenModel):
    """A cause, strategy, and auditable set of safe executable actions."""

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
    run_status: str
    step_status: str | None = None
    run_branch_sha: str | None = None
    index_fingerprint: str
    worktree_fingerprint: str
    artifact_fingerprint: str | None = None
    pending_response_id: str | None = None
    pending_response_state: str | None = None
    latest_cycle_substep: str | None = None

    @property
    def digest(self) -> str:
        """A deterministic SHA-256 over every progress-bearing field."""

        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(payload).hexdigest()}"


class NoProgressError(RuntimeError):
    """A mutating recovery command returned to an identical state.

    The exception carries the complete before/after evidence and at least one
    executable next action.  P4 will wire this contract into public commands;
    P1 only makes the fail-loud outcome precise and testable.
    """

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
        actions = ", ".join(action.kind.value for action in safe_actions)
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


def fingerprint_data(value: Any) -> str:
    """Return a stable SHA-256 for JSON-compatible observation data.

    This helper is intentionally generic enough for test fixtures and early
    observers.  Git snapshot object IDs remain separate evidence in P2.
    """

    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


__all__ = [
    "ABSENT_GIT_ENTRY",
    "BranchRelation",
    "DriverLiveness",
    "GitDelta",
    "GitEntryKind",
    "GitEntryObservation",
    "GitEntryVersion",
    "GitIndexStage",
    "GitObservation",
    "NoProgressError",
    "ProgressFingerprint",
    "RecoveryAction",
    "RecoveryActionKind",
    "RecoveryAssessment",
    "RecoveryCause",
    "RecoveryDisposition",
    "StateObservation",
    "fingerprint_data",
    "require_progress",
]

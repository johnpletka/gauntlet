"""P1 recovery vocabulary, invariants, and Git-state harness contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gauntlet.engine.recovery import (
    AbortAction,
    AdoptCommitsAction,
    BranchRelation,
    CommitKind,
    ContinueOnRecoveryBranchAction,
    DriverLiveness,
    EditThenRetryAction,
    GitCommitObservation,
    GitCommitPathChange,
    GitDelta,
    GitEntryKind,
    GitEntryObservation,
    GitEntryVersion,
    GitObservation,
    GitRenameObservation,
    HumanDecisionAction,
    NoProgressError,
    PathChangeKind,
    ProgressFingerprint,
    RebuildProjectionAction,
    RecoveryAction,
    RecoveryActionKind,
    RecoveryAssessment,
    RecoveryCause,
    RecoveryDisposition,
    RenamePlane,
    ResponseState,
    RestoreSnapshotAction,
    ResumeSessionAction,
    RetryAction,
    RunStatus,
    SnapshotAndRestartAction,
    StateObservation,
    StepStatus,
    fingerprint_data,
    require_progress,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40


def _action(kind: RecoveryActionKind) -> RecoveryAction:
    common = {"description": f"execute {kind.value}"}
    if kind is RecoveryActionKind.RETRY:
        return RetryAction(**common, operation="adapter_call", attempt_id="attempt-2")
    if kind is RecoveryActionKind.RESUME_SESSION:
        return ResumeSessionAction(**common, session_id="session-1", step_id="implement")
    if kind is RecoveryActionKind.EDIT_THEN_RETRY:
        return EditThenRetryAction(
            **common,
            artifact_path="runs/demo/plan.md",
            expected_fingerprint="sha256:artifact",
        )
    if kind is RecoveryActionKind.ADOPT_COMMITS:
        return AdoptCommitsAction(
            **common,
            base_sha=SHA_A,
            tip_sha=SHA_B,
            commit_shas=(SHA_B,),
        )
    if kind is RecoveryActionKind.SNAPSHOT_AND_RESTART:
        return SnapshotAndRestartAction(
            **common,
            target_ref="refs/heads/gauntlet/demo",
            target_sha=SHA_A,
            reason="partial work",
        )
    if kind is RecoveryActionKind.RESTORE_SNAPSHOT:
        return RestoreSnapshotAction(**common, snapshot_id="snapshot-1")
    if kind is RecoveryActionKind.CONTINUE_ON_RECOVERY_BRANCH:
        return ContinueOnRecoveryBranchAction(
            **common,
            branch_name="gauntlet/recovery/demo",
            start_sha=SHA_B,
        )
    if kind is RecoveryActionKind.REBUILD_PROJECTION:
        return RebuildProjectionAction(
            **common,
            journal_path=".gauntlet/state/demo/events",
            projection_path="runs/demo/run-1/manifest.json",
            evidence_fingerprint="sha256:evidence",
        )
    if kind is RecoveryActionKind.HUMAN_DECISION:
        return HumanDecisionAction(
            **common,
            decision_id="upstream-conflict-1",
            prompt="Choose how to resolve the approved artifact conflict",
        )
    if kind is RecoveryActionKind.ABORT:
        return AbortAction(**common, reason="retain evidence and stop")
    raise AssertionError(f"test action factory missing {kind}")


def _progress(**updates) -> ProgressFingerprint:
    values = {
        "run_id": "run-1",
        "current_step": "implement",
        "iteration": "P1",
        "attempt_id": "attempt-2",
        "run_status": RunStatus.RUNNING,
        "step_status": StepStatus.INTERRUPTED,
        "run_branch_sha": SHA_A,
        "index_fingerprint": "sha256:index",
        "worktree_fingerprint": "sha256:worktree",
        "artifact_fingerprint": "sha256:artifact",
        "pending_response_id": None,
        "pending_response_state": None,
        "latest_cycle_substep": "fix",
    }
    values.update(updates)
    return ProgressFingerprint(**values)


def _commit(
    sha: str,
    parent: str | tuple[str, ...],
    kind: CommitKind,
    *,
    path: str = "feature.py",
) -> GitCommitObservation:
    metadata = {}
    if kind is CommitKind.CHECKPOINT:
        metadata = {"phase_id": "P1", "checkpoint_id": "cp-1", "attempt_id": "a-1"}
    elif kind in {CommitKind.PHASE, CommitKind.FIX}:
        metadata = {"phase_id": "P1", "attempt_id": "a-1"}
    return GitCommitObservation(
        sha=sha,
        parents=(parent,) if isinstance(parent, str) else parent,
        author_name="Fixture",
        author_email="fixture@gauntlet.local",
        subject=f"{kind.value} commit",
        changed_paths=(
            GitCommitPathChange(kind=PathChangeKind.MODIFIED, path=path),
        ),
        kind=kind,
        classification_evidence=("author, subject, and changed paths inspected",),
        **metadata,
    )


@pytest.mark.parametrize(
    ("incident", "cause", "disposition", "action"),
    [
        (
            "invalid YAML or phase label",
            RecoveryCause.ARTIFACT_INVALID,
            RecoveryDisposition.EDIT_THEN_RETRY,
            RecoveryActionKind.EDIT_THEN_RETRY,
        ),
        (
            "provider outage",
            RecoveryCause.PROVIDER_UNAVAILABLE,
            RecoveryDisposition.RETRY,
            RecoveryActionKind.RETRY,
        ),
        (
            "quota exhausted",
            RecoveryCause.QUOTA_EXHAUSTED,
            RecoveryDisposition.RESUME_SESSION,
            RecoveryActionKind.RESUME_SESSION,
        ),
        (
            "driver killed with partial work",
            RecoveryCause.PROCESS_LOST,
            RecoveryDisposition.SNAPSHOT_AND_RESTART,
            RecoveryActionKind.SNAPSHOT_AND_RESTART,
        ),
        (
            "commit landed before manifest flush",
            RecoveryCause.BRANCH_AHEAD,
            RecoveryDisposition.ADOPT_COMMITS,
            RecoveryActionKind.ADOPT_COMMITS,
        ),
        (
            "forked branch",
            RecoveryCause.BRANCH_DIVERGED,
            RecoveryDisposition.CONTINUE_ON_RECOVERY_BRANCH,
            RecoveryActionKind.CONTINUE_ON_RECOVERY_BRANCH,
        ),
        (
            "rebuildable manifest projection",
            RecoveryCause.STATE_INCONSISTENT,
            RecoveryDisposition.REBUILD_PROJECTION,
            RecoveryActionKind.REBUILD_PROJECTION,
        ),
        (
            "ambiguous manifest and Git evidence",
            RecoveryCause.STATE_INCONSISTENT,
            RecoveryDisposition.RESTORE_SNAPSHOT,
            RecoveryActionKind.RESTORE_SNAPSHOT,
        ),
        (
            "approved artifact conflict",
            RecoveryCause.PRECONDITION_UNSATISFIED,
            RecoveryDisposition.HUMAN_DECISION,
            RecoveryActionKind.HUMAN_DECISION,
        ),
    ],
)
def test_incident_classes_have_orthogonal_causes_and_executable_actions(
    incident, cause, disposition, action
):
    selected = _action(action)
    assessment = RecoveryAssessment(
        cause=cause,
        disposition=disposition,
        evidence=(incident,),
        safe_actions=(selected, _action(RecoveryActionKind.ABORT)),
        recommended_action=action,
        progress_fingerprint=_progress().digest,
    )

    assert assessment.cause is cause
    assert assessment.disposition is disposition
    assert assessment.recommended_action is action
    assert assessment.safe_actions[0].kind is action
    assert assessment.safe_actions[0].requires_human_decision is (
        action is RecoveryActionKind.HUMAN_DECISION
    )


@pytest.mark.parametrize(
    "enum_type",
    [
        BranchRelation,
        CommitKind,
        DriverLiveness,
        GitDelta,
        GitEntryKind,
        PathChangeKind,
        RecoveryCause,
        RecoveryDisposition,
        RecoveryActionKind,
        ResponseState,
        RunStatus,
        StepStatus,
    ],
)
def test_recovery_enums_are_closed(enum_type):
    with pytest.raises(ValueError):
        enum_type("future-typo")


def test_state_observations_are_immutable_closed_and_pair_response_evidence():
    state = StateObservation(
        run_status=RunStatus.RUNNING,
        step_status=StepStatus.INTERRUPTED,
        attempt_id="a-1",
        liveness=DriverLiveness.ORPHANED,
    )
    with pytest.raises(ValidationError):
        state.run_status = RunStatus.DONE
    with pytest.raises(ValidationError):
        StateObservation(run_status="runnng", liveness=DriverLiveness.NONE)
    with pytest.raises(ValidationError):
        StateObservation(
            run_status=RunStatus.PARKED,
            liveness=DriverLiveness.NONE,
            pending_response_id="response-1",
        )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"kind": GitEntryKind.REGULAR}, "requires mode and object_id"),
        (
            {"kind": GitEntryKind.SYMLINK, "mode": "120000", "object_id": SHA_A},
            "must record its target",
        ),
        ({"kind": GitEntryKind.UNKNOWN}, "must explain missing evidence"),
        (
            {"kind": GitEntryKind.REGULAR, "mode": "120000", "object_id": SHA_A},
            "regular entries require mode",
        ),
    ],
)
def test_present_entry_versions_require_identifying_consistent_metadata(kwargs, message):
    with pytest.raises(ValidationError, match=message):
        GitEntryVersion(**kwargs)


def test_entry_deltas_are_validated_against_all_three_state_planes():
    original = GitEntryVersion(kind=GitEntryKind.REGULAR, mode="100644", object_id=SHA_A)
    changed = GitEntryVersion(kind=GitEntryKind.REGULAR, mode="100644", object_id=SHA_B)

    with pytest.raises(ValidationError, match="index_delta contradicts"):
        GitEntryObservation(
            path="feature.py",
            head=original,
            index=original,
            worktree=original,
            index_delta=GitDelta.MODIFIED,
        )
    with pytest.raises(ValidationError, match="worktree_delta contradicts"):
        GitEntryObservation(
            path="feature.py",
            head=original,
            index=changed,
            worktree=original,
            index_delta=GitDelta.MODIFIED,
            worktree_delta=GitDelta.UNCHANGED,
        )


def test_rename_requires_paired_source_and_destination_plane_evidence():
    original = GitEntryVersion(kind=GitEntryKind.REGULAR, mode="100644", object_id=SHA_A)

    with pytest.raises(ValidationError, match="paired source evidence"):
        GitEntryObservation(
            path="new.py",
            index_delta=GitDelta.RENAMED,
        )
    with pytest.raises(ValidationError, match="source must be identified and present"):
        GitRenameObservation(
            source_path="old.py",
            plane=RenamePlane.INDEX,
            source_before=GitEntryVersion(kind=GitEntryKind.ABSENT),
            source_after=GitEntryVersion(kind=GitEntryKind.ABSENT),
            similarity=100,
        )
    with pytest.raises(ValidationError, match="destination must be added"):
        GitEntryObservation(
            path="new.py",
            rename=GitRenameObservation(
                source_path="old.py",
                plane=RenamePlane.INDEX,
                source_before=original,
                source_after=GitEntryVersion(kind=GitEntryKind.ABSENT),
                similarity=100,
            ),
            index_delta=GitDelta.RENAMED,
        )


def test_rename_source_has_only_one_destination_in_each_state_plane():
    original = GitEntryVersion(kind=GitEntryKind.REGULAR, mode="100644", object_id=SHA_A)
    absent = GitEntryVersion(kind=GitEntryKind.ABSENT)

    def renamed_destination(path: str) -> GitEntryObservation:
        return GitEntryObservation(
            path=path,
            index=original,
            worktree=original,
            index_delta=GitDelta.RENAMED,
            rename=GitRenameObservation(
                source_path="old.py",
                plane=RenamePlane.INDEX,
                source_before=original,
                source_after=absent,
                similarity=100,
            ),
        )

    with pytest.raises(ValidationError, match="only one destination per plane"):
        GitObservation(
            checked_out_branch="main",
            head_sha=SHA_A,
            run_branch="gauntlet/demo",
            run_branch_sha=SHA_A,
            recorded_sha=SHA_A,
            branch_relation=BranchRelation.EQUAL,
            index_fingerprint="sha256:index",
            worktree_fingerprint="sha256:tree",
            dirty_entries=(
                renamed_destination("first.py"),
                renamed_destination("second.py"),
            ),
        )


def test_entry_observation_preserves_symlink_target_without_dereference():
    link = GitEntryVersion(
        kind=GitEntryKind.SYMLINK,
        mode="120000",
        object_id=SHA_A,
        symlink_target="../../outside",
    )
    entry = GitEntryObservation(
        path="runs/demo/PR.md",
        head=link,
        index=link,
        worktree=link,
        protected=True,
    )
    assert entry.worktree.symlink_target == "../../outside"
    with pytest.raises(ValidationError, match="repository-relative"):
        GitEntryObservation(path="../outside", worktree=link)


def test_branch_ahead_observation_carries_auditable_contiguous_commit_inventory():
    checkpoint = _commit(SHA_B, SHA_A, CommitKind.CHECKPOINT)
    bookkeeping = _commit(SHA_C, SHA_B, CommitKind.ENGINE_BOOKKEEPING)
    observation = GitObservation(
        checked_out_branch="main",
        head_sha=SHA_D,
        run_branch="gauntlet/demo",
        run_branch_sha=SHA_C,
        recorded_sha=SHA_A,
        branch_relation=BranchRelation.CHECKPOINT_AHEAD,
        run_branch_commits=(checkpoint, bookkeeping),
        index_fingerprint="sha256:index",
        worktree_fingerprint="sha256:tree",
    )

    assert observation.run_branch_commits[0].attempt_id == "a-1"
    assert observation.run_branch_commits[0].checkpoint_id == "cp-1"
    assert observation.run_branch_commits[-1].sha == observation.run_branch_sha
    assert observation.checked_out_branch == "main"


@pytest.mark.parametrize("kind", [CommitKind.PHASE, CommitKind.FIX])
def test_single_unmanifested_phase_or_fix_commit_is_implementation_ahead(kind):
    work = _commit(SHA_B, SHA_A, kind)
    observation = GitObservation(
        checked_out_branch="main",
        head_sha=SHA_A,
        run_branch="gauntlet/demo",
        run_branch_sha=SHA_B,
        recorded_sha=SHA_A,
        branch_relation=BranchRelation.IMPLEMENTATION_AHEAD,
        run_branch_commits=(work,),
        index_fingerprint="sha256:index",
        worktree_fingerprint="sha256:tree",
    )

    assert observation.run_branch_commits == (work,)


def test_implementation_ahead_accepts_phase_and_bookkeeping_but_mixed_rejects_it():
    phase = _commit(SHA_B, SHA_A, CommitKind.PHASE)
    bookkeeping = _commit(SHA_C, SHA_B, CommitKind.ENGINE_BOOKKEEPING)
    common = {
        "checked_out_branch": "main",
        "head_sha": SHA_A,
        "run_branch": "gauntlet/demo",
        "run_branch_sha": SHA_C,
        "recorded_sha": SHA_A,
        "run_branch_commits": (phase, bookkeeping),
        "index_fingerprint": "sha256:index",
        "worktree_fingerprint": "sha256:tree",
    }
    GitObservation(**common, branch_relation=BranchRelation.IMPLEMENTATION_AHEAD)
    with pytest.raises(ValidationError, match="mixed relation contradicts"):
        GitObservation(**common, branch_relation=BranchRelation.MIXED_AHEAD)


def test_unclassified_commit_range_has_an_explicit_fail_closed_relation():
    unknown = _commit(SHA_B, SHA_A, CommitKind.UNKNOWN)
    observation = GitObservation(
        checked_out_branch="main",
        head_sha=SHA_A,
        run_branch="gauntlet/demo",
        run_branch_sha=SHA_B,
        recorded_sha=SHA_A,
        branch_relation=BranchRelation.UNCLASSIFIED_AHEAD,
        run_branch_commits=(unknown,),
        index_fingerprint="sha256:index",
        worktree_fingerprint="sha256:tree",
    )
    assert observation.branch_relation is BranchRelation.UNCLASSIFIED_AHEAD


@pytest.mark.parametrize(
    "updates, message",
    [
        ({"run_branch_sha": SHA_B}, "equal relation requires identical"),
        (
            {
                "branch_relation": BranchRelation.CHECKPOINT_AHEAD,
                "run_branch_sha": SHA_B,
            },
            "requires inventoried commits",
        ),
        (
            {
                "branch_relation": BranchRelation.BEHIND,
                "run_branch_sha": SHA_B,
            },
            "merge base at run tip",
        ),
    ],
)
def test_branch_relations_reject_sha_incompatible_or_incomplete_evidence(updates, message):
    values = {
        "checked_out_branch": "main",
        "head_sha": SHA_A,
        "run_branch": "gauntlet/demo",
        "run_branch_sha": SHA_A,
        "recorded_sha": SHA_A,
        "branch_relation": BranchRelation.EQUAL,
        "index_fingerprint": "sha256:index",
        "worktree_fingerprint": "sha256:tree",
    }
    values.update(updates)
    with pytest.raises(ValidationError, match=message):
        GitObservation(**values)


def test_branch_role_and_parent_chain_labels_cannot_contradict_commit_evidence():
    operator = _commit(SHA_B, SHA_A, CommitKind.OPERATOR)
    with pytest.raises(ValidationError, match="checkpoint relation contradicts"):
        GitObservation(
            checked_out_branch="main",
            head_sha=SHA_A,
            run_branch="gauntlet/demo",
            run_branch_sha=SHA_B,
            recorded_sha=SHA_A,
            branch_relation=BranchRelation.CHECKPOINT_AHEAD,
            run_branch_commits=(operator,),
            index_fingerprint="sha256:index",
            worktree_fingerprint="sha256:tree",
        )

    merge = _commit(SHA_B, (SHA_A, SHA_D), CommitKind.PHASE)
    with pytest.raises(ValidationError, match="cannot contain merge commits"):
        GitObservation(
            checked_out_branch="main",
            head_sha=SHA_A,
            run_branch="gauntlet/demo",
            run_branch_sha=SHA_B,
            recorded_sha=SHA_A,
            branch_relation=BranchRelation.IMPLEMENTATION_AHEAD,
            run_branch_commits=(merge,),
            index_fingerprint="sha256:index",
            worktree_fingerprint="sha256:tree",
        )
    disconnected = _commit(SHA_C, SHA_D, CommitKind.OPERATOR)
    with pytest.raises(ValidationError, match="contiguous parent chain"):
        GitObservation(
            checked_out_branch="main",
            head_sha=SHA_A,
            run_branch="gauntlet/demo",
            run_branch_sha=SHA_C,
            recorded_sha=SHA_A,
            branch_relation=BranchRelation.OPERATOR_AHEAD,
            run_branch_commits=(disconnected,),
            index_fingerprint="sha256:index",
            worktree_fingerprint="sha256:tree",
        )


def test_commit_inventory_marks_approved_artifact_changes_explicitly():
    change = GitCommitPathChange(
        kind=PathChangeKind.MODIFIED,
        path="runs/demo/plan.md",
        protected=True,
        approved_artifact=True,
    )
    commit = GitCommitObservation(
        sha=SHA_B,
        parents=(SHA_A,),
        author_name="Operator",
        author_email="operator@gauntlet.local",
        subject="manual plan repair",
        changed_paths=(change,),
        kind=CommitKind.OPERATOR,
        classification_evidence=("non-engine identity",),
    )
    assert commit.changed_paths[0].approved_artifact


def test_actions_are_typed_and_cannot_be_advertised_without_execution_payloads():
    with pytest.raises(ValidationError):
        RestoreSnapshotAction(description="restore")
    with pytest.raises(ValidationError):
        ResumeSessionAction(description="resume", session_id="session-1")
    with pytest.raises(ValidationError):
        EditThenRetryAction(
            description="edit",
            artifact_path="../outside",
            expected_fingerprint="sha256:x",
        )
    with pytest.raises(ValidationError):
        AdoptCommitsAction(
            description="adopt",
            base_sha=SHA_A,
            tip_sha=SHA_B,
            commit_shas=(SHA_C,),
        )

    restore = _action(RecoveryActionKind.RESTORE_SNAPSHOT)
    rebuild = _action(RecoveryActionKind.REBUILD_PROJECTION)
    assert restore.snapshot_id == "snapshot-1"
    assert rebuild.projection_path.endswith("manifest.json")


def test_assessment_requires_a_safe_action_and_consistent_recommendation():
    common = {
        "cause": RecoveryCause.PROCESS_LOST,
        "disposition": RecoveryDisposition.SNAPSHOT_AND_RESTART,
        "evidence": ("driver is dead",),
        "progress_fingerprint": _progress().digest,
    }
    with pytest.raises(ValidationError, match="needs a safe action"):
        RecoveryAssessment(**common)
    with pytest.raises(ValidationError, match="must be one of safe_actions"):
        RecoveryAssessment(
            **common,
            safe_actions=(_action(RecoveryActionKind.ABORT),),
            recommended_action=RecoveryActionKind.SNAPSHOT_AND_RESTART,
        )


def test_progress_fingerprint_is_deterministic_and_every_plane_matters():
    baseline = _progress()
    assert baseline.digest == _progress().digest
    for field, changed in (
        ("attempt_id", "attempt-3"),
        ("run_branch_sha", SHA_B),
        ("index_fingerprint", "sha256:index-2"),
        ("worktree_fingerprint", "sha256:worktree-2"),
        ("artifact_fingerprint", "sha256:artifact-2"),
        ("pending_response_id", "response-1"),
        ("latest_cycle_substep", "confirm"),
    ):
        updates = {field: changed}
        if field == "pending_response_id":
            updates["pending_response_state"] = ResponseState.PENDING
        assert baseline.digest != _progress(**updates).digest


def test_no_progress_contract_fails_loudly_with_executable_actions():
    before = _progress()
    action = _action(RecoveryActionKind.SNAPSHOT_AND_RESTART)
    with pytest.raises(NoProgressError) as raised:
        require_progress(before, _progress(), safe_actions=(action,))
    assert raised.value.before == before
    assert "branch, index, worktree" in str(raised.value)
    assert "snapshot_and_restart" in str(raised.value)

    require_progress(before, _progress(attempt_id="attempt-3"), safe_actions=(action,))
    require_progress(before, _progress(), safe_actions=(action,), legitimate_wait=True)


def test_recovery_action_and_fingerprint_inputs_are_deeply_stable():
    action = _action(RecoveryActionKind.RESTORE_SNAPSHOT)
    with pytest.raises(ValidationError):
        action.snapshot_id = "snapshot-2"
    assert fingerprint_data({"b": 2, "a": 1}) == fingerprint_data({"a": 1, "b": 2})


def test_git_fixture_distinguishes_staged_and_worktree_versions(recovery_git):
    recovery_git.write("feature.py", "A\n")
    recovery_git.stage("feature.py")
    recovery_git.commit("track A")
    recovery_git.write("feature.py", "B staged\n")
    recovery_git.stage("feature.py")
    recovery_git.write("feature.py", "C unstaged\n")

    entry = recovery_git.observe_path("feature.py")
    assert entry.head.object_id != entry.index.object_id
    assert entry.index.object_id != entry.worktree.object_id
    assert entry.index_delta is GitDelta.MODIFIED
    assert entry.worktree_delta is GitDelta.MODIFIED


@pytest.mark.parametrize(
    ("mutation", "index_delta", "worktree_delta", "kind"),
    [
        ("untracked", GitDelta.UNCHANGED, GitDelta.UNTRACKED, GitEntryKind.REGULAR),
        ("deleted", GitDelta.UNCHANGED, GitDelta.DELETED, GitEntryKind.ABSENT),
        ("executable", GitDelta.UNCHANGED, GitDelta.MODE_CHANGED, GitEntryKind.REGULAR),
        ("symlink", GitDelta.UNCHANGED, GitDelta.TYPE_CHANGED, GitEntryKind.SYMLINK),
    ],
)
def test_git_fixture_constructs_worktree_state_matrix(
    recovery_git, mutation, index_delta, worktree_delta, kind, tmp_path
):
    rel = "state.txt"
    if mutation != "untracked":
        recovery_git.write(rel, "tracked\n")
        recovery_git.stage(rel)
        recovery_git.commit("track matrix path")
    if mutation == "untracked":
        recovery_git.write(rel, "new\n")
    elif mutation == "deleted":
        recovery_git.delete(rel)
    elif mutation == "executable":
        recovery_git.write(rel, "tracked\n", executable=True)
    else:
        recovery_git.delete(rel)
        recovery_git.symlink(rel, tmp_path / "outside-target")

    entry = recovery_git.observe_path(rel)
    assert entry.index_delta is index_delta
    assert entry.worktree_delta is worktree_delta
    assert entry.worktree.kind is kind


@pytest.mark.parametrize("staged", [False, True])
def test_git_fixture_observes_rename_as_paired_source_destination_evidence(
    recovery_git, staged
):
    recovery_git.write("old.py", "content\n")
    recovery_git.stage("old.py")
    recovery_git.commit("track rename source")
    recovery_git.rename("old.py", "new.py")
    plane = RenamePlane.WORKTREE
    if staged:
        recovery_git.stage("old.py", "new.py")
        plane = RenamePlane.INDEX

    entry = recovery_git.observe_rename("old.py", "new.py", plane=plane)

    assert entry.rename is not None
    assert entry.rename.source_path == "old.py"
    destination_after = entry.index if staged else entry.worktree
    assert entry.rename.source_before.object_id == destination_after.object_id
    assert entry.rename.source_after.kind is GitEntryKind.ABSENT
    assert entry.index_delta is (GitDelta.RENAMED if staged else GitDelta.UNCHANGED)
    assert entry.worktree_delta is (GitDelta.UNCHANGED if staged else GitDelta.RENAMED)


def test_git_fixture_observes_real_three_stage_conflict_as_conflicted(recovery_git):
    recovery_git.write("conflict.txt", "base\n")
    recovery_git.stage("conflict.txt")
    recovery_git.commit("conflict base")
    recovery_git.create_branch("side")
    recovery_git.write("conflict.txt", "main\n")
    recovery_git.stage("conflict.txt")
    recovery_git.commit("main change")
    recovery_git.switch("side")
    recovery_git.write("conflict.txt", "side\n")
    recovery_git.stage("conflict.txt")
    recovery_git.commit("side change")
    recovery_git.switch("main")
    recovery_git.merge_conflict("side")

    entry = recovery_git.observe_path("conflict.txt")
    assert tuple(stage.stage for stage in entry.index_stages) == (1, 2, 3)
    assert entry.index.kind is GitEntryKind.ABSENT
    assert entry.index_delta is GitDelta.UNMERGED
    assert entry.worktree_delta is GitDelta.CONFLICTED


def test_git_fixture_constructs_branch_relations_without_checkout_side_effects(
    recovery_git,
):
    main_tip = recovery_git.head_sha
    recovery_git.create_branch("gauntlet/demo")
    recovery_git.switch("gauntlet/demo")
    recovery_git.write("run.py", "run\n")
    recovery_git.stage("run.py")
    run_tip = recovery_git.commit("run work")
    recovery_git.switch("main")

    assert recovery_git.branch == "main"
    assert recovery_git.head_sha == main_tip
    assert run_tip != main_tip
    recovery_git.detach()
    assert recovery_git.branch is None

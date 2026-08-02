"""P1 recovery vocabulary, invariants, and Git-state harness contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gauntlet.engine.recovery import (
    BranchRelation,
    DriverLiveness,
    GitDelta,
    GitEntryKind,
    GitEntryObservation,
    GitEntryVersion,
    GitObservation,
    NoProgressError,
    ProgressFingerprint,
    RecoveryAction,
    RecoveryActionKind,
    RecoveryAssessment,
    RecoveryCause,
    RecoveryDisposition,
    StateObservation,
    fingerprint_data,
    require_progress,
)


SHA_A = "a" * 40
SHA_B = "b" * 40


def _action(kind: RecoveryActionKind) -> RecoveryAction:
    return RecoveryAction(
        kind=kind,
        description=f"execute {kind.value}",
        requires_human_decision=kind is RecoveryActionKind.HUMAN_DECISION,
    )


def _progress(**updates) -> ProgressFingerprint:
    values = {
        "run_id": "run-1",
        "current_step": "implement",
        "iteration": "P1",
        "attempt_id": "attempt-2",
        "run_status": "running",
        "step_status": "interrupted",
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
            "driver killed",
            RecoveryCause.PROCESS_LOST,
            RecoveryDisposition.SNAPSHOT_AND_RESTART,
            RecoveryActionKind.SNAPSHOT_AND_RESTART,
        ),
        (
            "dirty partial work",
            RecoveryCause.WORKTREE_PARTIAL,
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
            "human repair commit",
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
            "manifest inconsistent",
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


@pytest.mark.parametrize("enum_type", [
    BranchRelation,
    DriverLiveness,
    GitDelta,
    GitEntryKind,
    RecoveryCause,
    RecoveryDisposition,
    RecoveryActionKind,
])
def test_recovery_enums_are_closed(enum_type):
    with pytest.raises(ValueError):
        enum_type("future-typo")


def test_observations_are_immutable_and_reject_extra_inference():
    state = StateObservation(
        run_status="running",
        step_status="interrupted",
        attempt_id="a-1",
        liveness=DriverLiveness.ORPHANED,
    )
    with pytest.raises(ValidationError):
        state.run_status = "done"
    with pytest.raises(ValidationError):
        StateObservation(
            run_status="running",
            liveness=DriverLiveness.NONE,
            inferred_safe=True,
        )


def test_git_observation_distinguishes_missing_and_divergent_branches():
    missing = GitObservation(
        checked_out_branch="main",
        head_sha=SHA_A,
        run_branch="gauntlet/demo",
        run_branch_sha=None,
        recorded_sha=SHA_A,
        branch_relation=BranchRelation.MISSING,
        index_fingerprint="sha256:index",
        worktree_fingerprint="sha256:tree",
    )
    forked = missing.model_copy(
        update={
            "run_branch_sha": SHA_B,
            "branch_relation": BranchRelation.FORKED,
        }
    )
    assert missing.run_branch_sha is None
    assert forked.branch_relation is BranchRelation.FORKED
    assert forked.run_branch_sha == SHA_B

    with pytest.raises(ValidationError, match="missing run branch"):
        GitObservation(
            **missing.model_dump(exclude={"run_branch_sha"}),
            run_branch_sha=SHA_B,
        )


def test_entry_observation_preserves_symlink_target_without_dereference():
    link = GitEntryVersion(
        kind=GitEntryKind.SYMLINK,
        mode="120000",
        object_id="abc",
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
    with pytest.raises(ValidationError, match="symlink observation"):
        GitEntryVersion(kind=GitEntryKind.SYMLINK, mode="120000")
    with pytest.raises(ValidationError, match="repository-relative"):
        GitEntryObservation(path="../outside", worktree=link)


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
        assert baseline.digest != _progress(**{field: changed}).digest


def test_no_progress_contract_fails_loudly_with_executable_actions():
    before = _progress()
    action = _action(RecoveryActionKind.SNAPSHOT_AND_RESTART)
    with pytest.raises(NoProgressError) as raised:
        require_progress(before, _progress(), safe_actions=(action,))
    assert raised.value.before == before
    assert "branch, index, worktree" in str(raised.value)
    assert "snapshot_and_restart" in str(raised.value)

    require_progress(before, _progress(attempt_id="attempt-3"), safe_actions=(action,))
    require_progress(
        before,
        _progress(),
        safe_actions=(action,),
        legitimate_wait=True,
    )


def test_recovery_action_and_fingerprint_inputs_are_deeply_stable():
    action = RecoveryAction(
        kind=RecoveryActionKind.RESTORE_SNAPSHOT,
        description="restore snapshot",
        parameters=(("snapshot_id", "snap-1"),),
    )
    with pytest.raises(TypeError):
        action.parameters[0] = ("snapshot_id", "snap-2")
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

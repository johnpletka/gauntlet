"""#175: a fresh review and its fixer must share the actual checkout base."""

from pathlib import Path

import pytest

from conftest import git
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.orchestrator import Orchestrator, ResponseAction
from gauntlet.engine.pipeline import Pipeline
from test_cycle import (
    BASE_CONFIG, CONFIRM, CV, F, REVIEW, SeqAdapter, V,
    _build_cycle_orch, _kill, _track_run_bookkeeping, _transient_exc,
    cycle_repo, cycle_step, writer,
)


@pytest.mark.parametrize("dedicated", [False, True])
@pytest.mark.parametrize("entry", ["response", "plain", "reject", "on_fail"])
def test_redrive_preserves_operator_commits(cycle_repo, dedicated, entry):
    operator = cycle_repo
    work = operator
    if dedicated:
        work = operator.parent / "work"
        git(operator, "worktree", "add", "-qb", "gauntlet/demo", str(work))
    operator_head = gitops.head_sha(operator)
    phase_base = gitops.head_sha(work)
    (work / "phase.py").write_text("PHASE-IMPLEMENTATION\n")
    git(work, "add", "phase.py")
    git(work, "commit", "-qm", "P5: Implement phase")
    phase_sha = gitops.head_sha(work)
    seen = {}

    def review(cwd):
        seen["review"] = gitops.head_sha(Path(cwd))
        return REVIEW(F("F-NEW"))

    def fix(cwd):
        seen["fix"] = gitops.head_sha(Path(cwd))
        seen["plan"] = (Path(cwd) / "plan.md").read_text() if (Path(cwd) / "plan.md").exists() else None
        seen["record"] = (Path(cwd) / "operator-record.txt").exists()
        return writer("src.py", "new fix\n", {})(cwd)

    reviewer = SeqAdapter(
        REVIEW(F("F-OLD")),
        CONFIRM(CV("F-OLD")) if entry in ("reject", "on_fail") else _transient_exc(),
        review, CONFIRM(CV("F-NEW")),
    )
    adapters = {
        "reviewer": reviewer,
        "triage": SeqAdapter(V("F-OLD"), V("F-NEW")),
        "builder": SeqAdapter(writer("src.py", "old fix\n", {}), fix),
    }
    steps = [cycle_step(mode="code_review")]
    if entry in ("reject", "on_fail"):
        steps.append({"id": "gate", "type": "human_gate"})
    pipeline = Pipeline.model_validate({
        "name": "demo", "version": 1,
        "stages": [{"id": "s", "steps": steps}],
    })
    man = M.Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo" if dedicated else "main",
        base_branch="main", pipeline=M.PipelineRef(name="demo", version=1, hash="h"),
        commits=[M.CommitRecord(step_id="phase-commit", phase="P5", sha=phase_sha, base_sha=phase_base)],
    )
    run_dir = operator / "runs" / "demo" / "run-1"
    orch = Orchestrator(
        repo_root=operator, work_root=work, run_dir=run_dir,
        artifact_root=operator, config=RunConfig.model_validate(BASE_CONFIG),
        pipeline=pipeline, manifest=man, adapter_factory=lambda n: adapters[n],
    )
    assert orch.drive() == M.RUN_PARKED
    operator_shas = []
    for name, content in [("plan.md", "OPERATOR-ACCEPTANCE-CLAUSE\n"),
                          ("operator-record.txt", "completed operator step\n")]:
        (work / name).write_text(content)
        git(work, "add", name)
        git(work, "commit", "-qm", f"operator: update {name}")
        operator_shas.append(gitops.head_sha(work))

    if entry == "reject":
        status = orch.reject_gate("gate", notes="Fix the remaining defect", user="operator")
    else:
        if entry == "response":
            orch.response_action = ResponseAction(
                kind="append", step_id="cycle", text="Proceed with the amended plan", user="operator",
            )
        elif entry == "on_fail":
            # The exact reset shared by an in-budget on_fail route and a
            # plain resume of an exhausted tests-recheck route (#134).
            orch.reset_records_for_retry(man, pipeline.stages[0], "cycle", None)
        status = orch.drive()
    assert status == (M.RUN_PARKED if entry in ("reject", "on_fail") else M.RUN_DONE)
    assert seen["fix"] == seen["review"]
    assert seen["plan"] == "OPERATOR-ACCEPTANCE-CLAUSE\n"
    assert seen["record"]
    assert all(gitops.is_ancestor(work, sha, gitops.head_sha(work)) for sha in operator_shas)
    assert all(c.handoff_sha == seen["review"] for c in man.record("cycle").checkpoints)
    prompt = reviewer.calls[2]["prompt"]
    assert "PHASE-IMPLEMENTATION" in prompt  # full phase, not just bookkeeping
    assert "OPERATOR-ACCEPTANCE-CLAUSE" in prompt
    assert f"{phase_base}..{seen['review'][:10]}" in prompt
    assert "reset round-1" not in (man.record("cycle").notes or "")
    if dedicated:
        assert gitops.head_sha(operator) == operator_head
        assert not (operator / "operator-record.txt").exists()


def test_tracked_handoff_is_reused_after_fixer_interrupt(cycle_repo):
    """A flush commit is the checkpoint base, even with older manifest fixes."""
    repo = cycle_repo
    run_dir = repo / "runs" / "demo" / "run-1"
    _track_run_bookkeeping(repo, run_dir)
    seen = {}

    def review(cwd):
        seen["review"] = gitops.head_sha(Path(cwd))
        return REVIEW(F("F-1"))

    def interrupt(cwd):
        (Path(cwd) / "partial.py").write_text("unfinished\n")
        raise _transient_exc()

    adapters = {
        "reviewer": SeqAdapter(review, CONFIRM(CV("F-1"))),
        "triage": SeqAdapter(V("F-1")),
        "builder": SeqAdapter(interrupt, writer("src.py", "fixed\n", {})),
    }
    orch, man = _build_cycle_orch(repo, adapters)
    man.commits.append(M.CommitRecord(step_id="cycle", phase="P5.1", sha=gitops.head_sha(repo)))
    assert orch.drive() == M.RUN_PARKED
    assert man.record("cycle").checkpoints[0].handoff_sha == seen["review"]
    assert orch.drive() == M.RUN_DONE
    assert len(adapters["triage"].calls) == 1
    assert len(adapters["reviewer"].calls) == 2
    assert not (repo / "partial.py").exists()


def test_missing_triage_after_unfinalized_fix_still_rewinds(cycle_repo):
    """A fix checkpoint, even without a manifest commit, proves a stale fix."""
    def refix(cwd):
        assert not (Path(cwd) / "old-only.py").exists()
        return writer("src.py", "new fix\n", {})(cwd)

    adapters = {
        "reviewer": SeqAdapter(REVIEW(F("F-1")), _kill, CONFIRM(CV("F-1"))),
        "triage": SeqAdapter(V("F-1"), V("F-1")),
        "builder": SeqAdapter(writer("old-only.py", "stale\n", {}), refix),
    }
    orch, man = _build_cycle_orch(cycle_repo, adapters)
    with pytest.raises(KeyboardInterrupt):
        orch.drive()
    assert not man.commits
    (orch.run_dir / "artifacts/r1/triage.json").unlink()
    assert orch.drive() == M.RUN_DONE
    assert "reset round-1" in man.record("cycle").notes


def test_fresh_review_after_missing_artifact_invalidates_old_suffix(cycle_repo):
    """A second interruption must not splice new review into old triage/fix."""
    seen = {}

    def review(cwd):
        seen["review"] = gitops.head_sha(Path(cwd))
        return REVIEW(F("F-NEW"))

    def fix(cwd):
        assert gitops.head_sha(Path(cwd)) == seen["review"]
        assert (Path(cwd) / "old-only.py").exists()  # reviewed, so retain it
        return writer("new.py", "new fix\n", {})(cwd)

    adapters = {
        "reviewer": SeqAdapter(REVIEW(F("F-OLD")), _transient_exc(), review, CONFIRM(CV("F-NEW"))),
        "triage": SeqAdapter(V("F-OLD"), _transient_exc(), V("F-NEW")),
        "builder": SeqAdapter(writer("old-only.py", "first fix\n", {}), fix),
    }
    orch, man = _build_cycle_orch(cycle_repo, adapters)
    assert orch.drive() == M.RUN_PARKED
    (orch.run_dir / "artifacts/r1/findings.json").unlink()
    assert orch.drive() == M.RUN_PARKED
    assert [(c.round, c.sub_step) for c in man.record("cycle").checkpoints] == [(1, "review")]
    assert orch.drive() == M.RUN_DONE
    assert len(adapters["builder"].calls) == 2


def test_later_round_flush_keeps_fix_in_regression_diff(cycle_repo):
    run_dir = cycle_repo / "runs" / "demo" / "run-1"
    _track_run_bookkeeping(cycle_repo, run_dir)
    seen = []

    def review(cwd):
        seen.append(gitops.head_sha(Path(cwd)))
        return REVIEW(F("F-1", "blocking"))

    adapters = {
        "reviewer": SeqAdapter(review, CONFIRM(CV("F-1", "unresolved")), review, CONFIRM(CV("F-1"))),
        "triage": SeqAdapter(V("F-1"), V("F-1")),
        "esc": SeqAdapter(V("F-1"), V("F-1")),
        "builder": SeqAdapter(writer("src.py", "ROUND-ONE-FIX\n", {}), writer("src.py", "final\n", {})),
    }
    orch, man = _build_cycle_orch(
        cycle_repo, adapters, step_extra={"escalation_agent": "esc", "mode": "code_review"},
    )
    assert orch.drive() == M.RUN_DONE
    assert "ROUND-ONE-FIX" in adapters["reviewer"].calls[2]["prompt"]
    assert all(c.handoff_sha == seen[c.round - 1] for c in man.record("cycle").checkpoints)


def test_restored_review_handoff_does_not_reuse_dropped_fix(cycle_repo):
    """HEAD reset to a recorded review handoff no longer contains the fix (F-001)."""
    repo = cycle_repo
    seen = {}

    def refix(cwd):
        seen["fix_base"] = gitops.head_sha(Path(cwd))
        return writer("src.py", "fixed again\n", {})(cwd)

    adapters = {
        "reviewer": SeqAdapter(REVIEW(F("F-1")), _transient_exc(), REVIEW(F("F-1")), CONFIRM(CV("F-1"))),
        "triage": SeqAdapter(V("F-1"), V("F-1")),
        "builder": SeqAdapter(writer("src.py", "fixed\n", {}), refix),
    }
    orch, man = _build_cycle_orch(repo, adapters)
    assert orch.drive() == M.RUN_PARKED
    cps = man.record("cycle").checkpoints
    review_handoff = next(c.handoff_sha for c in cps if c.sub_step == "review")
    fix_sha = next(c.result_sha for c in cps if c.sub_step == "fix")
    assert fix_sha != review_handoff
    git(repo, "reset", "-q", "--hard", review_handoff)
    assert orch.drive() == M.RUN_DONE
    assert "checkpoint reuse invalidated" in (man.record("cycle").notes or "")
    assert len(adapters["reviewer"].calls) == 4
    assert len(adapters["triage"].calls) == 2
    assert seen["fix_base"] == review_handoff
    assert (repo / "src.py").read_text() == "fixed again\n"
    assert not gitops.is_ancestor(repo, fix_sha, gitops.head_sha(repo))

"""Orchestrator state machine: control flow, budget halt, gates, resume (F-003)."""

from __future__ import annotations

from pathlib import Path

import yaml

from gauntlet.adapters.base import Usage
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline

from conftest import FakeAdapter

BUILDER_CFG = {"agents": {"builder": {"adapter": "claude-code"}}}


def _build(
    repo: Path,
    pipeline_text: str,
    *,
    config: dict | None = None,
    adapters: dict | None = None,
    extra_context: dict | None = None,
    interrupted: str = "park",
    manifest: Manifest | None = None,
) -> Orchestrator:
    cfg = RunConfig.model_validate({**(config or BUILDER_CFG), "interrupted_step": interrupted})
    pipeline = Pipeline.model_validate(yaml.safe_load(pipeline_text))
    artifact_root = repo / "runs" / "demo"
    run_dir = artifact_root / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    man = manifest or Manifest(
        run_id="run-1",
        slug="demo",
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )
    adapters = adapters or {}
    return Orchestrator(
        repo_root=repo,
        run_dir=run_dir,
        artifact_root=artifact_root,
        config=cfg,
        pipeline=pipeline,
        manifest=man,
        adapter_factory=(lambda name: adapters[name]) if adapters else None,
        extra_context=extra_context or {},
    )


LINEAR = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, output: out.txt, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, message: "P1: implement phase\\n\\nbody of the commit."}
"""


def test_linear_run_to_commit(fixture_repo):
    adapter = FakeAdapter(writes={"src.py": "print(1)\n"}, text="done")
    orch = _build(fixture_repo, LINEAR, adapters={"builder": adapter})
    status = orch.drive()
    assert status == M.RUN_DONE
    assert orch.manifest.record("implement").status == M.DONE
    assert orch.manifest.record("commit").status == M.DONE
    assert len(orch.manifest.commits) == 1
    assert orch.manifest.commits[0].phase == "P1"
    assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: implement phase"
    # work tree is clean; the run's own out.txt/manifest under runs/ is excluded
    assert gitops.is_clean(fixture_repo, exclude=["runs"])


def test_when_skips_step(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: maybe, type: shell, run: "false", when: "enabled"}
      - {id: always, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text, extra_context={"enabled": False})
    assert orch.drive() == M.RUN_DONE
    assert orch.manifest.record("maybe").status == M.SKIPPED
    assert orch.manifest.record("always").status == M.DONE


def test_foreach_fans_out(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: s
    foreach: vars.items
    steps:
      - {id: work, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text, extra_context={"items": ["a", "b", "c"]})
    assert orch.drive() == M.RUN_DONE
    assert orch.manifest.record("work", "0").status == M.DONE
    assert orch.manifest.record("work", "2").status == M.DONE


def test_on_fail_routes_back_with_retries(fixture_repo):
    # tests fail until implement has run twice (marker appears on 2nd call).
    state = {"n": 0}

    def on_run(adapter, prompt, cwd):
        state["n"] += 1
        if state["n"] >= 2:
            (Path(cwd) / "marker.txt").write_text("ok")

    adapter = FakeAdapter(on_run=on_run)
    text = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "test -f marker.txt", on_fail: {route_to: implement, max_retries: 2}}
"""
    orch = _build(fixture_repo, text, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_DONE
    assert state["n"] == 2
    assert orch.manifest.record("tests").status == M.DONE


def test_on_fail_exhausted_retries_fails(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: tests, type: shell, run: "false", on_fail: {route_to: tests, max_retries: 1}}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_FAILED
    assert orch.manifest.record("tests").status == M.FAILED
    assert orch.manifest.record("tests").attempts == 2  # initial + 1 retry


def test_budget_guard_halts(fixture_repo):
    adapter = FakeAdapter(usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.5))
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: pricey, type: agent_task, agent: builder, budget_usd: 0.1, prompt_text: go}
"""
    orch = _build(fixture_repo, text, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("pricey").status == M.HALTED


def test_budget_guard_preserves_side_effect_metadata(fixture_repo):
    # F-001: a DONE result that already produced a commit + per-agent usage must
    # keep those fields when the guard converts it to HALTED — otherwise
    # _finalize records the step halted with no commit/usage, breaking FR-3.3
    # checkpointing and FR-9 branch/manifest consistency.
    from gauntlet.engine.execution import DONE, HALTED, StepResult

    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: pricey, type: agent_task, agent: builder, budget_usd: 0.1, prompt_text: go}
"""
    orch = _build(fixture_repo, text)
    step = next(s for s in orch.pipeline.all_steps() if s.id == "pricey")
    rec = M.StepRecord(id="pricey", type="agent_task", agent="builder")
    result = StepResult(
        status=DONE,
        usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.5),
        commit_sha="a" * 40,
        commit_phase="P1",
        commits=[("P1.1", "b" * 40)],
        usage_by_agent={"builder": Usage(cost_usd=0.5)},
        artifact_writes={"findings.json": Path("/tmp/findings.json")},
        notes="converged in round 1",
    )
    guarded = orch._apply_budget_guard(step, rec, result)
    assert guarded.status == HALTED
    assert guarded.commit_sha == "a" * 40
    assert guarded.commits == [("P1.1", "b" * 40)]
    assert "builder" in guarded.usage_by_agent
    assert guarded.artifact_writes  # side-effect metadata not discarded
    assert "converged in round 1" in guarded.notes  # original notes kept
    assert "budget halt" in guarded.notes


def test_human_gate_parks_then_approve_continues(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: a, type: shell, run: "true"}
      - {id: gate, type: human_gate, show: [plan.md]}
      - {id: b, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("gate").status == M.PARKED
    assert orch.manifest.record("b") is None  # not reached
    assert orch.approve_gate("gate", notes="lgtm") == M.RUN_DONE
    assert orch.manifest.record("b").status == M.DONE


def test_reject_gate_fails_run(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: gate, type: human_gate}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    assert orch.reject_gate("gate", notes="no") == M.RUN_FAILED


# ---- resume transaction boundary (review F-003) ----------------------------
def _seed_running_step(repo, step_id, step_type, base_sha) -> Manifest:
    man = Manifest(
        run_id="run-1",
        slug="demo",
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )
    man.upsert(
        StepRecord(
            id=step_id,
            type=step_type,
            agent="builder",
            status=M.RUNNING,
            base_sha=base_sha,
            attempts=1,
            started="t0",
        )
    )
    return man


def test_resume_dirty_agent_step_parks_under_park_policy(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")  # killed mid-edit
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""
    adapter = FakeAdapter()
    orch = _build(fixture_repo, text, adapters={"builder": adapter}, manifest=man,
                  interrupted="park")
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("implement").status == M.INTERRUPTED
    assert adapter.calls == []  # never re-ran the agent over a dirty tree


def test_resume_dirty_agent_step_resets_under_reset_policy(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""
    adapter = FakeAdapter(writes={"clean.py": "real output\n"})
    orch = _build(fixture_repo, text, adapters={"builder": adapter}, manifest=man,
                  interrupted="reset_to_base")
    assert orch.drive() == M.RUN_DONE
    assert adapter.calls  # re-ran after reset
    assert not (fixture_repo / "partial.py").exists()  # partial work discarded
    # a backup ref preserved the discarded partial work (F-010-style safety)
    refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/backup/")
    assert "refs/gauntlet/backup/" in refs


def test_resume_dirty_artifact_under_runroot_is_detected(fixture_repo):
    # Review F-001: a partial *declared artifact* under runs/<slug> (not just a
    # repo-root file) must still be seen as a mid-edit interruption and parked.
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "runs" / "demo").mkdir(parents=True)
    (fixture_repo / "runs" / "demo" / "plan.md").write_text("half-written plan")
    man = _seed_running_step(fixture_repo, "author", "agent_task", base)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: author, type: agent_task, agent: builder, output: plan.md, prompt_text: go}
"""
    adapter = FakeAdapter()
    orch = _build(fixture_repo, text, adapters={"builder": adapter}, manifest=man,
                  interrupted="park")
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("author").status == M.INTERRUPTED
    assert adapter.calls == []  # not re-run over the partial artifact


def test_step_foreach_skips_completed_iterations_on_resume(fixture_repo):
    # Review F-004: a resumed step-level foreach must not re-run done iterations.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: work, type: agent_task, agent: builder, foreach: vars.items, prompt_text: go}
"""
    adapter = FakeAdapter()
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="x"))
    man.upsert(StepRecord(id="work", type="agent_task", iteration="0", status=M.DONE))
    orch = _build(fixture_repo, text, adapters={"builder": adapter},
                  extra_context={"items": ["a", "b", "c"]}, manifest=man)
    assert orch.drive() == M.RUN_DONE
    # iteration 0 was already done; only 1 and 2 ran
    assert len(adapter.calls) == 2


def test_gate_inside_foreach_is_approvable(fixture_repo):
    # Review F-004: a human_gate parked inside a foreach must be reachable.
    text = """
name: demo
version: 1
stages:
  - id: s
    foreach: vars.items
    steps:
      - {id: gate, type: human_gate}
"""
    orch = _build(fixture_repo, text, extra_context={"items": ["a", "b"]})
    assert orch.drive() == M.RUN_PARKED
    # the first iteration's gate is parked; approve targets it across iterations
    assert orch.approve_gate("gate") in (M.RUN_PARKED, M.RUN_DONE)


def test_shell_timeout_halts(fixture_repo):
    # Review F-006: a shell step exceeding its timeout halts at a checkpoint.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: slow, type: shell, run: "sleep 5", timeout_s: 0.3}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("slow").status == M.HALTED


def test_resume_mid_commit_reconciles_without_double_commit(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    # Simulate: engine recorded base + ran commit, the commit landed, then the
    # process died before recording the SHA. Reproduce the landed commit:
    (fixture_repo / "feature.py").write_text("done\n")
    msg = "P1: implement phase\n\nbody."
    landed = gitops.commit_all(
        fixture_repo, msg, identity=gitops.Identity("Builder", "b@gauntlet.local")
    )
    assert landed != base
    man = _seed_running_step(fixture_repo, "commit", "commit", base)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, message: "P1: implement phase\\n\\nbody."}
"""
    orch = _build(fixture_repo, text, manifest=man)
    assert orch.drive() == M.RUN_DONE
    assert gitops.head_sha(fixture_repo) == landed  # no second commit
    assert len(orch.manifest.commits) == 1
    assert orch.manifest.commits[0].sha == landed


# A gauntlet-phases block whose `goal:` carries an unquoted `schema:` — a
# colon-space mid-scalar that YAML reads as a nested mapping ("mapping values
# are not allowed here"). This is the exact defect that crashed the
# gauntlet-resume-response run's resume.
MALFORMED_PLAN = (
    "# Plan\n\n"
    "```gauntlet-phases\n"
    "- id: P1\n"
    "  title: Broken phase\n"
    "  goal: the implement step has no schema: field and must not change\n"
    "```\n"
)


def test_malformed_plan_phases_parks_instead_of_crashing(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: phases
    foreach: plan.phases
    steps:
      - {id: implement, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text)
    (orch.artifact_root / "plan.md").write_text(MALFORMED_PLAN)
    # A malformed block must not escape drive() as an uncaught PlanPhasesError
    # (which would leave the write-ahead RUN_RUNNING persisted); it parks.
    assert orch.drive() == M.RUN_PARKED
    # The persisted manifest reflects the park — never a stale "running" that
    # `gauntlet status` would report as a live run.
    reloaded = Manifest.load(orch.manifest_path)
    assert reloaded.status == M.RUN_PARKED
    assert any("gauntlet-phases" in w for w in reloaded.warnings), reloaded.warnings


def test_malformed_plan_phases_before_gate_parks_with_reason(fixture_repo):
    # Mirrors the real run: a human_gate precedes the phases stage, but the
    # context built to even process the gate eagerly parses plan.md. A malformed
    # block must park with the parse error recorded, not crash before the gate.
    text = """
name: demo
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-approve, type: human_gate, show: [plan.md]}
  - id: phases
    foreach: plan.phases
    steps:
      - {id: implement, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text)
    (orch.artifact_root / "plan.md").write_text(MALFORMED_PLAN)
    assert orch.drive() == M.RUN_PARKED
    reloaded = Manifest.load(orch.manifest_path)
    assert reloaded.status == M.RUN_PARKED
    # The park reason is persisted data, not something a human must infer.
    assert any("plan.md" in w for w in reloaded.warnings), reloaded.warnings

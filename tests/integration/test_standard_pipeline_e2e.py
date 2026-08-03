"""P5 end-to-end: the full `standard` pipeline on the toy PRD, live CLIs.

This is the FR-10.1 / FR-3 acceptance run: a human-authored toy PRD
(`tests/fixtures/toy/prd.md`) taken prd → plan → phase(s) → commits end-to-end
through `gauntlet run`, with the harness driving everything between the human
gates (which the test approves programmatically). It needs the real claude +
codex CLIs authenticated and an API key for the cheap tier, so it is marked
`integration` and skipped by default (`uv run pytest` runs units only).

Convergence depends on live models, so the completion assertions are deliberately
structural — the PRD/plan cycles ran, the phase loop produced ``slugify.py`` with
passing tests, the branch history matches FR-9, and the cost report attributes
spend per profile with classification well under the run total (FR-3). A separate
bounded test verifies that non-convergence stops only at a governed boundary; it
does not substitute for this suite's full live completion gate.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import gitops, manifest as M, proposals as P
from gauntlet.engine.feedback import FeedbackData, TriageCorrection
from gauntlet.engine.pipeline import content_hash, load_pipeline
from gauntlet.engine.planphases import extract_phases, phase_section
from gauntlet.engine.report import build_report
from gauntlet.engine.run import RunManager

pytestmark = [pytest.mark.integration]

REPO = Path(__file__).resolve().parents[2]
TOY_PRD = (REPO / "tests" / "fixtures" / "toy" / "prd.md").read_text()
HOOK_BIN = shutil.which("gauntlet-judge-hook") or str(
    REPO / ".venv" / "bin" / "gauntlet-judge-hook"
)

# Real frontier/strong/cheap profiles, pinned like the bootstrap's own config.
CONFIG = """\
base_branch: main
branch_prefix: "gauntlet/"
run_root: runs
# This fixture's human-authored PRD requires exactly one implementation phase.
# Give that atomic phase enough capacity for the original five FRs plus any
# clarifying requirements ratified during the live PRD review. The default
# production bound is exercised elsewhere; leaving it at three here creates a
# plan-author conflict that can only be resolved by changing this very fixture.
max_frs_per_phase: 10
# `--with pytest` makes both the normal test step and the disposable collector
# independent of an ambient activated venv and of which dev-dependency table a
# live builder happens to author. The collector preserves this project-owned
# launcher argument while normalizing only its own output flags.
test_command: "uv run --no-project --with pytest pytest -q"
agents:
  builder:
    adapter: claude-code
    model: opus
    permission_mode: acceptEdits
    allowed_tools: [Bash, Read, Write, Edit, Grep, Glob]
    base_flags: ["--setting-sources", "project"]
    step_timeout_s: 3600
  reviewer: {adapter: codex, model: gpt-5.5, sandbox: read-only}
  # The shipped standard.yaml has referenced these since the pipeline-
  # effectiveness run's P1 (gemini panel member), FR-6.3 (mechanic), and P5
  # (verifier); the fixture was never updated, so this test failed pipeline
  # validation before any agent ran (found during the PR #59 review-fix pass).
  # `gemini` is a PROFILE NAME — on this machine only an OpenAI key is
  # provisioned (pins.yaml), so the second panel member runs on the available
  # api provider rather than a live Gemini model id.
  gemini: {adapter: api, model: gpt-5-mini}
  mechanic: {adapter: api, model: gpt-5-mini}
  verifier:
    adapter: claude-code
    model: opus
    permission_mode: acceptEdits
    allowed_tools: [Bash, Read, Grep, Glob, Edit, Write]
    base_flags: ["--setting-sources", "project"]
  triage: {adapter: api, model: gpt-5-mini}
  escalation: {adapter: api, model: gpt-5}
  judge_llm: {adapter: api, model: gpt-5-mini}
identities:
  builder: {name: "Gauntlet Builder (claude)", email: "builder@gauntlet.local"}
  reviewer: {name: "Gauntlet Reviewer (codex)", email: "reviewer@gauntlet.local"}
  triage: {name: "Gauntlet Triage", email: "triage@gauntlet.local"}
"""

SCOPED_PHASE_PLAN = """\
# Scoped-context integration plan

**Status:** Approved test fixture.

## P1 — Implement the slug utility

Create `slugify.py` and unit tests implementing every requirement in the toy
PRD. Keep the implementation dependency-free.

```gauntlet-phases
- id: P1
  title: Implement the slug utility
  goal: Implement and test the dependency-free slugify function.
  frs: [FR-1, FR-2, FR-3, FR-4, FR-5]
  acceptance:
    - id: P1-A1
      clause: slugify implements all five functional requirements with unit tests.
```
"""

SCOPED_PHASE_PIPELINE = """\
name: scoped-phase
version: 1
stages:
  - id: phases
    foreach: plan.phases
    steps:
      - {id: implement, type: agent_task, agent: builder,
         prompt: prompts/implement-phase.md,
         inputs: [{name: prd.md, mode: reference}, {name: plan.md, mode: phase}],
         halt_on: "UPSTREAM CONFLICT"}
      - {id: tests, type: shell, run: "{{config.test_command}}", timeout_s: 1800}
      - {id: phase-commit, type: commit,
         message: "P1: Implement scoped slug utility\\n\\nExercise reference and phase context through the live builder."}
"""


def _bounded_live_standard_pipeline() -> str:
    """Use the shipped pipeline with a one-round live-model test budget.

    This fixture exercises the explicit fail-closed path independently from the
    full completion gate. If a blocking finding remains after the bounded round,
    the expected result is the standard human escalation.
    """
    source = (REPO / "pipelines" / "standard.yaml").read_text()
    return re.sub(r"max_rounds:\s*\d+", "max_rounds: 1", source)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _scaffold(
    tmp_path: Path,
    *,
    pipeline_text: str | None = None,
    plan_text: str | None = None,
) -> Path:
    """A scratch repo carrying the real assets + the human toy PRD (FR-10.1)."""
    repo = tmp_path / "toyrepo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@gauntlet.local")
    _git(repo, "config", "commit.gpgsign", "false")
    for d in ("schemas", "prompts"):
        shutil.copytree(REPO / d, repo / d)
    (repo / "pipelines").mkdir()
    pipeline_path = repo / "pipelines" / "standard.yaml"
    if pipeline_text is None:
        shutil.copy2(REPO / "pipelines" / "standard.yaml", pipeline_path)
    else:
        pipeline_path.write_text(pipeline_text)
    shutil.copy2(REPO / "policy.yaml", repo / "policy.yaml")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG)
    # a minimal uv project so `uv run pytest` works inside the phase loop
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'toy'\nversion = '0.0.0'\nrequires-python = '>=3.12'\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.py[cod]\n.pytest_cache/\nuv.lock\nruns/*/artifacts/\n"
    )
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": HOOK_BIN, "timeout": 15}]}]}
    }))
    (repo / "runs" / "toy").mkdir(parents=True)
    (repo / "runs" / "toy" / "prd.md").write_text(TOY_PRD)  # human-authored (FR-10.1)
    if plan_text is not None:
        (repo / "runs" / "toy" / "plan.md").write_text(plan_text)
    (repo / "README.md").write_text("toy\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed toy project")
    _git(repo, "branch", "-M", "main")
    return repo


def _assert_sanctioned_human_park(mgr: RunManager) -> None:
    """Assert the standard pipeline stopped at a designed human boundary.

    Whether independent live reviewers agree within three rounds is not a
    deterministic property of Gauntlet. The deterministic contract is that an
    unresolved blocking finding parks with durable evidence and a required
    human response instead of silently continuing or failing incoherently. A
    builder may likewise discover that implementation contradicts an approved
    artifact; FR-10.4 requires that conflict to park rather than be worked
    around.
    """
    man = mgr.status("toy")
    assert man.status == M.RUN_PARKED, man.model_dump()
    parked = [
        step
        for step in man.steps
        if step.id == man.current_step and step.status == M.PARKED
    ]
    assert parked, man.model_dump()
    step = parked[-1]
    assert step.halt_reason is None
    notes = step.notes or ""
    if step.parked_reason == M.PARKED_REASON_GATE:
        assert step.type == "human_gate" and step.id == "phase-gate"
        assert "awaiting human decision" in notes
        iteration = step.iteration
        cycle = man.record("impl-cycle", iteration)
        acceptance = man.record("acceptance-recheck", iteration)
        tests = man.record("tests-recheck", iteration)
        assert cycle and cycle.status == M.DONE and "converged" in (cycle.notes or "")
        assert acceptance and acceptance.status == M.DONE
        assert tests and tests.status == M.DONE
    elif step.type == "adversarial_cycle":
        assert step.parked_reason == M.PARKED_REASON_RESPONSE
        if "max_rounds=" in notes:
            assert "human must resolve" in notes
        else:
            assert "escalation: finding(s)" in notes
            assert "upstream artifact (FR-10.4 upstream invalidation)" in notes
    else:
        assert step.parked_reason == M.PARKED_REASON_RESPONSE
        assert step.type == "agent_task" and step.id == "implement"
        assert "UPSTREAM CONFLICT" in notes
        assert "backed up to refs/gauntlet/backup/" in notes
        assert "restored the clean tree" in notes


def _assert_governed_live_stop(mgr: RunManager) -> None:
    """Accept only an explicit human boundary or a pinned fail-closed fault."""
    man = mgr.status("toy")
    if man.status == M.RUN_PARKED:
        current = [step for step in man.steps if step.id == man.current_step]
        assert current, man.model_dump()
        step = current[-1]
        if step.status == M.HALTED:
            # A generated one-phase plan can still contain an implementation
            # commit that claims deferrals to phases the plan does not define.
            # The acceptance gate must fail closed (FR-3.3); this is a governed
            # live-system outcome, not an end-to-end completion substitute.
            assert step.type == "acceptance_gate"
            assert step.id == "acceptance-gate"
            assert step.halt_reason == M.HALT_REASON_PRECONDITION
            notes = step.notes or ""
            assert "defers work to nonexistent phase(s)" in notes
            assert "a deferral must target a real plan phase" in notes
            assert "fail closed, FR-3.3" in notes
            assert gitops.is_clean(
                mgr.repo_root, exclude=["runs"]
            ), gitops.status_porcelain(mgr.repo_root, exclude=["runs"])
            return
        _assert_sanctioned_human_park(mgr)
        return

    assert man.status == M.RUN_FAILED, man.model_dump()
    failed = [
        step
        for step in man.steps
        if step.id == man.current_step and step.status == M.FAILED
    ]
    assert failed, man.model_dump()
    step = failed[-1]
    assert step.type == "adversarial_cycle"
    assert step.halt_reason == M.HALT_REASON_ADAPTER_ERROR
    notes = step.notes or ""
    assert "fixer made no changes" in notes
    assert "accepted finding(s)" in notes
    assert "failing closed" in notes
    assert gitops.is_clean(mgr.repo_root, exclude=["runs"]), gitops.status_porcelain(
        mgr.repo_root, exclude=["runs"]
    )


def _resume_fixture_plan_escalation(mgr: RunManager) -> str:
    """Resolve one plan-review ownership mistake through the public workflow.

    Live reviewers sometimes classify tests or commands that P1 is supposed to
    create as an already-approved upstream artifact.  The fixture's human policy
    is explicit: neither the ratified PRD nor the test harness may be rewritten;
    the plan must make the requested deterministic check a P1 deliverable.  This
    exercises the sanctioned ``resume --response`` recovery path and does not
    relax the completion assertion.
    """
    man = mgr.status("toy")
    assert man.status == M.RUN_PARKED, man.model_dump()
    assert man.current_step == "plan-cycle", man.model_dump()
    cycle = man.record("plan-cycle")
    assert cycle is not None and cycle.status == M.PARKED
    assert cycle.type == "adversarial_cycle"
    assert cycle.parked_reason == M.PARKED_REASON_RESPONSE
    notes = cycle.notes or ""
    assert "escalation: finding(s)" in notes
    assert "upstream artifact (FR-10.4 upstream invalidation)" in notes

    decision = (
        "Keep the approved toy PRD and this test fixture unchanged. Tests, "
        "commands, or acceptance scripts that P1 is expected to create are P1 "
        "deliverables, not already-approved upstream artifacts. Resolve the "
        "escalated finding inside plan.md by specifying its deterministic P1 "
        "acceptance check; do not defer it upstream."
    )
    return mgr.resume("toy", response=decision, use_judge=True)


@pytest.mark.skipif(
    shutil.which("claude") is None,
    reason="scoped-context end-to-end needs the claude CLI",
)
def test_implement_phase_runs_with_scoped_reference_and_phase_context(tmp_path):
    """P6 FR-1.1: a real implement phase completes with scoped context.

    A one-phase pipeline using the shipped implement prompt passes prd.md by
    `reference` and plan.md by `phase`.  Plan-author/reviewer convergence is
    deliberately outside this test (and covered by the full standard-pipeline
    case below), so a model's opinion of a generated plan cannot obscure the
    scoped-context contract this case is meant to exercise.

    The persisted implement prompt must carry the repo-relative paths + this
    phase's plan excerpt — not the full documents inlined. Guards F-001's
    fail-closed contract end-to-end: an empty excerpt surfaces here, not
    silently.
    """
    repo = _scaffold(
        tmp_path,
        pipeline_text=SCOPED_PHASE_PIPELINE,
        plan_text=SCOPED_PHASE_PLAN,
    )
    mgr = RunManager(repo)
    pipe = repo / "pipelines" / "standard.yaml"

    plan = (repo / "runs" / "toy" / "plan.md").read_text()
    assert mgr.start("toy", pipe, use_judge=True) == M.RUN_DONE, (
        mgr.status("toy").model_dump()
    )

    # the phase actually completed: a numbered phase commit exists and the toy
    # was implemented (the normal phase-completion path, not a park).
    man = mgr.status("toy")
    assert any(
        p.split(".")[0].lstrip("P").isdigit() for p in (c.phase for c in man.commits)
    )
    assert (repo / "slugify.py").exists()

    # the persisted implement prompt used scoped context (FR-1.1): paths present,
    # bodies not inlined, and the plan's CURRENT-PHASE excerpt sliced in.
    run_dir = mgr.layout("toy").active_run_dir()
    prompts = sorted((run_dir / "steps").glob("implement*/prompt.md"))
    assert prompts, sorted((run_dir / "steps").iterdir())
    prompt = prompts[0].read_text()  # first phase iteration
    assert "runs/toy/prd.md" in prompt          # prd BY REFERENCE (path only)
    assert "by reference" in prompt             # reference-mode marker
    assert "runs/toy/plan.md" in prompt         # plan full-document path
    assert "current-phase excerpt" in prompt    # phase-mode marker
    # the excerpt is the current phase's prose section, sliced deterministically.
    first_phase_id = extract_phases(plan)[0]["id"]
    section = phase_section(plan, first_phase_id)
    assert section, f"no locatable section for {first_phase_id} (F-001)"
    assert section.splitlines()[0] in prompt    # the phase heading is injected


@pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("codex") is None,
    reason="standard end-to-end needs both claude and codex CLIs",
)
def test_bounded_live_standard_pipeline_stops_only_at_governed_boundary(tmp_path):
    repo = _scaffold(tmp_path, pipeline_text=_bounded_live_standard_pipeline())
    mgr = RunManager(repo)
    pipe = repo / "pipelines" / "standard.yaml"

    status = mgr.start("toy", pipe, use_judge=True)
    if mgr.status("toy").current_step != "prd-approve":
        _assert_governed_live_stop(mgr)
        return
    assert status == M.RUN_PARKED, mgr.status("toy").model_dump()

    status = mgr.approve("toy", use_judge=True)
    if mgr.status("toy").current_step != "plan-approve":
        _assert_governed_live_stop(mgr)
        return
    assert status == M.RUN_PARKED

    status = mgr.approve("toy", use_judge=True)
    if status == M.RUN_DONE:
        return
    _assert_governed_live_stop(mgr)


@pytest.mark.skipif(
    shutil.which("claude") is None or shutil.which("codex") is None,
    reason="standard end-to-end needs both claude and codex CLIs",
)
def test_standard_pipeline_end_to_end_on_toy_prd(tmp_path):
    """The shipped standard pipeline completes through real adapter boundaries."""
    repo = _scaffold(tmp_path)
    mgr = RunManager(repo)
    pipe = repo / "pipelines" / "standard.yaml"

    # PRD gate: the cycle reviews the human PRD, then parks for ratification.
    status = mgr.start("toy", pipe, use_judge=True)
    assert status == M.RUN_PARKED, mgr.status("toy").model_dump()
    assert mgr.status("toy").current_step == "prd-approve"

    # Plan gate: builder authors plan.md, the cycle reviews it, then parks for
    # ratification.
    status = mgr.approve("toy", use_judge=True)
    if status == M.RUN_PARKED and mgr.status("toy").current_step == "plan-cycle":
        status = _resume_fixture_plan_escalation(mgr)
    assert status == M.RUN_PARKED, mgr.status("toy").model_dump()
    assert mgr.status("toy").current_step == "plan-approve"
    plan = (repo / "runs" / "toy" / "plan.md").read_text()
    assert "gauntlet-phases" in plan
    phase_ids = [phase["id"] for phase in extract_phases(plan)]
    assert phase_ids == ["P1"], phase_ids

    # The phase implements, tests, commits, and completes review/fix/confirm.
    # A non-clean but converged evidence set parks at the explicit phase gate;
    # approve that human boundary, but no other parked state, then require the
    # standard pipeline to complete retro and PR drafting.
    status = mgr.approve("toy", use_judge=True)
    if status == M.RUN_PARKED:
        assert mgr.status("toy").current_step == "phase-gate", (
            mgr.status("toy").model_dump()
        )
        _assert_sanctioned_human_park(mgr)
        status = mgr.approve("toy", use_judge=True)
    assert status == M.RUN_DONE, mgr.status("toy").model_dump()

    man = mgr.status("toy")
    # FR-9 history: PLAN baseline + at least one numbered phase commit.
    phases = [c.phase for c in man.commits]
    assert "PLAN" in phases
    assert any(p.split(".")[0].lstrip("P").isdigit() for p in phases)
    # the toy was actually implemented and its tests pass
    assert (repo / "slugify.py").exists()
    # FR-9 clean history: the final tree is committed — only the run's own
    # bookkeeping under runs/ is excluded. Asserted directly, not vacuously
    # (review F-006): a dirty worktree at run end is a hard failure.
    assert gitops.is_clean(repo, exclude=["runs"]), gitops.status_porcelain(
        repo, exclude=["runs"]
    )

    # FR-3 acceptance: classification (triage) is a small, measured share of
    # total cost. The triage row and its percentage MUST be present — a missing
    # row or null percentage fails the acceptance rather than passing vacuously
    # (review F-006).
    report = build_report(man)
    assert report.total_cost, (
        "run reported no priced total cost; FR-3 cost acceptance is unmeasurable"
    )
    tri = next((a for a in report.agents if a.agent == "triage"), None)
    assert tri is not None, "no triage cost row; classification spend not attributed"
    assert tri.pct_cost is not None, "triage percentage is null; cannot verify FR-3"
    assert tri.pct_cost < 5.0

    # FR-9.8: a completed run drafts PR.md but does not open or push it.
    pr = repo / "runs" / "toy" / "PR.md"
    assert pr.exists() and "Not opened, not pushed" in pr.read_text()

    # --- FR-6 acceptance: feedback (captured after the run) drives a real,
    # human-reviewed proposal (P7 plan §"Real-data", review F-001/F-006) -------
    run_dir = mgr.layout("toy").active_run_dir()
    # the retro stage ran and self-critiqued each role at run end (FR-6.2)
    assert (run_dir / "retro" / "retro-builder.md").exists()
    assert (run_dir / "retro" / "retro-reviewer.md").exists()

    # Seed a deliberate triage error in feedback, captured AFTER the run: name a
    # real finding from the run's triage and mark its verdict wrong (FR-6.1).
    triage = json.loads((run_dir / "artifacts" / "triage.json").read_text())
    a_verdict = (triage.get("verdicts") or [{}])[0]
    seeded_fid = a_verdict.get("finding_id", "F-001")
    mgr.save_feedback("toy", FeedbackData(
        outcome_rating="mixed",
        reviewer_misses="the reviewer under-weighted an input-validation gap",
        triage_corrections=[TriageCorrection(
            finding_id=seeded_fid, correct_verdict="legitimate",
            note="this was a real defect the triager wrongly dismissed")],
        notes="Sharpen the triage rubric so input-validation gaps are not "
              "dismissed as bikeshedding.",
    ), run_dir=run_dir)

    # Late feedback drives proposal generation (FR-6.1 → FR-6.3): re-synthesise.
    # The normal retrospective deliberately uses the cheap triage profile and
    # retains malformed diffs as invalid evidence. This acceptance path goes
    # further: it requires a real diff that can be human-approved and applied,
    # so route only this late-feedback synthesis call through the configured
    # strong escalation profile. Classification during the run remains cheap,
    # preserving the FR-3 cost assertion above.
    generated = mgr.regenerate_proposals(
        "toy",
        adapter_factory=lambda _name: mgr.config.profile("escalation").build_adapter(),
    )
    assert generated, "no proposals generated from seeded feedback (FR-6 acceptance)"
    pending = [p for p in P.list_proposals(run_dir / "retro" / "proposals")
               if p.status == P.PENDING and p.valid]
    assert pending, "no valid, applyable prompt-diff proposal (FR-6 acceptance)"

    # Human approves one; the approved diff is applied + committed and the
    # CHANGELOG accumulates (FR-6.4/6.5). Capture the target's hash to prove the
    # next run's manifest would see the new version (FR-6 acceptance).
    target = pending[0].targets[0]
    before = content_hash((repo / target).read_text())
    approved = pending[0].name
    results = mgr.review_proposals(
        "toy", decide=lambda p: ("approve", "") if p.name == approved else ("reject", "x"),
    )
    applied = [r for r in results if r["action"] == "applied"]
    assert applied, f"approval did not apply any proposal: {results}"
    after = content_hash((repo / target).read_text())
    assert before != after, "approved proposal did not change the target asset"
    assert "## " in (repo / "prompts" / "CHANGELOG.md").read_text()
    # FR-6 acceptance: a fresh run's manifest would record the new asset version.
    # _prompt_hashes derives content_hash from disk, so for any asset the manifest
    # tracks (prompt templates, policy.yaml), the recorded hash now equals the
    # post-apply content — provably the new version, not the old one. (Some
    # targets, e.g. the triage few-shot corpus, are not referenced by a step and
    # so are not manifest-hashed; the content change above is the guarantee there.)
    pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
    hashes = RunManager(repo)._prompt_hashes(pipeline)
    if target in hashes:
        assert hashes[target] == after

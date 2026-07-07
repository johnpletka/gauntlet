"""Evidence-tiered gates (pipeline-effectiveness FR-4, P8).

Covers the strict §4.2 clean-signal predicate (each single violation parks;
all-clean auto-approves with a durable evidence snapshot), the two disjoint
F-008 cases (load-reject of a statically verifier-less / document gate vs. the
runtime `verifier: not_configured` park), the PR.md enumeration, and the
reversal circuit breaker.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from gauntlet.engine import gates, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import (
    AutoApproval,
    CommitRecord,
    Manifest,
    PipelineRef,
    StepRecord,
)
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline
from gauntlet.engine.pr import render_pr
from gauntlet.engine.validate import PipelineValidationError, validate_pipeline

# A per-phase code-gate stage: a verifier-configured cycle, the acceptance gate,
# the tests, then an `auto_when_clean` gate. `max_rounds: 2` is pinned in-fixture
# (P9 coupling) so P9's later 2→3 bump cannot silently invalidate the round-1
# convergence these predicate fixtures assert.
PHASE_STAGE = """
name: demo
version: 1
stages:
  - id: phases
    foreach: vars.phases
    steps:
      - {id: impl-cycle, type: adversarial_cycle, mode: code_review,
         reviewers: [{profile: reviewer, lens: correctness}],
         triager: triage, fixer: builder, verifier: verifier, max_rounds: 2}
      - {id: acceptance-gate, type: acceptance_gate, collector: pytest}
      - {id: tests, type: shell, run: "true"}
      - {id: gate, type: human_gate, policy: auto_when_clean,
         show: [findings.json]}
"""

CLEAN_CYCLE_METRICS = {
    "rounds": 1,
    "findings_total": 0,
    "verifier": {"profile": "verifier", "findings_total": 0, "legit_findings": 0},
}


def _pipeline(text: str = PHASE_STAGE) -> Pipeline:
    return Pipeline.model_validate(yaml.safe_load(text))


def _gate_step(pipeline: Pipeline, gate_id: str = "gate"):
    return next(s for s in pipeline.all_steps() if s.id == gate_id)


def _seed_clean_manifest(
    *, cycle_metrics: dict | None = None, iteration: str = "0"
) -> Manifest:
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )
    man.upsert(StepRecord(
        id="impl-cycle", type="adversarial_cycle", status=M.DONE,
        iteration=iteration,
        metrics=CLEAN_CYCLE_METRICS if cycle_metrics is None else cycle_metrics,
    ))
    man.upsert(StepRecord(
        id="acceptance-gate", type="acceptance_gate", status=M.DONE,
        iteration=iteration,
    ))
    man.upsert(StepRecord(id="tests", type="shell", status=M.DONE, iteration=iteration))
    return man


def _evaluate(man, pipeline, *, findings=None, verdicts=None, iteration="0"):
    return gates.evaluate_clean_gate(
        man, pipeline, _gate_step(pipeline), iteration,
        load_findings=lambda: (findings or [], verdicts or []),
    )


# --- P8-A2 / predicate: the all-clean case is clean --------------------------
def test_all_clean_predicate_holds():
    man, pipeline = _seed_clean_manifest(), _pipeline()
    decision = _evaluate(man, pipeline)
    assert decision.clean is True
    assert decision.misses == []
    ev = decision.evidence
    assert ev["rounds"] == 1
    assert ev["blocking"] == 0 and ev["major"] == 0
    assert ev["escalations"] == 0 and ev["reviewer_mutations"] == 0
    assert ev["acceptance_gate"] == "pass"
    assert ev["verifier"] == M.VERIFIER_CLEAN
    assert ev["tests"] == "passed"


# --- P9-A6: FR-6/FR-4 interaction — a carried remainder is never auto-approved
def test_carried_remainder_open_at_gate_parks_and_is_cited():
    # A cycle whose final round holds a carried/open remainder fails the clean
    # predicate and parks: the carried remainder makes the round non-converged with
    # an open fix_now-derived finding, so the strict conjunction cannot hold. The
    # miss cites the remainder explicitly, and no auto_approval is recorded.
    man = _seed_clean_manifest(cycle_metrics={**CLEAN_CYCLE_METRICS, "rounds": 3})
    findings = [{"id": "F-1-r2-c0", "severity": "major", "category": "correctness",
                 "carried_from": "F-1"}]
    verdicts = [{"finding_id": "F-1-r2-c0", "verdict": "legitimate"}]
    d = _evaluate(man, _pipeline(), findings=findings, verdicts=verdicts)
    assert d.clean is False
    assert any("carried remainder" in m for m in d.misses)
    assert any("F-1-r2-c0" in m for m in d.misses)
    assert man.auto_approvals == []  # the predicate never records an auto-approval on a miss


# --- P9-A5: the P8 clean-gate predicate is unaffected by the max_rounds 2→3 bump
def test_clean_gate_holds_under_max_rounds_3():
    # The cycle/gate coupling: a cycle that converges in round 1 under max_rounds:3
    # still satisfies the clean predicate (round-1 convergence is what the gate
    # reads, not the round budget). Re-validates the P8 assumptions at max_rounds 3.
    pipeline = _pipeline(PHASE_STAGE.replace("max_rounds: 2", "max_rounds: 3"))
    man = _seed_clean_manifest()
    d = _evaluate(man, pipeline)
    assert d.clean is True
    assert d.evidence["rounds"] == 1


# --- P8-A1: each single predicate violation parks ----------------------------
def test_violation_not_converged_round_1_parks():
    man = _seed_clean_manifest(
        cycle_metrics={**CLEAN_CYCLE_METRICS, "rounds": 2},
    )
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert any("round 1" in m for m in d.misses)


def test_violation_blocking_legitimate_finding_parks():
    man = _seed_clean_manifest()
    findings = [{"id": "F-1", "severity": "blocking", "category": "correctness"}]
    verdicts = [{"finding_id": "F-1", "verdict": "legitimate"}]
    d = _evaluate(man, _pipeline(), findings=findings, verdicts=verdicts)
    assert d.clean is False
    assert d.evidence["blocking"] == 1
    assert any("blocking" in m for m in d.misses)


def test_violation_major_legitimate_finding_parks():
    man = _seed_clean_manifest()
    findings = [{"id": "F-2", "severity": "major", "category": "spec-gap"}]
    verdicts = [{"finding_id": "F-2", "verdict": "legitimate"}]
    d = _evaluate(man, _pipeline(), findings=findings, verdicts=verdicts)
    assert d.clean is False
    assert d.evidence["major"] == 1


def test_minor_or_nonlegitimate_finding_does_not_block():
    # A minor legitimate finding and a major *bikeshedding* (non-legitimate)
    # finding are both clean-compatible: neither is a blocking/major LEGITIMATE
    # finding, so the predicate still holds.
    man = _seed_clean_manifest()
    findings = [
        {"id": "F-3", "severity": "minor", "category": "correctness"},
        {"id": "F-4", "severity": "major", "category": "correctness"},
    ]
    verdicts = [
        {"finding_id": "F-3", "verdict": "legitimate"},
        {"finding_id": "F-4", "verdict": "bikeshedding"},
    ]
    d = _evaluate(man, _pipeline(), findings=findings, verdicts=verdicts)
    assert d.clean is True
    assert d.evidence["blocking"] == 0 and d.evidence["major"] == 0


def test_violation_reviewer_mutation_parks():
    man = _seed_clean_manifest()
    findings = [{"id": "F-R1-MUTATION-1", "severity": "major",
                 "category": "principle-violation"}]
    verdicts = [{"finding_id": "F-R1-MUTATION-1", "verdict": "bikeshedding"}]
    d = _evaluate(man, _pipeline(), findings=findings, verdicts=verdicts)
    assert d.clean is False
    assert d.evidence["reviewer_mutations"] == 1
    assert any("mutation" in m for m in d.misses)


def test_violation_verifier_findings_parks():
    man = _seed_clean_manifest(cycle_metrics={
        **CLEAN_CYCLE_METRICS,
        "verifier": {"profile": "verifier", "findings_total": 2, "legit_findings": 1},
    })
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert d.evidence["verifier"] == M.VERIFIER_FINDINGS


def test_violation_cycle_not_done_parks():
    man = _seed_clean_manifest()
    man.record("impl-cycle", "0").status = M.PARKED
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert any("converge" in m for m in d.misses)


def test_violation_escalation_response_history_parks():
    # A cycle that carried a human --response (escalation resolved by a human) is
    # not a rubber-stamp candidate: the zero-escalations conjunct fails.
    man = _seed_clean_manifest()
    man.record("impl-cycle", "0").human_responses = [
        M.HumanResponse(response_id="impl-cycle-resp-1", response_text="x",
                        timestamp="t", user="u", response_attempt=1, state="consumed")
    ]
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert any("escalation" in m for m in d.misses)


def test_violation_acceptance_gate_not_passed_parks():
    man = _seed_clean_manifest()
    man.record("acceptance-gate", "0").status = M.HALTED
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert d.evidence["acceptance_gate"] == "fail"


def test_violation_tests_not_green_parks():
    man = _seed_clean_manifest()
    man.record("tests", "0").status = M.FAILED
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert d.evidence["tests"] == "failed"


def test_absent_acceptance_gate_parks_closed():
    # A required conjunct that never RAN cannot be proven clean: an
    # auto_when_clean gate with no acceptance_gate before it must park, not
    # auto-approve on a `not_run` snapshot (fail-open regression, review F-001).
    man = _seed_clean_manifest()
    man.steps = [r for r in man.steps if r.id != "acceptance-gate"]
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert d.evidence["acceptance_gate"] == "not_run"
    assert any("acceptance" in m for m in d.misses)


def test_absent_tests_shell_parks_closed():
    # Same as above for the "tests green" conjunct: no test shell before the gate
    # cannot prove tests are green, so it parks (review F-001).
    man = _seed_clean_manifest()
    man.steps = [r for r in man.steps if r.id != "tests"]
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert d.evidence["tests"] == "not_run"
    assert any("test" in m for m in d.misses)


def test_missing_findings_artifacts_fails_closed():
    # A loader that cannot read the round artifacts fails the finding conjuncts
    # closed — the predicate cannot PROVE zero blocking/major findings.
    man, pipeline = _seed_clean_manifest(), _pipeline()

    def _boom():
        raise FileNotFoundError("no findings.json")

    d = gates.evaluate_clean_gate(
        man, pipeline, _gate_step(pipeline), "0", load_findings=_boom,
    )
    assert d.clean is False
    assert any("fail" in m.lower() for m in d.misses)


# --- P8-A6: runtime `verifier: not_configured` parks (review F-008) ----------
def test_runtime_verifier_not_configured_parks_distinct_from_load_reject():
    # The pipeline PASSES load (the cycle declares `verifier:`), but the cycle's
    # runtime metrics carry NO verifier result — a resumed/legacy manifest that
    # predates the verifier, or a dynamically-skipped verifier. The predicate
    # records `verifier: not_configured` and parks — reached distinctly from the
    # load-reject case (which never lets a statically verifier-less pipeline run).
    metrics = {"rounds": 1, "findings_total": 0}  # note: no "verifier" key
    man = _seed_clean_manifest(cycle_metrics=metrics)
    pipeline = _pipeline()
    # the stage's cycle DOES declare a verifier, so this pipeline is load-valid:
    cfg = RunConfig.model_validate({"agents": {
        "reviewer": {"adapter": "codex"}, "triage": {"adapter": "codex"},
        "builder": {"adapter": "claude-code"}, "verifier": {"adapter": "claude-code"},
    }})
    report = validate_pipeline(pipeline, cfg)
    assert report.ok()  # passed load — case (b) is not the load-reject case (a)
    d = _evaluate(man, pipeline)
    assert d.clean is False
    assert d.evidence["verifier"] == M.VERIFIER_NOT_CONFIGURED
    assert any("not_configured" in m for m in d.misses)


# --- P8-A4: load-time rejection (static config gaps) -------------------------
def _cfg() -> RunConfig:
    return RunConfig.model_validate({"agents": {
        "reviewer": {"adapter": "codex"}, "triage": {"adapter": "codex"},
        "builder": {"adapter": "claude-code"}, "verifier": {"adapter": "claude-code"},
    }})


def test_load_rejects_auto_when_clean_on_document_gate():
    # A gate in a NON-foreach stage is a document (PRD/plan) gate — auto-approval
    # is rejected at load; document ratification stays unconditionally human.
    text = """
name: demo
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-approve, type: human_gate, policy: auto_when_clean}
"""
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(_pipeline(text), _cfg())
    assert any("document" in e for e in exc.value.errors)


def test_load_rejects_auto_when_clean_code_phase_without_verifier():
    # A code-phase gate whose stage configures NO verifier sub-step is rejected
    # at load (static config gap) — never reaching runtime to record not_configured.
    text = """
name: demo
version: 1
stages:
  - id: phases
    foreach: vars.phases
    steps:
      - {id: impl-cycle, type: adversarial_cycle, mode: code_review,
         reviewers: [{profile: reviewer, lens: correctness}],
         triager: triage, fixer: builder, max_rounds: 2}
      - {id: gate, type: human_gate, policy: auto_when_clean}
"""
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(_pipeline(text), _cfg())
    assert any("verifier sub-step" in e for e in exc.value.errors)


def test_load_accepts_auto_when_clean_code_phase_with_verifier():
    report = validate_pipeline(_pipeline(PHASE_STAGE), _cfg())
    assert report.ok()


def test_load_rejects_unknown_gate_policy():
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: g, type: human_gate, policy: sometimes}
"""
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(_pipeline(text), _cfg())
    assert any("unknown policy" in e for e in exc.value.errors)


def test_load_accepts_default_always_gate():
    # A gate with no policy (today's behavior) is valid anywhere, incl. document
    # stages — backward compatible.
    text = """
name: demo
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-approve, type: human_gate}
"""
    report = validate_pipeline(_pipeline(text), _cfg())
    assert report.ok()


# --- P8-A5: reversal circuit breaker -----------------------------------------
def test_recorded_reversal_disables_subsequent_auto_approval():
    man = _seed_clean_manifest()
    # Record a prior auto-approval, then a human reversal of it (rollback to P8).
    man.auto_approvals.append(AutoApproval(gate_id="gate", phase="P8", evidence={}, at="t0"))
    n = gates.record_reversals(
        man, min_phase_num=8, user="operator", notes="rollback", at="t1",
    )
    assert n == 1
    assert man.auto_approval_disabled is True
    assert man.auto_approvals[0].reversed_at == "t1"
    # A subsequent otherwise-all-clean gate now parks (breaker short-circuit).
    d = _evaluate(man, _pipeline())
    assert d.clean is False
    assert any("reversal" in m for m in d.misses)


def test_reversal_below_boundary_not_recorded():
    man = _seed_clean_manifest()
    man.auto_approvals.append(AutoApproval(gate_id="gate", phase="P3", evidence={}, at="t0"))
    n = gates.record_reversals(
        man, min_phase_num=8, user="operator", notes="rollback", at="t1",
    )
    assert n == 0
    assert man.auto_approval_disabled is False


# --- P8-A3: PR.md enumerates auto-approved gates with evidence ----------------
def test_pr_enumerates_auto_approved_gates(tmp_path):
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:h"),
    )
    man.commits = [CommitRecord(step_id="phase-commit", phase="P8", sha="c" * 40)]
    man.auto_approvals.append(AutoApproval(
        gate_id="gate", phase="P8", policy="auto_when_clean",
        evidence={"rounds": 1, "blocking": 0, "major": 0, "verifier": "clean",
                  "tests": "passed", "acceptance_gate": "pass"},
        at="2026-07-07T00:00:00Z",
    ))
    text = render_pr(man, prd_text="# X\n\nbody", plan_text="", run_dir=tmp_path)
    assert "Auto-approved gates" in text
    assert "gate" in text and "P8" in text
    assert "verifier=clean" in text and "rounds=1" in text


# --- P8-A2 / P8-A1: orchestrator drives auto-approve vs park ------------------
def _orch(repo: Path, man: Manifest, *, phases) -> Orchestrator:
    cfg = RunConfig.model_validate({"agents": {"builder": {"adapter": "claude-code"}}})
    pipeline = _pipeline(PHASE_STAGE)
    artifact_root = repo / "runs" / "demo"
    run_dir = artifact_root / "run-1"
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    return Orchestrator(
        repo_root=repo, run_dir=run_dir, artifact_root=artifact_root,
        config=cfg, pipeline=pipeline, manifest=man,
        extra_context={"phases": phases},
    )


def _write_round_artifacts(run_dir: Path, findings, verdicts):
    art = run_dir / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    (art / "findings.json").write_text(json.dumps(
        {"findings": findings, "open_questions": [], "summary": ""}))
    (art / "triage.json").write_text(json.dumps({"verdicts": verdicts}))


def test_orchestrator_auto_approves_clean_gate(fixture_repo):
    man = _seed_clean_manifest()
    orch = _orch(fixture_repo, man, phases=[{"id": "P8", "acceptance": []}])
    _write_round_artifacts(orch.run_dir, findings=[], verdicts=[])
    status = orch.drive()
    assert status == M.RUN_DONE
    gate = man.record("gate", "0")
    assert gate.status == M.DONE
    assert len(man.auto_approvals) == 1
    rec = man.auto_approvals[0]
    assert rec.gate_id == "gate" and rec.phase == "P8"
    assert rec.policy == M.GATE_POLICY_AUTO_WHEN_CLEAN
    assert rec.evidence["verifier"] == M.VERIFIER_CLEAN
    assert rec.evidence["rounds"] == 1
    # a notification was sent via the advisory channel (FR-4.1)
    assert any("auto-approval" in w for w in man.warnings)


def test_orchestrator_parks_on_predicate_miss(fixture_repo):
    # rounds=2 (not converged in round 1) → the gate parks for a human exactly as
    # today, and NO auto_approval record is written.
    man = _seed_clean_manifest(cycle_metrics={**CLEAN_CYCLE_METRICS, "rounds": 2})
    orch = _orch(fixture_repo, man, phases=[{"id": "P8", "acceptance": []}])
    _write_round_artifacts(orch.run_dir, findings=[], verdicts=[])
    status = orch.drive()
    assert status == M.RUN_PARKED
    gate = man.record("gate", "0")
    assert gate.status == M.PARKED
    assert gate.parked_reason == M.PARKED_REASON_GATE
    assert man.auto_approvals == []


def test_orchestrator_breaker_parks_clean_gate_after_reversal(fixture_repo):
    # P8-A5 end-to-end: a recorded reversal flips the run policy, so a subsequent
    # otherwise-all-clean gate parks rather than auto-approving.
    man = _seed_clean_manifest()
    man.auto_approval_disabled = True
    man.auto_approvals.append(AutoApproval(gate_id="prior", phase="P7",
                                           evidence={}, at="t0", reversed_at="t1"))
    orch = _orch(fixture_repo, man, phases=[{"id": "P8", "acceptance": []}])
    _write_round_artifacts(orch.run_dir, findings=[], verdicts=[])
    status = orch.drive()
    assert status == M.RUN_PARKED
    assert man.record("gate", "0").status == M.PARKED
    # no NEW auto-approval landed (only the prior, reversed one remains)
    assert all(a.gate_id == "prior" for a in man.auto_approvals)


def test_pr_marks_reversed_auto_approval(tmp_path):
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:h"),
    )
    man.commits = [CommitRecord(step_id="phase-commit", phase="P8", sha="c" * 40)]
    man.auto_approval_disabled = True
    man.auto_approvals.append(AutoApproval(
        gate_id="gate", phase="P8", evidence={"verifier": "clean"},
        at="t0", reversed_at="t1", reversed_by="operator",
    ))
    text = render_pr(man, prd_text="# X\n\nbody", plan_text="", run_dir=tmp_path)
    assert "REVERSED" in text
    assert "disabled" in text.lower()

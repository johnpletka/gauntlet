"""The default `standard.yaml` pipeline (P5, FR-5.1).

It must load and validate against the repo's real `.gauntlet/config.yaml`, and
encode the 3-gate workflow exactly: prd-cycle → prd-approve → plan-author →
plan-cycle → plan-approve → foreach plan.phases [implement → tests →
phase-commit → impl-cycle] → retro.
"""

from __future__ import annotations

from pathlib import Path

from gauntlet.engine.config import RunConfig
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.validate import validate_pipeline

REPO = Path(__file__).resolve().parents[2]


def _load():
    pipeline, phash = load_pipeline(REPO / "pipelines" / "standard.yaml")
    config = RunConfig.load(REPO / ".gauntlet" / "config.yaml")
    return pipeline, phash, config


def test_standard_validates_with_real_config():
    pipeline, phash, config = _load()
    report = validate_pipeline(pipeline, config)
    assert report.ok()
    assert phash.startswith("sha256:")


def test_standard_stage_and_step_shape():
    pipeline, _, _ = _load()
    assert [s.id for s in pipeline.stages] == ["prd", "plan", "phases", "retro"]
    by_id = {s.id: s for s in pipeline.stages}

    prd = [st.id for st in by_id["prd"].steps]
    assert prd == ["prd-cycle", "prd-approve"]
    plan = [st.id for st in by_id["plan"].steps]
    assert plan == ["plan-author", "plan-cycle", "plan-lint", "plan-approve"]
    assert by_id["plan"].steps[2].type == "phase_lint"
    phases = [st.id for st in by_id["phases"].steps]
    assert phases == [
        "implement", "tests", "phase-commit", "acceptance-gate", "impl-cycle",
        "acceptance-recheck",
    ]
    # FR-3.2: the deterministic completeness gate runs after commit, before the
    # reviewer cycle, and names its single collector (v1: pytest).
    ag = by_id["phases"].steps[3]
    assert ag.type == "acceptance_gate"
    assert ag.get("collector") == "pytest"
    # PR #59 review F1/F2: the SAME deterministic check re-runs after the cycle,
    # so a fix round that renames a cited test (or defers work in a P<N>.x
    # commit body) is re-verified against the tree the phase actually ships.
    recheck = by_id["phases"].steps[5]
    assert recheck.type == "acceptance_gate"
    assert recheck.get("collector") == "pytest"
    assert recheck.get("map") == ag.get("map")
    assert by_id["phases"].foreach == "plan.phases"
    assert [st.id for st in by_id["retro"].steps] == ["retrospective"]


def test_prd_and_plan_cycles_carry_stage_phase_labels():
    # Ratified 2026-06-12 (BOOTSTRAP-NOTES #28): the doc cycles commit as
    # PRD/PLAN, not numeric phases.
    pipeline, _, _ = _load()
    steps = {st.id: st for st in pipeline.all_steps()}
    assert steps["prd-cycle"].get("phase") == "PRD"
    assert steps["prd-cycle"].get("mode") == "artifact"
    assert steps["prd-cycle"].get("artifact") == "prd.md"
    assert steps["plan-cycle"].get("phase") == "PLAN"
    assert steps["plan-cycle"].get("artifact") == "plan.md"
    assert steps["impl-cycle"].get("mode") == "code_review"


def test_cycles_bind_all_roles_and_escalation():
    pipeline, _, _ = _load()
    for cid in ("prd-cycle", "plan-cycle", "impl-cycle"):
        step = next(s for s in pipeline.all_steps() if s.id == cid)
        assert step.get("triager") == "triage"
        assert step.get("fixer") == "builder"
        assert step.get("escalation_agent") == "escalation"


def test_cycle_max_rounds_reflects_p9_carry_budget():
    # pipeline-effectiveness FR-6.1 (P9): the shipped plan-cycle/impl-cycle rise
    # to max_rounds 3 so a carried remainder has a round to land before max-rounds
    # escalation parks; prd-cycle stays at 2 (the bump is scoped to plan/impl per
    # FR-6.1). This is the "reads shipped config, re-validated at 3" P9 exit check.
    pipeline, _, _ = _load()
    steps = {st.id: st for st in pipeline.all_steps()}
    assert steps["prd-cycle"].get("max_rounds") == 2
    assert steps["plan-cycle"].get("max_rounds") == 3
    assert steps["impl-cycle"].get("max_rounds") == 3


def test_cycles_use_the_v1_ensemble_panel():
    # pipeline-effectiveness FR-1.1 / Q2: the shipped cycles run the ratified
    # two-member panel — the codex reviewer (correctness lens) plus the Gemini
    # api profile (spec-coverage lens) — instead of a single reviewer.
    pipeline, _, _ = _load()
    for cid in ("prd-cycle", "plan-cycle", "impl-cycle"):
        step = next(s for s in pipeline.all_steps() if s.id == cid)
        panel = step.get("reviewers")
        assert panel == [
            {"profile": "reviewer", "lens": "correctness"},
            {"profile": "gemini", "lens": "spec-coverage"},
        ], cid
        # the singular reviewer role is subsumed by the panel
        assert step.get("reviewer") is None


def test_referenced_prompt_templates_exist():
    pipeline, _, _ = _load()
    for step in pipeline.all_steps():
        for key in ("prompt", "review_prompt"):
            ref = step.get(key)
            if ref:
                assert (REPO / ref).exists(), f"{step.id}:{key} -> {ref}"
    # the document vs code_review reviewer variants (plan P5 deliverable)
    assert (REPO / "prompts" / "review-document.md").exists()
    assert (REPO / "prompts" / "review-code.md").exists()
    assert (REPO / "prompts" / "plan-author.md").exists()
    assert (REPO / "prompts" / "implement-phase.md").exists()


def test_phase_loop_routes_test_failures_back_to_implement():
    pipeline, _, _ = _load()
    tests = next(s for s in pipeline.all_steps() if s.id == "tests")
    assert tests.on_fail is not None
    assert tests.on_fail.route_to == "implement"
    assert tests.on_fail.max_retries >= 1

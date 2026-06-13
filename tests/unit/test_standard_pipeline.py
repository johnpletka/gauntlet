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
    assert plan == ["plan-author", "plan-cycle", "plan-approve"]
    phases = [st.id for st in by_id["phases"].steps]
    assert phases == ["implement", "tests", "phase-commit", "impl-cycle"]
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
        assert step.get("reviewer") == "reviewer"
        assert step.get("triager") == "triage"
        assert step.get("fixer") == "builder"
        assert step.get("escalation_agent") == "escalation"
        assert step.get("max_rounds") == 2


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

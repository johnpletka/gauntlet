"""Switchover #2 (plan P4, review F-006): the bootstrap pipeline is real.

`pipelines/bootstrap.yaml` must load and validate against the P3 loader with
the repo's actual `.gauntlet/config.yaml` — these are the files
`gauntlet run gauntlet-bootstrap` will execute for P5–P7.
"""

from __future__ import annotations

from pathlib import Path

from gauntlet.engine.config import RunConfig
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.validate import validate_pipeline

REPO = Path(__file__).resolve().parents[2]


def _load():
    pipeline, phash = load_pipeline(REPO / "pipelines" / "bootstrap.yaml")
    config = RunConfig.load(REPO / ".gauntlet" / "config.yaml")
    return pipeline, phash, config


def test_bootstrap_pipeline_validates_with_real_config():
    pipeline, phash, config = _load()
    report = validate_pipeline(pipeline, config)
    assert report.ok()
    assert phash.startswith("sha256:")


def test_bootstrap_pipeline_expresses_p5_to_p7_loop():
    pipeline, _, _ = _load()
    assert [s.id for s in pipeline.stages] == ["p5", "p6", "p7"]
    for stage in pipeline.stages:
        types = [step.type for step in stage.steps]
        assert types == ["agent_task", "shell", "commit",
                         "adversarial_cycle", "human_gate"], stage.id
        cycle = stage.steps[3]
        assert cycle.get("reviewer") == "reviewer"
        assert cycle.get("triager") == "triage"
        assert cycle.get("fixer") == "builder"
        assert cycle.get("escalation_agent") == "escalation"
        assert cycle.get("max_rounds") == 2
        assert cycle.get("mode") == "code_review"


def test_bootstrap_prompt_templates_exist():
    pipeline, _, _ = _load()
    for step in pipeline.all_steps():
        ref = step.get("prompt")
        if ref:
            assert (REPO / ref).exists(), ref
    # the cycle's default templates (used when a step does not override)
    for name in ("cycle-review.md", "cycle-fix.md", "cycle-confirm.md", "triage.md"):
        assert (REPO / "prompts" / name).exists(), name


def test_bootstrap_prd_pointer_passes_entry_contract(tmp_path):
    from gauntlet.engine.run import PRD_STUB_MARKER

    content = (REPO / "runs" / "gauntlet-bootstrap" / "prd.md").read_text()
    assert PRD_STUB_MARKER not in content
    assert "PRD-gauntlet.md" in content  # points at the canonical spec

"""Pipeline loader + load-time validation (FR-5.3/5.6, FR-2.3, §8)."""

from pathlib import Path

import pytest

from gauntlet.engine.config import RunConfig
from gauntlet.engine.pipeline import (
    content_hash,
    load_pipeline,
    upstream_cycle_for_gate,
    upstream_cycle_id_for_gate,
)
from gauntlet.engine.validate import PipelineValidationError, validate_pipeline

GOOD_PIPELINE = """
name: demo
version: 1
stages:
  - id: plan
    steps:
      - {id: author, type: agent_task, agent: builder, output: plan.md, prompt_text: "go"}
  - id: phases
    steps:
      - {id: implement, type: agent_task, agent: builder, inputs: [prd.md, plan.md]}
      - {id: tests, type: shell, run: "{{config.test_command}}", on_fail: {route_to: implement, max_retries: 2}}
      - {id: phase-commit, type: commit, message_agent: triage}
"""

CONFIG = {
    "agents": {
        "builder": {"adapter": "claude-code", "permission_mode": "acceptEdits"},
        "triage": {"adapter": "api", "model": "haiku"},
        "reviewer": {"adapter": "codex", "sandbox": "read-only"},
    }
}


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "pipe.yaml"
    p.write_text(text)
    return p


def test_load_and_validate_good_pipeline(tmp_path):
    path = _write(tmp_path, GOOD_PIPELINE)
    pipeline, phash = load_pipeline(path)
    assert pipeline.name == "demo"
    assert phash.startswith("sha256:")
    report = validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))
    assert report.ok()


def test_content_hash_is_stable_and_sensitive():
    assert content_hash("a") == content_hash("a")
    assert content_hash("a") != content_hash("b")


# --- upstream_cycle_for_gate: the single gate→cycle rule (FR-8.2, F-001) ------
def test_upstream_cycle_same_stage_cycle_is_named(tmp_path):
    # A gate downstream of a cycle in its own non-foreach stage resolves to it —
    # the case the reject path actually re-drives.
    text = """
name: d
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-cycle, type: adversarial_cycle}
      - {id: plan-approve, type: human_gate}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    step, stage = upstream_cycle_for_gate(pipeline, "plan-approve")
    assert step is not None and step.id == "plan-cycle"
    assert stage.id == "plan"
    assert upstream_cycle_id_for_gate(pipeline, "plan-approve") == "plan-cycle"


def test_upstream_cycle_foreach_gate_is_none(tmp_path):
    # A gate inside a foreach stage is terminal on reject (iteration re-arming is
    # out of scope), so it must NOT name a cycle — status/web would otherwise
    # advertise a re-run reject never performs (F-001).
    text = """
name: d
version: 1
stages:
  - id: phases
    foreach: plan.phases
    steps:
      - {id: phase-cycle, type: adversarial_cycle}
      - {id: phase-approve, type: human_gate}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    assert upstream_cycle_for_gate(pipeline, "phase-approve") == (None, None)
    assert upstream_cycle_id_for_gate(pipeline, "phase-approve") is None


def test_upstream_cycle_prior_stage_cycle_is_not_named(tmp_path):
    # A cycle in an EARLIER stage is not the one a reject re-drives; a gate with no
    # same-stage cycle resolves to None even though a cycle precedes it globally.
    text = """
name: d
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-cycle, type: adversarial_cycle}
  - id: build
    steps:
      - {id: build-approve, type: human_gate}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    assert upstream_cycle_id_for_gate(pipeline, "build-approve") is None


def test_upstream_cycle_unknown_gate_is_none(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: g, type: human_gate}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    assert upstream_cycle_id_for_gate(pipeline, "nope") is None


def test_duplicate_step_id_rejected(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: x, type: shell, run: "true"}
      - {id: x, type: shell, run: "true"}
"""
    with pytest.raises(ValueError, match="duplicate step id"):
        load_pipeline(_write(tmp_path, text))


def test_dangling_artifact_rejected(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, inputs: [nonexistent.md]}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="dangling"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_reserved_human_response_input_rejected(tmp_path):
    # review F-002: declaring the reserved synthetic artifact as an input would
    # let a `--response` resume silently replace it and emit two identically
    # named blocks. Validation must reject it deterministically at load.
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: author, type: agent_task, agent: builder, output: plan.md, prompt_text: go}
      - {id: implement, type: agent_task, agent: builder, inputs: [plan.md, human-response.md]}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="reserved.*human-response"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_reserved_human_response_output_rejected(tmp_path):
    # review F-002: a step that PRODUCES `human-response.md` collides with the
    # engine-generated synthetic artifact just as an input does.
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: author, type: agent_task, agent: builder, output: human-response.md, prompt_text: go}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="reserved.*human-response"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_capability_violation_repo_write_on_api(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: triage}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="repo-write"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_repo_write_false_lets_api_review(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: review, type: agent_task, agent: triage, repo_write: false}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    assert validate_pipeline(pipeline, RunConfig.model_validate(CONFIG)).ok()


def test_validate_without_output_rejected(tmp_path):
    # review F-003: a `validate:` with no `output:` has no artifact to run
    # against, so the runtime would silently skip validation (fail open). Reject
    # at load. (builder is resume-capable, so only the no-output shape trips.)
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: author, type: agent_task, agent: builder, validate: plan_phases, prompt_text: go}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="validate.*but no .output"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_validate_on_non_resume_agent_rejected(tmp_path):
    # review F-004: `validate:` on an agent whose adapter cannot resume (api)
    # means the in-session repair loop would repair in a fresh, context-less call.
    # Reject at load — the resume-specific error must be present.
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: author, type: agent_task, agent: triage, output: plan.md, validate: plan_phases, prompt_text: go}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="cannot resume its session"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_banned_flag_rejected(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder}
"""
    cfg = {
        "agents": {
            "builder": {
                "adapter": "claude-code",
                "base_flags": ["--dangerously-skip-permissions"],
            }
        }
    }
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="banned flag"):
        validate_pipeline(pipeline, RunConfig.model_validate(cfg))


def test_unknown_agent_profile_rejected(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: ghost}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="undefined agent profile"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_malformed_reviewers_entry_rejected_at_load(tmp_path):
    # PR #59 review F-009: a reviewers: entry that is neither a profile string
    # nor a {profile, lens} mapping (a YAML typo — here a bare number) must fail
    # at load, not silently shrink the panel below its configured size.
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: cycle, type: adversarial_cycle, mode: artifact, artifact: prd.md,
         reviewers: [42, {profile: reviewer, lens: null}],
         triager: triage, fixer: builder, max_rounds: 1}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="reviewer"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_unknown_on_fail_target_rejected(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: tests, type: shell, run: "true", on_fail: {route_to: nowhere, max_retries: 1}}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="not a step in the same stage"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_cross_stage_on_fail_route_rejected(tmp_path):
    # route_to targets a step in a DIFFERENT stage -> rejected at load (F-005),
    # since runtime routing is stage-local and would otherwise crash.
    text = """
name: d
version: 1
stages:
  - id: a
    steps:
      - {id: build, type: shell, run: "true"}
  - id: b
    steps:
      - {id: tests, type: shell, run: "true", on_fail: {route_to: build, max_retries: 1}}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="not a step in the same stage"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_max_turns_rejected_at_load(tmp_path):
    # max_turns is unenforceable on the pinned CLIs -> rejected (F-006).
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, max_turns: 5}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="max_turns"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


def test_max_turns_on_profile_rejected(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder}
"""
    cfg = {"agents": {"builder": {"adapter": "claude-code", "max_turns": 3}}}
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="max_turns"):
        validate_pipeline(pipeline, RunConfig.model_validate(cfg))


def test_unknown_step_type_rejected(tmp_path):
    text = """
name: d
version: 1
stages:
  - id: s
    steps:
      - {id: x, type: teleport}
"""
    pipeline, _ = load_pipeline(_write(tmp_path, text))
    with pytest.raises(PipelineValidationError, match="unknown step type"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))

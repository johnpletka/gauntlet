"""Pipeline loader + load-time validation (FR-5.3/5.6, FR-2.3, §8)."""

from pathlib import Path

import pytest

from gauntlet.engine.config import RunConfig
from gauntlet.engine.pipeline import content_hash, load_pipeline
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
    with pytest.raises(PipelineValidationError, match="unknown step"):
        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))


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

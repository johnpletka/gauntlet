"""Prompt↔engine contract round-trips (#64).

The shipped `plan-author.md` prompt is the contract a fresh run's planner
follows literally; `phase_lint` and the `plan_phases` validator are the gates
that reject what the contract omits. Issue #64 was exactly that drift: the
prompt's `gauntlet-phases` example lacked the `acceptance:` lists the gate
requires, so every fresh run wedged at plan-lint. These tests round-trip the
prompt's own embedded example through the parser, the lint, and the in-step
validator, so contract drift fails the build instead of a user's run.

Parametrized over BOTH shipped copies (repo-canonical and scaffold) — redundant
with test_init's byte-equality check, but this contract must hold for each file
on its own.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.engine.config import RunConfig
from gauntlet.engine.execution import DONE, StepContext
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.pipeline import Pipeline, Step
from gauntlet.engine.planphases import (
    _BLOCK_RE,
    acceptance_clause_errors,
    extract_phases,
)
from gauntlet.engine.steptypes import handle_phase_lint
from gauntlet.engine.validators import _validate_plan_phases
from gauntlet.logging.redact import RedactingWriter

REPO = Path(__file__).resolve().parents[2]

PROMPT_PATHS = [
    "prompts/plan-author.md",
    "src/gauntlet/scaffold/prompts/plan-author.md",
]


def _prompt_text(rel: str) -> str:
    return (REPO / rel).read_text()


def _ctx(repo: Path) -> StepContext:
    """A minimal StepContext for phase_lint (no schemas needed)."""
    artifact_root = repo / "runs" / "demo"
    artifact_root.mkdir(parents=True, exist_ok=True)
    cfg = RunConfig.model_validate({"agents": {"builder": {"adapter": "claude-code"}}})
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    return StepContext(
        repo_root=repo, run_dir=artifact_root / "run-1", artifact_root=artifact_root,
        config=cfg, pipeline=Pipeline.model_validate(
            {"name": "demo", "version": 1, "stages": []}),
        manifest=man, record=StepRecord(id="s", type="phase_lint"),
        writer=RedactingWriter(),
        judge_env={"GAUNTLET_JUDGE_TOKEN": "tok", "GAUNTLET_JUDGE_MODE": "unattended"},
    )


def _lint_step() -> Step:
    return Step.model_validate(
        {"id": "plan-lint", "type": "phase_lint", "artifact": "plan.md"}
    )


def _synthesized_plan(text: str) -> str:
    """A minimal plan.md built from the prompt's own example block.

    Prose `## <id> — <title>` headings are synthesized per phase (the prompt
    requires them of the planner; the example block itself only carries the
    machine-readable list).
    """
    phases = extract_phases(text)
    body = _BLOCK_RE.search(text).group(1)
    heads = "".join(
        f"## {p['id']} — {p['title']}\n\n{p['goal']}\n\n" for p in phases
    )
    return f"# Plan\n\n{heads}```gauntlet-phases\n{body}\n```\n"


@pytest.mark.parametrize("prompt_path", PROMPT_PATHS)
def test_prompt_example_parses_with_gate_required_fields(prompt_path):
    # The example is the prompt file's only gauntlet-phases block, so
    # extract_phases works on the raw prompt; a second example block added
    # later trips the exactly-one error — itself a drift alarm.
    phases = extract_phases(_prompt_text(prompt_path))
    assert phases, f"{prompt_path} carries no parseable gauntlet-phases example"
    # The prompt promises `frs` is always emitted and `acceptance` is required;
    # the example must model both or planners will drift back to #64.
    assert all(p.get("frs") for p in phases)
    assert acceptance_clause_errors(phases) == []


@pytest.mark.parametrize("prompt_path", PROMPT_PATHS)
def test_prompt_example_round_trips_through_phase_lint(prompt_path, fixture_repo):
    ctx = _ctx(fixture_repo)
    plan = _synthesized_plan(_prompt_text(prompt_path))
    (ctx.artifact_root / "plan.md").write_text(plan)
    result = handle_phase_lint(_lint_step(), ctx)
    assert result.status == DONE, result.notes
    # The example must also clear the size lint at the default bound — a
    # WARNING here means the prompt models oversized phases.
    assert "WARNING" not in result.notes


@pytest.mark.parametrize("prompt_path", PROMPT_PATHS)
def test_prompt_example_passes_in_step_validator(prompt_path):
    # The plan-author's in-step repair loop gates on this validator before
    # phase_lint ever runs; the prompt's example must satisfy it too.
    plan = _synthesized_plan(_prompt_text(prompt_path))
    assert _validate_plan_phases(plan) is None

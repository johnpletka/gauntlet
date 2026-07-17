"""Scoped context assembly — input modes + capability gating (P6, FR-1.1/1.3).

Covers the deterministic per-input context modes (`inline`/`reference`/`phase`),
their load-time fail-closed capability + path checks, the `reads_repo` adapter
capability, and the plan-section slicer that `phase` mode uses.
"""

from __future__ import annotations

import pytest

from gauntlet.adapters.api import ApiAdapter
from gauntlet.adapters.claude_code import ClaudeCodeAdapter
from gauntlet.adapters.codex import CodexAdapter
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.pipeline import (
    INPUT_MODE_INLINE,
    INPUT_MODE_PHASE,
    INPUT_MODE_REFERENCE,
    Pipeline,
    Step,
    iter_inputs,
)
from gauntlet.engine.planphases import phase_section
from gauntlet.engine.steptypes import _render_prompt
from gauntlet.engine.validate import PipelineValidationError, validate_pipeline
from gauntlet.logging.redact import RedactingWriter


# --- reads_repo capability (FR-1.3) -----------------------------------------
def test_adapter_reads_repo_capability_declarations():
    # The CLI agents run in the repo and can read files; the in-process api
    # adapter cannot. Fail-closed default is False.
    assert ClaudeCodeAdapter.capabilities.reads_repo is True
    assert CodexAdapter.capabilities.reads_repo is True
    assert ApiAdapter.capabilities.reads_repo is False


def test_adapter_max_input_chars_declarations():
    # codex's app-server rejects any turn over 1 MiB of input wholesale
    # (`input_too_large`, observed live on the clerk-auth P3 review), so the
    # adapter declares the cap and prompt builders can fall back to
    # by-reference context. The others declare no known cap.
    assert CodexAdapter.capabilities.max_input_chars == 1_048_576
    assert ClaudeCodeAdapter.capabilities.max_input_chars is None
    assert ApiAdapter.capabilities.max_input_chars is None


# --- iter_inputs normalization (FR-1.1) -------------------------------------
def test_iter_inputs_normalizes_strings_and_mappings():
    step = Step.model_validate({
        "id": "s", "type": "agent_task",
        "inputs": ["prd.md", {"name": "plan.md", "mode": "phase"},
                   {"name": "x.md", "mode": "reference"}, {"name": "y.md"}],
    })
    refs = iter_inputs(step)
    assert refs == [
        ("prd.md", INPUT_MODE_INLINE),
        ("plan.md", INPUT_MODE_PHASE),
        ("x.md", INPUT_MODE_REFERENCE),
        ("y.md", INPUT_MODE_INLINE),  # a mapping with no mode defaults inline
    ]


def test_iter_inputs_rejects_unknown_mode():
    step = Step.model_validate({
        "id": "s", "type": "agent_task",
        "inputs": [{"name": "prd.md", "mode": "summary"}],
    })
    with pytest.raises(ValueError, match="unknown mode"):
        iter_inputs(step)


def test_iter_inputs_rejects_input_without_name():
    step = Step.model_validate({
        "id": "s", "type": "agent_task", "inputs": [{"mode": "reference"}],
    })
    with pytest.raises(ValueError, match="no `name`"):
        iter_inputs(step)


# --- phase_section slicer (FR-1.1) ------------------------------------------
_PLAN = """# Implementation plan

Orientation prose.

## P1 — First phase
P1-BODY alpha
### P1 detail
still P1

## P2 — Second phase
P2-BODY beta

## P3 — Third phase
P3-BODY gamma

## Sequencing notes
not a phase

```gauntlet-phases
- id: P1
  title: First phase
  goal: do p1
```
"""


def test_phase_section_slices_a_middle_phase():
    sec = phase_section(_PLAN, "P2")
    assert sec is not None
    assert sec.startswith("## P2 — Second phase")
    assert "P2-BODY beta" in sec
    assert "P1-BODY" not in sec and "P3-BODY" not in sec


def test_phase_section_includes_subheadings_stops_at_next_phase():
    sec = phase_section(_PLAN, "P1")
    assert "### P1 detail" in sec and "still P1" in sec  # deeper heading stays in
    assert "P2-BODY" not in sec                          # sibling terminates it


def test_phase_section_last_phase_stops_at_non_phase_heading():
    sec = phase_section(_PLAN, "P3")
    assert "P3-BODY gamma" in sec
    assert "Sequencing notes" not in sec  # stops at the next same-level heading
    assert "gauntlet-phases" not in sec


def test_phase_section_ignores_lookalike_and_missing():
    assert phase_section(_PLAN, "P30") is None   # P3 heading is not P30
    assert phase_section(_PLAN, "P9") is None     # absent


# --- _render_prompt modes (FR-1.1) ------------------------------------------
def _ctx(repo, *, iteration_item=None, iteration_index=None):
    artifact_root = repo / "runs" / "demo"
    artifact_root.mkdir(parents=True, exist_ok=True)
    cfg = RunConfig.model_validate({"agents": {"builder": {"adapter": "claude-code"}}})
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    from gauntlet.engine.execution import StepContext

    return StepContext(
        repo_root=repo, run_dir=artifact_root / "run-1", artifact_root=artifact_root,
        config=cfg, pipeline=Pipeline.model_validate(
            {"name": "demo", "version": 1, "stages": []}),
        manifest=man, record=StepRecord(id="implement", type="agent_task"),
        writer=RedactingWriter(),
        iteration_item=iteration_item, iteration_index=iteration_index,
    )


def test_render_reference_mode_injects_path_not_body(fixture_repo):
    ctx = _ctx(fixture_repo)
    (ctx.artifact_root / "prd.md").write_text("PRD-FULL-BODY-SENTINEL\n" * 50)
    step = Step.model_validate({
        "id": "implement", "type": "agent_task", "agent": "builder",
        "prompt_text": "BASE", "inputs": [{"name": "prd.md", "mode": "reference"}],
    })
    prompt = _render_prompt(step, ctx)
    assert "PRD-FULL-BODY-SENTINEL" not in prompt      # body not inlined
    assert "runs/demo/prd.md" in prompt                # repo-relative path is
    assert "by reference" in prompt


def test_render_phase_mode_injects_excerpt_and_path(fixture_repo):
    ctx = _ctx(fixture_repo, iteration_item={"id": "P2", "title": "t", "goal": "g"},
               iteration_index=1)
    (ctx.artifact_root / "plan.md").write_text(_PLAN)
    step = Step.model_validate({
        "id": "implement", "type": "agent_task", "agent": "builder",
        "prompt_text": "BASE", "inputs": [{"name": "plan.md", "mode": "phase"}],
    })
    prompt = _render_prompt(step, ctx)
    assert "P2-BODY beta" in prompt          # the current phase's section
    assert "P1-BODY" not in prompt and "P3-BODY" not in prompt
    assert "runs/demo/plan.md" in prompt     # full-document path


def test_render_phase_mode_fails_closed_when_section_missing(fixture_repo):
    # F-001: `phase` mode must NOT ship an implement prompt that quietly omits
    # its scoped context. When the current phase has no locatable `## <id> …`
    # section, rendering fails closed (raises) rather than emitting a soft note.
    ctx = _ctx(fixture_repo, iteration_item={"id": "P9", "title": "t", "goal": "g"},
               iteration_index=0)
    (ctx.artifact_root / "plan.md").write_text(_PLAN)  # has P1/P2/P3, not P9
    step = Step.model_validate({
        "id": "implement", "type": "agent_task", "agent": "builder",
        "prompt_text": "BASE", "inputs": [{"name": "plan.md", "mode": "phase"}],
    })
    with pytest.raises(ValueError, match="no locatable section for phase P9"):
        _render_prompt(step, ctx)


def test_render_inline_mode_unchanged(fixture_repo):
    ctx = _ctx(fixture_repo)
    (ctx.artifact_root / "prd.md").write_text("PRD-FULL-BODY-SENTINEL\n")
    step = Step.model_validate({
        "id": "implement", "type": "agent_task", "agent": "builder",
        "prompt_text": "BASE", "inputs": ["prd.md"],
    })
    prompt = _render_prompt(step, ctx)
    assert "PRD-FULL-BODY-SENTINEL" in prompt  # default still inlines the body


def test_scoped_prompt_is_far_smaller_than_inline(fixture_repo):
    # FR-1.1 acceptance: a scoped implement prompt is well under 25% of the
    # equivalent all-inline prompt on a multi-phase run.
    ctx = _ctx(fixture_repo, iteration_item={"id": "P2", "title": "t", "goal": "g"},
               iteration_index=1)
    prd = "# PRD\n" + "prd body line to pad the document\n" * 1000   # ~34KB
    plan = "# Plan\n\n" + "".join(
        f"## P{n} — Phase {n}\n" + f"P{n}-BODY padding line here\n" * 350 + "\n"
        for n in (1, 2, 3)
    )  # ~27KB, 3 phases
    (ctx.artifact_root / "prd.md").write_text(prd)
    (ctx.artifact_root / "plan.md").write_text(plan)

    def _step(inputs):
        return Step.model_validate({
            "id": "implement", "type": "agent_task", "agent": "builder",
            "prompt_text": "BASE", "inputs": inputs,
        })

    inline = _render_prompt(_step(["prd.md", "plan.md"]), ctx)
    scoped = _render_prompt(_step([
        {"name": "prd.md", "mode": "reference"},
        {"name": "plan.md", "mode": "phase"},
    ]), ctx)
    assert len(scoped) <= 0.25 * len(inline)
    assert "runs/demo/prd.md" in scoped and "runs/demo/plan.md" in scoped
    assert "P2-BODY" in scoped and "P1-BODY" not in scoped


# --- load-time fail-closed checks (FR-1.3) ----------------------------------
_CFG = {
    "agents": {
        "builder": {"adapter": "claude-code"},   # reads_repo True
        "classifier": {"adapter": "api", "model": "m"},  # reads_repo False
    },
}


def _pipeline(inputs, agent="builder"):
    return Pipeline.model_validate({
        "name": "p", "version": 1,
        "stages": [{"id": "s", "steps": [
            {"id": "implement", "type": "agent_task", "agent": agent,
             "prompt_text": "x", "inputs": inputs},
        ]}],
    })


def test_reference_on_non_reading_profile_raises_at_load():
    # (a) a reference/phase input bound to an api-adapter profile (reads_repo
    # False) fails load, naming the step, input, and profile.
    pipe = _pipeline([{"name": "prd.md", "mode": "reference"}], agent="classifier")
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(pipe, RunConfig.model_validate(_CFG))
    msg = str(exc.value)
    assert "implement" in msg and "prd.md" in msg and "classifier" in msg
    assert "reads_repo" in msg


def test_reference_path_escaping_repo_root_raises_at_load(fixture_repo):
    # (b) a reference path that resolves outside the repo root is a named path
    # error at load. `../../../` from runs/demo escapes the repo root entirely.
    escaping = "../../../evil.md"
    pipe = _pipeline([{"name": escaping, "mode": "reference"}])
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(
            pipe, RunConfig.model_validate(_CFG),
            seeds=frozenset({"prd.md", "plan.md", escaping}),
            repo_root=fixture_repo, artifact_root=fixture_repo / "runs" / "demo",
        )
    assert "outside the repo root" in str(exc.value) and escaping in str(exc.value)


def test_phase_mode_requires_plan_md():
    # (c-1) phase mode is plan.md-only.
    pipe = _pipeline([{"name": "prd.md", "mode": "phase"}])
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(pipe, RunConfig.model_validate(_CFG))
    assert "valid only for plan.md" in str(exc.value)


def test_reference_to_missing_seed_raises_at_load(fixture_repo):
    # (c-2) a reference to a seed that is not on disk (and not produced by an
    # earlier step) is a dead reference caught at load.
    ar = fixture_repo / "runs" / "demo"
    ar.mkdir(parents=True)
    # prd.md is a seed but does not exist under artifact_root here.
    pipe = _pipeline([{"name": "prd.md", "mode": "reference"}])
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(
            pipe, RunConfig.model_validate(_CFG),
            repo_root=fixture_repo, artifact_root=ar,
        )
    assert "does not resolve to a file" in str(exc.value)


def test_reference_to_produced_artifact_skips_existence(fixture_repo):
    # A reference to an artifact PRODUCED by an earlier step passes load even
    # though it is not on disk yet — it will exist when the step runs.
    ar = fixture_repo / "runs" / "demo"
    ar.mkdir(parents=True)
    pipe = Pipeline.model_validate({
        "name": "p", "version": 1,
        "stages": [{"id": "s", "steps": [
            {"id": "author", "type": "agent_task", "agent": "builder",
             "prompt_text": "x", "output": "made.md"},
            {"id": "use", "type": "agent_task", "agent": "builder",
             "prompt_text": "x", "inputs": [{"name": "made.md", "mode": "reference"}]},
        ]}],
    })
    report = validate_pipeline(
        pipe, RunConfig.model_validate(_CFG),
        repo_root=fixture_repo, artifact_root=ar,
    )
    assert report.ok()


def test_reference_to_existing_seed_passes(fixture_repo):
    ar = fixture_repo / "runs" / "demo"
    ar.mkdir(parents=True)
    (ar / "prd.md").write_text("real seed\n")
    pipe = _pipeline([{"name": "prd.md", "mode": "reference"}])
    report = validate_pipeline(
        pipe, RunConfig.model_validate(_CFG),
        repo_root=fixture_repo, artifact_root=ar,
    )
    assert report.ok()

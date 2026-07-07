"""Trend-informed plan authoring (P7, FR-5.3).

The plan author sizes phases; without measured history it sizes blind (#54
cause 4). These tests cover the measured-history block injected into the
plan-author input: the stats block for a repo with completed runs (P7-A1), the
explicit no-history block for an empty repo (P7-A2), and the `max_frs_per_phase`
size bound always present (P7-A3). The block math is from the manifest, so it is
testable against fixture manifests written to disk.
"""

from __future__ import annotations

from pathlib import Path

from gauntlet.engine.config import ProviderWindow, RunConfig
from gauntlet.engine.execution import StepContext
from gauntlet.engine.manifest import (
    CommitRecord,
    Manifest,
    PipelineRef,
    RUN_DONE,
    RUN_PARKED,
    StepRecord,
    UsageTotals,
)
from gauntlet.engine.pipeline import Pipeline, Step
from gauntlet.engine.steptypes import _render_prompt
from gauntlet.engine.trend import (
    collect_step_type_stats,
    iter_completed_manifests,
    render_plan_author_history,
)
from gauntlet.logging.redact import RedactingWriter


# --- fixtures ---------------------------------------------------------------
def _completed_manifest(
    run_id: str,
    *,
    status: str = RUN_DONE,
    cycle_cost: float = 1.8,
    cycle_seconds: int = 300,
    total_cost: float = 4.0,
    phases: tuple[str, ...] = ("P1", "P2"),
) -> Manifest:
    man = Manifest(
        run_id=run_id, slug="demo", branch="b", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="h"),
        status=status,
    )
    man.steps.append(StepRecord(
        id="impl-cycle", type="adversarial_cycle",
        started="2026-06-13T00:00:00+00:00",
        ended=f"2026-06-13T00:{cycle_seconds // 60:02d}:{cycle_seconds % 60:02d}+00:00",
        usage=UsageTotals(cost_usd=cycle_cost),
    ))
    man.steps.append(StepRecord(
        id="plan-author", type="agent_task",
        started="2026-06-13T00:10:00+00:00", ended="2026-06-13T00:12:00+00:00",
        usage=UsageTotals(cost_usd=0.4),
    ))
    for i, p in enumerate(phases):
        man.commits.append(CommitRecord(step_id="commit", phase=p, sha=chr(97 + i) * 40))
    man.totals = UsageTotals(input_tokens=100, output_tokens=20, cost_usd=total_cost)
    return man


def _write_run(run_root: Path, slug: str, man: Manifest) -> Path:
    run_dir = run_root / slug / man.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    man.write_atomic(run_dir / "manifest.json")
    return run_dir


# --- iter_completed_manifests ------------------------------------------------
def test_iter_completed_manifests_only_yields_done_runs(tmp_path: Path):
    run_root = tmp_path / "runs"
    _write_run(run_root, "a", _completed_manifest("run-2026-06-13T00-00-00"))
    _write_run(run_root, "b", _completed_manifest("run-2026-06-14T00-00-00", status=RUN_PARKED))
    ids = [m.run_id for m in iter_completed_manifests(run_root)]
    assert ids == ["run-2026-06-13T00-00-00"]  # the parked run is excluded


def test_iter_completed_manifests_empty_root(tmp_path: Path):
    assert list(iter_completed_manifests(tmp_path / "does-not-exist")) == []


def test_iter_completed_manifests_skips_corrupt_manifest(tmp_path: Path):
    run_root = tmp_path / "runs"
    _write_run(run_root, "a", _completed_manifest("run-2026-06-13T00-00-00"))
    bad = run_root / "b" / "run-bad"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{ not json")
    ids = [m.run_id for m in iter_completed_manifests(run_root)]
    assert ids == ["run-2026-06-13T00-00-00"]  # corrupt sibling contributes nothing


# --- collect_step_type_stats -------------------------------------------------
def test_collect_step_type_stats_aggregates_cost_and_duration():
    m1 = _completed_manifest("run-1", cycle_cost=2.0, cycle_seconds=300)
    m2 = _completed_manifest("run-2", cycle_cost=4.0, cycle_seconds=600)
    stats = {s.step_type: s for s in collect_step_type_stats([m1, m2])}
    cyc = stats["adversarial_cycle"]
    assert cyc.n_steps == 2
    assert cyc.mean_cost == 3.0            # (2.0 + 4.0) / 2
    assert cyc.median_cost == 3.0
    assert cyc.mean_duration == 450.0      # (300 + 600) / 2
    assert "agent_task" in stats           # plan-author step counted too


def test_collect_step_type_stats_ignores_unpriced_and_untimed_steps():
    man = _completed_manifest("run-1")
    # A step with no cost and no timestamps contributes to n_steps but not the
    # cost/duration distributions.
    man.steps.append(StepRecord(id="gate", type="human_gate"))
    stats = {s.step_type: s for s in collect_step_type_stats([man])}
    gate = stats["human_gate"]
    assert gate.n_steps == 1
    assert gate.mean_cost is None and gate.mean_duration is None


# --- render_plan_author_history: P7-A1 (stats block) ------------------------
def test_render_history_stats_block_with_completed_run(tmp_path: Path):
    run_root = tmp_path / "runs"
    _write_run(run_root, "demo", _completed_manifest("run-2026-06-13T00-00-00"))
    block = render_plan_author_history(run_root, max_frs_per_phase=3)
    assert "measured phase history for sizing" in block
    assert "Measured per-step-type cost/duration across 1 completed run(s)" in block
    assert "adversarial_cycle" in block
    assert "$1.8000" in block              # the cycle's cost is surfaced
    assert "Per-run cost per phase" in block
    assert "run-2026-06-13T00-00-00" in block
    assert "no measured per-phase costs" not in block  # NOT the no-history block


# --- render_plan_author_history: P7-A2 (no-history block) -------------------
def test_render_history_no_history_block_when_empty(tmp_path: Path):
    block = render_plan_author_history(tmp_path / "runs", max_frs_per_phase=3)
    assert "No completed run history is available in this repo yet" in block
    assert "no measured per-phase costs to size against" in block
    # An empty history renders the explicit block, not silence.
    assert block.strip() != ""
    assert "Measured per-step-type" not in block


def test_render_history_no_history_when_only_parked_runs(tmp_path: Path):
    run_root = tmp_path / "runs"
    _write_run(run_root, "demo", _completed_manifest("run-x", status=RUN_PARKED))
    block = render_plan_author_history(run_root, max_frs_per_phase=3)
    assert "No completed run history is available" in block


# --- render_plan_author_history: P7-A3 (size bound) -------------------------
def test_render_history_states_size_bound_with_history(tmp_path: Path):
    run_root = tmp_path / "runs"
    _write_run(run_root, "demo", _completed_manifest("run-1"))
    block = render_plan_author_history(run_root, max_frs_per_phase=3)
    assert "max_frs_per_phase = 3" in block


def test_render_history_states_size_bound_without_history(tmp_path: Path):
    block = render_plan_author_history(tmp_path / "runs", max_frs_per_phase=5)
    assert "max_frs_per_phase = 5" in block  # the configured, non-default bound


# --- window budget (harness-efficiency FR-10) -------------------------------
def test_render_history_includes_window_budget_when_configured(tmp_path: Path):
    providers = {
        "anthropic": ProviderWindow(window_hours=5, window_budget=1000000, budget_unit="tokens"),
    }
    block = render_plan_author_history(
        tmp_path / "runs", max_frs_per_phase=3, providers=providers
    )
    assert "Provider window budget" in block
    assert "anthropic" in block and "1000000 tokens per 5h window" in block


def test_render_history_omits_window_budget_when_none(tmp_path: Path):
    block = render_plan_author_history(tmp_path / "runs", max_frs_per_phase=3)
    assert "Provider window budget" not in block


# --- injection through _render_prompt ---------------------------------------
def _plan_author_ctx(repo: Path, config: RunConfig | None = None) -> StepContext:
    artifact_root = repo / "runs" / "demo"
    artifact_root.mkdir(parents=True, exist_ok=True)
    cfg = config or RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "claude-code"}}}
    )
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    return StepContext(
        repo_root=repo, run_dir=artifact_root / "run-1", artifact_root=artifact_root,
        config=cfg, pipeline=Pipeline.model_validate(
            {"name": "demo", "version": 1, "stages": []}),
        manifest=man, record=StepRecord(id="plan-author", type="agent_task"),
        writer=RedactingWriter(),
    )


def _write_plan_author_template(repo: Path) -> None:
    (repo / "prompts").mkdir(parents=True, exist_ok=True)
    (repo / "prompts" / "plan-author.md").write_text("BASE PLAN AUTHOR PROMPT\n")


def test_render_prompt_injects_history_for_plan_author(fixture_repo):
    _write_plan_author_template(fixture_repo)
    ctx = _plan_author_ctx(fixture_repo)
    _write_run(fixture_repo / "runs", "demo", _completed_manifest("run-2026-06-13T00-00-00"))
    (ctx.artifact_root / "prd.md").write_text("PRD BODY\n")
    step = Step.model_validate({
        "id": "plan-author", "type": "agent_task", "agent": "builder",
        "prompt": "prompts/plan-author.md", "inputs": ["prd.md"],
    })
    prompt = _render_prompt(step, ctx)
    assert "BASE PLAN AUTHOR PROMPT" in prompt              # template body
    assert "PRD BODY" in prompt                             # input artifact
    assert "measured phase history for sizing" in prompt    # P7-A1 injected block
    assert "max_frs_per_phase = 3" in prompt                # P7-A3 size bound


def test_render_prompt_injects_no_history_block_for_plan_author(fixture_repo):
    _write_plan_author_template(fixture_repo)
    ctx = _plan_author_ctx(fixture_repo)  # no completed runs on disk
    step = Step.model_validate({
        "id": "plan-author", "type": "agent_task", "agent": "builder",
        "prompt": "prompts/plan-author.md",
    })
    prompt = _render_prompt(step, ctx)
    assert "No completed run history is available in this repo yet" in prompt  # P7-A2
    assert "max_frs_per_phase = 3" in prompt


def test_render_prompt_no_history_block_for_non_plan_author_step(fixture_repo):
    ctx = _plan_author_ctx(fixture_repo)
    _write_run(fixture_repo / "runs", "demo", _completed_manifest("run-1"))
    step = Step.model_validate({
        "id": "implement", "type": "agent_task", "agent": "builder",
        "prompt_text": "SOME OTHER STEP",
    })
    prompt = _render_prompt(step, ctx)
    assert "measured phase history for sizing" not in prompt
    assert "max_frs_per_phase" not in prompt

"""`gauntlet report` cost breakdown (P5, FR-3.2 / FR-3 acceptance)."""

from __future__ import annotations

import pytest

from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord, UsageTotals
from gauntlet.engine.report import build_report, render_report


def _usage(i, o, cost=None):
    return UsageTotals(input_tokens=i, output_tokens=o, cost_usd=cost)


def _manifest() -> Manifest:
    man = Manifest(
        run_id="run-x", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:h"),
    )
    # One implement step (builder) + one cycle step (reviewer + triage split).
    man.steps.append(StepRecord(id="implement", type="agent_task", agent="builder",
                                usage=_usage(900, 100, 0.90)))
    man.steps.append(StepRecord(id="impl-cycle", type="adversarial_cycle",
                                usage=_usage(120, 30, 0.10)))
    man.agent_usage["builder"] = _usage(900, 100, 0.90)
    man.agent_usage["reviewer"] = _usage(100, 20, 0.07)
    man.agent_usage["triage"] = _usage(20, 10, 0.03)  # classification: < 5%
    man.totals = _usage(1020, 130, 1.00)
    return man


def test_per_agent_percentages_and_classification_under_5pct():
    data = build_report(_manifest())
    by = {a.agent: a for a in data.agents}
    assert by["builder"].pct_cost == pytest.approx(90.0)
    assert by["reviewer"].pct_cost == pytest.approx(7.0)
    # FR-3 acceptance: the classification profile (triage) is < 5% of run cost.
    assert by["triage"].pct_cost == pytest.approx(3.0)
    assert by["triage"].pct_cost < 5.0
    assert data.total_cost == 1.0
    assert not data.tokens_only


def test_render_has_per_step_and_per_agent_tables():
    text = render_report(_manifest())
    assert "Per agent profile:" in text
    assert "Per step:" in text
    assert "builder" in text and "triage" in text
    assert "impl-cycle" in text  # per-step row
    assert "$1.0000" in text     # total cost
    assert "3.0%" in text        # triage share


def test_tokens_only_is_flagged_as_estimate():
    man = Manifest(
        run_id="r", slug="d", branch="b", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
    )
    # No cost reported anywhere (subscription-auth CLI; PRD §12 Q3).
    man.steps.append(StepRecord(id="implement", type="agent_task", agent="builder",
                                usage=_usage(500, 50)))
    man.agent_usage["builder"] = _usage(500, 50)
    man.totals = _usage(500, 50)
    data = build_report(man)
    assert data.total_cost is None
    assert data.tokens_only
    text = render_report(man)
    assert "tokens only" in text
    assert "estimates" in text.lower()


def test_empty_manifest_renders_without_error():
    man = Manifest(
        run_id="r", slug="d", branch="b", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
    )
    text = render_report(man)
    assert "no per-agent usage recorded" in text


# --- FR-7.4: cache-effectiveness columns per step type and per profile -------
def _cached(i, o, cached, cost=None):
    return UsageTotals(input_tokens=i, output_tokens=o,
                       cached_input_tokens=cached, cost_usd=cost)


def _cache_manifest() -> Manifest:
    man = Manifest(
        run_id="run-x", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:h"),
    )
    # implement: 250 fresh + 750 cached input → 75% cache read.
    man.steps.append(StepRecord(id="implement", type="agent_task", agent="builder",
                                usage=_cached(250, 100, 750, 0.90)))
    # a second agent_task with no cache reads at all → real 0%, not blank.
    man.steps.append(StepRecord(id="plan", type="agent_task", agent="builder",
                                usage=_cached(200, 40, 0, 0.10)))
    man.agent_usage["builder"] = _cached(450, 140, 750, 1.00)
    man.agent_usage["reviewer"] = _cached(0, 0, 0)  # no ingest → share is None
    man.totals = _cached(450, 140, 750, 1.00)
    return man


def test_cache_read_share_per_profile_and_step_type():
    data = build_report(_cache_manifest())
    by_agent = {a.agent: a for a in data.agents}
    # builder: 750 cached / (450 fresh + 750 cached) = 62.5%.
    assert by_agent["builder"].cached_input_tokens == 750
    assert by_agent["builder"].cache_read_share == pytest.approx(62.5)
    # reviewer had no ingest at all → share is None (rendered "—", not "0%").
    assert by_agent["reviewer"].cache_read_share is None
    # per step type: both steps are agent_task → aggregated 750 / (450 + 750).
    by_type = {s.step_type: s for s in data.step_types}
    assert "agent_task" in by_type
    assert by_type["agent_task"].cache_read_share == pytest.approx(62.5)
    # run-level cache-read share.
    assert data.total_cache_read_share == pytest.approx(62.5)


# --- FR-7.4: cold-start fresh-input metric (resume-capable profiles) ---------
def _cold_manifest() -> Manifest:
    man = Manifest(
        run_id="run-x", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:h"),
    )
    # builder supports resume: two steps share one session. The FIRST is the cold
    # start (800 fresh ingest); the second is a warm continuation (100 fresh, 700
    # served from cache) — only the first counts toward cold-start fresh input.
    man.steps.append(StepRecord(id="plan", type="agent_task", agent="builder",
                                session_id="sess-b", usage=_cached(800, 100, 0, 0.8)))
    man.steps.append(StepRecord(id="implement", type="agent_task", agent="builder",
                                session_id="sess-b", usage=_cached(100, 40, 700, 0.2)))
    # reviewer does NOT support resume (api adapter): its ingest is never a cold
    # session start; a shell lint step has no agent at all.
    man.steps.append(StepRecord(id="review", type="agent_task", agent="reviewer",
                                usage=_cached(200, 20, 0, 0.1)))
    man.steps.append(StepRecord(id="lint", type="shell",
                                usage=_cached(50, 0, 0, 0.0)))
    man.agent_usage["builder"] = _cached(900, 140, 700, 1.0)
    man.agent_usage["reviewer"] = _cached(200, 20, 0, 0.1)
    man.totals = _cached(1150, 160, 700, 1.1)
    return man


def test_cold_start_counts_only_first_turn_of_a_resumed_session():
    data = build_report(_cold_manifest(), resume_capable={"builder"})
    by_agent = {a.agent: a for a in data.agents}
    # builder's cold-start fresh input is the first-turn ingest (800), NOT its
    # total input (900) — the warm continuation's 100 fresh tokens are excluded.
    assert by_agent["builder"].cold_start_input_tokens == 800
    assert by_agent["builder"].input_tokens == 900
    # reviewer is not resume-capable → the metric does not apply (None → "—"),
    # distinct from a real 0.
    assert by_agent["reviewer"].cold_start_input_tokens is None
    # run-level cold-start total is the sum of cold starts (just the 800).
    assert data.total_cold_start_input == 800


def test_cold_start_per_step_type_excludes_non_resume_types():
    data = build_report(_cold_manifest(), resume_capable={"builder"})
    by_type = {s.step_type: s for s in data.step_types}
    # agent_task carries at least one resume-capable step → cold-start = 800.
    assert by_type["agent_task"].cold_start_input_tokens == 800
    # shell has no resume-capable step → the metric is N/A (None, not 0).
    assert by_type["shell"].cold_start_input_tokens is None


def test_cold_start_none_when_no_resume_profiles_known():
    # Absent capability info (the default), the metric is unavailable everywhere —
    # never fabricated from total input.
    data = build_report(_cold_manifest())
    assert data.total_cold_start_input is None
    assert all(a.cold_start_input_tokens is None for a in data.agents)


def test_render_shows_cold_start_column():
    text = render_report(_cold_manifest(), resume_capable={"builder"})
    assert "cold-in" in text            # the new column header
    assert "cold-start fresh input" in text  # per-step-type section title
    assert "800" in text                # builder's cold-start ingest
    assert "cold-start in" in text      # totals line


def test_zero_cached_renders_zero_percent_not_blank():
    # A profile/step with fresh input but zero cache reads is 0.0%, never blank —
    # "no cache benefit" must be distinguishable from "no usage" (FR-7.4).
    man = Manifest(
        run_id="r", slug="d", branch="b", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
    )
    man.steps.append(StepRecord(id="s", type="agent_task", agent="builder",
                                usage=_cached(500, 50, 0, 0.5)))
    man.agent_usage["builder"] = _cached(500, 50, 0, 0.5)
    man.totals = _cached(500, 50, 0, 0.5)
    data = build_report(man)
    assert data.agents[0].cache_read_share == 0.0
    text = render_report(man)
    assert "0.0%" in text          # rendered as a real 0%, not "—"
    assert "cache%" in text        # the per-profile + per-step-type columns
    assert "Cache read share per step type" in text

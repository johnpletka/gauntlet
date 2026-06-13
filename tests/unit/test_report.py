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

"""`gauntlet report --trend` metrics (P7, FR-6.6).

Trend math is computed from the manifest (the cycle persists its per-round
tallies into StepRecord.metrics), so this exercises the math against fixture
manifests — the plan's P7 test strategy. Judge ask-rate reads judge-audit.jsonl.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.engine.manifest import (
    CommitRecord,
    Manifest,
    PipelineRef,
    StepRecord,
    UsageTotals,
)
from gauntlet.engine.trend import build_run_trend, judge_ask_rate, render_trend


def _manifest() -> Manifest:
    man = Manifest(
        run_id="run-2026-06-13T00-00-00", slug="demo", branch="b", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="h"),
    )
    # One cycle step: 2 rounds, 4 findings, verdicts + confirm tallies.
    man.steps.append(StepRecord(
        id="impl-cycle", type="adversarial_cycle",
        metrics={
            "rounds": 2, "findings_total": 4, "accepted_total": 3,
            "accepted_resolved_total": 2,
            "verdict_counts": {"legitimate": 3, "bikeshedding": 1},
            "confirm_counts": {"resolved": 2, "unresolved": 1},
        },
    ))
    # `attempts` IS the failure count (FR-6): a tests step that failed twice
    # then passed has attempts == 2 → 2 loops (review F-004).
    man.steps.append(StepRecord(id="tests", type="shell", attempts=2, iteration="0"))
    # A single fail-then-pass (attempts == 1) → 1 loop. The old `attempts - 1`
    # math dropped this case entirely (review F-004); it must count now.
    man.steps.append(StepRecord(id="tests", type="shell", attempts=1, iteration="1"))
    man.commits.append(CommitRecord(step_id="phase-commit", phase="P1", sha="a" * 40))
    man.commits.append(CommitRecord(step_id="impl-cycle", phase="P1.1", sha="b" * 40))
    man.totals = UsageTotals(input_tokens=1000, output_tokens=200, cost_usd=2.0)
    return man


def test_trend_math_from_fixture_manifest():
    t = build_run_trend(_manifest())
    assert t.rounds == 2
    assert t.findings_total == 4
    assert t.findings_per_round == pytest.approx(2.0)
    # 3 of 4 verdicts legitimate
    assert t.pct_legitimate == pytest.approx(75.0)
    # 2 of 3 ACCEPTED fixes survived the confirm pass (declined findings'
    # expected `unresolved` verdicts are excluded from the denominator — F-004)
    assert t.fix_survival == pytest.approx(2 / 3 * 100)
    # 2 (failed-twice) + 1 (single fail-then-pass) loops — the single-failure
    # step is no longer dropped (review F-004).
    assert t.test_failure_loops == 3
    # one numbered phase (P1); P1.1 collapses to P1
    assert t.phases == 1
    assert t.cost_per_phase == pytest.approx(2.0)


def test_fix_survival_ignores_declined_findings(tmp_path: Path):
    # F-004: a round can accept 1 finding (resolved) and decline 3 others, whose
    # confirm verdicts are the expected `unresolved`. Survival must be 100% (the
    # one accepted fix held), NOT 1/4 — declined verdicts are not failed fixes.
    man = Manifest(
        run_id="r", slug="d", branch="b", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
    )
    man.steps.append(StepRecord(
        id="c", type="adversarial_cycle",
        metrics={
            "rounds": 1, "findings_total": 4, "accepted_total": 1,
            "accepted_resolved_total": 1,
            "verdict_counts": {"legitimate": 1, "bikeshedding": 3},
            "confirm_counts": {"resolved": 1, "unresolved": 3},
        },
    ))
    t = build_run_trend(man)
    assert t.fix_survival == pytest.approx(100.0)


def test_trend_handles_run_without_metrics():
    man = Manifest(
        run_id="r", slug="d", branch="b", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
    )
    t = build_run_trend(man)
    assert t.findings_per_round is None
    assert t.pct_legitimate is None
    assert t.fix_survival is None
    assert t.phases == 0
    assert t.cost_per_phase is None
    # renders without error even with nothing to show
    assert "trend" in render_trend([t]).lower()


def test_judge_ask_rate_from_audit(tmp_path: Path):
    audit = tmp_path / "judge-audit.jsonl"
    lines = [
        {"decision": "allow", "source": "fast-path"},
        {"decision": "allow", "source": "llm"},     # an ask→classify
        {"decision": "deny", "source": "llm"},       # an ask→classify
        {"decision": "allow", "source": "fast-path"},
    ]
    audit.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    assert judge_ask_rate(audit) == pytest.approx(50.0)
    assert judge_ask_rate(tmp_path / "absent.jsonl") is None


def test_trend_ask_rate_wired_through_build(tmp_path: Path):
    audit = tmp_path / "judge-audit.jsonl"
    audit.write_text(json.dumps({"decision": "allow", "source": "llm"}) + "\n")
    t = build_run_trend(_manifest(), judge_audit_path=audit)
    assert t.judge_ask_rate == pytest.approx(100.0)

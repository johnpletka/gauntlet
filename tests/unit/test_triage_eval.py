"""Triage-eval math (plan P4 / review F-009): corpus load, scoring, reporting."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.engine.triage_eval import (
    CorpusEntry,
    evaluate,
    load_corpus,
    render_report,
)

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "prompts" / "triage-corpus.jsonl"


def E(eid, severity, label_verdict, label_action="fix_now"):
    return CorpusEntry(
        id=eid, source="test", context="ctx",
        finding={"id": eid, "severity": severity, "category": "correctness",
                 "location": "x", "claim": "c", "evidence": "e"},
        label={"verdict": label_verdict, "action": label_action},
    )


def P(verdict, action="fix_now", confidence="high"):
    return {"verdict": verdict, "action": action, "confidence": confidence,
            "reasoning": "r"}


# --- the shipped corpus -------------------------------------------------------
def test_corpus_loads_and_is_big_and_stratified_enough():
    entries = load_corpus(CORPUS)
    assert len(entries) >= 30  # PRD §9: hand-labeled set of ~30 findings
    severities = {e.severity for e in entries}
    assert {"blocking", "major", "minor"} <= severities  # stratified (F-009)
    assert sum(1 for e in entries if e.severity == "blocking") >= 5
    for e in entries:
        assert e.label["verdict"] in (
            "legitimate", "bikeshedding", "premature_optimization", "not_applicable"
        )
        assert e.finding["claim"] and e.finding["evidence"] and e.context


def test_corpus_findings_conform_to_findings_schema():
    from gauntlet.adapters._structured import validate_schema

    schema = json.loads((REPO / "schemas" / "findings.json").read_text())
    entries = load_corpus(CORPUS)
    validate_schema(
        {"findings": [e.finding for e in entries],
         "open_questions": [], "summary": "corpus conformance check"},
        schema,
    )


# --- scoring -------------------------------------------------------------------
def test_evaluate_counts_agreement_and_confusion():
    entries = [E("a", "blocking", "legitimate"), E("b", "minor", "bikeshedding")]
    report = evaluate(entries, {
        "a": P("legitimate"),
        "b": P("legitimate"),  # disagreement
    })
    assert report.total == 2 and report.agreements == 1
    assert report.agreement == 0.5
    assert report.confusion["blocking"][("legitimate", "legitimate")] == 1
    assert report.confusion["minor"][("bikeshedding", "legitimate")] == 1
    assert report.disagreements[0]["id"] == "b"


def test_blocking_reject_miss_is_caught_by_escalation_rule():
    # The shipped needs_escalation rule escalates EVERY blocking finding, so a
    # blocking->reject miss can never be unescalated; the exit criterion is
    # the live proof that this structural guarantee holds.
    entries = [E("a", "blocking", "legitimate")]
    report = evaluate(entries, {"a": P("not_applicable", action="reject")})
    assert report.blocking_misses_escalated == ["a"]
    assert report.blocking_misses_unescalated == []
    assert report.passes_exit_criteria() is False  # 0% agreement fails anyway


def test_exit_criteria_thresholds():
    entries = [E(str(i), "major", "legitimate") for i in range(20)]
    perfect = {str(i): P("legitimate") for i in range(20)}
    assert evaluate(entries, perfect).passes_exit_criteria()
    three_wrong = dict(perfect)
    for i in range(3):
        three_wrong[str(i)] = P("bikeshedding")
    assert evaluate(entries, three_wrong).agreement == 0.85
    assert evaluate(entries, three_wrong).passes_exit_criteria()
    four_wrong = dict(three_wrong)
    four_wrong["3"] = P("bikeshedding")
    assert not evaluate(entries, four_wrong).passes_exit_criteria()


def test_render_report_contains_matrix_and_verdict():
    entries = [E("a", "blocking", "legitimate")]
    report = evaluate(entries, {"a": P("legitimate")})
    md = render_report(report, model="cheap-model", corpus_path="c.jsonl")
    assert "100.0%" in md and "### blocking (n=1)" in md
    assert "**PASS**" in md and "cheap-model" in md

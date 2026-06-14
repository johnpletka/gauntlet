"""P4 assumption test: cheap-model triage accuracy over the hand-labeled corpus.

Runs the *configured* triage profile (``.gauntlet/config.yaml``) over
``prompts/triage-corpus.jsonl`` with the exact prompt the cycle ships, and
asserts the plan's exit criteria: ≥ 85% verdict agreement AND zero
blocking-severity findings misclassified into a reject category without
escalation, reported as a per-severity confusion matrix (review F-009).

The report is written (through the redacting writer) to
``runs/gauntlet-bootstrap/manual/p4-triage-accuracy.md`` — the recorded
artifact the exit criteria require.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gauntlet.engine.config import RunConfig
from gauntlet.engine.cycle import triage_prompt
from gauntlet.engine.triage_eval import evaluate, load_corpus, render_report
from gauntlet.logging.redact import RedactingWriter

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
CORPUS = REPO / "prompts" / "triage-corpus.jsonl"
TEMPLATE = REPO / "prompts" / "triage.md"
REPORT = REPO / "runs" / "gauntlet-bootstrap" / "manual" / "p4-triage-accuracy.md"


@pytest.fixture(autouse=True)
def _need_key():
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        pytest.skip("no API key in environment for the triage-accuracy run")


def _verdict_schema() -> dict:
    schema = json.loads((REPO / "schemas" / "triage.json").read_text())
    verdict = schema["definitions"]["verdict"]
    verdict["properties"].pop("escalated", None)
    return verdict


def test_triage_accuracy_meets_p4_exit_criteria():
    config = RunConfig.load(REPO / ".gauntlet" / "config.yaml")
    adapter = config.profile("triage").build_adapter()
    model = config.profile("triage").model
    template = TEMPLATE.read_text()
    schema = _verdict_schema()
    entries = load_corpus(CORPUS)
    assert len(entries) >= 30

    predictions = {}
    for entry in entries:
        prompt = triage_prompt(template, entry.finding, context=entry.context)
        result = adapter.run(prompt, schema=schema)
        predictions[entry.id] = result.structured

    report = evaluate(entries, predictions)
    md = render_report(report, model=model, corpus_path=str(CORPUS.relative_to(REPO)))
    RedactingWriter().write_text(REPORT, md)

    assert report.agreement >= 0.85, (
        f"verdict agreement {report.agreement:.1%} < 85% — iterate the "
        f"rubric/few-shots first (plan P4); see {REPORT}"
    )
    assert not report.blocking_misses_unescalated, (
        "blocking finding(s) misclassified into a reject category WITHOUT "
        f"escalation: {report.blocking_misses_unescalated}; see {REPORT}"
    )

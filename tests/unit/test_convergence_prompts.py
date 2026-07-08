"""P9 prompt-content + triage-corpus checks (convergence honesty, FR-6.1–6.4).

The engine forcing rule and confirm-carry are deterministic (test_cycle.py); the
PROMPT half is probabilistic, so these tests pin the instructions the shipped
templates must carry: enumerated-obligation checklists (FR-6.2), remainder
capture + severity rule + intra-document consistency (FR-6.1/6.4), and the
untestable-oracle blocking rule in the review/triage prompts (FR-6.3). The
scaffold twins are held byte-identical by test_init's drift check, so testing the
root prompts is sufficient.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "prompts"


def _read(name: str) -> str:
    return (PROMPTS / name).read_text()


# --- FR-6.1 / FR-6.2 / FR-6.4: cycle-confirm.md ------------------------------
def test_cycle_confirm_has_remainder_capture_and_severity_rule():
    text = _read("cycle-confirm.md")
    assert "carried_from" in text                       # remainder capture
    assert "partially_resolved" in text
    # the FR-6.1 severity rule names the two blocking categories concretely
    assert "leakage" in text.lower()
    assert "golden" in text.lower() and "parity" in text.lower()
    assert "blocking" in text.lower() and "major" in text.lower()


def test_cycle_confirm_mirrors_enumerated_obligation_check():
    text = _read("cycle-confirm.md")
    assert "FR-6.2" in text
    assert "enumerated" in text.lower()
    assert "partially_resolved" in text  # any uncovered item ⇒ partial


def test_cycle_confirm_has_intra_document_consistency_rule():
    text = _read("cycle-confirm.md")
    assert "FR-6.4" in text
    assert "contradict" in text.lower() and "section" in text.lower()


# --- FR-6.1 / B2: cycle-rereview.md carried-remainder exemption --------------
def test_cycle_rereview_exempts_carried_remainders_from_restatement():
    # B2: the re-review prompt must NOT invite restatement of a carried
    # remainder — a remainder is unaddressed by construction at review time
    # (its fix happens later in the round), and a restatement re-enters triage
    # where a decline can silently close a pre-accepted obligation.
    text = _read("cycle-rereview.md")
    assert "carried_from" in text
    assert "pre-accepted" in text.lower()
    assert "FR-6.1" in text
    # the exemption lives in the Do-NOT list: no restating, no re-litigating
    do_not = text.split("**Do NOT:**", 1)[1]
    assert "remainder" in do_not.lower()


# --- FR-6.2: cycle-fix.md enumerated-obligation checklist --------------------
def test_cycle_fix_has_enumerated_obligation_checklist():
    text = _read("cycle-fix.md")
    assert "FR-6.2" in text
    assert "checklist" in text.lower()
    assert "deferral" in text.lower()  # state deferrals explicitly, not silently drop


# --- FR-6.3: untestable-oracle rule in review + triage prompts ---------------
def test_review_prompts_have_untestable_oracle_rule():
    for name in ("review-code.md", "review-document.md"):
        text = _read(name)
        assert "FR-6.3" in text, name
        assert "oracle" in text.lower(), name
        assert "blocking" in text.lower(), name
        assert "fixture matrix" in text.lower(), name


def test_triage_has_untestable_oracle_rule():
    text = _read("triage.md")
    assert "FR-6.3" in text
    assert "oracle" in text.lower()
    assert "fixture matrix" in text.lower()


def test_triage_corpus_encodes_untestable_oracle_case():
    # FR-6.3 acceptance: a labeled corpus entry (the PLAN F-006 case from issue
    # #49) encodes the rule for the triage-accuracy harness.
    entries = [
        json.loads(line)
        for line in (PROMPTS / "triage-corpus.jsonl").read_text().splitlines()
        if line.strip()
    ]
    oracle = next(e for e in entries if e["id"] == "issue49-F-006")
    assert oracle["label"] == {"verdict": "legitimate", "action": "fix_now"}
    assert "FR-6.3" in oracle["context"]
    assert "oracle" in oracle["finding"]["claim"].lower()

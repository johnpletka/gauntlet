"""Human feedback capture (P7, FR-6.1): feedback.md + machine mirror round-trip."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from gauntlet.engine.feedback import (
    VERDICTS,
    FeedbackData,
    TriageCorrection,
    read_feedback,
    write_feedback,
)
from gauntlet.logging.redact import RedactingWriter


def test_triage_correction_rejects_unknown_verdict():
    # review: a bad enum must not reach feedback.json -> proposal synthesis /
    # corpus seeding. The model rejects anything outside VERDICTS.
    with pytest.raises(ValidationError):
        TriageCorrection(finding_id="F-1", correct_verdict="legit")


@pytest.mark.parametrize("verdict", VERDICTS)
def test_triage_correction_accepts_every_valid_verdict(verdict):
    assert TriageCorrection(finding_id="F-1", correct_verdict=verdict).correct_verdict == verdict


def _data() -> FeedbackData:
    return FeedbackData(
        run_id="run-x",
        outcome_rating="mixed",
        reviewer_misses="missed the off-by-one in the loader",
        triage_corrections=[
            TriageCorrection(finding_id="F-007", correct_verdict="legitimate",
                             note="triager wrongly called this bikeshedding"),
        ],
        notes="overall solid",
    )


def test_write_then_read_round_trips(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    write_feedback(run_dir, _data(), RedactingWriter())
    loaded = read_feedback(run_dir)
    assert loaded is not None
    assert loaded.outcome_rating == "mixed"
    assert loaded.triage_corrections[0].finding_id == "F-007"
    assert loaded.triage_corrections[0].correct_verdict == "legitimate"


def test_feedback_md_is_human_readable(tmp_path: Path):
    run_dir = tmp_path / "run-1"
    md_path = write_feedback(run_dir, _data(), RedactingWriter())
    assert md_path.name == "feedback.md"
    text = md_path.read_text()
    assert "## Outcome rating" in text
    assert "F-007" in text and "legitimate" in text
    # machine mirror written alongside for the synthesis pass (data over inference)
    assert (run_dir / "retro" / "feedback.json").exists()


def test_read_feedback_absent_is_none(tmp_path: Path):
    assert read_feedback(tmp_path / "no-run") is None

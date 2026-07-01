"""Commit-format validator (FR-9.2/9.4)."""

from gauntlet.engine.commit_format import header_prefix, validate_commit_message

GOOD = "P3: Add the pipeline engine\n\nWhat changed and why. Validates FR-5."


def test_accepts_well_formed_phase_commit():
    assert validate_commit_message(GOOD) is None


def test_accepts_fix_round_and_reviewer_headers():
    assert validate_commit_message("P3.1: Address review — fix x\n\nF-001: ...") is None
    assert validate_commit_message("P3.r1: Reviewer-applied changes — y\n\nbody") is None


def test_accepts_review_flow_stage_label():
    # The lightweight `gauntlet review` cycle carries `phase: REVIEW`, so its
    # accepted-fix commits land as REVIEW.x / REVIEW.rx (PRD FR-8.2/FR-3.4).
    assert validate_commit_message("REVIEW.1: Address review — fix z\n\nF-001: ...") is None
    assert validate_commit_message("REVIEW.r1: Reviewer-applied changes — w\n\nbody") is None
    assert header_prefix("REVIEW.1: Address review — fix z\n\nbody") == "REVIEW.1"


def test_rejects_overlong_header():
    long = "P3: " + "x" * 80
    err = validate_commit_message(long + "\n\nbody")
    assert err and "72" in err.reason


def test_rejects_missing_phase_prefix():
    err = validate_commit_message("Add a thing\n\nbody")
    assert err and "PN" in err.reason


def test_rejects_non_blank_second_line():
    err = validate_commit_message("P1: ok\nbody immediately")
    assert err and "blank" in err.reason


def test_rejects_empty_body():
    err = validate_commit_message("P1: ok\n\n   ")
    assert err and "body" in err.reason


def test_rejects_empty_message():
    assert validate_commit_message("") is not None


def test_header_prefix_extraction():
    assert header_prefix(GOOD) == "P3"
    assert header_prefix("P3.1: x\n\ny") == "P3.1"
    assert header_prefix("P3.r2: x\n\ny") == "P3.r2"
    assert header_prefix("nope") is None


# --- PRD/PLAN stage labels (FR-10.4 resolution, BOOTSTRAP-NOTES #28) ----------
def test_accepts_document_cycle_stage_labels():
    for header in ("PRD: Tighten the problem statement",
                   "PRD.1: Address review — close 3 findings",
                   "PLAN.2: Address review — rescope P6",
                   "PLAN.r1: Reviewer-applied changes — 1 path(s)"):
        assert validate_commit_message(header + "\n\nbody.") is None, header


def test_stage_label_prefix_extraction():
    assert header_prefix("PRD.1: x\n\ny") == "PRD.1"
    assert header_prefix("PLAN.r1: x\n\ny") == "PLAN.r1"
    assert header_prefix("PRD: x\n\ny") == "PRD"


def test_rejects_arbitrary_word_prefixes():
    # The amendment admits exactly PRD/PLAN, not free-text stage names.
    for header in ("DOCS: update readme", "PRDX.1: sneaky", "PLANB: nope",
                   "prd.1: lowercase"):
        assert validate_commit_message(header + "\n\nbody.") is not None, header

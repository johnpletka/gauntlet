"""Commit-format validator (FR-9.2/9.4)."""

from gauntlet.engine.commit_format import header_prefix, validate_commit_message

GOOD = "P3: Add the pipeline engine\n\nWhat changed and why. Validates FR-5."


def test_accepts_well_formed_phase_commit():
    assert validate_commit_message(GOOD) is None


def test_accepts_fix_round_and_reviewer_headers():
    assert validate_commit_message("P3.1: Address review — fix x\n\nF-001: ...") is None
    assert validate_commit_message("P3.r1: Reviewer-applied changes — y\n\nbody") is None


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

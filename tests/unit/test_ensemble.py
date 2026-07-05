"""Deterministic pre-triage merge/dedup for ensemble review (FR-1.2, PRD §6).

Every §6 rule has a unit here: location-string parsing for each grammar form,
line-range∩line-range (incl. touching endpoints), section-prefix (`4` vs `4.2`
vs `42`), line-vs-whole-file, non-overlap (adjacent sections, disjoint ranges,
different files), invalid-location fail-open, the claim keyword-core threshold,
the crafted overlapping merge (duplicate marked + sources aggregated + one
primary), the distinct-claim case kept as two primaries, and the severity
tie-break with panel-order then lexicographic-id tie-breaks.
"""

from __future__ import annotations

from gauntlet.engine import ensemble as E


# --- location parsing (each grammar form) ------------------------------------
def test_parse_single_line():
    loc = E.parse_location("src/foo.py:12")
    assert (loc.file, loc.start, loc.end, loc.section, loc.valid) == (
        "src/foo.py", 12, 12, None, True
    )


def test_parse_inclusive_range():
    loc = E.parse_location("src/foo.py:12-15")
    assert (loc.start, loc.end, loc.section) == (12, 15, None)


def test_parse_range_normalizes_reversed_endpoints():
    loc = E.parse_location("src/foo.py:15-12")
    assert (loc.start, loc.end) == (12, 15)


def test_parse_section_paragraph_and_hash_and_bare():
    for raw, expected in [
        ("prd.md:§5", "5"),
        ("prd.md:#5.2", "5.2"),
        ("plan.md:overview", "overview"),
        ("prd.md:§ FR-6.1", "fr-6.1"),
    ]:
        loc = E.parse_location(raw)
        assert loc.section == expected, raw
        assert loc.start is None and loc.end is None


def test_parse_whole_file_forms():
    for raw in ("src/foo.py", "src/foo.py:", "  src/foo.py  "):
        loc = E.parse_location(raw)
        assert loc.whole_file, raw
        assert loc.file == "src/foo.py"


def test_parse_invalid_numericish_location():
    for raw in ("src/foo.py:12-", "src/foo.py:1-2-3", "src/foo.py:-5"):
        loc = E.parse_location(raw)
        assert loc.valid is False, raw


def test_parse_none_and_empty():
    assert E.parse_location(None).whole_file is False  # file is None
    assert E.parse_location(None).file is None
    assert E.parse_location("").file is None


# --- line-range overlap (incl. touching endpoints) ---------------------------
def _line(file, a, b):
    return E.Location(file=file, start=a, end=b, section=None)


def test_line_ranges_overlap_and_disjoint():
    assert E.locations_overlap(_line("f", 10, 20), _line("f", 15, 25))
    assert E.locations_overlap(_line("f", 10, 20), _line("f", 20, 30))  # touching
    assert not E.locations_overlap(_line("f", 10, 20), _line("f", 21, 30))


def test_line_ranges_different_file_never_overlap():
    assert not E.locations_overlap(_line("a", 10, 20), _line("b", 10, 20))


# --- section-prefix (4 vs 4.2 vs 42) -----------------------------------------
def test_section_prefix_rule():
    assert E.section_is_prefix("4", "4.2")
    assert E.section_is_prefix("4", "4")
    assert not E.section_is_prefix("4", "42")
    assert not E.section_is_prefix("4.2", "4.3")


def _sec(file, s):
    return E.Location(file=file, start=None, end=None, section=s)


def test_section_overlap_uses_prefix():
    assert E.locations_overlap(_sec("d", "5"), _sec("d", "5.2"))
    assert not E.locations_overlap(_sec("d", "5.1"), _sec("d", "5.2"))  # adjacent
    assert not E.locations_overlap(_sec("d", "5"), _sec("d", "52"))


# --- mixed line-vs-whole-file ------------------------------------------------
def _wf(file):
    return E.Location(file=file, start=None, end=None, section=None)


def test_whole_file_overlaps_line_and_section_same_file():
    assert E.locations_overlap(_wf("f"), _line("f", 3, 9))
    assert E.locations_overlap(_line("f", 3, 9), _wf("f"))
    assert E.locations_overlap(_wf("f"), _sec("f", "2"))
    assert E.locations_overlap(_wf("f"), _wf("f"))


def test_whole_file_different_file_never_overlaps():
    assert not E.locations_overlap(_wf("a"), _line("b", 1, 2))


def test_line_and_section_never_overlap_across_kinds():
    # neither is whole-file: a bare line range and a section in the same file
    assert not E.locations_overlap(_line("f", 1, 5), _sec("f", "5"))


# --- invalid location (fail-open, no drop) -----------------------------------
def test_invalid_location_overlaps_nothing():
    bad = E.parse_location("f:1-2-3")
    assert bad.valid is False
    assert not E.locations_overlap(bad, _line("f", 1, 2))
    assert not E.locations_overlap(bad, _wf("f"))
    assert not E.locations_overlap(bad, bad)


# --- claim fingerprint + keyword-core threshold ------------------------------
def test_fingerprint_drops_stopwords_no_stemming():
    fp = E.claim_fingerprint("The handoff is not committed before the reviewer runs")
    assert "handoff" in fp and "committed" in fp and "reviewer" in fp
    assert "the" not in fp and "is" not in fp and "before" not in fp
    # "not" is a negation, deliberately retained (fail toward keeping)
    assert "not" in fp


def test_keyword_core_at_and_below_threshold():
    a = E.claim_fingerprint("resume reuses the completed member artifact")
    b = E.claim_fingerprint("resume reuses completed member artifact again")
    # identical content tokens -> Jaccard 1.0 -> share core
    assert E.fingerprints_share_core(a, b, 0.5)
    lo = E.claim_fingerprint("window admission underestimates panel cost")
    hi = E.claim_fingerprint("window admission scales estimate by panel size")
    # {window, admission, underestimates, panel, cost} vs
    # {window, admission, scales, estimate, panel, size}: 3/8 = 0.375 < 0.5
    assert not E.fingerprints_share_core(lo, hi, 0.5)


def test_empty_fingerprint_shares_nothing():
    assert not E.fingerprints_share_core(frozenset(), E.claim_fingerprint("x y z"), 0.5)


# --- merge: crafted overlapping pair -> one primary, marked duplicate --------
def _f(fid, sev, cat, loc, claim, source, lens):
    return {
        "id": fid, "severity": sev, "category": cat, "location": loc,
        "claim": claim, "evidence": "e", "suggested_fix": None,
        "source": source, "lens": lens,
    }


PANEL = {"codex": 0, "gemini": 1}


def test_overlapping_findings_merge_to_one_primary_with_sources():
    a = _f("codex:F-001", "major", "correctness", "src/x.py:10-20",
           "the counter overflows past the max window budget", "codex", "correctness")
    b = _f("gemini:F-001", "major", "correctness", "src/x.py:15",
           "counter overflows past max window budget silently", "gemini", "spec-coverage")
    res = E.merge_findings([a, b], panel_order=PANEL, threshold=0.5)
    assert len(res.primaries) == 1
    assert len(res.duplicates) == 1
    primary = res.primaries[0]
    dup = res.duplicates[0]
    assert dup["duplicate_of"] == primary["id"]
    assert set(primary["sources"]) == {"codex", "gemini"}
    assert "duplicate_of" not in primary
    # owner map: both findings resolve to the primary
    assert res.owner_of["codex:F-001"] == primary["id"]
    assert res.owner_of["gemini:F-001"] == primary["id"]


# --- distinct-claim case (same file+overlap+category, divergent claims) ------
def test_distinct_claims_kept_as_two_primaries():
    # whole-file finding overlaps every finding in the file (mixed rule), same
    # category, but the claim fingerprints diverge below threshold -> no merge.
    whole = _f("codex:F-002", "major", "security", "src/y.py",
               "secrets are logged to the transcript in plaintext", "codex", "security")
    line = _f("gemini:F-002", "major", "security", "src/y.py:42",
              "network egress is permitted under the default deny posture", "gemini", "security")
    res = E.merge_findings([whole, line], panel_order=PANEL, threshold=0.5)
    assert len(res.primaries) == 2
    assert res.duplicates == []
    assert all("duplicate_of" not in p for p in res.primaries)


def test_different_file_never_merges():
    a = _f("codex:F-003", "minor", "style", "a.py:1", "same claim words here", "codex", "correctness")
    b = _f("gemini:F-003", "minor", "style", "b.py:1", "same claim words here", "gemini", "spec-coverage")
    res = E.merge_findings([a, b], panel_order=PANEL, threshold=0.5)
    assert len(res.primaries) == 2


# --- severity tie-break: highest severity is primary -------------------------
def test_severity_tiebreak_picks_highest_then_panel_then_id():
    lo = _f("gemini:F-010", "minor", "correctness", "z.py:5",
            "off by one error in the loop bound", "gemini", "spec-coverage")
    hi = _f("codex:F-010", "blocking", "correctness", "z.py:5",
            "off by one error in the loop bound", "codex", "correctness")
    res = E.merge_findings([lo, hi], panel_order=PANEL, threshold=0.5)
    assert len(res.primaries) == 1
    assert res.primaries[0]["severity"] == "blocking"
    assert res.primaries[0]["source"] == "codex"
    assert res.duplicates[0]["duplicate_of"] == "codex:F-010"


def test_severity_tie_uses_panel_order_then_id():
    # same severity, so tie-break falls to panel order (codex index 0 wins).
    a = _f("gemini:F-020", "major", "correctness", "z.py:5",
           "off by one error in the loop bound", "gemini", "spec-coverage")
    b = _f("codex:F-020", "major", "correctness", "z.py:5",
           "off by one error in the loop bound", "codex", "correctness")
    res = E.merge_findings([a, b], panel_order=PANEL, threshold=0.5)
    assert res.primaries[0]["source"] == "codex"


def test_single_member_passthrough_marks_no_duplicates():
    a = _f("codex:F-001", "major", "correctness", "z.py:5", "a real defect here", "codex", "correctness")
    res = E.merge_findings([a], panel_order={"codex": 0}, threshold=0.5)
    assert res.primaries == [{**a, "sources": ["codex"]}]
    assert res.duplicates == []


def test_merge_is_deterministic_regardless_of_input_order():
    a = _f("codex:F-030", "major", "correctness", "z.py:10-20",
           "the resume path drops the completed member artifact", "codex", "correctness")
    b = _f("gemini:F-030", "major", "correctness", "z.py:15",
           "resume path drops completed member artifact on reentry", "gemini", "spec-coverage")
    c = _f("gemini:F-031", "minor", "security", "other.py",
           "an unrelated distinct security concern entirely", "gemini", "security")
    r1 = E.merge_findings([a, b, c], panel_order=PANEL, threshold=0.5)
    r2 = E.merge_findings([c, b, a], panel_order=PANEL, threshold=0.5)
    assert r1.findings == r2.findings
    assert [p["id"] for p in r1.primaries] == [p["id"] for p in r2.primaries]

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

import json
from pathlib import Path

import jsonschema
import pytest

from gauntlet.engine import ensemble as E

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "findings.json"


def _findings_schema():
    return json.loads(_SCHEMA_PATH.read_text())


def _record(finding):
    return {"findings": [finding], "open_questions": [], "summary": ""}


def test_legacy_findings_record_without_new_fields_validates():
    # A pre-ensemble / single-reviewer artifact omits source/lens/duplicate_of/
    # sources entirely; the extended schema must still accept it (FR-1.2 / F-007).
    legacy = {
        "id": "F-001", "severity": "major", "category": "correctness",
        "location": "src/x.py:10", "claim": "c", "evidence": "e",
        "suggested_fix": None,
    }
    jsonschema.validate(instance=_record(legacy), schema=_findings_schema())


def test_ensemble_primary_and_duplicate_records_validate():
    schema = _findings_schema()
    primary = {
        "id": "codex:F-001", "severity": "major", "category": "correctness",
        "location": "src/x.py:10", "claim": "c", "evidence": "e",
        "suggested_fix": None, "source": "codex", "lens": "correctness",
        "sources": ["codex", "gemini"],
    }
    duplicate = {
        "id": "gemini:F-001", "severity": "minor", "category": "correctness",
        "location": "src/x.py:12", "claim": "c", "evidence": "e",
        "suggested_fix": None, "source": "gemini", "lens": "spec-coverage",
        "duplicate_of": "codex:F-001",
    }
    jsonschema.validate(
        instance={"findings": [primary, duplicate], "open_questions": [], "summary": ""},
        schema=schema,
    )


def test_reviewer_output_schema_strips_ensemble_fields():
    # The strict per-member schema handed to the reviewer adapter must recover the
    # pre-ensemble finding shape: every property in `required`, none of the four
    # engine-annotated fields present (F-007 / byte-identical single-reviewer).
    from gauntlet.engine.cycle import _reviewer_output_schema

    strict = _reviewer_output_schema(_findings_schema())
    item = strict["properties"]["findings"]["items"]
    props = set(item["properties"])
    assert props == {"id", "severity", "category", "location", "claim", "evidence", "suggested_fix"}
    for field in E.ENSEMBLE_FIELDS:
        assert field not in props
    # strict-mode convention preserved: every property is required, no extras.
    assert set(item["required"]) == props
    assert item["additionalProperties"] is False


def test_confirmer_output_schema_promotes_carried_from_to_required():
    # F-001 (P9): the persisted confirm schema keeps `carried_from` OPTIONAL
    # (additive, PRD §6), so the strict native-output schema must be DERIVED by
    # promoting it into `required` — required-but-nullable, the F-007 convention —
    # without mutating the persisted input.
    from gauntlet.engine.cycle import _confirmer_output_schema

    persisted = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / "confirm.json").read_text()
    )
    persisted_item = persisted["properties"]["new_findings"]["items"]
    assert "carried_from" not in persisted_item["required"]  # persisted: optional

    strict = _confirmer_output_schema(persisted)
    strict_item = strict["properties"]["new_findings"]["items"]
    # every property (incl. carried_from) is required in the strict shape
    assert set(strict_item["required"]) == set(strict_item["properties"])
    assert "carried_from" in strict_item["required"]
    assert strict_item["properties"]["carried_from"]["type"] == ["string", "null"]
    assert strict_item["additionalProperties"] is False
    # derivation is non-mutating: the persisted schema is unchanged
    assert "carried_from" not in persisted_item["required"]


def test_unknown_finding_field_still_rejected():
    # additionalProperties:false is preserved — an unknown field fails closed.
    bad = {
        "id": "F-001", "severity": "major", "category": "correctness",
        "location": "x", "claim": "c", "evidence": "e", "suggested_fix": None,
        "bogus": "nope",
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=_record(bad), schema=_findings_schema())


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


def test_section_prefix_holds_for_slash_and_gt_paths():
    # PR #59 review F-001: the PRD's own §6 worked example — "§5 overlaps
    # §5/FR-6.1; §5/FR-6.1 does NOT overlap §5/FR-6.2" — must hold for the
    # heading-path notations reviewers actually emit, not only dotted ids.
    a = E.parse_location("prd.md:§5")
    b = E.parse_location("prd.md:§5/FR-6.1")
    c = E.parse_location("prd.md:§5/FR-6.2")
    assert E.locations_overlap(a, b)
    assert E.locations_overlap(b, a)          # symmetric
    assert not E.locations_overlap(b, c)      # siblings never overlap
    # ">"-separated paths and per-segment markers canonicalize identically
    d = E.parse_location("prd.md:#5 > §FR-6.1")
    assert E.locations_overlap(a, d) and E.locations_overlap(b, d)
    # whitespace inside a heading TITLE is one segment, never a separator —
    # "deployment" must not prefix-match "deployment strategy"
    assert E.parse_location("doc.md:§deployment strategy").section == "deployment strategy"
    assert not E.locations_overlap(
        E.parse_location("doc.md:§deployment"),
        E.parse_location("doc.md:§deployment strategy"),
    )


def test_common_reviewer_line_shapes_parse_as_lines():
    # PR #59 review F-008: these silently degraded to section-kind in a code
    # file, where the cross-kind rule made them undeduplicable against genuine
    # line ranges. Each must overlap a plain line cite inside its span.
    line13 = E.parse_location("src/foo.py:13")
    linecol = E.parse_location("src/foo.py:13:5")
    assert (linecol.start, linecol.end, linecol.section) == (13, 13, None)
    assert E.locations_overlap(linecol, line13)
    commas = E.parse_location("src/foo.py:12, 15")
    assert (commas.start, commas.end) == (12, 15)  # conservative envelope
    assert E.locations_overlap(commas, line13)
    annotated = E.parse_location("src/foo.py:12-14 (the loop)")
    assert (annotated.start, annotated.end) == (12, 14)
    assert E.locations_overlap(annotated, line13)


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

"""Normative schemas (PRD §7, plan P4): shape, enums, and real-artifact fit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapters._structured import validate_schema

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "schemas"
MANUAL = REPO / "runs" / "gauntlet-bootstrap" / "manual"


def _load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def test_findings_schema_enums_match_prd_section_7():
    schema = _load("findings.json")
    props = schema["properties"]["findings"]["items"]["properties"]
    assert props["severity"]["enum"] == ["blocking", "major", "minor", "nit"]
    assert props["category"]["enum"] == [
        "correctness", "spec-gap", "security", "performance",
        "principle-violation", "style",
    ]
    # §7-optional is spelled required-but-nullable: codex native structured
    # output (strict mode) demands every property in `required` (pinned, P4).
    items = schema["properties"]["findings"]["items"]
    assert "suggested_fix" in items["required"]
    assert items["properties"]["suggested_fix"]["type"] == ["string", "null"]


def test_triage_schema_enums_match_prd_section_7():
    schema = _load("triage.json")
    verdict = schema["definitions"]["verdict"]["properties"]
    assert verdict["verdict"]["enum"] == [
        "legitimate", "bikeshedding", "premature_optimization", "not_applicable",
    ]
    assert verdict["action"]["enum"] == ["fix_now", "defer", "reject"]
    # P4 additions (BOOTSTRAP-NOTES #5/#6, review F-009) live in the NEW
    # schemas only; the PRD §7 excerpt is untouched.
    assert verdict["confidence"]["enum"] == ["high", "medium", "low"]
    assert "target_artifact" in verdict


def test_confirm_schema_verdict_enum_matches_fr_9_5():
    schema = _load("confirm.json")
    verdict = schema["properties"]["verdicts"]["items"]["properties"]["verdict"]
    assert verdict["enum"] == [
        "resolved", "partially_resolved", "unresolved", "regression_introduced",
    ]


@pytest.mark.parametrize("cycle", ["p1-cycle-r1", "p2-cycle-r1", "p3-cycle-r1"])
def test_real_bootstrap_findings_validate_against_normative_schema(cycle):
    # The hand-collected P1-P3 review outputs must fit the schema that now
    # governs the cycle — otherwise the corpus and the machinery disagree.
    findings = json.loads((MANUAL / cycle / "findings.json").read_text())
    validate_schema(findings, _load("findings.json"))


def test_findings_schema_rejects_bad_severity():
    bad = {"findings": [{
        "id": "F-001", "severity": "catastrophic", "category": "style",
        "location": "x", "claim": "c", "evidence": "e",
    }]}
    with pytest.raises(ValueError):
        validate_schema(bad, _load("findings.json"))

"""Normative schemas (PRD §7, plan P4): shape, enums, and real-artifact fit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapters._structured import validate_schema

REPO = Path(__file__).resolve().parents[2]
SCHEMAS = REPO / "schemas"
SCAFFOLD_SCHEMAS = REPO / "src" / "gauntlet" / "scaffold" / "schemas"
MANUAL = REPO / "runs" / "gauntlet-bootstrap" / "manual"


def _load(name: str) -> dict:
    return json.loads((SCHEMAS / name).read_text())


def _finding(category: str) -> dict:
    """A full findings payload, valid but for the given `category`."""
    return {"findings": [{
        "id": "F-001", "severity": "major", "category": category,
        "location": "src.py:1", "claim": "c", "evidence": "e",
        "suggested_fix": None,
    }], "open_questions": [], "summary": "s"}


def test_findings_schema_enums_match_prd_section_7():
    schema = _load("findings.json")
    props = schema["properties"]["findings"]["items"]["properties"]
    assert props["severity"]["enum"] == ["blocking", "major", "minor", "nit"]
    # `behavioral` is the additive P4 migration (FR-2.4): appended, not
    # reordered, so pre-migration outputs stay valid.
    assert props["category"]["enum"] == [
        "correctness", "spec-gap", "security", "performance",
        "principle-violation", "style", "behavioral",
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


# --- P4: behavioral-category schema migration (FR-2.4) -----------------------
# The `behavioral` category is added additively across the schema and every
# category-enforcing consumer at once; these tests prove the schema half of the
# migration. Consumer flow (merge -> triage -> confirm) is exercised end-to-end
# in test_ensemble_cycle.py (P4-A2).


@pytest.mark.parametrize("category", [
    "correctness", "spec-gap", "security", "performance",
    "principle-violation", "style",
])
def test_pre_migration_categories_still_validate(category):
    # P4-A1: every pre-`behavioral` category still validates against the
    # migrated schema — the migration is additive, nothing pre-existing breaks.
    validate_schema(_finding(category), _load("findings.json"))


def test_pre_migration_findings_fixture_validates_against_migrated_schema():
    # P4-A1: a whole findings artifact authored before the migration (using only
    # the original categories, no `behavioral` value anywhere) still validates.
    pre_migration = {"findings": [
        {"id": "F-001", "severity": "blocking", "category": "correctness",
         "location": "src.py:1", "claim": "off-by-one", "evidence": "e",
         "suggested_fix": None},
        {"id": "F-002", "severity": "minor", "category": "style",
         "location": "src.py:9", "claim": "naming", "evidence": "e",
         "suggested_fix": "rename"},
    ], "open_questions": [], "summary": "s"}
    validate_schema(pre_migration, _load("findings.json"))


def test_behavioral_category_validates_after_migration():
    # P4-A2 (schema half): a finding with category `behavioral` validates — the
    # verifier's finding class is accepted end-to-end at the persisted-record
    # schema before P5 ever wires verifier execution.
    validate_schema(_finding("behavioral"), _load("findings.json"))


def test_findings_schema_rejects_unknown_category_fail_closed():
    # P4-A3: a category outside the enum is rejected at validation (fail closed);
    # it is never coerced to another category or silently dropped.
    with pytest.raises(ValueError):
        validate_schema(_finding("behaviorial"), _load("findings.json"))  # typo
    with pytest.raises(ValueError):
        validate_schema(_finding("made-up-category"), _load("findings.json"))


# --- P9: confirm remainder carry schema (FR-6.1, review F-001/F-007) ---------
# `new_findings` expands to a full findings-schema object plus the ADDITIVE,
# OPTIONAL `carried_from` field (PRD §6). The migration is additive: a
# pre-migration confirm output validates unchanged whether `new_findings` is
# empty OR carries entries that predate `carried_from`. The strict native-output
# shape (carried_from promoted into `required`, required-but-nullable) is DERIVED
# in code (cycle._confirmer_output_schema), not baked into this persisted schema.


def test_confirm_empty_new_findings_still_validates():
    # P9-A4: a pre-migration confirm output with an EMPTY new_findings validates
    # unchanged against the migrated schema.
    validate_schema(
        {"verdicts": [], "new_findings": [], "summary": "s"}, _load("confirm.json")
    )


def test_confirm_new_findings_item_carried_from_is_optional_nullable():
    # P9 / review F-001 + PR #59 review F-3: the persisted schema requires only
    # the TRUE pre-migration trio (severity, claim, location) — the additive-
    # migration contract, PRD §6. Everything the migration added or the modern
    # engine stamps (id, category, evidence, suggested_fix, carried_from) is
    # optional on the persisted record; the strict confirmer-derived schema
    # (cycle._confirmer_output_schema) is what requires the full shape natively.
    item = _load("confirm.json")["properties"]["new_findings"]["items"]
    assert item["properties"]["carried_from"]["type"] == ["string", "null"]
    assert set(item["required"]) == {"severity", "claim", "location"}


def test_confirm_pre_migration_new_findings_entry_without_carried_from_validates():
    # P9-A4 / review F-001: a `new_findings` entry without a carried_from key
    # (a modern ordinary regression) still validates — the additive-migration
    # compatibility requirement ("entries without carried_from still validate").
    legacy = {"verdicts": [], "summary": "s", "new_findings": [
        {"id": "N", "severity": "blocking", "category": "correctness",
         "location": "a.py:1", "claim": "regressed", "evidence": "e",
         "suggested_fix": None}]}
    validate_schema(legacy, _load("confirm.json"))


def test_confirm_true_pre_migration_new_findings_entry_validates():
    # PR #59 review F-3 (the P9.1 partial, finished): the GENUINE pre-migration
    # item shape was exactly {severity, claim, location} (verified against
    # main:schemas/confirm.json) — the prior fixture anachronistically carried
    # id/category/evidence/suggested_fix, a shape no pre-migration engine ever
    # emitted, so demoting only carried_from left every real legacy non-empty
    # new_findings failing on four required fields. PRD §6: "a pre-migration
    # confirm output with no new_findings (or entries without carried_from)
    # still validates" — proven here with the true legacy shape.
    true_legacy = {"verdicts": [], "summary": "s", "new_findings": [
        {"severity": "blocking", "claim": "regressed", "location": "a.py:1"}]}
    validate_schema(true_legacy, _load("confirm.json"))


def test_confirmer_strict_schema_promotes_all_item_properties():
    # The persisted-additive / strict-derived split (F-001 architecture):
    # relaxing the persisted record must NOT relax what a live confirmer may
    # emit — the native output schema still requires the full item shape.
    from gauntlet.engine.cycle import _confirmer_output_schema

    strict = _confirmer_output_schema(_load("confirm.json"))
    item = strict["properties"]["new_findings"]["items"]
    assert set(item["required"]) == set(item["properties"])
    assert "carried_from" in item["required"] and "evidence" in item["required"]


def test_confirm_diff_regression_entry_with_null_carried_from_validates():
    # P9-A4: an ordinary diff-regression new_findings entry (carried_from: null)
    # validates — proving the required-but-nullable convention, not an
    # absent-optional field, is what keeps confirm outputs valid.
    regression = {"verdicts": [], "summary": "s", "new_findings": [
        {"id": "N", "severity": "blocking", "category": "correctness",
         "location": "a.py:1", "claim": "regressed", "evidence": "e",
         "suggested_fix": None, "carried_from": None}]}
    validate_schema(regression, _load("confirm.json"))


def test_confirm_carried_remainder_entry_validates():
    # P9: a carried remainder new_findings entry (carried_from set to a finding id)
    # validates against the migrated confirm schema.
    carried = {"verdicts": [], "summary": "s", "new_findings": [
        {"id": "F-001-r1-c0", "severity": "major", "category": "correctness",
         "location": "a.py:3", "claim": "the specific remainder", "evidence": "e",
         "suggested_fix": None, "carried_from": "F-001"}]}
    validate_schema(carried, _load("confirm.json"))


def test_findings_schema_allows_optional_carried_from():
    # P9: findings.json accepts an engine-annotated carried_from (like
    # duplicate_of); a legacy finding that omits it still validates (additive).
    schema = _load("findings.json")
    carried = {"findings": [{"id": "F-001-r1-c0", "severity": "major",
        "category": "correctness", "location": "a.py:1", "claim": "c",
        "evidence": "e", "suggested_fix": None, "carried_from": "F-001"}],
        "open_questions": [], "summary": "s"}
    validate_schema(carried, schema)
    validate_schema(_finding("correctness"), schema)  # omits carried_from → still valid


def test_scaffold_confirm_schema_matches_root():
    # The scaffold copy adopters receive must carry the same P9 carry migration
    # (carried_from optional/additive) as the governing root schema — a drift would
    # ship adopters a confirm schema that rejects legacy artifacts (review F-001).
    root = (SCHEMAS / "confirm.json").read_text()
    scaffold = (SCAFFOLD_SCHEMAS / "confirm.json").read_text()
    assert root == scaffold
    item = json.loads(scaffold)["properties"]["new_findings"]["items"]
    assert "carried_from" not in item["required"]


def test_scaffold_findings_schema_matches_root_after_migration():
    # The scaffold copy adopters receive must carry the same migrated enum as the
    # governing root schema — a drift would ship adopters an unmigrated consumer.
    root = (SCHEMAS / "findings.json").read_text()
    scaffold = (SCAFFOLD_SCHEMAS / "findings.json").read_text()
    assert root == scaffold
    props = json.loads(scaffold)["properties"]["findings"]["items"]["properties"]
    assert "behavioral" in props["category"]["enum"]

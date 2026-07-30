"""Structured phase-list extraction for `foreach: plan.phases` (P5, FR-5.1)."""

from __future__ import annotations

import pytest

from gauntlet.engine.planphases import (
    PlanPhasesError,
    extract_phases,
    frs_declaration_errors,
    load_plan_phases,
    missing_phase_sections,
)

PLAN = """\
# Implementation plan

Some prose about the approach.

```gauntlet-phases
- id: P1
  title: Core model
  goal: Persist and reload; validates the schema round-trips.
- id: P2
  title: HTTP API
  goal: CRUD endpoints; validates the model covers the operations.
```

More prose, including a normal yaml example that must NOT be parsed as phases:

```yaml
some: example
```
"""


def test_extracts_phase_list():
    phases = extract_phases(PLAN)
    assert [p["id"] for p in phases] == ["P1", "P2"]
    assert phases[0]["title"] == "Core model"
    assert "round-trips" in phases[0]["goal"]


def test_no_block_returns_none():
    assert extract_phases("# plan with no phase block\n\njust prose") is None


def test_ordinary_yaml_block_is_not_mistaken_for_phases():
    text = "```yaml\n- id: P1\n  title: x\n```\n"
    assert extract_phases(text) is None


def test_missing_plan_file_returns_none(tmp_path):
    assert load_plan_phases(tmp_path / "nope.md") is None


def test_load_from_file(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text(PLAN)
    phases = load_plan_phases(p)
    assert [x["id"] for x in phases] == ["P1", "P2"]


# --- fail closed on malformed blocks ----------------------------------------
def test_two_blocks_rejected():
    text = PLAN + "\n```gauntlet-phases\n- id: P3\n  title: y\n```\n"
    with pytest.raises(PlanPhasesError, match="exactly one"):
        extract_phases(text)


def test_non_list_rejected():
    with pytest.raises(PlanPhasesError, match="non-empty YAML list"):
        extract_phases("```gauntlet-phases\nid: P1\n```\n")


def test_empty_list_rejected():
    with pytest.raises(PlanPhasesError, match="non-empty YAML list"):
        extract_phases("```gauntlet-phases\n[]\n```\n")


def test_bad_phase_id_rejected():
    text = "```gauntlet-phases\n- id: phase-one\n  title: x\n```\n"
    with pytest.raises(PlanPhasesError, match="P<n>"):
        extract_phases(text)


def test_duplicate_phase_id_rejected():
    text = (
        "```gauntlet-phases\n"
        "- id: P1\n  title: a\n  goal: do a\n"
        "- id: P1\n  title: b\n  goal: do b\n```\n"
    )
    with pytest.raises(PlanPhasesError, match="duplicate"):
        extract_phases(text)


def test_missing_title_rejected():
    text = "```gauntlet-phases\n- id: P1\n  goal: no title here\n```\n"
    with pytest.raises(PlanPhasesError, match="missing a 'title'"):
        extract_phases(text)


def test_missing_goal_rejected():
    # F-004: a phase with id+title but no goal must fail closed, not fan out.
    text = "```gauntlet-phases\n- id: P1\n  title: x\n```\n"
    with pytest.raises(PlanPhasesError, match="goal"):
        extract_phases(text)


def test_empty_goal_rejected():
    text = "```gauntlet-phases\n- id: P1\n  title: x\n  goal: '   '\n```\n"
    with pytest.raises(PlanPhasesError, match="goal"):
        extract_phases(text)


def test_invalid_yaml_rejected():
    text = "```gauntlet-phases\n- id: P1\n   : broken\n  title: x\n```\n"
    with pytest.raises(PlanPhasesError):
        extract_phases(text)


# --- declared `frs:` list — shape-when-present (#66, FR-3.4) -----------------
def _phase_with_frs(frs_yaml: str) -> str:
    return (
        "```gauntlet-phases\n"
        f"- id: P1\n  title: x\n  goal: do it\n  frs: {frs_yaml}\n```\n"
    )


def test_valid_frs_list_passes_through():
    phases = extract_phases(_phase_with_frs("[FR-1.1, FR-2]"))
    assert phases[0]["frs"] == ["FR-1.1", "FR-2"]


def test_deep_and_lettered_frs_tokens_accepted():
    # Real PRDs subdivide requirements (scheduled-restart declared FR-5.1.a);
    # rejecting a legitimate id shape would wedge the plan gate (#64's class).
    phases = extract_phases(_phase_with_frs("[FR-5.1.a, FR-1.1.1, FR-2.3.b2]"))
    assert phases[0]["frs"] == ["FR-5.1.a", "FR-1.1.1", "FR-2.3.b2"]


def test_absent_frs_is_fine():
    phases = extract_phases("```gauntlet-phases\n- id: P1\n  title: x\n  goal: g\n```\n")
    assert "frs" not in phases[0]


def test_empty_frs_list_rejected():
    # An empty declared list would silently exempt the phase from the size
    # lint — fail closed; omit the key instead.
    with pytest.raises(PlanPhasesError, match="non-empty list of FR ids"):
        extract_phases(_phase_with_frs("[]"))


def test_non_list_frs_rejected():
    with pytest.raises(PlanPhasesError, match="non-empty list of FR ids"):
        extract_phases(_phase_with_frs("FR-1.1"))


@pytest.mark.parametrize("bad", ["FR-x", "fr-1.1", "4", "FR-1 and FR-2", "FR-1."])
def test_bad_frs_token_rejected(bad):
    with pytest.raises(PlanPhasesError, match="must be an FR id"):
        extract_phases(_phase_with_frs(f"['{bad}']"))


def test_duplicate_frs_token_rejected():
    with pytest.raises(PlanPhasesError, match="duplicate frs entry"):
        extract_phases(_phase_with_frs("[FR-1.1, FR-1.1]"))


def test_frs_declaration_errors_requires_presence_per_phase():
    # The author-time gate (PR #73 review P1): a phase omitting `frs` is an
    # error HERE, while extract_phases stays lenient for pre-`frs` plans.
    phases = extract_phases(
        "```gauntlet-phases\n"
        "- id: P1\n  title: a\n  goal: g\n  frs: [FR-1.1]\n"
        "- id: P2\n  title: b\n  goal: g\n```\n"
    )
    errors = frs_declaration_errors(phases)
    assert len(errors) == 1
    assert "P2" in errors[0] and "declares no 'frs:' list" in errors[0]


def test_frs_declaration_errors_empty_when_all_declared():
    phases = extract_phases(_phase_with_frs("[FR-1.1]"))
    assert frs_declaration_errors(phases) == []


# --- locatable prose sections (F-001, FR-1.1) -------------------------------
_HEADED_PLAN = """\
# Plan

## P1 — First
p1 prose

## P2 — Second
p2 prose

```gauntlet-phases
- id: P1
  title: First
  goal: do p1
- id: P2
  title: Second
  goal: do p2
```
"""


def test_missing_phase_sections_empty_when_all_locatable():
    phases = extract_phases(_HEADED_PLAN)
    assert missing_phase_sections(_HEADED_PLAN, phases) == []


def test_missing_phase_sections_reports_phases_without_headings():
    # Same phase list, but the prose only carries a P1 heading — P2 is missing.
    text = (
        "# Plan\n\n## P1 — First\np1 prose\n\n"
        "```gauntlet-phases\n"
        "- id: P1\n  title: First\n  goal: do p1\n"
        "- id: P2\n  title: Second\n  goal: do p2\n```\n"
    )
    phases = extract_phases(text)
    assert missing_phase_sections(text, phases) == ["P2"]

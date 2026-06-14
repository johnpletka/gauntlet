"""Structured phase-list extraction for `foreach: plan.phases` (P5, FR-5.1)."""

from __future__ import annotations

import pytest

from gauntlet.engine.planphases import (
    PlanPhasesError,
    extract_phases,
    load_plan_phases,
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

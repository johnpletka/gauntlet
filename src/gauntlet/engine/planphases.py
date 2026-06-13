"""Structured phase list extraction for ``foreach: plan.phases`` (FR-5.1).

The plan-author writes the human-readable ``plan.md`` AND, inside it, a single
fenced ````gauntlet-phases```` block holding the machine-readable phase list the
phases stage fans out over. Keeping the list *inside* plan.md (rather than a
second artifact) means the one approved document carries both the prose a human
ratifies at the plan gate and the exact phases the engine then executes — they
cannot drift apart.

Determinism over cleverness (§2): this is a fenced-block scan + YAML parse, not
a free-form plan parser. The block is authoritative; if it is malformed the
loader fails closed rather than guessing phases.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

# A fenced ```gauntlet-phases ... ``` block. The info string must be exactly
# `gauntlet-phases` so an ordinary ```yaml example in the plan is never mistaken
# for the phase list.
_BLOCK_RE = re.compile(
    r"^```gauntlet-phases[ \t]*\n(.*?)\n```[ \t]*$",
    re.MULTILINE | re.DOTALL,
)

_PHASE_ID_RE = re.compile(r"^P\d+$")


class PlanPhasesError(ValueError):
    """The plan's ``gauntlet-phases`` block is present but malformed."""


def extract_phases(plan_text: str) -> list[dict[str, Any]] | None:
    """Return the structured phase list from a plan's text, or ``None``.

    ``None`` means no ``gauntlet-phases`` block at all (the plan stage has not
    produced one yet). A present-but-malformed block raises :class:`PlanPhasesError`
    — fail closed, never silently fan out over a guess.
    """
    matches = _BLOCK_RE.findall(plan_text)
    if not matches:
        return None
    if len(matches) > 1:
        raise PlanPhasesError(
            f"plan declares {len(matches)} gauntlet-phases blocks; exactly one is "
            "allowed so the phase list is unambiguous"
        )
    try:
        data = yaml.safe_load(matches[0])
    except yaml.YAMLError as exc:
        raise PlanPhasesError(f"gauntlet-phases block is not valid YAML: {exc}") from None
    if not isinstance(data, list) or not data:
        raise PlanPhasesError("gauntlet-phases block must be a non-empty YAML list")

    seen: set[str] = set()
    phases: list[dict[str, Any]] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise PlanPhasesError(f"phase #{i + 1} is not a mapping: {item!r}")
        pid = item.get("id")
        if not isinstance(pid, str) or not _PHASE_ID_RE.match(pid):
            raise PlanPhasesError(
                f"phase #{i + 1} has id {pid!r}; phase ids must match 'P<n>' "
                "(numeric phases drive sequencing and rollback, FR-9.9/FR-10.3)"
            )
        if pid in seen:
            raise PlanPhasesError(f"duplicate phase id {pid!r} in the plan")
        seen.add(pid)
        if not item.get("title"):
            raise PlanPhasesError(f"phase {pid} is missing a 'title'")
        # The plan-author contract (prompts/plan-author.md) requires every phase
        # to carry a goal, and implement-phase.md identifies the current phase by
        # id/title/goal — a phase with no goal would fan out into implementation
        # with nothing to anchor it. Fail closed rather than fan out over a guess.
        goal = item.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise PlanPhasesError(
                f"phase {pid} is missing a non-empty 'goal' (each phase must "
                "carry id/title/goal; implement-phase.md keys off the goal)"
            )
        phases.append(item)
    return phases


def load_plan_phases(plan_path: Path) -> list[dict[str, Any]] | None:
    """Read ``plan.md`` and return its phase list, or ``None`` if absent.

    Returns ``None`` when the plan file does not exist yet (before the plan
    stage) or has no phase block, so ``foreach: plan.phases`` only resolves once
    an approved plan declares phases (FR-10.2).
    """
    if not plan_path.exists():
        return None
    return extract_phases(plan_path.read_text())

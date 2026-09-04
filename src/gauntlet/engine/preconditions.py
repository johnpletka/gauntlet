"""Read-only plan preflight: required paths and environment variable names.

Provisioning happens separately, before approval. Plan text is never executed.
Malformed items fail closed; environment values never enter diagnostics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The supported precondition kinds — the discriminating key of a declared item.
KIND_PATH = "path"
KIND_ENV = "env"
KINDS = (KIND_PATH, KIND_ENV)

# Keys each kind admits. Anything else is an unknown key → fail closed.
_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    KIND_PATH: frozenset({KIND_PATH, "description"}),
    KIND_ENV: frozenset({KIND_ENV, "description"}),
}

# The scope label for items declared at the block's top level.
PLAN_SCOPE = "plan"

# Pipeline option values (validated at load in engine/validate.py).
#   human_gate `preflight: plan_preconditions` — `gauntlet approve` runs the
#   resolver over every plan item before approving (issue #134).
#   any step `preconditions_from: plan` — the orchestrator re-resolves the
#   plan-level items plus the current foreach phase's before the handler runs.
PREFLIGHT_PLAN_PRECONDITIONS = "plan_preconditions"
PRECONDITIONS_FROM_PLAN = "plan"

# POSIX-portable environment variable name.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

class PreconditionSpecError(ValueError):
    """A declared precondition item is malformed (unknown shape or key)."""


@dataclass(frozen=True)
class Precondition:
    """One declared, shape-validated precondition item."""

    kind: str  # path | env
    target: str  # the path or the env var NAME
    scope: str  # PLAN_SCOPE or the declaring phase id (e.g. "P2")
    description: str | None = None
    def label(self) -> str:
        """A one-line, secret-free rendering: kind + target + scope (+ description)."""
        text = f"{self.kind} {self.target} [{self.scope}]"
        if self.description:
            text += f" — {self.description}"
        return text


@dataclass(frozen=True)
class Unmet:
    """One unmet precondition: the item and, precisely, why."""

    item: Precondition
    reason: str

    def render(self) -> str:
        return f"{self.item.label()} — {self.reason}"


# --- shape validation --------------------------------------------------------
def parse_preconditions(raw: Any, *, scope: str) -> list[Precondition]:
    """Validate a declared ``preconditions:`` list; raise on any bad shape.

    ``scope`` names where the list was declared (:data:`PLAN_SCOPE` or a phase
    id) so every error message and every rendered item says which phase owns
    it. Fail closed (§2): not-a-list, a non-mapping entry, an entry declaring
    zero or several kinds, an unknown key, a wrong-typed value, or a
    ``timeout_s`` all raise :class:`PreconditionSpecError`
    with a message precise enough to fix the plan from.
    """
    where = "plan-level" if scope == PLAN_SCOPE else f"phase {scope}"
    if not isinstance(raw, list):
        raise PreconditionSpecError(
            f"{where} 'preconditions' must be a list of {{path|env, "
            f"description?}} items, got {type(raw).__name__}"
        )
    items: list[Precondition] = []
    for i, entry in enumerate(raw):
        label = f"{where} precondition #{i + 1}"
        if not isinstance(entry, dict):
            raise PreconditionSpecError(f"{label} is not a mapping: {entry!r}")
        if "command" in entry:
            raise PreconditionSpecError(
                f"{label}: command preconditions are unsupported; provision separately "
                "and declare a path or environment variable to check"
            )
        kinds = [k for k in KINDS if k in entry]
        if len(kinds) != 1:
            raise PreconditionSpecError(
                f"{label} must declare exactly one of 'path' or 'env' "
                f"(found {kinds or 'none'}): {entry!r}"
            )
        kind = kinds[0]
        unknown = sorted(set(entry) - _ALLOWED_KEYS[kind])
        if unknown:
            raise PreconditionSpecError(
                f"{label} ({kind}) has unknown key(s) {unknown}; allowed keys "
                f"for a '{kind}' item are {sorted(_ALLOWED_KEYS[kind])}"
            )
        target = entry[kind]
        if not isinstance(target, str) or not target.strip():
            raise PreconditionSpecError(
                f"{label}: '{kind}' must be a non-empty string, got {target!r}"
            )
        target = target.strip()
        if kind == KIND_ENV and not _ENV_NAME_RE.match(target):
            raise PreconditionSpecError(
                f"{label}: 'env' must be an environment variable NAME "
                f"(letters, digits, underscores), got {target!r}"
            )
        description = entry.get("description")
        if description is not None and (
            not isinstance(description, str) or not description.strip()
        ):
            raise PreconditionSpecError(
                f"{label}: 'description' must be a non-empty string when present"
            )
        items.append(
            Precondition(
                kind=kind,
                target=target,
                scope=scope,
                description=description.strip() if description else None,
            )
        )
    return items


# --- resolution --------------------------------------------------------------
def resolve_preconditions(
    items: Sequence[Precondition],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> list[Unmet]:
    """Check every item without executing plan text or exposing env values."""
    unmet: list[Unmet] = []
    for item in items:
        if item.kind == KIND_PATH:
            reason = _check_path(item, cwd)
        elif item.kind == KIND_ENV:
            reason = _check_env(item, env)
        else:  # unreachable for parsed items; fail closed for a hand-built one
            reason = f"unknown precondition kind {item.kind!r}"
        if reason is not None:
            unmet.append(Unmet(item, reason))
    return unmet


def _check_path(item: Precondition, cwd: Path) -> str | None:
    path = Path(item.target)
    resolved = path if path.is_absolute() else cwd / path
    if resolved.exists():
        return None
    return f"missing: {resolved}"


def _check_env(item: Precondition, env: Mapping[str, str]) -> str | None:
    # Presence and emptiness only — the value never leaves this frame.
    value = env.get(item.target)
    if value is None:
        return f"environment variable {item.target} is not set"
    if not value.strip():
        return f"environment variable {item.target} is set but empty"
    return None


def render_checklist(
    items: Sequence[Precondition], unmet: Sequence[Unmet]
) -> str:
    """A ``preflight.txt``-style record of every checked item and its verdict."""
    failed = {id(u.item): u for u in unmet}
    lines = []
    for item in items:
        hit = failed.get(id(item))
        lines.append(f"[{'UNMET' if hit else 'ok'}] {hit.render() if hit else item.label()}")
    return "\n".join(lines) + "\n"

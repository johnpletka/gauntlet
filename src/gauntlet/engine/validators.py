"""Named artifact validators for ``agent_task``'s ``validate:`` field (FR-2.1).

An ``agent_task`` may declare ``validate: <name>`` to check its ``output``
artifact *inside the step*, at authoring time, instead of letting a malformed
structured block sail through to a later fail-closed gate (``phase_lint``) or a
mid-run parse crash. On a validation failure the engine re-invokes the same
agent session with the exact error and a bounded repair loop (FR-2.1); after
exhaustion it parks with ``parked_reason=artifact_invalid`` and the error
verbatim (FR-2.2), so a hand-edit-then-revalidate is sanctioned and audited.

Two validator kinds are addressable by name:

* a **named** validator (``plan_phases``) — a registered deterministic check;
* a **JSON-schema ref** (``schema:<repo-relative path under asset_root>``) —
  extract JSON from the artifact and validate it against that schema, reusing
  ``schemas/*.json`` and the shared structured-output helpers.

Determinism over cleverness (§2): a validator is a YAML parse / JSON-schema
match, never a model call. Fail closed: an unknown validator name — or a schema
ref that does not resolve under the repo — is a pipeline misconfiguration and
raises :class:`UnknownValidatorError`, distinct from an *artifact* defect (which
returns an error string the engine feeds back to the agent).
"""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.adapters._structured import extract_json, validate_schema
from gauntlet.engine.planphases import (
    PlanPhasesError,
    acceptance_clause_errors,
    extract_phases,
    frs_declaration_errors,
    missing_phase_sections,
)

# A ``validate:`` value with this prefix names a JSON-schema file (repo-relative,
# under the configured ``asset_root``) rather than a registered named validator.
SCHEMA_REF_PREFIX = "schema:"


class UnknownValidatorError(ValueError):
    """The ``validate:`` name is not a known validator / resolvable schema ref.

    A configuration fault (fail closed), never an artifact defect — the caller
    surfaces it as a terminal step error, not a repairable park.
    """


def _validate_plan_phases(text: str) -> str | None:
    """Validate a plan.md ``gauntlet-phases`` block (wraps :func:`extract_phases`).

    Returns ``None`` when the block parses to a non-empty phase list, else a
    human-readable error. A *missing* block (``extract_phases`` → ``None``) is an
    error here: the plan-author's whole job is to emit the phase list the
    ``foreach: plan.phases`` fan-out executes, so its absence is a defect to
    repair, not a valid document.
    """
    try:
        phases = extract_phases(text)
    except PlanPhasesError as exc:
        return f"gauntlet-phases block is invalid — {exc}"
    if not phases:
        return (
            "no gauntlet-phases block found; the plan must declare its phase "
            "list in a fenced ```gauntlet-phases``` block (FR-5.1)"
        )
    # FR-1.1: `phase`-mode context slices each phase's prose section by its ATX
    # heading; a phase in the list with no locatable `## <id> …` heading would
    # silently lose its implement-prompt excerpt (fail open). Reject it here so
    # the plan-author repairs the prose before approval.
    missing = missing_phase_sections(text, phases)
    if missing:
        return (
            "no locatable prose section for phase(s) "
            f"{', '.join(missing)}: every phase in the gauntlet-phases list must "
            "have a matching '## <id> …' heading so `phase`-mode context can "
            "slice its section (FR-1.1); add the heading(s) to the plan prose"
        )
    # FR-3.1: every phase must carry a well-formed `acceptance:` list of testable
    # clauses so the acceptance_gate can prove each maps to a real test. A
    # clause-less (or malformed) phase is a defect the plan-author repairs before
    # approval — fail closed.
    acc_errors = acceptance_clause_errors(phases)
    if acc_errors:
        return "; ".join(acc_errors)
    # #66: every fresh phase must declare its FR scope in an `frs:` list so the
    # phase-size lint counts declared refs, never the prose-sweep fallback (whose
    # incidental cross-references can false-positive a park at the gate). Author
    # time only — `extract_phases` and `phase_lint` stay lenient so pre-`frs`
    # plans (mid-flight or approved) still load and lint.
    frs_errors = frs_declaration_errors(phases)
    if frs_errors:
        return "; ".join(frs_errors)
    return None


# Registered named validators. A ``validate:`` value that is not a schema ref
# must be a key here, else :func:`validate_artifact` fails closed.
_NAMED_VALIDATORS = {
    "plan_phases": _validate_plan_phases,
}


def known_validator(name: str) -> bool:
    """True iff *name* names a registered validator or a schema ref (shape only)."""
    return name.startswith(SCHEMA_REF_PREFIX) or name in _NAMED_VALIDATORS


def _validate_against_schema(
    ref: str, text: str, *, repo_root: Path, asset_root: str
) -> str | None:
    """Validate *text* (parsed as JSON) against the schema at *ref* (FR-2.1).

    Raises :class:`UnknownValidatorError` if the schema file does not resolve
    under the configured ``asset_root`` (a misconfigured pipeline), returns an
    error string if the artifact is not JSON or does not conform (a repairable
    artifact defect).

    Path containment (review F-002): the contract is a repo-relative schema ref
    *under* ``asset_root``. Two shapes escape a naive ``root / ref`` join and are
    rejected before any filesystem access — fail closed, never read a file
    outside the configured asset root:

    * an **absolute** ref — ``root / "/etc/x"`` discards ``root`` entirely;
    * a ``../`` **traversal** that resolves outside ``asset_root``.
    """
    root = (repo_root / asset_root).resolve()
    if Path(ref).is_absolute():
        raise UnknownValidatorError(
            f"validator schema {ref!r} is an absolute path; schema refs must be "
            f"repo-relative under asset_root {asset_root!r} (review F-002)"
        )
    path = (root / ref).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise UnknownValidatorError(
            f"validator schema {ref!r} escapes asset_root {asset_root!r} "
            f"(resolves to {path}); schema refs must stay within it (review F-002)"
        )
    if not path.exists():
        raise UnknownValidatorError(
            f"validator schema {ref!r} not found at {path} (asset_root "
            f"{asset_root!r}); check the step's `validate:` reference"
        )
    schema = json.loads(path.read_text())
    try:
        instance = extract_json(text)
    except ValueError as exc:
        return f"artifact is not parseable JSON: {exc}"
    try:
        validate_schema(instance, schema)
    except ValueError as exc:
        return str(exc)
    return None


def validate_artifact(
    name: str, text: str, *, repo_root: Path, asset_root: str
) -> str | None:
    """Run the named validator against *text*; ``None`` if valid, else an error.

    ``name`` is either a schema ref (``schema:<path>``) or a registered named
    validator. An unknown name raises :class:`UnknownValidatorError` (fail
    closed) — the distinction the caller relies on: an error *string* is fed
    back to the agent for repair; the *exception* is a terminal misconfiguration.
    """
    if name.startswith(SCHEMA_REF_PREFIX):
        ref = name[len(SCHEMA_REF_PREFIX):].strip()
        if not ref:
            raise UnknownValidatorError(
                f"validator {name!r} names an empty schema path"
            )
        return _validate_against_schema(
            ref, text, repo_root=repo_root, asset_root=asset_root
        )
    validator = _NAMED_VALIDATORS.get(name)
    if validator is None:
        raise UnknownValidatorError(
            f"unknown artifact validator {name!r}; known: "
            f"{sorted(_NAMED_VALIDATORS)} or a {SCHEMA_REF_PREFIX}<path> ref"
        )
    return validator(text)

"""Deferral reconciliation + phase-size lint support (FR-3.3, FR-3.4).

The remaining two #54 guardrails, expressed as deterministic parsing over data
the pipeline already records — no LLM judgment (CLAUDE.md §2):

* **Deferral reconciliation (FR-3.3).** A phase may explicitly push work to a
  later phase, recorded two ways: as ``"Deferred to P<N>: …"`` prose in a commit
  body (the CLAUDE.md §7 / ``prompts/commit-message.md`` convention) and as a
  structured ``deferrals[]`` entry in ``artifacts/acceptance-map.json`` (§6). Both
  are validated against the plan's *actual* phase ids — a deferral to a
  nonexistent phase parks the run (fail closed: a deferral that points nowhere is
  silently-dropped work). An *open* deferral targeting a phase is injected
  verbatim into that phase's implement prompt, so the builder cannot forget the
  obligation a prior phase handed forward.

* **Phase-size lint (FR-3.4).** :func:`distinct_fr_refs` counts the distinct
  ``FR-<n>[.<m>]`` references a phase's prose carries; ``phase_lint`` warns (or,
  configured to, parks) past ``max_frs_per_phase`` — oversized phases are where
  partial delivery hides (#54 cause 4).

Everything here is a pure function over text/records so two builders following it
produce identical results and the reconciliation is trivially unit-testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A ``Deferred to P<N>`` reference in a commit body / prose (CLAUDE.md §7,
# prompts/commit-message.md: ``Deferred to P6: …``). Case-insensitive on the
# leading letter only (git bodies capitalise it); the phase id is ``P`` + digits
# at a word boundary, then an optional ``:`` and the rest of the line as the
# deferral text. ``.`` stops at the newline, so group(2) is exactly that line's
# remainder — no MULTILINE needed and no bleed into the next line.
_DEFERRAL_RE = re.compile(r"[Dd]eferred\s+to\s+(P\d+)\b[ \t]*:?[ \t]*(.*)")

# A distinct FR reference token for the size lint (FR-3.4): ``FR-3`` or
# ``FR-3.4``. Word-bounded so ``FR-34`` is one token, not ``FR-3`` + ``4``.
_FR_REF_RE = re.compile(r"\bFR-\d+(?:\.\d+)?\b")

# A plan phase id (``P1``, ``P2`` …). FR-3.3 reconciles only *phase-style*
# deferral targets against the plan; a structured ``deferrals[]`` entry whose
# ``to_phase`` is NOT phase-shaped (e.g. ``post-v1`` / ``FUTURE.md``) is a
# deliberate out-of-run deferral, not a phantom phase — see :func:`phantom_deferrals`.
_PHASE_ID_RE = re.compile(r"^P\d+$")

# The two size-lint dispositions (FR-3.4): warn (default — surface in notes, do
# not block) or park (halt the plan gate). Fail closed on anything else.
SIZE_LINT_WARN = "warn"
SIZE_LINT_PARK = "park"
SIZE_LINT_MODES = frozenset({SIZE_LINT_WARN, SIZE_LINT_PARK})


@dataclass(frozen=True)
class Deferral:
    """One reconciled deferral: the target phase, its verbatim text, provenance.

    ``text`` is preserved exactly as authored so the implement-prompt injection
    is verbatim (FR-3.3); ``source`` names where it came from (a commit sha or an
    acceptance-map phase) purely for the audit trail / dedup.
    """

    to_phase: str
    text: str
    source: str

    def render(self) -> str:
        """A single injected/audited line; the deferral text appears verbatim."""
        body = self.text.strip()
        suffix = f": {body}" if body else ""
        return f"- Deferred to {self.to_phase}{suffix}  (source: {self.source})"


def parse_body_deferrals(body: str, *, source: str) -> list[Deferral]:
    """Extract ``Deferred to P<N>: …`` references from a commit-body / prose string.

    Each match yields a :class:`Deferral` whose ``text`` is the remainder of that
    line (verbatim, trimmed of surrounding whitespace). Order-preserving; a body
    with no reference yields an empty list.
    """
    out: list[Deferral] = []
    for m in _DEFERRAL_RE.finditer(body or ""):
        out.append(Deferral(to_phase=m.group(1), text=m.group(2).strip(), source=source))
    return out


def deferrals_from_map(mapping: Any, *, source: str) -> list[Deferral]:
    """Extract the structured ``deferrals[]`` of an acceptance map (§6).

    Each entry is ``{text, to_phase}`` (validated by the acceptance-map schema
    before this runs, so the shape is trusted); a malformed/absent list yields an
    empty result — reconciliation of an ill-formed map is the schema's job, not
    this parser's.
    """
    out: list[Deferral] = []
    if not isinstance(mapping, dict):
        return out
    for entry in mapping.get("deferrals") or []:
        if not isinstance(entry, dict):
            continue
        to_phase = entry.get("to_phase")
        text = entry.get("text")
        if isinstance(to_phase, str) and isinstance(text, str):
            out.append(Deferral(to_phase=to_phase, text=text, source=source))
    return out


def phantom_deferrals(
    deferrals: list[Deferral], known_phase_ids: set[str]
) -> list[Deferral]:
    """Phase-style deferrals whose target is not an actual plan phase (FR-3.3).

    A non-empty result is a fail-closed park condition: the referenced phase does
    not exist, so the deferred work would land nowhere. Only *phase-shaped*
    targets (``P<N>``) are reconciled — a structured ``deferrals[]`` entry that
    defers to a non-phase like ``post-v1`` / ``FUTURE.md`` (the CLAUDE.md §7
    out-of-run deferral convention) is intentional, not a phantom phase, so it is
    never flagged (this is FR-3.3's "'Deferred to P<N>'-style" scope).
    """
    return [
        d
        for d in deferrals
        if _PHASE_ID_RE.match(d.to_phase) and d.to_phase not in known_phase_ids
    ]


def open_deferrals_for(phase_id: str, deferrals: list[Deferral]) -> list[Deferral]:
    """Deferrals targeting ``phase_id``, deduped on ``(to_phase, text)`` (FR-3.3).

    "Open" is positional: injection runs when rendering the target phase's
    implement prompt, so every deferral pointing at the phase about to be built is
    by definition still open. Dedup collapses the same obligation recorded in both
    a commit body and the acceptance map (identical text) to one line while keeping
    genuinely distinct deferrals; the first-seen source wins for provenance.
    """
    seen: set[tuple[str, str]] = set()
    out: list[Deferral] = []
    for d in deferrals:
        if d.to_phase != phase_id:
            continue
        key = (d.to_phase, d.text.strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def distinct_fr_refs(text: str) -> set[str]:
    """The set of distinct ``FR-<n>[.<m>]`` references in a phase's prose (FR-3.4)."""
    return set(_FR_REF_RE.findall(text or ""))

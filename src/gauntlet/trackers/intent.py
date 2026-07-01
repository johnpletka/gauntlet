"""Deterministic, tracker-agnostic ``intent.md`` renderer (§6, FR-2.6).

``render_intent`` turns a normalized :class:`Issue` (tracker source) or a plain
problem-statement string (manual ``--intent`` / ``-m`` / ``$EDITOR`` source)
into the ``intent.md`` body the reviewer is given. It is a pure function — same
inputs, byte-identical output — so its result is golden-testable and the review
run's intent snapshot is reproducible (determinism over cleverness).

v1 performs **no** heading detection, regex extraction, or section-splitting:
the full source body lands verbatim under ``## Problem``. The ``## Repro`` /
``## Expected`` sections are reserved for future providers that expose discrete
structured fields; no v1 code path emits them.
"""

from __future__ import annotations

from gauntlet.trackers.base import Issue

# The manual-intent sources, mapped to their manifest `source` value (§6).
_MANUAL_SOURCES = frozenset({"intent-file", "message", "editor"})


def _independence_label(independent: bool) -> str:
    return "independent" if independent else "non-independent"


def render_intent(
    content: Issue | str,
    *,
    provenance: str,
    independent: bool,
    source: str,
    provider: str | None = None,
) -> str:
    """Render ``intent.md`` deterministically (§6).

    ``content`` is a normalized :class:`Issue` when ``source == "issue"``
    (tracker), otherwise the raw problem-statement string. ``provenance`` and
    ``independent`` are recorded verbatim on the ``<provenance: …>`` line and are
    also passed into the reviewer prompt (FR-2.2) so it calibrates the
    problem-correctness axis. ``provider`` names the tracker on the
    ``<source: …>`` line (tracker source only).

    Always emits: the ``# Intent`` header, a ``<provenance: …>`` line, and a
    ``## Problem`` section carrying the source text verbatim. The ``<source: …>``
    line is emitted only for a tracker source; the reserved ``## Repro`` /
    ``## Expected`` sections are omitted entirely in v1.
    """
    lines: list[str] = []
    if source == "issue":
        if not isinstance(content, Issue):
            raise TypeError(
                "render_intent(source='issue') requires an Issue, "
                f"got {type(content).__name__}"
            )
        lines.append(f"# Intent — {content.identifier} · {content.title}")
        src = provider or "tracker"
        lines.append(f"<source: {src} {content.identifier} · {content.url}>")
        body = content.description
    else:
        if source not in _MANUAL_SOURCES:
            raise ValueError(
                f"render_intent: unknown source {source!r}; expected 'issue' or "
                f"one of {sorted(_MANUAL_SOURCES)}"
            )
        if not isinstance(content, str):
            raise TypeError(
                f"render_intent(source={source!r}) requires a str, "
                f"got {type(content).__name__}"
            )
        # A manual source has no discrete title; the header carries "(manual)".
        lines.append("# Intent — (manual)")
        body = content

    lines.append(f"<provenance: {provenance} · {_independence_label(independent)}>")
    lines.append("")
    lines.append("## Problem")
    lines.append(body)
    # Single trailing newline; deterministic regardless of the body's own.
    return "\n".join(lines).rstrip("\n") + "\n"

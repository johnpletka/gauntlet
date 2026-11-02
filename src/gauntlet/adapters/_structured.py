"""Shared structured-output helpers: JSON extraction + schema validation."""

from __future__ import annotations

import json
import re
from typing import Any

import jsonschema

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)

# Bounded structural repair (#169). Models occasionally emit JSON that is valid
# except for one bracket slip — an extra ``}`` after a long string, a trailing
# comma — and repeat the same slip on every re-ask. A handful of local,
# position-anchored edits recovers those without another model call. The bound
# keeps this a repair of a near-miss, never a guess at free-form text; the
# caller's schema validation still applies to whatever the repair yields.
_MAX_REPAIRS = 4
_CLOSERS = "}]"
_CONTEXT_CHARS = 40


def extract_json(text: str) -> Any:
    """Extract a JSON value from model text output.

    Tries, in order: the whole text, the first fenced code block, the
    outermost ``{...}`` / ``[...]`` span. Each is parsed strictly first; only
    when none parses is a bounded structural repair attempted (see
    :func:`parse_json_output`). Raises ``ValueError`` — naming the parse error
    and its position — if nothing parses.
    """
    return parse_json_output(text)[0]


def parse_json_output(text: str) -> tuple[Any, list[str]]:
    """Like :func:`extract_json`, also returning the repairs applied.

    The repair list is empty for output that parsed as-is. Each entry is a
    short human-readable note (``"removed stray '}' at char 1580"``) so a
    caller can record that the value it is about to trust was repaired.
    """
    candidates = _candidates(text)
    errors: list[tuple[str, json.JSONDecodeError]] = []
    for candidate in candidates:
        try:
            return json.loads(candidate), []
        except json.JSONDecodeError as exc:
            errors.append((candidate, exc))
    for candidate in candidates:
        repaired = _repair(candidate)
        if repaired is not None:
            return repaired
    raise ValueError(_describe_failure(errors))


def _candidates(text: str) -> list[str]:
    candidates = [text.strip()]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    # De-duplicate, keeping order (the whole text often IS the span).
    return list(dict.fromkeys(c for c in candidates if c))


def _repair(candidate: str) -> tuple[Any, list[str]] | None:
    """Depth-first search over at most ``_MAX_REPAIRS`` local edits.

    Every edit is anchored at the decoder's own error position, so the search
    only ever touches the byte the parser stopped on (or the comma just before
    it). Returns the first value that parses, with the edits that produced it.
    """
    def search(s: str, notes: list[str]) -> tuple[Any, list[str]] | None:
        try:
            return json.loads(s), notes
        except json.JSONDecodeError as exc:
            if len(notes) >= _MAX_REPAIRS:
                return None
            for fixed, note in _edits(s, exc):
                found = search(fixed, notes + [note])
                if found is not None:
                    return found
            return None

    return search(candidate, [])


def _edits(s: str, exc: json.JSONDecodeError) -> list[tuple[str, str]]:
    """Candidate single edits for the error at ``exc.pos``, most likely first."""
    pos = exc.pos
    ch = s[pos] if pos < len(s) else ""
    edits: list[tuple[str, str]] = []
    if ch and ch in _CLOSERS:
        if exc.msg.startswith("Expecting ',' delimiter"):
            # A closer that does not match the open container: either it is a
            # stray extra (the observed `}}` slip) or the container it belongs
            # after was never closed. Try removing it first, then closing the
            # open container in front of it (a `}` inside an array means the
            # array is open; a `]` inside an object means the object is).
            edits.append((s[:pos] + s[pos + 1 :], f"removed stray {ch!r} at char {pos}"))
            missing = "]" if ch == "}" else "}"
            edits.append(
                (s[:pos] + missing + s[pos:], f"inserted missing {missing!r} at char {pos}")
            )
        elif exc.msg.startswith("Extra data") and not s[pos:].strip(_CLOSERS + " \t\r\n"):
            # Only surplus closers after a complete value.
            edits.append((s[:pos].rstrip(), f"removed trailing {s[pos:].strip()!r} at char {pos}"))
        else:
            prev = len(s[:pos].rstrip()) - 1
            if prev >= 0 and s[prev] == ",":
                edits.append(
                    (s[:prev] + s[prev + 1 :], f"removed trailing ',' at char {prev}")
                )
    return edits


def _describe_failure(errors: list[tuple[str, json.JSONDecodeError]]) -> str:
    """A position-bearing failure message (#169).

    Reports the candidate the decoder got furthest into — the JSON the model
    was evidently trying to write — so the failing bytes are visible in the
    adapter error (and the failure record built from it) without replaying the
    raw events.
    """
    base = "no parseable JSON found in output text"
    if not errors:
        return base
    candidate, exc = max(errors, key=lambda item: item[1].pos)
    lo = max(0, exc.pos - _CONTEXT_CHARS)
    hi = min(len(candidate), exc.pos + _CONTEXT_CHARS)
    return (
        f"{base}: {exc.msg} at line {exc.lineno} column {exc.colno} "
        f"(char {exc.pos} of {len(candidate)}); near {candidate[lo:hi]!r}"
    )


def validate_schema(instance: Any, schema: dict) -> None:
    """Validate ``instance`` against a JSON schema; raises ``ValueError``."""
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"schema validation failed: {exc.message}") from exc


def schema_instruction(schema: dict) -> str:
    """Prompt block instructing a model to answer as schema-conforming JSON."""
    return (
        "\n\nRespond with a single JSON value that conforms exactly to this "
        "JSON schema. Output only the JSON — no prose, no code fences.\n"
        f"{json.dumps(schema, indent=2)}\n"
    )

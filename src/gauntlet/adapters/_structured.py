"""Shared structured-output helpers: JSON extraction + schema validation."""

from __future__ import annotations

import json
import re
from typing import Any

import jsonschema

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Extract a JSON value from model text output.

    Tries, in order: the whole text, the first fenced code block, the
    outermost ``{...}`` / ``[...]`` span. Raises ``ValueError`` if nothing
    parses.
    """
    candidates = [text.strip()]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError("no parseable JSON found in output text")


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

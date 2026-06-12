"""Redacting writer (FR-4.4; plan P1 review F-005, full list P4).

No log line the bootstrap writes lands on disk unredacted: transcripts,
captured event streams, and the judge audit log all pass through
:class:`RedactingWriter`. From P4 the transcript logger is its home and the
list is configurable (``redaction:`` in `.gauntlet/config.yaml`), default-on —
these logs are intended for git.

Design per BOOTSTRAP-NOTES #7:
- Exact matching of known secret env-var *values* is the primary mechanism.
- Credential-pattern regexes are the fallback, and they are boundary-aware
  with minimum lengths (a naive ``sk-`` once matched "ask-with-warning").
- Every hit records *which pattern fired*, so false positives are diagnosable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# Env vars whose names suggest credentials; their *values* get masked.
SECRET_ENV_NAME_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE
)

# Values shorter than this are too likely to collide with ordinary text
# (e.g. PASSWORD=dev) to be safely masked by exact match.
MIN_SECRET_VALUE_LENGTH = 8

# Fallback credential patterns: boundary-aware, with minimum lengths.
# Order matters — more specific prefixes first so the hit names stay precise.
CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("bearer-token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._~+/=-]{20,}")),
    (
        "private-key-block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
)


class ExtraPattern(BaseModel):
    """One configured fallback regex (FR-4.4). Compiled at config load so a bad
    pattern fails the run up front, not mid-write."""

    name: str
    regex: str

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.regex, re.DOTALL)


class RedactionSettings(BaseModel):
    """The configurable redaction list (FR-4.4), default-on.

    ``extra_env_vars`` are env-var *names* whose values get masked even when the
    name heuristic (KEY/TOKEN/SECRET/...) would miss them. ``extra_patterns``
    extend the built-in credential regexes — they never replace them; the
    defaults are the floor, configuration only adds.
    """

    extra_env_vars: list[str] = Field(default_factory=list)
    extra_patterns: list[ExtraPattern] = Field(default_factory=list)
    min_value_length: int = MIN_SECRET_VALUE_LENGTH


def build_redactor(
    settings: RedactionSettings | None = None,
    env: Mapping[str, str] | None = None,
) -> Redactor:
    """Construct a :class:`Redactor` from configured settings (FR-4.4)."""
    settings = settings or RedactionSettings()
    return Redactor(
        env,
        extra_patterns=tuple(
            (p.name, p.compiled()) for p in settings.extra_patterns
        ),
        min_value_length=settings.min_value_length,
        extra_env_names=frozenset(settings.extra_env_vars),
    )


def _secret_variants(value: str) -> set[str]:
    """A secret's raw form plus its JSON-escaped serializations."""
    return {
        value,
        json.dumps(value)[1:-1],  # ensure_ascii=True spelling
        json.dumps(value, ensure_ascii=False)[1:-1],
    }


@dataclass(frozen=True)
class RedactionHit:
    """One redaction occurrence: which pattern fired, how many times."""

    pattern: str  # e.g. "env:OPENAI_API_KEY" or "openai-style-key"
    count: int


class Redactor:
    """Masks secrets in text. Env-var values first, then pattern fallbacks."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        extra_patterns: tuple[tuple[str, re.Pattern[str]], ...] = (),
        min_value_length: int = MIN_SECRET_VALUE_LENGTH,
        extra_env_names: frozenset[str] = frozenset(),
    ) -> None:
        if env is None:
            import os

            env = os.environ
        # Each secret is matched in its raw form AND its JSON-escaped forms:
        # writers serialize before redacting, and json.dumps escapes quotes,
        # backslashes and newlines, so the raw value alone would miss them
        # (review P1 F-004). Longest variants first so overlaps mask fully.
        self._env_secrets: list[tuple[str, str]] = sorted(
            (
                (name, variant)
                for name, value in env.items()
                if (SECRET_ENV_NAME_RE.search(name) or name in extra_env_names)
                and len(value) >= min_value_length
                for variant in _secret_variants(value)
            ),
            key=lambda item: len(item[1]),
            reverse=True,
        )
        self._patterns = CREDENTIAL_PATTERNS + extra_patterns

    def redact(self, text: str) -> tuple[str, list[RedactionHit]]:
        """Return ``(redacted_text, hits)``; hits name the patterns that fired."""
        hits: list[RedactionHit] = []
        env_counts: dict[str, int] = {}
        for name, variant in self._env_secrets:
            count = text.count(variant)
            if count:
                text = text.replace(variant, f"[REDACTED:env:{name}]")
                env_counts[name] = env_counts.get(name, 0) + count
        hits.extend(
            RedactionHit(pattern=f"env:{name}", count=count)
            for name, count in env_counts.items()
        )
        for pattern_name, pattern in self._patterns:
            text, count = pattern.subn(f"[REDACTED:{pattern_name}]", text)
            if count:
                hits.append(RedactionHit(pattern=pattern_name, count=count))
        return text, hits


class RedactingWriter:
    """File writer that redacts everything it puts on disk.

    Accumulates hits in ``hits_log`` (path, pattern, count) so a caller can
    audit what fired — including diagnosing false positives.
    """

    def __init__(self, redactor: Redactor | None = None) -> None:
        self.redactor = redactor or Redactor()
        self.hits_log: list[tuple[Path, RedactionHit]] = []

    def write_text(self, path: Path | str, text: str) -> list[RedactionHit]:
        path = Path(path)
        redacted, hits = self._record(path, text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redacted)
        return hits

    def append_line(self, path: Path | str, line: str) -> list[RedactionHit]:
        path = Path(path)
        redacted, hits = self._record(path, line.rstrip("\n"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(redacted + "\n")
        return hits

    def append_jsonl(self, path: Path | str, obj: Any) -> list[RedactionHit]:
        return self.append_line(path, json.dumps(obj, ensure_ascii=False))

    def _record(self, path: Path, text: str) -> tuple[str, list[RedactionHit]]:
        redacted, hits = self.redactor.redact(text)
        self.hits_log.extend((path, hit) for hit in hits)
        return redacted, hits

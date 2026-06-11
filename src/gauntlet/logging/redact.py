"""Minimal redacting writer (FR-4.4 down-payment; plan P1, review F-005).

From P1 onward, no log line the bootstrap writes lands on disk unredacted:
manual transcripts, captured event streams, and (from P2) the judge audit log
all pass through :class:`RedactingWriter`.

Design per BOOTSTRAP-NOTES #7:
- Exact matching of known secret env-var *values* is the primary mechanism.
- Credential-pattern regexes are the fallback, and they are boundary-aware
  with minimum lengths (a naive ``sk-`` once matched "ask-with-warning").
- Every hit records *which pattern fired*, so false positives are diagnosable.

The full configurable redaction list ships with the P4 logger.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    ) -> None:
        if env is None:
            import os

            env = os.environ
        # Longest values first so overlapping secrets mask fully.
        self._env_secrets: list[tuple[str, str]] = sorted(
            (
                (name, value)
                for name, value in env.items()
                if SECRET_ENV_NAME_RE.search(name)
                and len(value) >= min_value_length
            ),
            key=lambda item: len(item[1]),
            reverse=True,
        )
        self._patterns = CREDENTIAL_PATTERNS + extra_patterns

    def redact(self, text: str) -> tuple[str, list[RedactionHit]]:
        """Return ``(redacted_text, hits)``; hits name the patterns that fired."""
        hits: list[RedactionHit] = []
        for name, value in self._env_secrets:
            count = text.count(value)
            if count:
                text = text.replace(value, f"[REDACTED:env:{name}]")
                hits.append(RedactionHit(pattern=f"env:{name}", count=count))
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

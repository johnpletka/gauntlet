"""Transcript logging (FR-4): redacting writer (P1) + transcript logger (P4)."""

from gauntlet.logging.redact import (
    RedactingWriter,
    RedactionHit,
    RedactionSettings,
    Redactor,
    build_redactor,
)
from gauntlet.logging.transcript import (
    GITIGNORE_GUIDANCE,
    StepLogger,
    render_transcript,
    write_run_index,
)

__all__ = [
    "Redactor",
    "RedactingWriter",
    "RedactionHit",
    "RedactionSettings",
    "build_redactor",
    "GITIGNORE_GUIDANCE",
    "StepLogger",
    "render_transcript",
    "write_run_index",
]

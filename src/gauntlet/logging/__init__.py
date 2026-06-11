"""Transcript logging. P1 ships the minimal redacting writer; the full
transcript logger (FR-4) lands in P4."""

from gauntlet.logging.redact import RedactingWriter, RedactionHit, Redactor

__all__ = ["Redactor", "RedactingWriter", "RedactionHit"]

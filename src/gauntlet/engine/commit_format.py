"""Commit-message format validation (FR-9.2/9.4, CLAUDE.md §7).

The ``commit`` step has the ``message_agent`` draft a message; the engine then
validates it against the enforced format and rejects + redrafts on violation.
The message is *data* (model-authored), so it is validated, never trusted.

Accepted header forms (the phase prefix is structural, not free text):
- ``P3: <imperative summary>``            phase commit (FR-9.2)
- ``P3.1: Address review — <summary>``    fix round (FR-9.4)
- ``P3.r1: Reviewer-applied changes — …`` reviewer-attributed (FR-9.6, P4)
- ``PRD.1:`` / ``PLAN.r1:`` etc.          document-cycle stage labels: the
  PRD/plan review loops in FR-5.1's ``standard.yaml`` are not numbered
  phases, so their fix rounds carry the stage name instead (FR-10.4
  resolution ratified 2026-06-12, BOOTSTRAP-NOTES #28). Rollback targets
  stay numeric — ``PRD``/``PLAN`` commits are not ``--phase N`` boundaries.
- ``REVIEW.1:`` / ``REVIEW.r1:``          the lightweight ``gauntlet review``
  flow's single ``adversarial_cycle`` carries ``phase: REVIEW`` (no numbered
  phase), so its accepted-fix commits land as ``REVIEW.x`` in place on the
  branch under review (PRD "Lightweight Issue Workflow" FR-8.2/FR-3.4). Like
  ``PRD``/``PLAN`` it is a stage label, not a numeric rollback boundary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HEADER_MAX = 72

# P<n> | PRD | PLAN | REVIEW, optionally .<round> or .r<round>, then ": " + summary.
_PREFIX = r"(?:P\d+|PRD|PLAN|REVIEW)"
_HEADER_RE = re.compile(rf"^{_PREFIX}(?:\.\d+|\.r\d+)?: \S.*$")


@dataclass(frozen=True)
class FormatError:
    reason: str


def validate_commit_message(message: str) -> FormatError | None:
    """Return ``None`` if the message is well-formed, else a :class:`FormatError`.

    Rules (single source of truth for the engine's reject + redraft loop):
    1. A header line matching the ``PN[.x|.rx]: summary`` shape, ≤ 72 chars.
    2. A blank second line.
    3. A non-empty body (the reasoning — what/why, refs, deferrals).
    """
    if not message or not message.strip():
        return FormatError("commit message is empty")
    # Normalize trailing newline handling; keep interior structure intact.
    lines = message.rstrip("\n").split("\n")
    header = lines[0]
    if len(header) > HEADER_MAX:
        return FormatError(
            f"header is {len(header)} chars; must be ≤ {HEADER_MAX} (FR-9.2)"
        )
    if not _HEADER_RE.match(header):
        return FormatError(
            "header must match 'PN: summary' / 'PN.x: ...' / 'PN.rx: ...' "
            "(or the PRD/PLAN stage-label forms, BOOTSTRAP-NOTES #28) "
            f"(got {header!r})"
        )
    if len(lines) < 2 or lines[1].strip() != "":
        return FormatError("second line must be blank (header/body separator)")
    body = "\n".join(lines[2:]).strip()
    if not body:
        return FormatError("body is empty; the reasoning is part of the deliverable")
    return None


def header_prefix(message: str) -> str | None:
    """Return the ``PN[.x]`` prefix of a well-formed header, else ``None``.

    Used by the mid-commit resume reconciliation (review F-003): match a commit
    that already exists in ``git log`` against the phase the engine intended.
    """
    header = message.rstrip("\n").split("\n", 1)[0]
    m = re.match(rf"^({_PREFIX}(?:\.\d+|\.r\d+)?):", header)
    return m.group(1) if m else None

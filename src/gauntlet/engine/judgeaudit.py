"""Per-invocation judge authorization accounting from the append-only audit.

The judge is a separate process, so its JSONL audit is the one adapter-neutral
record of every PreToolUse decision.  Agent adapters expose incompatible event
shapes (and Claude's buffered mode may expose only the final result); counting
the audit avoids inferring denials from prose in a transcript.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JudgeToolCounts:
    allowed: int = 0
    denied: int = 0
    fail_closed_denied: int = 0
    denial_reasons: tuple[str, ...] = ()

    @property
    def total(self) -> int:
        return self.allowed + self.denied

    @property
    def all_denied(self) -> bool:
        return self.denied > 0 and self.allowed == 0


def audit_offset(path: Path) -> int:
    """Return the byte boundary before an agent invocation (0 if absent)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def counts_since(path: Path, *, offset: int, step_id: str) -> JudgeToolCounts:
    """Count this step's allow/deny decisions appended after ``offset``.

    Malformed/truncated rows are ignored rather than guessed. They remain in the
    source audit for diagnosis, while the caller simply declines to claim a
    count it cannot prove.
    """
    allowed = denied = fail_closed = 0
    reasons: list[str] = []
    try:
        size = path.stat().st_size
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(offset if 0 <= offset <= size else 0)
            for line in fh:
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(row, dict) or row.get("step_id") != step_id:
                    continue
                decision = row.get("decision")
                if decision == "allow":
                    allowed += 1
                elif decision == "deny":
                    denied += 1
                    if row.get("source") == "fail-closed":
                        fail_closed += 1
                    reason = row.get("rationale")
                    if isinstance(reason, str) and reason and reason not in reasons:
                        reasons.append(reason)
    except OSError:
        return JudgeToolCounts()
    return JudgeToolCounts(
        allowed=allowed,
        denied=denied,
        fail_closed_denied=fail_closed,
        denial_reasons=tuple(reasons[:3]),
    )

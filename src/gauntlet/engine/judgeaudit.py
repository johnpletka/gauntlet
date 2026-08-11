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

# Tools whose *allow* is the only objective evidence that a repo-write agent
# could have changed the tree through a judged call (issue #101). The set
# mirrors the tools policy.yaml writes rules against (Write/Edit/NotebookEdit/
# MultiEdit) plus Bash — a blocked builder's denied `git`/file-mutating shell
# calls are exactly the observed #101 shape. Counting *every* Bash call as
# mutating means a read-only allowed Bash call (`ls`, `cat`) can mask a real
# blockage — an accepted false NEGATIVE: the guard then simply behaves as
# today, and phase-commit still fails loud on the empty tree.
MUTATING_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"})


@dataclass(frozen=True)
class JudgeToolCounts:
    allowed: int = 0
    denied: int = 0
    fail_closed_denied: int = 0
    denial_reasons: tuple[str, ...] = ()
    # Per-tool classification (issue #101, additive): the #83 `all_denied`
    # guard is defeated by a single allowed read-only call, so a builder whose
    # every Write/Edit/Bash was denied could still be marked DONE. These count
    # only calls to MUTATING_TOOLS; the existing fields keep their semantics.
    mutating_allowed: int = 0
    mutating_denied: int = 0
    mutating_fail_closed_denied: int = 0

    @property
    def total(self) -> int:
        return self.allowed + self.denied

    @property
    def all_denied(self) -> bool:
        return self.denied > 0 and self.allowed == 0

    @property
    def all_mutating_denied(self) -> bool:
        """Some mutating call was denied and none was ever allowed (#101)."""
        return self.mutating_denied > 0 and self.mutating_allowed == 0


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
    mut_allowed = mut_denied = mut_fail_closed = 0
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
                tool = row.get("tool_name")
                # #101 fail-closed classification asymmetry: an ALLOWED call
                # counts as mutating only when its tool is provably in the set
                # (an unproven row must not suppress the vacuous-done guard),
                # while a DENIED call with a missing/non-string tool_name is
                # counted as mutating — the module contract is to never claim
                # more safety than the audit proves.
                if decision == "allow":
                    allowed += 1
                    if tool in MUTATING_TOOLS:
                        mut_allowed += 1
                elif decision == "deny":
                    denied += 1
                    is_fail_closed = row.get("source") == "fail-closed"
                    if is_fail_closed:
                        fail_closed += 1
                    if tool in MUTATING_TOOLS or not isinstance(tool, str):
                        mut_denied += 1
                        if is_fail_closed:
                            mut_fail_closed += 1
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
        mutating_allowed=mut_allowed,
        mutating_denied=mut_denied,
        mutating_fail_closed_denied=mut_fail_closed,
    )

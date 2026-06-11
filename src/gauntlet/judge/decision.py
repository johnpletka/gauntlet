"""Judge decision types shared by the policy engine, service, and hook clients."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Decision = Literal["allow", "deny", "ask"]
Source = Literal["fast-path", "llm", "fail-closed"]


class JudgeDecision(BaseModel):
    """A single allow/deny/ask decision with provenance for the audit log."""

    decision: Decision
    source: Source
    rationale: str
    risk_category: str | None = None
    # name of the policy rule that matched, when source == fast-path
    matched_rule: str | None = None

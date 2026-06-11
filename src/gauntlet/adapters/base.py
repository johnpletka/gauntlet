"""Adapter contract: protocol, result model, capabilities, errors.

`AgentResult` follows PRD §4.1 exactly: text, structured, session_id, usage,
raw_events, exit_code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Usage(BaseModel):
    """Token/cost accounting for one adapter invocation (FR-3.2).

    ``cost_usd`` is ``None`` on the degraded tokens-only path (PRD §12 Q3:
    subscription-auth CLIs may not report cost).
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None


class AgentResult(BaseModel):
    """Result of one adapter invocation, per PRD §4.1."""

    text: str
    structured: Any | None = None
    session_id: str | None = None
    usage: Usage | None = None
    raw_events: list[dict[str, Any]] = Field(default_factory=list)
    exit_code: int


class AdapterCapabilities(BaseModel):
    """Declared capabilities, checked by pipeline load-time validation (FR-2.3)."""

    repo_write: bool
    structured_output: Literal["native", "best_effort", "none"]
    resume: bool


@runtime_checkable
class AgentAdapter(Protocol):
    """Common interface over Claude Code, Codex, and raw-API agents (PRD §4.1)."""

    name: str
    capabilities: AdapterCapabilities

    def run(
        self,
        prompt: str,
        *,
        session: str | None = None,
        schema: dict | None = None,
        cwd: Path | None = None,
        extra_flags: list[str] | None = None,
    ) -> AgentResult: ...


class AdapterError(Exception):
    """Base adapter failure. Carries a partial result for checkpointing.

    ``partial`` holds whatever could be salvaged (parsed events, captured
    output, exit code) so the engine can persist a checkpointable error
    record instead of losing the evidence (FR-3.3).
    """

    def __init__(self, message: str, *, partial: AgentResult | None = None) -> None:
        super().__init__(message)
        self.partial = partial


class AgentTimeoutError(AdapterError):
    """The CLI invocation exceeded its hard timeout and was killed (FR-3.3)."""


class AgentFailedError(AdapterError):
    """The CLI ran and produced parseable output, but reported failure
    (nonzero exit, is_error, turn.failed). Fail closed: a failed call never
    surfaces as a normal AgentResult (review P1 F-001)."""


class MalformedOutputError(AdapterError):
    """Adapter output could not be parsed (or failed schema validation)."""


class UnsupportedFeatureError(AdapterError):
    """The adapter does not support a requested feature (e.g. resume on api)."""

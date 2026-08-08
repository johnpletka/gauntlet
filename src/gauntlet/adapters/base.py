"""Adapter contract: protocol, result model, capabilities, errors.

`AgentResult` follows PRD §4.1 exactly: text, structured, session_id, usage,
raw_events, exit_code.
"""

from __future__ import annotations

from collections.abc import Callable
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
    # harness-efficiency FR-1.3: can this adapter READ the repo working tree?
    # Reference/`phase` context modes (FR-1.1) inject a repo-relative *path* the
    # agent must open itself, so they are valid only on a repo-reading profile.
    # Fail closed: default False, so an adapter that has not declared the
    # capability can never be handed an unreadable-path prompt. The CLI agents
    # (claude-code, codex) set it True; the in-process `api` adapter leaves it
    # False (it has no repo access). This is the adapter-class declaration; a
    # profile's *effective* capability is this value under that profile's actual
    # sandbox, verified by `gauntlet doctor`'s read probe (FR-6.4, P7).
    reads_repo: bool = False
    # The adapter's hard per-invocation input size in characters, or None when
    # no cap is known. An over-cap prompt is rejected WHOLESALE by the CLI
    # (codex app-server `input_too_large` at 1 MiB), killing the step — so
    # prompt builders consult this to fall back to by-reference context (the
    # agent reads the payload from the repo itself) instead of inlining a
    # payload the adapter can never accept. Declaration only — nothing trims a
    # prompt to fit (a silent trim would narrow the agent's view; fail closed).
    max_input_chars: int | None = None


# --- failure classification (harness-efficiency FR-3.1) ----------------------
# Every nonzero-exit / reported-failure outcome is classified into one of these
# kinds. The two ``transient_*`` kinds are recoverable interruptions the engine
# PARKS on (worktree + session preserved) and continues by resuming the CLI
# session; ``terminal`` halts the run for a human. Fail closed: anything that
# does not match the pinned per-adapter marker allowlist (§6) is ``terminal`` —
# the harness never auto-continues past an unrecognized error.
FAILURE_TRANSIENT_USAGE_LIMIT = "transient_usage_limit"
FAILURE_TRANSIENT_OVERLOAD = "transient_overload"
# A typed transport/dependency failure (plan §5.2, P5): provider timeout,
# connection/DNS failure, or a non-overload server-side fault. Recoverable
# infrastructure, not a semantic agent failure — the engine retries it with
# bounded backoff and, on exhaustion, parks ``provider_unavailable`` for a
# plain resume (never ``--response``; R7, issue #63).
FAILURE_TRANSIENT_DEPENDENCY = "transient_dependency"
FAILURE_TERMINAL = "terminal"

# The transient kinds, as a set, for the "is this a park-and-resume condition?"
# test the orchestrator and cycle make.
TRANSIENT_FAILURE_KINDS = frozenset(
    {
        FAILURE_TRANSIENT_USAGE_LIMIT,
        FAILURE_TRANSIENT_OVERLOAD,
        FAILURE_TRANSIENT_DEPENDENCY,
    }
)

# The transient kinds the P5 dependency-retry policy (plan §5.2) covers with
# bounded in-process retries and, on exhaustion, a ``provider_unavailable``
# park: transport/dependency faults (timeout / connection / DNS / 5xx) and
# provider overload. A ``transient_usage_limit`` (quota/429) deliberately stays
# OUT of this set — it parks immediately with the provider's reset deadline and
# retries through the existing plain-resume / FR-3.4 scheduled-resume path
# (its persisted budget is ``ScheduledResume.attempts``).
DEPENDENCY_FAILURE_KINDS = frozenset(
    {FAILURE_TRANSIENT_OVERLOAD, FAILURE_TRANSIENT_DEPENDENCY}
)


class FailureInfo(BaseModel):
    """Adapter → engine classification of one failed invocation (FR-3.1, §6).

    Built by the adapter's ``classify_failure`` from the pinned marker allowlist
    and carried on :class:`AgentFailedError`. ``kind`` is one of the four
    ``FAILURE_*`` values above; ``retry_after_s`` is populated ONLY from a
    structured field on the error envelope (a typed ``retry_after`` / a
    ``Retry-After`` header), never scraped from prose (§7), and is ``None`` when
    absent. ``marker`` names the matched allowlist entry (or ``"unmatched"``) for
    the audit trail; ``raw_excerpt`` is ≤500 chars of the structured error field
    that drove the decision.
    """

    kind: Literal[
        "transient_usage_limit",
        "transient_overload",
        "transient_dependency",
        "terminal",
    ] = FAILURE_TERMINAL
    retry_after_s: int | None = None
    marker: str = "unmatched"
    raw_excerpt: str = ""

    @property
    def is_transient(self) -> bool:
        return self.kind in TRANSIENT_FAILURE_KINDS


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
        sink: Callable[[str], None] | None = None,
    ) -> AgentResult:
        """Run the agent and return its result.

        ``sink`` (live-run-observability FR-2/FR-7): an optional callable that
        receives each complete NDJSON stdout line as it arrives, for live
        persistence. Only the subprocess/NDJSON CLI adapters honor it, and only
        when their output mode is message-granular and qualified
        (:meth:`streams_to_sink`); the in-process API adapter accepts it for
        interface parity and ignores it (a durable Non-Goal — token-delta stream
        breaks the per-line redaction unit). ``None`` ⇒ the buffered path."""
        ...


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
    surfaces as a normal AgentResult (review P1 F-001).

    ``failure_info`` (harness-efficiency FR-3.1) is the adapter's classification
    of the failure. A ``transient_*`` kind tells the engine to PARK-and-resume
    (worktree + session preserved) instead of failing the step; a ``terminal``
    kind (the fail-closed default) halts for a human. ``None`` when an older
    adapter raised the error without classifying — the engine treats an absent
    ``failure_info`` as terminal, so the fail-closed property holds either way.
    """

    def __init__(
        self,
        message: str,
        *,
        partial: AgentResult | None = None,
        failure_info: "FailureInfo | None" = None,
    ) -> None:
        super().__init__(message, partial=partial)
        self.failure_info = failure_info


class MalformedOutputError(AdapterError):
    """Adapter output could not be parsed (or failed schema validation)."""


class SessionNotFoundError(AdapterError):
    """A ``--resume``/``resume`` invocation named a session the CLI no longer
    knows (unknown/expired). Distinct from a normal failure so a usage-limit
    continuation resume (FR-3.3) can fall back to a full re-run with no session
    instead of surfacing an error — resuming a dead session is recoverable, not
    a run-halting fault."""


class UnsupportedFeatureError(AdapterError):
    """The adapter does not support a requested feature (e.g. resume on api)."""

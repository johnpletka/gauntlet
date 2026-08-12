"""Pinned failure-marker allowlist + per-adapter classification (FR-3.1, §6).

Every nonzero-exit / reported-failure outcome is classified into a
:class:`~gauntlet.adapters.base.FailureInfo` by matching **only** structured
error-envelope fields at fixed paths against the pinned allowlist below. The
match is fail-closed: anything that does not match an allowlist entry —
including a typed envelope whose only quota signal is an unrecognized
human-readable message — is ``terminal`` and halts the run for a human (§7). A
matched ``message``/``result`` string is an exact-position field checked against
a *pinned* regex, categorically different from parsing arbitrary free-text prose
in transcript/agent output, which is never matched.

Adding a new marker means adding a rule here **and** a captured fixture under
``.gauntlet/failure-fixtures/`` (contract-tested: every rule has a fixture, and
every fixture classifies to its rule — mirroring the ``.gauntlet/pins.yaml``
discipline, re-verified when a pinned CLI version changes; BOOTSTRAP-NOTES #26).

``retry_after_s`` is read ONLY from a structured field on the same envelope
(e.g. a LiteLLM ``retry_after`` attribute / ``Retry-After`` header), never
scraped from a prose "resets at 5:40pm" message (§7); absent ⇒ ``None``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from gauntlet.adapters.base import (
    FAILURE_TERMINAL,
    FAILURE_TRANSIENT_DEPENDENCY,
    FAILURE_TRANSIENT_OVERLOAD,
    FAILURE_TRANSIENT_USAGE_LIMIT,
    FailureInfo,
)

MAX_EXCERPT = 500  # §6 FailureInfo.raw_excerpt cap


@dataclass(frozen=True)
class MarkerRule:
    """One pinned allowlist entry (§6 "Failure-marker allowlist" table row).

    ``field`` is the structured path matched (a result-event key for claude, a
    ``turn.failed`` ``error.*`` key for codex, or the exception class for api).
    ``rule`` is ``regex`` (any pattern in ``values`` matches, case-insensitive),
    ``in`` (exact membership), or ``equals``. ``fixture`` names the captured
    envelope proving the rule; ``real_capture`` records whether it was harvested
    from a live failed run (True) or synthesized from the documented CLI shape
    pending a live capture (False) — a synthesized entry is fail-closed-safe (a
    non-matching real error still parks nothing and halts terminally).
    """

    name: str
    adapter: str
    field: str
    rule: str
    kind: str
    values: tuple[str, ...]
    fixture: str
    real_capture: bool


# --- claude-code -------------------------------------------------------------
# `claude -p --output-format json` surfaces the failure text in the result
# event's ``result`` field (verified against the captured envelopes; ``subtype``
# is "success" even on a usage-limit error, so it is a defensive-only match). The
# PRD table names the field "message"; on this CLI that field is ``result`` —
# a pinned-fixture detail the PRD explicitly leaves to capture (§6 "exact pinned
# values captured in fixtures, not frozen in this PRD").
CLAUDE_RULES: tuple[MarkerRule, ...] = (
    MarkerRule(
        name="claude_usage_limit_subtype",
        adapter="claude-code",
        field="subtype",
        rule="in",
        kind=FAILURE_TRANSIENT_USAGE_LIMIT,
        values=("usage_limit", "rate_limit"),
        fixture="claude/usage-limit-subtype.json",
        real_capture=False,  # defensive: not observed on 2.1.190, pinned for forward-compat
    ),
    MarkerRule(
        name="claude_usage_limit_message",
        adapter="claude-code",
        field="result",
        rule="regex",
        kind=FAILURE_TRANSIENT_USAGE_LIMIT,
        values=(r"session limit", r"usage limit", r"rate limit"),
        fixture="claude/usage-limit.json",
        real_capture=True,
    ),
    MarkerRule(
        name="claude_overload_subtype",
        adapter="claude-code",
        field="subtype",
        rule="in",
        kind=FAILURE_TRANSIENT_OVERLOAD,
        values=("overloaded",),
        fixture="claude/overload-subtype.json",
        real_capture=False,  # defensive
    ),
    MarkerRule(
        name="claude_overload_message",
        adapter="claude-code",
        field="result",
        rule="regex",
        kind=FAILURE_TRANSIENT_OVERLOAD,
        values=(r"overloaded",),
        fixture="claude/overload.json",
        real_capture=True,
    ),
    # P5 (plan §5.2): typed transport/dependency phrasings in the pinned
    # ``result`` field. Deliberately NARROW: the real-captured
    # "Connection closed mid-response" envelope stays TERMINAL (a truncated,
    # partially-consumed response is not a clean pre-response transport fault)
    # — that negative pin is contract-tested and this rule must not absorb it.
    MarkerRule(
        name="claude_dependency_message",
        adapter="claude-code",
        field="result",
        rule="regex",
        kind=FAILURE_TRANSIENT_DEPENDENCY,
        values=(
            r"request timed out",
            r"fetch failed",
            r"network error",
            r"ECONNREFUSED",
            r"ETIMEDOUT",
            r"getaddrinfo",
        ),
        fixture="claude/dependency.json",
        real_capture=False,  # synthesized: no live capture yet (tracked gap)
    ),
)

# --- codex -------------------------------------------------------------------
# `codex exec --json` emits a ``turn.failed`` event carrying ``error.message``
# (verified capture). ``error.code``/``error.type`` are matched defensively when
# present (not observed on 0.139.0, which carries only ``error.message``).
CODEX_RULES: tuple[MarkerRule, ...] = (
    MarkerRule(
        name="codex_usage_limit_code",
        adapter="codex",
        field="error.code",
        rule="in",
        kind=FAILURE_TRANSIENT_USAGE_LIMIT,
        values=("usage_limit_reached", "rate_limited"),
        fixture="codex/usage-limit-code.json",
        real_capture=False,  # defensive
    ),
    MarkerRule(
        name="codex_usage_limit_message",
        adapter="codex",
        field="error.message",
        rule="regex",
        kind=FAILURE_TRANSIENT_USAGE_LIMIT,
        values=(r"usage limit", r"rate limit"),
        fixture="codex/usage-limit.json",
        real_capture=True,
    ),
    MarkerRule(
        name="codex_overload_type",
        adapter="codex",
        field="error.type",
        rule="in",
        kind=FAILURE_TRANSIENT_OVERLOAD,
        values=("overloaded_error",),
        fixture="codex/overload-type.json",
        real_capture=False,  # defensive
    ),
    MarkerRule(
        name="codex_overload_message",
        adapter="codex",
        field="error.message",
        rule="regex",
        kind=FAILURE_TRANSIENT_OVERLOAD,
        values=(r"overloaded",),
        fixture="codex/overload.json",
        real_capture=False,  # synthesized: no live overload capture yet
    ),
    # Issue #119 live capture: after codex's stream reconnect sequence, the
    # authoritative ``turn.failed`` envelope reported that the selected model
    # was "at capacity". This is provider overload, not a content decision, so
    # it consumes the persisted dependency-retry budget and eventually parks
    # ``provider_unavailable`` rather than forcing a human ``--response``.
    MarkerRule(
        name="codex_capacity_message",
        adapter="codex",
        field="error.message",
        rule="regex",
        kind=FAILURE_TRANSIENT_OVERLOAD,
        values=(r"\bat capacity\b",),
        fixture="codex/capacity.json",
        real_capture=True,
    ),
    # P5 (plan §5.2): typed transport/dependency phrasings in the pinned
    # ``error.message`` field.
    MarkerRule(
        name="codex_dependency_message",
        adapter="codex",
        field="error.message",
        rule="regex",
        kind=FAILURE_TRANSIENT_DEPENDENCY,
        values=(
            r"request timed out",
            r"connection refused",
            r"network error",
            r"dns error",
            r"error sending request",
        ),
        fixture="codex/dependency.json",
        real_capture=False,  # synthesized: no live capture yet (tracked gap)
    ),
    # Issue #96: provider unavailability surfaced through the agent's own
    # error event, not the transport-retry path. The live capture is codex's
    # websocket retry loop giving up against an upstream 503 — "Reconnecting...
    # 2/5 (unexpected status 503 Service Unavailable)" (gauntlet 1.0.8,
    # 2026-08-09, cf-ray a28855dbc8007a0f-ATL). Same pinned message position
    # as every other codex rule; kind ``transient_dependency`` so it takes the
    # persisted retry budget and, on exhaustion, the R7 provider_unavailable
    # park — never a terminal halt that forces ``--response`` for pure
    # infrastructure. The "Reconnecting" pattern requires the ``n/m`` retry
    # counter so ordinary prose mentioning reconnection never matches.
    MarkerRule(
        name="codex_provider_unavailable_message",
        adapter="codex",
        field="error.message",
        rule="regex",
        kind=FAILURE_TRANSIENT_DEPENDENCY,
        values=(
            r"unexpected status 5\d\d",
            r"service unavailable",
            r"reconnecting\.*\s*\d+\s*/\s*\d+",
            r"connection reset",
        ),
        fixture="codex/provider-unavailable.json",
        real_capture=True,  # harvested from the issue-#96 run (2026-08-09)
    ),
    # Issue #119 live startup-fatal signature. The ChatGPT desktop app and a
    # separately installed codex CLI can share ``models_cache.json`` while
    # expecting different schemas; the older reader then exits before emitting
    # any structured failure event. Stderr is consulted ONLY when no
    # ``turn.failed``/``error`` event exists (see ``classify_codex_failure``), so
    # a cosmetic cache warning can never override an authoritative terminal
    # event. The conjunctive regex deliberately does not make arbitrary startup
    # crashes retryable.
    MarkerRule(
        name="codex_models_cache_schema_startup",
        adapter="codex",
        field="stderr",
        rule="regex",
        kind=FAILURE_TRANSIENT_DEPENDENCY,
        values=(
            r"models(?:_|\s+)cache.*missing field\s+[`'\"]?base_instructions",
        ),
        fixture="codex/models-cache-schema.json",
        real_capture=True,
    ),
)

# --- api (LiteLLM) -----------------------------------------------------------
# LiteLLM raises typed exception classes; classification reads the class name
# (stable across LiteLLM's provider mapping) — a 429 is RateLimitError, an
# upstream 5xx/overload is InternalServerError/ServiceUnavailableError.
API_RULES: tuple[MarkerRule, ...] = (
    MarkerRule(
        name="api_rate_limit",
        adapter="api",
        field="exception_class",
        rule="in",
        kind=FAILURE_TRANSIENT_USAGE_LIMIT,
        values=("RateLimitError",),
        fixture="api/rate-limit.json",
        real_capture=False,  # synthesized descriptor (LiteLLM exception shape)
    ),
    MarkerRule(
        name="api_overload",
        adapter="api",
        field="exception_class",
        rule="in",
        kind=FAILURE_TRANSIENT_OVERLOAD,
        values=("InternalServerError", "ServiceUnavailableError"),
        fixture="api/overload.json",
        real_capture=False,  # synthesized descriptor
    ),
    # P5 (plan §5.2, issue #63): a LiteLLM timeout raises ``litellm.Timeout``
    # (class name "Timeout"; some providers surface ``APITimeoutError``). The
    # fixture is the LIVE envelope from the #63 run (coaching-side-drawer
    # r1-triage, gauntlet 0.7.0) that was mis-halted terminal.
    MarkerRule(
        name="api_timeout",
        adapter="api",
        field="exception_class",
        rule="in",
        kind=FAILURE_TRANSIENT_DEPENDENCY,
        values=("Timeout", "APITimeoutError"),
        fixture="api/timeout.json",
        real_capture=True,  # harvested from issue #63's failed run
    ),
    # Connection/DNS failures: LiteLLM maps them to ``APIConnectionError``.
    MarkerRule(
        name="api_connection",
        adapter="api",
        field="exception_class",
        rule="in",
        kind=FAILURE_TRANSIENT_DEPENDENCY,
        values=("APIConnectionError", "ConnectionError"),
        fixture="api/connection.json",
        real_capture=False,  # synthesized descriptor
    ),
)

ALL_RULES: tuple[MarkerRule, ...] = CLAUDE_RULES + CODEX_RULES + API_RULES


def _excerpt(value: Any) -> str:
    return str(value)[:MAX_EXCERPT] if value is not None else ""


def _match(rule: MarkerRule, value: Any) -> bool:
    if value is None:
        return False
    text = str(value)
    if rule.rule == "regex":
        return any(re.search(p, text, re.IGNORECASE) for p in rule.values)
    # "in" / "equals": exact, case-sensitive membership.
    return text in rule.values


def _terminal(excerpt: Any) -> FailureInfo:
    return FailureInfo(kind=FAILURE_TERMINAL, marker="unmatched", raw_excerpt=_excerpt(excerpt))


# --- per-adapter classifiers -------------------------------------------------
def classify_claude_failure(
    result_event: dict | None, exit_code: int
) -> FailureInfo:
    """Classify a claude ``-p`` failure from its result event (fail-closed).

    ``result_event`` is the last ``type == "result"`` event (may be ``None`` for
    a nonzero exit with no parseable result). A nonzero exit with no matching
    marker is ``terminal`` — never auto-continued.
    """
    ev = result_event or {}
    for rule in CLAUDE_RULES:
        value = ev.get(rule.field)
        if _match(rule, value):
            return FailureInfo(
                kind=rule.kind, marker=rule.name,
                retry_after_s=None, raw_excerpt=_excerpt(value),
            )
    excerpt = ev.get("result") if ev else f"exit code {exit_code}"
    return _terminal(excerpt)


def codex_failure_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return codex's authoritative structured failure envelope.

    A reconnecting turn may emit one or more intermediate ``error`` events and
    finish with a ``turn.failed`` carrying the actual provider verdict. Prefer
    the LAST ``turn.failed`` when present; otherwise use the LAST ``error``.
    This keeps an early stream-disconnect notice from masking a later capacity
    verdict, while a final unrecognized failure still fails closed to terminal.
    Malformed non-dict elements are ignored rather than crashing the classifier.
    """
    failures = [
        event
        for event in events
        if isinstance(event, dict) and event.get("type") in ("turn.failed", "error")
    ]
    for event in reversed(failures):
        if event.get("type") == "turn.failed":
            return event
    return failures[-1] if failures else None


def classify_codex_failure(
    events: list[dict[str, Any]], exit_code: int, *, stderr: str = ""
) -> FailureInfo:
    """Classify a codex ``exec`` failure, preferring its structured envelope.

    Startup stderr is eligible only when codex emitted no structured failure
    event. That exception is deliberately narrow: it covers pinned pre-event
    startup fatals without allowing a cosmetic warning to reclassify a real
    terminal ``turn.failed`` outcome.
    """
    event = codex_failure_event(events)
    err = event.get("error") if event else None
    if isinstance(err, str):
        err = {"message": err}
    elif not isinstance(err, dict):
        err = {}
    # A bare ``message`` on the event itself (no ``error`` sub-object) is a
    # legitimate codex shape too; fold it in.
    if "message" not in err and event and isinstance(event.get("message"), str):
        err = {**err, "message": event["message"]}
    for rule in CODEX_RULES:
        if rule.field == "stderr":
            continue
        key = rule.field.split(".", 1)[1]  # "error.message" -> "message"
        value = err.get(key)
        if _match(rule, value):
            return FailureInfo(
                kind=rule.kind, marker=rule.name,
                retry_after_s=None, raw_excerpt=_excerpt(value),
            )
    if event is None:
        for rule in CODEX_RULES:
            if rule.field == "stderr" and _match(rule, stderr):
                return FailureInfo(
                    kind=rule.kind,
                    marker=rule.name,
                    retry_after_s=None,
                    raw_excerpt=_excerpt(stderr),
                )
    excerpt = err.get("message") if err else (stderr or f"exit code {exit_code}")
    return _terminal(excerpt)


def _read_retry_after(exc: Exception) -> int | None:
    """Read a STRUCTURED retry hint off a LiteLLM exception (never from prose).

    Prefer a typed ``retry_after`` attribute; else a ``Retry-After`` response
    header. Anything non-integer (or a prose value) is ignored — ``None``.
    """
    raw = getattr(exc, "retry_after", None)
    if raw is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        if isinstance(headers, dict):
            raw = headers.get("Retry-After") or headers.get("retry-after")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _classify_api(
    exception_class: str, retry_after: int | None, excerpt: str
) -> FailureInfo:
    for rule in API_RULES:
        if exception_class in rule.values:
            return FailureInfo(
                kind=rule.kind, marker=rule.name,
                retry_after_s=retry_after,
                raw_excerpt=excerpt[:MAX_EXCERPT],
            )
    return _terminal(excerpt)


def classify_api_failure(exc: Exception) -> FailureInfo:
    """Classify a LiteLLM exception by its class name (fail-closed)."""
    return _classify_api(
        type(exc).__name__, _read_retry_after(exc), str(exc)
    )


# --- session-not-found detection (FR-3.3 continuation fallback) --------------
# When a usage-limit continuation resume names a session the CLI no longer
# knows, the engine falls back to a full re-run (no session) rather than
# surfacing an error — a dead session is recoverable, not a run-halting fault.
# Detection is best-effort against pinned phrasings (no live capture yet); a
# false positive is fail-safe (it just triggers a full re-run, the today path).
_SESSION_NOT_FOUND_RE = re.compile(
    r"no conversation found"
    r"|session (?:id )?.{0,40}?(?:not found|unknown|expired|does not exist)"
    r"|could not (?:find|resume) (?:the )?session"
    r"|unknown session"
    r"|no session (?:found|to resume)",
    re.IGNORECASE,
)


def looks_like_session_not_found(text: str | None) -> bool:
    """True when *text* matches a pinned "resume target session is gone" phrasing.

    Best-effort and fail-safe: only consulted when a session WAS requested, and a
    match merely routes to a full re-run (FR-3.3), never to auto-continuing past
    an unknown error. Re-pin with a live capture when observed.
    """
    return bool(text) and _SESSION_NOT_FOUND_RE.search(text) is not None


# --- contract-test support ---------------------------------------------------
def classify_captured(adapter: str, data: Any) -> FailureInfo:
    """Dispatch a captured fixture envelope to its adapter's classifier.

    Used by the contract test to prove every fixture classifies to its rule.
    ``data`` is the fixture's parsed JSON: a claude result event (dict), a codex
    ``{"events": [...]}`` object, or an api ``{"exception_class", "message",
    "retry_after"}`` descriptor.
    """
    if adapter == "claude-code":
        return classify_claude_failure(data, exit_code=1)
    if adapter == "codex":
        return classify_codex_failure(
            list(data.get("events", [])),
            exit_code=1,
            stderr=str(data.get("stderr", "")),
        )
    if adapter == "api":
        return _classify_api(
            data.get("exception_class", ""),
            data.get("retry_after"),
            str(data.get("message", "")),
        )
    raise ValueError(f"unknown adapter {adapter!r} for fixture classification")

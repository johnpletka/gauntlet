"""Bounded dependency-retry policy (recovery-redesign plan §5.2, P5).

Transport/dependency failures — typed timeout, connection/DNS, 429-adjacent
overload, and 5xx server errors — are recoverable infrastructure faults, not
semantic agent failures. The two adapter call sites (``cycle._run_sub`` and
``steptypes.handle_agent_task``'s ``_invoke``) route a classified
``transient_dependency``/``transient_overload`` failure through this policy:

* the retry budget is **persisted** on ``StepRecord.dependency_attempts`` and
  flushed write-ahead *before* the retry sleep, so a crash between retries
  resumes with the consumed budget intact — never a fresh budget per process
  (plan §5.2: "persisted attempt counts");
* delays are exponential with **deterministic** jitter (derived from the
  run/step/attempt identity, never ``random`` — the engine stays replayable),
  honoring a structured ``Retry-After`` when the envelope carried one;
* a delay that exceeds ``dependency_retry_max_delay_s`` is not hot-waited in
  process: the caller parks ``provider_unavailable`` immediately with that
  deadline recorded, and a plain ``gauntlet resume`` retries after it (R7 —
  never ``--response`` for a retry).

``_sleep`` is module-level so tests inject a no-op clock; production uses
``time.sleep``.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time

from gauntlet.adapters.base import DEPENDENCY_FAILURE_KINDS, FailureInfo

# Patchable sleep for deterministic tests; every retry wait goes through this.
_sleep = time.sleep

# Serializes budget increments + manifest flushes across the triage fan-out's
# worker threads (the pool retries leaves concurrently; the persisted budget is
# step-scoped, so the read-modify-write must not race).
_BUDGET_LOCK = threading.Lock()


def is_dependency_failure(info: "FailureInfo | None") -> bool:
    """True when *info* names a retryable transport/dependency kind (§5.2)."""
    return info is not None and info.kind in DEPENDENCY_FAILURE_KINDS


def _jitter_fraction(key: str) -> float:
    """Deterministic jitter in ``[0, 0.5]`` derived from the attempt identity."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") / 0xFFFFFFFF * 0.5


def backoff_delay_s(
    attempt: int,
    *,
    base_s: float,
    cap_s: float,
    retry_after_s: int | None,
    key: str,
) -> float:
    """The delay before retry ``attempt`` (1-based), jittered + Retry-After-aware.

    Exponential (``base * 2**(attempt-1)``) with deterministic jitter, capped at
    ``cap_s``; a structured ``Retry-After`` raises the floor (the provider's own
    hint always wins upward). The returned value may EXCEED ``cap_s`` only via
    ``Retry-After`` — the caller then parks with the deadline instead of
    hot-waiting.
    """
    delay = min(cap_s, base_s * (2 ** max(0, attempt - 1)))
    delay *= 1.0 + _jitter_fraction(key)
    delay = min(delay, cap_s)
    if retry_after_s is not None:
        delay = max(delay, float(retry_after_s))
    return delay


def park_deadline_s(record, config, info: "FailureInfo | None") -> int:
    """Seconds until an exhausted ``provider_unavailable`` park may retry.

    A structured ``Retry-After`` wins; else the NEXT exponential backoff step
    after the consumed budget. Always ≥1, so the park always carries a concrete
    deadline — the P4.1 F-006 rule that makes it a legitimate wait, not a wedge.
    """
    if info is not None and info.retry_after_s is not None:
        return max(1, int(info.retry_after_s))
    base = config.dependency_retry_base_s
    return max(1, math.ceil(base * (2 ** max(0, record.dependency_attempts))))


def _flush(ctx) -> None:
    """Write-ahead flush of the consumed budget (crash-proof, plan §5.2)."""
    if ctx.persist is not None:
        ctx.persist()
    else:  # standalone handler invocation (no orchestrator): write directly
        ctx.manifest.write_atomic(ctx.run_dir / "manifest.json")


def consume_retry(ctx, info: "FailureInfo | None", *, site: str) -> float | None:
    """Consume one persisted retry from the step's dependency budget.

    Returns the delay (seconds) to sleep before retrying, or ``None`` when the
    caller must PARK instead: the budget is exhausted, or the required delay
    (a long ``Retry-After``) exceeds the in-process wait cap. The increment and
    its manifest flush happen atomically under a lock BEFORE the delay is
    returned, so the budget survives a crash mid-sleep.
    """
    if not is_dependency_failure(info):
        return None
    config = ctx.config
    with _BUDGET_LOCK:
        attempts = ctx.record.dependency_attempts
        if attempts >= config.dependency_retry_attempts:
            return None
        delay = backoff_delay_s(
            attempts + 1,
            base_s=config.dependency_retry_base_s,
            cap_s=config.dependency_retry_max_delay_s,
            retry_after_s=info.retry_after_s if info is not None else None,
            key=f"{ctx.manifest.run_id}|{site}|{attempts + 1}",
        )
        if delay > config.dependency_retry_max_delay_s:
            return None  # a long Retry-After parks with the deadline instead
        ctx.record.dependency_attempts = attempts + 1
        _flush(ctx)
    return delay


def wait(delay: float) -> None:
    """The retry sleep — a thin, patchable indirection for tests."""
    _sleep(delay)


__all__ = [
    "backoff_delay_s",
    "consume_retry",
    "is_dependency_failure",
    "park_deadline_s",
    "wait",
]

"""Clock-time evidence: per-call capture + the ``gauntlet report`` time section.

Two halves, kept together because they share one vocabulary:

**Capture.** :func:`record_invocation` wraps every adapter call the engine makes
(a cycle sub-step, a single-agent step's call, the commit-message drafter) and
appends one :class:`~gauntlet.engine.manifest.Invocation` to the step record:
the engine's own UTC start/end stamps, the monotonic wall-clock width, the
agent profile, a label naming the work, and the outcome. The measurement is
the engine's, not the CLI's — codex exports no clock-time stats of its own,
claude-code does, and the harness must not care: both get the same record.

**Report.** :func:`build_timing` aggregates a run's clock time three ways —
per step, per agent profile (mapped to its adapter/model), and per activity
(``review`` / ``triage`` / ``fix`` / ``confirm`` / ``verify`` across every
cycle, plus each single-agent step by id) — and splits the overall wall-clock
span into disjoint *agent time* (the union of adapter-call intervals), *parked*
(by park reason), *host-suspended*, and the *other* remainder (engine, git,
tests, gaps). Parked
intervals are derived from the run's append-only state journal (every persist
carries a timestamp and the full state), so the split is data, never a guess;
a run without a journal shows ``—`` for parked rather than an estimate.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from gauntlet.adapters.base import (
    AdapterError,
    AgentTimeoutError,
    AgentVanishedError,
    MalformedOutputError,
    SessionNotFoundError,
)
from gauntlet.engine import manifest as M

# --- capture -----------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def outcome_of(exc: BaseException | None) -> str:
    """Map the exception an adapter call raised to an ``INVOCATION_OUTCOMES`` value."""
    if exc is None:
        return "ok"
    if isinstance(exc, AgentTimeoutError):
        return "timeout"
    if isinstance(exc, AgentVanishedError):
        return "vanished"
    if isinstance(exc, SessionNotFoundError):
        return "session_not_found"
    if isinstance(exc, MalformedOutputError):
        return "malformed"
    if isinstance(exc, AdapterError):
        return "failed"
    return "error"


def _provenance(
    ctx: Any, agent: str | None, adapter: Any, effort: str | None
) -> tuple[str | None, str | None, str | None]:
    """(adapter, model, effort) frozen for one call.

    The profile supplies adapter name, model and default effort; the BUILT
    adapter's ``model`` wins when it carries one (it is what was actually put on
    the command line); an explicit ``effort`` override (a cycle-/step-level
    ``effort:``) wins over the profile's. A context without a config, or a
    profile the config no longer knows, yields ``None`` — recorded honestly,
    never guessed.
    """
    profile = None
    config = getattr(ctx, "config", None)
    if agent and config is not None:
        try:
            profile = config.profile(agent)
        except (KeyError, AttributeError):
            profile = None
    adapter_name = getattr(profile, "adapter", None)
    model = getattr(adapter, "model", None) or getattr(profile, "model", None)
    return adapter_name, model, effort or getattr(profile, "effort", None)


@contextmanager
def record_invocation(
    ctx: Any,
    *,
    agent: str | None,
    label: str,
    adapter: Any = None,
    effort: str | None = None,
) -> Iterator[None]:
    """Time one adapter call and append an :class:`Invocation` to ``ctx.record``.

    Records on every exit — a returned result or a raised error — and re-raises
    unchanged, so wrapping a call never alters the engine's failure handling.
    The wall-clock width is monotonic (immune to clock steps); the stamps are
    UTC ISO for cross-referencing with the journal and the step's own
    ``started``/``ended``. ``adapter`` (the built adapter instance) and
    ``effort`` (an explicit override, else the profile's) freeze the
    adapter/model/effort provenance onto the record — see :func:`_provenance`.
    A context without a step record (a standalone handler test double) records
    nothing rather than failing the call.
    """
    record = getattr(ctx, "record", None)
    sink = getattr(record, "invocations", None)
    adapter_name, model, effort_used = _provenance(ctx, agent, adapter, effort)
    started = _utcnow_iso()
    t0 = time.monotonic()
    exc: BaseException | None = None
    try:
        yield
    except BaseException as caught:
        exc = caught
        raise
    finally:
        if isinstance(sink, list):
            sink.append(
                M.Invocation(
                    agent=agent,
                    label=label,
                    started=started,
                    ended=_utcnow_iso(),
                    wall_s=max(0.0, time.monotonic() - t0),
                    outcome=outcome_of(exc),
                    attempt=int(getattr(record, "attempts", 0) or 0),
                    adapter=adapter_name,
                    model=model,
                    effort=effort_used,
                )
            )


# --- aggregation ---------------------------------------------------------------

# Cycle sub-step labels (``r<N>-<kind>`` with an optional ensemble-member
# suffix) collapse to their kind; the order below is the report's row order.
CYCLE_KINDS = ("review", "triage", "fix", "confirm", "verify")
_CYCLE_LABEL = re.compile(r"^r\d+-(review|triage|fix|confirm|verify)(?:-|$)")


def activity_kind(label: str, step_id: str) -> str:
    """The activity a call belongs to: a cycle sub-step kind, else the step id.

    A single-agent step's calls (``call``, ``call-repair1``) and its
    commit-message drafts count under the step's own id (``implement``,
    ``plan-author``), which is how a pipeline names its work; every cycle's
    sub-steps pool by kind so "how long is review across the run?" is one row.
    """
    m = _CYCLE_LABEL.match(label)
    if m:
        return m.group(1)
    if label.startswith("response-disposition"):
        return "disposition"
    return step_id


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # engine stamps are UTC
    return dt


def _seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max(0.0, (end - start).total_seconds())


_Interval = tuple[datetime, datetime]


def _merge_intervals(intervals: Sequence[_Interval]) -> list[_Interval]:
    """Return the chronological union of valid half-open intervals."""
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[_Interval] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        prior_start, prior_end = merged[-1]
        merged[-1] = (prior_start, max(prior_end, end))
    return merged


def _clip_intervals(
    intervals: Sequence[_Interval], bounds: _Interval
) -> list[_Interval]:
    start_bound, end_bound = bounds
    return _merge_intervals(
        [
            (max(start, start_bound), min(end, end_bound))
            for start, end in intervals
            if end > start_bound and start < end_bound
        ]
    )


def _subtract_intervals(
    intervals: Sequence[_Interval], blockers: Sequence[_Interval]
) -> list[_Interval]:
    """Subtract the blocker union from the interval union."""
    blocked = _merge_intervals(blockers)
    out: list[_Interval] = []
    for start, end in _merge_intervals(intervals):
        cursor = start
        for block_start, block_end in blocked:
            if block_end <= cursor:
                continue
            if block_start >= end:
                break
            if block_start > cursor:
                out.append((cursor, min(block_start, end)))
            cursor = max(cursor, block_end)
            if cursor >= end:
                break
        if cursor < end:
            out.append((cursor, end))
    return out


def _interval_seconds(intervals: Sequence[_Interval]) -> float:
    return sum(
        (end - start).total_seconds()
        for start, end in _merge_intervals(intervals)
    )


def _leaf(rec: Any) -> str:
    return rec.id if rec.iteration is None else f"{rec.id}.{rec.iteration}"


def _frozen_model(inv: Any) -> str | None:
    """``adapter/model[@effort]`` as frozen on a call; None when nothing was."""
    adapter = getattr(inv, "adapter", None)
    model = getattr(inv, "model", None)
    if not adapter and not model:
        return None
    text = f"{adapter}/{model}" if adapter and model else (adapter or model)
    effort = getattr(inv, "effort", None)
    return f"{text}@{effort}" if effort else text


def parked_intervals_from_events(
    events: Sequence[Mapping[str, Any]], *, end: datetime
) -> dict[tuple[str, str | None], dict[str, list[_Interval]]]:
    """Per-step parked intervals by reason, replayed from journal events.

    Walks the journaled states in sequence order; each time a step's status
    leaves ``parked`` the interval since it entered is credited to the reason
    it parked with. A step still parked at the end of the journal is credited
    up to ``end`` (the caller's *now* for a live run). A ``human_gate`` park
    that carries no reason reads as ``gate``. Events without a state snapshot
    or a parseable stamp are skipped, never guessed at.
    """
    last: dict[tuple[str, str | None], tuple[str, str, datetime]] = {}
    out: dict[tuple[str, str | None], dict[str, list[_Interval]]] = {}

    def credit(
        key: tuple[str, str | None], reason: str, start: datetime, stop: datetime
    ) -> None:
        if stop <= start:
            return
        bucket = out.setdefault(key, {})
        bucket.setdefault(reason, []).append((start, stop))

    def seq(ev: Mapping[str, Any]) -> int:
        try:
            return int(ev.get("seq", 0))
        except (TypeError, ValueError):
            return 0

    for ev in sorted(events, key=seq):
        state_json = ev.get("state_json")
        ts = _parse_ts(ev.get("ts"))
        if not state_json or ts is None:
            continue
        try:
            state = json.loads(state_json)
        except ValueError:
            continue
        for rec in (state.get("steps") or []) if isinstance(state, dict) else []:
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            key = (str(rec["id"]), rec.get("iteration"))
            status = str(rec.get("status") or "")
            reason = rec.get("parked_reason") or (
                M.PARKED_REASON_GATE if rec.get("type") == "human_gate" else "unknown"
            )
            prev = last.get(key)
            if prev is None:
                last[key] = (status, str(reason), ts)
                continue
            if prev[0] != status or (
                status == M.PARKED and prev[1] != str(reason)
            ):
                if prev[0] == M.PARKED:
                    credit(key, prev[1], prev[2], ts)
                last[key] = (status, str(reason), ts)
    for key, (status, reason, since) in last.items():
        if status == M.PARKED:
            credit(key, reason, since, end)
    return out


def parked_seconds_from_events(
    events: Sequence[Mapping[str, Any]], *, end: datetime
) -> dict[tuple[str, str | None], dict[str, float]]:
    """Per-step parked seconds by reason, derived from replayed intervals."""
    return {
        key: {
            reason: _interval_seconds(intervals)
            for reason, intervals in by_reason.items()
        }
        for key, by_reason in parked_intervals_from_events(events, end=end).items()
    }


@dataclass
class StepTiming:
    leaf: str
    type: str
    agent: str | None
    status: str
    attempts: int
    wall_s: float | None  # started → ended (→ now while running); spans parks
    active_s: float | None  # sum of this step's adapter-call wall time; None = no calls recorded
    calls: int
    parked: dict[str, float] | None  # by reason; None = no journal to replay
    by_kind: dict[str, float] = field(default_factory=dict)


@dataclass
class AgentTiming:
    agent: str
    model: str | None
    calls: int
    active_s: float | None
    pct: float | None


@dataclass
class KindTiming:
    kind: str
    calls: int
    active_s: float
    pct: float | None


@dataclass
class TimingData:
    run_id: str
    slug: str
    status: str
    in_progress: bool
    first_started: str | None
    last_ended: str | None
    overall_s: float | None
    # Disjoint elapsed time covered by at least one adapter call after parked and
    # host-suspended intervals take precedence. This is the overall partition's
    # agent-time component, so it can never exceed ``overall_s``.
    active_s: float | None
    # Sum of every call's duration. Concurrent work overlaps, so this can exceed
    # wall time; it is the denominator for per-agent/per-activity shares.
    agent_seconds_s: float | None
    calls: int
    parked: dict[str, float] | None  # by reason; None = no journal
    suspended_s: float
    other_s: float | None
    steps: list[StepTiming] = field(default_factory=list)
    agents: list[AgentTiming] = field(default_factory=list)
    kinds: list[KindTiming] = field(default_factory=list)
    has_journal: bool = False
    steps_without_calls: int = 0


def build_timing(
    manifest: Any,
    *,
    events: Sequence[Mapping[str, Any]] | None = None,
    model_of: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> TimingData:
    """Aggregate a run's clock time from its manifest (+ optional journal events).

    ``events`` are the run's journal events (``journal.read_events``); absent
    or empty, parked time is reported as unavailable rather than estimated.
    ``model_of`` maps agent-profile name → display model (``adapter/model``),
    resolved by the caller from config. ``now`` closes open intervals of a run
    that is still running or parked (defaults to the current UTC time).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    model_of = model_of or {}
    in_progress = manifest.status in (M.RUN_RUNNING, M.RUN_PARKED)
    has_journal = bool(events)
    parked_intervals_by_step = (
        parked_intervals_from_events(events, end=now) if has_journal else {}
    )

    steps: list[StepTiming] = []
    by_agent_calls: dict[str, int] = {}
    by_agent_s: dict[str, float] = {}
    by_agent_models: dict[str, list[str]] = {}  # frozen "adapter/model[@effort]" seen
    by_kind_calls: dict[str, int] = {}
    by_kind_s: dict[str, float] = {}
    total_agent_seconds: float | None = None
    agent_intervals: list[_Interval] = []
    invocation_interval_missing = False
    fallback_parked_intervals: dict[
        tuple[str, str | None], dict[str, list[_Interval]]
    ] = {}
    total_calls = 0
    without_calls = 0
    for rec in manifest.steps:
        start = _parse_ts(rec.started)
        end = _parse_ts(rec.ended)
        if end is None and rec.status == M.RUNNING:
            end = now
        wall = _seconds(start, end)
        invocations = list(getattr(rec, "invocations", None) or [])
        active: float | None = None
        kinds: dict[str, float] = {}
        if invocations:
            active = 0.0
            for inv in invocations:
                active += inv.wall_s
                inv_start = _parse_ts(inv.started)
                inv_end = _parse_ts(inv.ended)
                if inv_start is None or inv_end is None or inv_end < inv_start:
                    invocation_interval_missing = True
                else:
                    agent_intervals.append((inv_start, inv_end))
                kind = activity_kind(inv.label, rec.id)
                kinds[kind] = kinds.get(kind, 0.0) + inv.wall_s
                by_kind_calls[kind] = by_kind_calls.get(kind, 0) + 1
                by_kind_s[kind] = by_kind_s.get(kind, 0.0) + inv.wall_s
                name = inv.agent or "—"
                by_agent_calls[name] = by_agent_calls.get(name, 0) + 1
                by_agent_s[name] = by_agent_s.get(name, 0.0) + inv.wall_s
                frozen = _frozen_model(inv)
                if frozen and frozen not in by_agent_models.setdefault(name, []):
                    by_agent_models[name].append(frozen)
            total_agent_seconds = (total_agent_seconds or 0.0) + active
            total_calls += len(invocations)
        elif rec.agent is not None or rec.type == "adversarial_cycle":
            without_calls += 1
        if has_journal:
            parked = {
                reason: _interval_seconds(intervals)
                for reason, intervals in parked_intervals_by_step.get(
                    (rec.id, rec.iteration), {}
                ).items()
            }
        elif rec.type == "human_gate" and start is not None:
            # No journal to replay, but a gate step's own span IS its park: it
            # starts when it parks and ends when a human decides (or is still
            # parked now). Credited as data; every other park kind stays
            # unavailable rather than guessed.
            gate_end = now if rec.status == M.PARKED else end
            gate_s = _seconds(start, gate_end)
            parked = {M.PARKED_REASON_GATE: gate_s} if gate_s is not None else None
            if gate_end is not None and gate_end > start:
                fallback_parked_intervals[(rec.id, rec.iteration)] = {
                    M.PARKED_REASON_GATE: [(start, gate_end)]
                }
        else:
            parked = None
        steps.append(
            StepTiming(
                leaf=_leaf(rec), type=rec.type, agent=rec.agent, status=rec.status,
                attempts=rec.attempts, wall_s=wall, active_s=active,
                calls=len(invocations), parked=parked, by_kind=kinds,
            )
        )

    # Overall span: earliest step start → latest step end, or now while the run
    # is live (running or parked — a park is time the run is waiting).
    starts = [t for t in (_parse_ts(r.started) for r in manifest.steps) if t]
    ends = [t for t in (_parse_ts(r.ended) for r in manifest.steps) if t]
    first = min(starts) if starts else None
    if first is None:
        last: datetime | None = None
    elif in_progress:
        last = now
    else:
        last = max(ends) if ends else now
    overall = _seconds(first, last)

    interval_source = (
        parked_intervals_by_step if has_journal else fallback_parked_intervals
    )
    parked_raw_by_reason: dict[str, list[_Interval]] = {}
    for by_reason in interval_source.values():
        for reason, intervals in by_reason.items():
            parked_raw_by_reason.setdefault(reason, []).extend(intervals)

    suspension_intervals: list[_Interval] = []
    suspension_interval_missing = False
    for suspension in getattr(manifest, "suspensions", []) or []:
        suspension_start = _parse_ts(suspension.start)
        suspension_end = _parse_ts(suspension.end)
        if (
            suspension_start is None
            or suspension_end is None
            or suspension_end < suspension_start
        ):
            suspension_interval_missing = True
        else:
            suspension_intervals.append((suspension_start, suspension_end))

    parked_total: dict[str, float] | None = None
    suspended = 0.0
    active_wall: float | None = None
    other: float | None = None
    if first is not None and last is not None:
        bounds = (first, last)
        suspended_intervals = _clip_intervals(suspension_intervals, bounds)
        suspended = _interval_seconds(suspended_intervals)

        # The overall split is a partition, not a sum of independently
        # overlapping counters: host suspension wins over parks, parks win over
        # adapter calls, and the remainder is genuinely "other". Per-agent and
        # per-activity tables deliberately retain agent-seconds below.
        parked_occupied: list[_Interval] = []
        if has_journal or parked_raw_by_reason:
            parked_total = {}
            ordered_reasons = sorted(
                parked_raw_by_reason,
                key=lambda reason: (
                    min(start for start, _ in parked_raw_by_reason[reason]), reason
                ),
            )
            for reason in ordered_reasons:
                raw = _clip_intervals(parked_raw_by_reason[reason], bounds)
                effective = _subtract_intervals(
                    raw, [*suspended_intervals, *parked_occupied]
                )
                parked_total[reason] = _interval_seconds(effective)
                parked_occupied = _merge_intervals([*parked_occupied, *effective])

        if total_agent_seconds is not None and not invocation_interval_missing:
            effective_agent = _subtract_intervals(
                _clip_intervals(agent_intervals, bounds),
                [*suspended_intervals, *parked_occupied],
            )
            active_wall = _interval_seconds(effective_agent)
        if (
            overall is not None
            and active_wall is not None
            and not suspension_interval_missing
        ):
            other = max(
                0.0,
                overall
                - active_wall
                - sum((parked_total or {}).values())
                - suspended,
            )

    # Per agent profile: every profile that made a call, plus any profile the
    # manifest billed usage to without recorded calls (a pre-timing run) so the
    # table still lists it (with `—`).
    names = sorted(set(by_agent_calls) | set(getattr(manifest, "agent_usage", {}) or {}))
    agents = [
        AgentTiming(
            agent=name,
            # What actually ran (frozen on the calls) beats today's config.
            model=(
                " | ".join(by_agent_models[name])
                if by_agent_models.get(name) else model_of.get(name)
            ),
            calls=by_agent_calls.get(name, 0),
            active_s=by_agent_s.get(name) if name in by_agent_calls else None,
            pct=(
                100.0 * by_agent_s[name] / total_agent_seconds
                if name in by_agent_calls and total_agent_seconds
                else None
            ),
        )
        for name in names
    ]
    ordered = [k for k in CYCLE_KINDS if k in by_kind_s] + sorted(
        (k for k in by_kind_s if k not in CYCLE_KINDS),
        key=lambda k: -by_kind_s[k],
    )
    kinds_out = [
        KindTiming(
            kind=k, calls=by_kind_calls[k], active_s=by_kind_s[k],
            pct=(
                100.0 * by_kind_s[k] / total_agent_seconds
                if total_agent_seconds else None
            ),
        )
        for k in ordered
    ]
    return TimingData(
        run_id=manifest.run_id, slug=manifest.slug, status=manifest.status,
        in_progress=in_progress,
        first_started=first.isoformat(timespec="seconds") if first else None,
        last_ended=last.isoformat(timespec="seconds") if last else None,
        overall_s=overall, active_s=active_wall,
        agent_seconds_s=total_agent_seconds, calls=total_calls,
        parked=parked_total, suspended_s=suspended, other_s=other,
        steps=steps, agents=agents, kinds=kinds_out, has_journal=has_journal,
        steps_without_calls=without_calls,
    )


# --- rendering ---------------------------------------------------------------


def fmt_duration(secs: float | None) -> str:
    """``—`` for unknown; ``42s`` / ``12m 05s`` / ``2h 03m`` otherwise."""
    if secs is None:
        return "—"
    secs = max(0.0, secs)
    if secs < 60:
        return f"{secs:.0f}s"
    if secs < 3600:
        m, s = divmod(int(round(secs)), 60)
        return f"{m}m {s:02d}s"
    h, rem = divmod(int(round(secs)), 3600)
    return f"{h}h {rem // 60:02d}m"


def _pct(p: float | None) -> str:
    return f"{p:.1f}%" if p is not None else "—"


def _parked_cell(parked: dict[str, float] | None) -> str:
    if parked is None:
        return "—"
    return fmt_duration(sum(parked.values()))


def _breakdown(parts: Mapping[str, float]) -> str:
    if not parts:
        return ""
    order = [k for k in CYCLE_KINDS if k in parts] + sorted(
        k for k in parts if k not in CYCLE_KINDS
    )
    return " · ".join(f"{k} {fmt_duration(parts[k])}" for k in order)


def render_timing(data: TimingData) -> str:
    """Render the time section as plain text for ``gauntlet report``."""
    span = ""
    if data.first_started:
        span = f"  ({data.first_started} → {data.last_ended}"
        span += ", in progress)" if data.in_progress else ")"
    lines = [
        f"Time report — run {data.run_id} ({data.slug}) [{data.status}]",
        f"  overall:     {fmt_duration(data.overall_s):>9}{span}",
        f"  agent time:  {fmt_duration(data.active_s):>9}  "
        f"(elapsed wall covered by {data.calls} adapter call(s))",
        f"  agent-secs:  {fmt_duration(data.agent_seconds_s):>9}  "
        "(sum across calls; concurrent work can exceed overall)",
    ]
    parked_line = f"  parked:      {_parked_cell(data.parked):>9}"
    if data.parked:
        parked_line += "  (" + " · ".join(
            f"{r} {fmt_duration(s)}" for r, s in sorted(data.parked.items(), key=lambda kv: -kv[1])
        ) + ")"
    lines.append(parked_line)
    lines.append(f"  suspended:   {fmt_duration(data.suspended_s):>9}  (host sleep, FR-5.1)")
    lines.append(
        f"  other:       {fmt_duration(data.other_s):>9}  "
        "(engine, git, tests, gaps between calls"
        + ("; includes non-gate parks — no journal" if not data.has_journal else "")
        + ")"
    )
    lines += [
        "",
        "Per agent profile (agent-seconds inside its calls):",
        f"  {'agent':<16} {'model':<28} {'calls':>6} {'time':>10} {'% time':>8}",
    ]
    for a in data.agents:
        lines.append(
            f"  {a.agent:<16} {(a.model or '—'):<28} {a.calls:>6} "
            f"{fmt_duration(a.active_s):>10} {_pct(a.pct):>8}"
        )
    if not data.agents:
        lines.append("  (no agent calls recorded)")
    lines += [
        "",
        "Per activity (agent-seconds; cycle sub-steps pooled, other steps by id):",
        f"  {'activity':<20} {'calls':>6} {'time':>10} {'% time':>8}",
    ]
    for k in data.kinds:
        lines.append(
            f"  {k.kind:<20} {k.calls:>6} {fmt_duration(k.active_s):>10} {_pct(k.pct):>8}"
        )
    if not data.kinds:
        lines.append("  (no agent calls recorded)")
    lines += [
        "",
        "Per step:",
        f"  {'step':<22} {'type':<18} {'status':<9} {'wall':>9} {'agent':>9} "
        f"{'parked':>9} {'calls':>6}  breakdown",
    ]
    for s in data.steps:
        lines.append(
            f"  {s.leaf:<22} {s.type:<18} {s.status:<9} {fmt_duration(s.wall_s):>9} "
            f"{fmt_duration(s.active_s):>9} {_parked_cell(s.parked):>9} {s.calls:>6}  "
            f"{_breakdown(s.by_kind)}".rstrip()
        )
    if not data.steps:
        lines.append("  (no steps recorded)")
    notes = []
    if not data.has_journal:
        notes.append(
            "Note: no state journal for this run — only human-gate parks (a gate "
            "step's own start → end) are counted; usage-limit / response / "
            "provider parks are unavailable (—) and remain inside `other`."
        )
    if data.steps_without_calls:
        notes.append(
            f"Note: {data.steps_without_calls} agent step(s) recorded no per-call "
            "timing (run predates it) — their agent time reads —; `wall` still "
            "spans start → end including parks."
        )
    if notes:
        lines.append("")
        lines += notes
    return "\n".join(lines) + "\n"

"""``gauntlet report`` — per-step / per-agent-profile cost breakdown (FR-3.2).

Reads a run manifest and renders two tables: usage per step and usage per agent
profile, with each profile's share of total run cost. The per-profile table is
what answers the FR-3 acceptance check — "triage/judge/retro steps individually
cost < 5% of total" — because a single ``adversarial_cycle`` step bills several
profiles, so step-level totals alone cannot attribute classification spend.

Cost is ``None`` on the degraded tokens-only path (PRD §12 Q3: subscription-auth
CLIs may not report cost); those rows are flagged as estimates rather than shown
as ``$0`` (`--trend` metrics are an FR-6.6 / P7 deliverable, not part of this).

Beyond cost, the report renders the FR-7.4 cache-effectiveness metrics from data
the manifest already records: cache-read share (``cached/(input+cached)``) and
the fresh-input tokens attributable to *cold session starts* (first-turn ingest
of steps whose profile supports resume) — per agent profile, per step type, and
run-wide. The cold-start metric isolates the unavoidable first-turn ingest from
input that a within-session continuation should serve from cache, making "does
scoped/reference context turn cold ingest into cache reads?" measurable.
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Any


def _cache_read_share(input_tokens: int, cached_input_tokens: int) -> float | None:
    """Cache-read share of input (FR-7.4): ``cached / (input + cached)``.

    ``None`` only when there was NO input at all (nothing to attribute); a profile
    with fresh input but zero cache reads is ``0.0`` (rendered ``0.0%``), never a
    blank — the acceptance distinguishes "no cache benefit" from "no usage".
    """
    denom = (input_tokens or 0) + (cached_input_tokens or 0)
    if denom <= 0:
        return None
    return 100.0 * (cached_input_tokens or 0) / denom


def _cold_start_flags(steps: Any, resume_capable: AbstractSet[str]) -> list[bool]:
    """Per-step "is this a cold session start?" flags (FR-7.4).

    A step is a cold start when its agent profile supports resume AND it is the
    FIRST step (in manifest order) to use its ``session_id`` — the first-turn
    ingest that a within-session continuation should thereafter serve from cache.
    A resume-capable step with no recorded ``session_id`` is its own cold start.
    A step on a non-resume profile is never a cold start: the metric targets
    resume-capable profiles (FR-7.4), which is what lets the cold-start total read
    differently from total input. Aligned index-for-index with ``steps``.
    """
    seen_sessions: set[str] = set()
    flags: list[bool] = []
    for rec in steps:
        cold = False
        if rec.agent is not None and rec.agent in resume_capable:
            sid = rec.session_id
            if sid is None:
                cold = True  # no continuity recorded → its own cold start
            elif sid not in seen_sessions:
                seen_sessions.add(sid)
                cold = True
        flags.append(cold)
    return flags


@dataclass
class AgentLine:
    agent: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float | None
    pct_cost: float | None  # share of total run cost, when both are priced
    cache_read_share: float | None  # cached / (input + cached), FR-7.4
    # Fresh input attributable to cold session starts (FR-7.4): first-turn ingest
    # of this profile's steps, when the profile supports resume. None for a
    # non-resume profile (the metric targets resume-capable profiles), which is
    # what lets it read differently from total input_tokens.
    cold_start_input_tokens: int | None = None


@dataclass
class StepTypeLine:
    step_type: str
    input_tokens: int
    cached_input_tokens: int
    cache_read_share: float | None  # cached / (input + cached), FR-7.4
    cold_start_input_tokens: int | None = None  # FR-7.4; None when no resume steps


@dataclass
class ReportData:
    run_id: str
    slug: str
    status: str
    total_input: int
    total_output: int
    total_cached_input: int
    total_cost: float | None
    total_cache_read_share: float | None  # FR-7.4
    total_cold_start_input: int | None = None  # FR-7.4; None when no resume steps
    agents: list[AgentLine] = field(default_factory=list)
    step_types: list[StepTypeLine] = field(default_factory=list)
    tokens_only: bool = False  # any usage lacked a cost → totals are an estimate


def build_report(
    manifest: Any, *, resume_capable: AbstractSet[str] | None = None
) -> ReportData:
    """Aggregate a run's usage into the cost + cache-effectiveness report.

    ``resume_capable`` is the set of agent-profile names whose adapter supports
    session resume (``AdapterCapabilities.resume``), resolved by the caller from
    config. It gates the FR-7.4 cold-start fresh-input metric: only resume-capable
    profiles have a "cold session start" worth measuring against warm within-
    session cache reads. Absent (``None``) → treated as empty (metric unavailable,
    rendered ``—``), so the report still builds from a manifest alone.
    """
    resume_capable = resume_capable or frozenset()
    totals = manifest.totals
    total_cost = totals.cost_usd
    # Cold session starts (FR-7.4): first-turn ingest per resume-capable profile.
    cold_flags = _cold_start_flags(manifest.steps, resume_capable)
    by_agent_cold: dict[str, int] = {}
    for rec, cold in zip(manifest.steps, cold_flags):
        if cold and rec.agent is not None:
            by_agent_cold[rec.agent] = by_agent_cold.get(rec.agent, 0) + (
                rec.usage.input_tokens or 0
            )
    agents: list[AgentLine] = []
    any_unpriced = total_cost is None
    for name in sorted(manifest.agent_usage):
        u = manifest.agent_usage[name]
        pct = None
        if u.cost_usd is not None and total_cost:
            pct = 100.0 * u.cost_usd / total_cost
        if u.cost_usd is None and (u.input_tokens or u.output_tokens):
            any_unpriced = True
        agents.append(
            AgentLine(
                agent=name,
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cached_input_tokens=u.cached_input_tokens or 0,
                cost_usd=u.cost_usd,
                pct_cost=pct,
                cache_read_share=_cache_read_share(
                    u.input_tokens or 0, u.cached_input_tokens or 0
                ),
                # A resume-capable profile always reports a number (0 when it had
                # no cold-start ingest); a non-resume profile reports None (—).
                cold_start_input_tokens=(
                    by_agent_cold.get(name, 0) if name in resume_capable else None
                ),
            )
        )
    # Per step-type cache effectiveness (FR-7.4): aggregate each step's own usage
    # by its declared type. A single adversarial_cycle bills several profiles, so
    # this is a different cut than the per-profile table above and answers "does
    # reference-mode context (FR-1) turn cold inline ingest into cache reads?"
    by_type_in: dict[str, int] = {}
    by_type_cached: dict[str, int] = {}
    by_type_cold: dict[str, int] = {}
    by_type_has_resume: dict[str, bool] = {}
    order: list[str] = []
    for rec, cold in zip(manifest.steps, cold_flags):
        u = rec.usage
        if not (u.input_tokens or u.cached_input_tokens):
            continue  # a step with no ingest contributes nothing to the ratio
        if rec.type not in by_type_in:
            order.append(rec.type)
        by_type_in[rec.type] = by_type_in.get(rec.type, 0) + (u.input_tokens or 0)
        by_type_cached[rec.type] = (
            by_type_cached.get(rec.type, 0) + (u.cached_input_tokens or 0)
        )
        if rec.agent is not None and rec.agent in resume_capable:
            by_type_has_resume[rec.type] = True
            if cold:
                by_type_cold[rec.type] = by_type_cold.get(rec.type, 0) + (
                    u.input_tokens or 0
                )
    step_types = [
        StepTypeLine(
            step_type=t,
            input_tokens=by_type_in[t],
            cached_input_tokens=by_type_cached[t],
            cache_read_share=_cache_read_share(by_type_in[t], by_type_cached[t]),
            cold_start_input_tokens=(
                by_type_cold.get(t, 0) if by_type_has_resume.get(t) else None
            ),
        )
        for t in order
    ]
    # Run-level cold-start total: only meaningful when at least one resume-capable
    # step ran (else None → —, distinct from a real 0).
    any_resume_step = any(
        rec.agent is not None and rec.agent in resume_capable
        for rec in manifest.steps
    )
    total_cold = (
        sum(
            (rec.usage.input_tokens or 0)
            for rec, cold in zip(manifest.steps, cold_flags)
            if cold
        )
        if any_resume_step
        else None
    )
    return ReportData(
        run_id=manifest.run_id,
        slug=manifest.slug,
        status=manifest.status,
        total_input=totals.input_tokens or 0,
        total_output=totals.output_tokens or 0,
        total_cached_input=totals.cached_input_tokens or 0,
        total_cost=total_cost,
        total_cache_read_share=_cache_read_share(
            totals.input_tokens or 0, totals.cached_input_tokens or 0
        ),
        total_cold_start_input=total_cold,
        agents=agents,
        step_types=step_types,
        tokens_only=any_unpriced,
    )


def _cost_cell(cost: float | None) -> str:
    return f"${cost:.4f}" if cost is not None else "— (tokens only)"


def _pct_cell(pct: float | None) -> str:
    return f"{pct:.1f}%" if pct is not None else "—"


def _cache_cell(share: float | None) -> str:
    # FR-7.4: a real 0% (fresh input, no cache read) is shown as `0.0%`, never a
    # blank; `—` means there was no input to attribute.
    return f"{share:.1f}%" if share is not None else "—"


def _cold_cell(tokens: int | None) -> str:
    # FR-7.4 cold-start fresh input: a resume-capable cut shows the count (0 when
    # nothing cold-started); `—` means the cut has no resume-capable step, so the
    # metric does not apply — distinct from a real 0.
    return str(tokens) if tokens is not None else "—"


def render_report(
    manifest: Any, *, resume_capable: AbstractSet[str] | None = None
) -> str:
    """Render the cost report as plain text for the CLI.

    ``resume_capable`` (see :func:`build_report`) gates the FR-7.4 cold-start
    column; when omitted every cut shows ``—`` for it.
    """
    data = build_report(manifest, resume_capable=resume_capable)
    lines = [
        f"Cost report — run {data.run_id} ({data.slug}) [{data.status}]",
        "",
        "Per agent profile:",
        f"  {'agent':<16} {'in':>10} {'out':>10} {'cached':>10} "
        f"{'cache%':>8} {'cold-in':>10} {'cost':>16} {'% cost':>8}",
    ]
    for a in data.agents:
        lines.append(
            f"  {a.agent:<16} {a.input_tokens:>10} {a.output_tokens:>10} "
            f"{a.cached_input_tokens:>10} {_cache_cell(a.cache_read_share):>8} "
            f"{_cold_cell(a.cold_start_input_tokens):>10} "
            f"{_cost_cell(a.cost_usd):>16} {_pct_cell(a.pct_cost):>8}"
        )
    if not data.agents:
        lines.append("  (no per-agent usage recorded)")
    # Per step-type cache effectiveness + cold-start fresh input (FR-7.4).
    lines += [
        "",
        "Cache read share per step type + cold-start fresh input (FR-7.4):",
        f"  {'step type':<20} {'in':>10} {'cached':>10} {'cache%':>8} {'cold-in':>10}",
    ]
    for s in data.step_types:
        lines.append(
            f"  {s.step_type:<20} {s.input_tokens:>10} "
            f"{s.cached_input_tokens:>10} {_cache_cell(s.cache_read_share):>8} "
            f"{_cold_cell(s.cold_start_input_tokens):>10}"
        )
    if not data.step_types:
        lines.append("  (no per-step ingest recorded)")
    lines += [
        "",
        "Per step:",
        f"  {'step':<22} {'type':<18} {'agent':<12} {'in':>9} {'out':>9} {'cost':>16}",
    ]
    for rec in manifest.steps:
        leaf = rec.id if rec.iteration is None else f"{rec.id}.{rec.iteration}"
        u = rec.usage
        lines.append(
            f"  {leaf:<22} {rec.type:<18} {(rec.agent or '—'):<12} "
            f"{(u.input_tokens or 0):>9} {(u.output_tokens or 0):>9} "
            f"{_cost_cell(u.cost_usd):>16}"
        )
    lines += [
        "",
        f"Totals: {data.total_input} in / {data.total_output} out / "
        f"{data.total_cached_input} cached "
        f"({_cache_cell(data.total_cache_read_share)} cache read) / "
        f"{_cold_cell(data.total_cold_start_input)} cold-start in / "
        f"{_cost_cell(data.total_cost)}",
    ]
    if data.tokens_only:
        lines.append(
            "Note: some calls reported tokens only (no cost); cost figures are "
            "estimates / partial (PRD §12 Q3)."
        )
    return "\n".join(lines) + "\n"

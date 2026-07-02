"""``gauntlet report`` — per-step / per-agent-profile cost breakdown (FR-3.2).

Reads a run manifest and renders two tables: usage per step and usage per agent
profile, with each profile's share of total run cost. The per-profile table is
what answers the FR-3 acceptance check — "triage/judge/retro steps individually
cost < 5% of total" — because a single ``adversarial_cycle`` step bills several
profiles, so step-level totals alone cannot attribute classification spend.

Cost is ``None`` on the degraded tokens-only path (PRD §12 Q3: subscription-auth
CLIs may not report cost); those rows are flagged as estimates rather than shown
as ``$0`` (`--trend` metrics are an FR-6.6 / P7 deliverable, not part of this).
"""

from __future__ import annotations

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


@dataclass
class AgentLine:
    agent: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    cost_usd: float | None
    pct_cost: float | None  # share of total run cost, when both are priced
    cache_read_share: float | None  # cached / (input + cached), FR-7.4


@dataclass
class StepTypeLine:
    step_type: str
    input_tokens: int
    cached_input_tokens: int
    cache_read_share: float | None  # cached / (input + cached), FR-7.4


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
    agents: list[AgentLine] = field(default_factory=list)
    step_types: list[StepTypeLine] = field(default_factory=list)
    tokens_only: bool = False  # any usage lacked a cost → totals are an estimate


def build_report(manifest: Any) -> ReportData:
    totals = manifest.totals
    total_cost = totals.cost_usd
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
            )
        )
    # Per step-type cache effectiveness (FR-7.4): aggregate each step's own usage
    # by its declared type. A single adversarial_cycle bills several profiles, so
    # this is a different cut than the per-profile table above and answers "does
    # reference-mode context (FR-1) turn cold inline ingest into cache reads?"
    by_type_in: dict[str, int] = {}
    by_type_cached: dict[str, int] = {}
    order: list[str] = []
    for rec in manifest.steps:
        u = rec.usage
        if not (u.input_tokens or u.cached_input_tokens):
            continue  # a step with no ingest contributes nothing to the ratio
        if rec.type not in by_type_in:
            order.append(rec.type)
        by_type_in[rec.type] = by_type_in.get(rec.type, 0) + (u.input_tokens or 0)
        by_type_cached[rec.type] = (
            by_type_cached.get(rec.type, 0) + (u.cached_input_tokens or 0)
        )
    step_types = [
        StepTypeLine(
            step_type=t,
            input_tokens=by_type_in[t],
            cached_input_tokens=by_type_cached[t],
            cache_read_share=_cache_read_share(by_type_in[t], by_type_cached[t]),
        )
        for t in order
    ]
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


def render_report(manifest: Any) -> str:
    """Render the cost report as plain text for the CLI."""
    data = build_report(manifest)
    lines = [
        f"Cost report — run {data.run_id} ({data.slug}) [{data.status}]",
        "",
        "Per agent profile:",
        f"  {'agent':<16} {'in':>10} {'out':>10} {'cached':>10} "
        f"{'cache%':>8} {'cost':>16} {'% cost':>8}",
    ]
    for a in data.agents:
        lines.append(
            f"  {a.agent:<16} {a.input_tokens:>10} {a.output_tokens:>10} "
            f"{a.cached_input_tokens:>10} {_cache_cell(a.cache_read_share):>8} "
            f"{_cost_cell(a.cost_usd):>16} {_pct_cell(a.pct_cost):>8}"
        )
    if not data.agents:
        lines.append("  (no per-agent usage recorded)")
    # Per step-type cache effectiveness (FR-7.4).
    lines += [
        "",
        "Cache read share per step type (FR-7.4):",
        f"  {'step type':<20} {'in':>10} {'cached':>10} {'cache%':>8}",
    ]
    for s in data.step_types:
        lines.append(
            f"  {s.step_type:<20} {s.input_tokens:>10} "
            f"{s.cached_input_tokens:>10} {_cache_cell(s.cache_read_share):>8}"
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
        f"{_cost_cell(data.total_cost)}",
    ]
    if data.tokens_only:
        lines.append(
            "Note: some calls reported tokens only (no cost); cost figures are "
            "estimates / partial (PRD §12 Q3)."
        )
    return "\n".join(lines) + "\n"

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


@dataclass
class AgentLine:
    agent: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    pct_cost: float | None  # share of total run cost, when both are priced


@dataclass
class ReportData:
    run_id: str
    slug: str
    status: str
    total_input: int
    total_output: int
    total_cost: float | None
    agents: list[AgentLine] = field(default_factory=list)
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
                cost_usd=u.cost_usd,
                pct_cost=pct,
            )
        )
    return ReportData(
        run_id=manifest.run_id,
        slug=manifest.slug,
        status=manifest.status,
        total_input=totals.input_tokens or 0,
        total_output=totals.output_tokens or 0,
        total_cost=total_cost,
        agents=agents,
        tokens_only=any_unpriced,
    )


def _cost_cell(cost: float | None) -> str:
    return f"${cost:.4f}" if cost is not None else "— (tokens only)"


def _pct_cell(pct: float | None) -> str:
    return f"{pct:.1f}%" if pct is not None else "—"


def render_report(manifest: Any) -> str:
    """Render the cost report as plain text for the CLI."""
    data = build_report(manifest)
    lines = [
        f"Cost report — run {data.run_id} ({data.slug}) [{data.status}]",
        "",
        "Per agent profile:",
        f"  {'agent':<16} {'in':>10} {'out':>10} {'cost':>16} {'% cost':>8}",
    ]
    for a in data.agents:
        lines.append(
            f"  {a.agent:<16} {a.input_tokens:>10} {a.output_tokens:>10} "
            f"{_cost_cell(a.cost_usd):>16} {_pct_cell(a.pct_cost):>8}"
        )
    if not data.agents:
        lines.append("  (no per-agent usage recorded)")
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
        f"{_cost_cell(data.total_cost)}",
    ]
    if data.tokens_only:
        lines.append(
            "Note: some calls reported tokens only (no cost); cost figures are "
            "estimates / partial (PRD §12 Q3)."
        )
    return "\n".join(lines) + "\n"

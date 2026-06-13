"""``gauntlet report --trend`` — cross-run improvement metrics (FR-6.6).

The trend lines that tell you whether prompt/policy changes are actually
helping: findings per review round, % findings triaged legitimate, % accepted
fixes that survive the confirm pass, test-failure loops per phase, judge
ask-rate, and cost per phase — one row per run, oldest first.

Everything except the judge ask-rate is computed from the manifest alone (the
adversarial_cycle persists its per-round tallies into ``StepRecord.metrics``),
so the math is testable against fixture manifests (the plan's P7 test strategy).
The ask-rate reads ``judge-audit.jsonl`` when a run dir is available.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PHASE_RE = re.compile(r"^P\d+$")


@dataclass
class TrendMetrics:
    run_id: str
    slug: str
    status: str
    rounds: int
    findings_total: int
    findings_per_round: float | None
    pct_legitimate: float | None
    fix_survival: float | None
    test_failure_loops: int
    phases: int
    cost_per_phase: float | None
    judge_ask_rate: float | None
    total_cost: float | None


def build_run_trend(manifest: Any, *, judge_audit_path: Path | None = None) -> TrendMetrics:
    rounds = 0
    findings_total = 0
    verdict_counts: dict[str, int] = {}
    confirm_counts: dict[str, int] = {}
    for rec in manifest.steps:
        m = rec.metrics or {}
        if not m:
            continue
        rounds += int(m.get("rounds", 0) or 0)
        findings_total += int(m.get("findings_total", 0) or 0)
        for k, v in (m.get("verdict_counts") or {}).items():
            verdict_counts[k] = verdict_counts.get(k, 0) + int(v)
        for k, v in (m.get("confirm_counts") or {}).items():
            confirm_counts[k] = confirm_counts.get(k, 0) + int(v)

    total_verdicts = sum(verdict_counts.values())
    pct_legitimate = (
        100.0 * verdict_counts.get("legitimate", 0) / total_verdicts
        if total_verdicts else None
    )
    total_confirm = sum(confirm_counts.values())
    fix_survival = (
        100.0 * confirm_counts.get("resolved", 0) / total_confirm
        if total_confirm else None
    )
    findings_per_round = findings_total / rounds if rounds else None

    test_failure_loops = sum(
        max((rec.attempts or 0) - 1, 0)
        for rec in manifest.steps
        if rec.type == "shell" and (rec.attempts or 0) > 1
    )

    phases = _count_phases(manifest)
    total_cost = manifest.totals.cost_usd
    cost_per_phase = (total_cost / phases) if (total_cost is not None and phases) else None

    return TrendMetrics(
        run_id=manifest.run_id,
        slug=manifest.slug,
        status=manifest.status,
        rounds=rounds,
        findings_total=findings_total,
        findings_per_round=findings_per_round,
        pct_legitimate=pct_legitimate,
        fix_survival=fix_survival,
        test_failure_loops=test_failure_loops,
        phases=phases,
        cost_per_phase=cost_per_phase,
        judge_ask_rate=judge_ask_rate(judge_audit_path),
        total_cost=total_cost,
    )


def _count_phases(manifest: Any) -> int:
    """Distinct numbered phases (P1, P2, …) the run committed; fall back to any
    distinct top-level phase prefix (PRD/PLAN runs have no numbered phases)."""
    numbered = {c.phase.split(".")[0] for c in manifest.commits if _PHASE_RE.match(c.phase.split(".")[0])}
    if numbered:
        return len(numbered)
    return len({c.phase.split(".")[0] for c in manifest.commits})


def judge_ask_rate(audit_path: Path | None) -> float | None:
    """Fraction of judge decisions resolved on the LLM (ask→classify) rung.

    The deterministic fast path is the cheap, desirable case; a high ask-rate is
    the signal FR-6.3 acts on ("asked 14 times, always allowed → propose a
    fast-path rule"). ``None`` when no audit log is available.
    """
    if audit_path is None or not audit_path.exists():
        return None
    total = 0
    asks = 0
    for line in audit_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        source = entry.get("source") or entry.get("rung")
        decision = entry.get("decision")
        if source == "llm" or decision == "ask":
            asks += 1
    return (100.0 * asks / total) if total else None


# --- rendering ---------------------------------------------------------------
def _pct(v: float | None) -> str:
    return f"{v:.1f}%" if v is not None else "—"


def _num(v: float | None, fmt: str = "{:.2f}") -> str:
    return fmt.format(v) if v is not None else "—"


def _cost(v: float | None) -> str:
    return f"${v:.4f}" if v is not None else "—"


def render_trend(rows: list[TrendMetrics]) -> str:
    lines = [
        "Improvement trend (FR-6.6) — one row per run, oldest first",
        "",
        f"  {'run':<26} {'find/rnd':>9} {'%legit':>7} {'fix-surv':>9} "
        f"{'test-loops':>11} {'ask-rate':>9} {'cost/phase':>11}",
    ]
    for r in rows:
        lines.append(
            f"  {r.run_id:<26} {_num(r.findings_per_round):>9} "
            f"{_pct(r.pct_legitimate):>7} {_pct(r.fix_survival):>9} "
            f"{r.test_failure_loops:>11} {_pct(r.judge_ask_rate):>9} "
            f"{_cost(r.cost_per_phase):>11}"
        )
    if not rows:
        lines.append("  (no runs with recorded metrics)")
    return "\n".join(lines) + "\n"

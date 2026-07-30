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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterator

import pydantic

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
    total_duration: float | None  # summed wall-clock seconds of timed steps
    duration_per_phase: float | None  # seconds


def build_run_trend(manifest: Any, *, judge_audit_path: Path | None = None) -> TrendMetrics:
    rounds = 0
    findings_total = 0
    accepted_total = 0
    accepted_resolved_total = 0
    verdict_counts: dict[str, int] = {}
    for rec in manifest.steps:
        m = rec.metrics or {}
        if not m:
            continue
        rounds += int(m.get("rounds", 0) or 0)
        findings_total += int(m.get("findings_total", 0) or 0)
        accepted_total += int(m.get("accepted_total", 0) or 0)
        accepted_resolved_total += int(m.get("accepted_resolved_total", 0) or 0)
        for k, v in (m.get("verdict_counts") or {}).items():
            verdict_counts[k] = verdict_counts.get(k, 0) + int(v)

    total_verdicts = sum(verdict_counts.values())
    pct_legitimate = (
        100.0 * verdict_counts.get("legitimate", 0) / total_verdicts
        if total_verdicts else None
    )
    # FR-6.6: "% accepted fixes that survive the confirm pass" — resolved
    # ACCEPTED fixes over accepted fixes, NOT over all confirm verdicts. Declined
    # findings carry an expected `unresolved` confirm verdict and must not depress
    # the metric (F-004).
    fix_survival = (
        100.0 * accepted_resolved_total / accepted_total
        if accepted_total else None
    )
    findings_per_round = findings_total / rounds if rounds else None

    # `attempts` IS the failure counter now (FR-6): a tests step that failed
    # twice then passed has attempts == 2 → 2 failed loops, and a single
    # fail-then-pass has attempts == 1 → 1 loop. (The old `attempts - 1` undercounted
    # by one and dropped single-failure runs entirely — review F-004.)
    test_failure_loops = sum(
        rec.attempts or 0
        for rec in manifest.steps
        if rec.type == "shell"
    )

    phases = _count_phases(manifest)
    total_cost = manifest.totals.cost_usd
    cost_per_phase = (total_cost / phases) if (total_cost is not None and phases) else None

    # FR-5.3 wants a per-phase DURATION distribution alongside cost, so the plan
    # author can size phases against a window's wall-clock budget, not just its
    # dollar budget. Sum the timed steps' wall-clock and divide by phase count.
    step_durations = [d for d in (_step_duration_seconds(r) for r in manifest.steps) if d is not None]
    total_duration = sum(step_durations) if step_durations else None
    duration_per_phase = (total_duration / phases) if (total_duration is not None and phases) else None

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
        total_duration=total_duration,
        duration_per_phase=duration_per_phase,
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


# --- trend-informed plan authoring (FR-5.3, P7) ------------------------------
# The plan-author sizes phases; without measured history it sizes blind (#54
# cause 4: oversized phases hide partial delivery). These helpers surface the
# repo's completed-run cost/duration history + the size bound to the plan-author
# input as ADVISORY data — the plan stays human-ratified; nothing auto-tunes
# (§2.2). All math is from the manifest, so it is testable against fixtures.


@dataclass
class StepTypeStats:
    """Aggregated cost/duration for one step type across completed runs."""

    step_type: str
    n_steps: int = 0
    costs: list[float] = field(default_factory=list)  # priced steps only
    durations: list[float] = field(default_factory=list)  # seconds, timed steps

    @property
    def mean_cost(self) -> float | None:
        return sum(self.costs) / len(self.costs) if self.costs else None

    @property
    def median_cost(self) -> float | None:
        return median(self.costs) if self.costs else None

    @property
    def mean_duration(self) -> float | None:
        return sum(self.durations) / len(self.durations) if self.durations else None

    @property
    def median_duration(self) -> float | None:
        return median(self.durations) if self.durations else None


def _step_duration_seconds(rec: Any) -> float | None:
    """Wall-clock seconds for a step, or ``None`` when its timestamps are absent
    or unparseable (a step killed mid-run may lack ``ended``). A negative delta
    (clock skew / hand-edited manifest) is discarded rather than trusted."""
    started, ended = getattr(rec, "started", None), getattr(rec, "ended", None)
    if not started or not ended:
        return None
    try:
        delta = (datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds()
    except ValueError:
        return None
    return delta if delta >= 0 else None


def iter_completed_manifests(run_root: Path) -> Iterator[Any]:
    """Yield every completed (``status == done``) run manifest under ``run_root``.

    Deterministic order (slug dir, then run-instance dir, both sorted). A
    manifest that is missing, unreadable, or not yet done is skipped — history
    is advisory, so a corrupt sibling run never fails the render (it just
    contributes nothing)."""
    from gauntlet.engine.manifest import RUN_DONE, Manifest

    if not run_root.exists():
        return
    for slug_dir in sorted(p for p in run_root.iterdir() if p.is_dir()):
        for run_dir in sorted(slug_dir.glob("run-*")):
            man_path = run_dir / "manifest.json"
            if not man_path.exists():
                continue
            try:
                man = Manifest.load(man_path)
            except (OSError, ValueError, pydantic.ValidationError):
                # OSError: unreadable file. ValueError/ValidationError: unparseable
                # JSON or JSON that is syntactically valid but violates the manifest
                # schema. pydantic.ValidationError is listed explicitly rather than
                # relying on its incidental ValueError base — a single bad historical
                # run must never fail the render.
                continue
            if man.status == RUN_DONE:
                yield man


def collect_step_type_stats(manifests: list[Any]) -> list[StepTypeStats]:
    """Per-step-type cost/duration distributions across the given manifests,
    sorted by step type for a stable rendered block."""
    by_type: dict[str, StepTypeStats] = {}
    for man in manifests:
        for rec in man.steps:
            stats = by_type.setdefault(rec.type, StepTypeStats(step_type=rec.type))
            stats.n_steps += 1
            cost = getattr(rec.usage, "cost_usd", None) if getattr(rec, "usage", None) else None
            if cost is not None:
                stats.costs.append(cost)
            dur = _step_duration_seconds(rec)
            if dur is not None:
                stats.durations.append(dur)
    return [by_type[k] for k in sorted(by_type)]


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    total = int(round(seconds))
    m, s = divmod(total, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s"


def _plain(v: float) -> str:
    """A float rendered without a trailing ``.0`` and never in scientific
    notation (``:g`` would print 1000000 as ``1e+06``)."""
    return str(int(v)) if float(v).is_integer() else str(v)


def _render_window_budgets(providers: dict[str, Any]) -> list[str]:
    """A window-budget line per configured provider (harness-efficiency FR-10);
    empty when no provider window is configured."""
    lines: list[str] = []
    for name in sorted(providers):
        w = providers[name]
        lines.append(
            f"  {name}: {_plain(w.window_budget)} {w.budget_unit} per "
            f"{_plain(w.window_hours)}h window"
        )
    return lines


def render_plan_author_history(
    run_root: Path,
    *,
    max_frs_per_phase: int,
    providers: dict[str, Any] | None = None,
) -> str:
    """The measured-history block appended to the plan-author input (FR-5.3).

    Always returns a block: a stats block when ≥ 1 completed run exists in the
    repo, else the explicit "no history" block (never silence — P7-A2). The
    ``max_frs_per_phase`` size bound is always stated (P7-A3), as is the
    per-provider window budget where a harness-efficiency FR-10 config exists.
    """
    header = "--- measured phase history for sizing (FR-5.3) ---"
    bound = (
        f"Size bound: max_frs_per_phase = {max_frs_per_phase}. A phase carrying "
        f"more than this many distinct FR references trips the phase-size lint — "
        f"oversized phases are where partial delivery hides (#54). Keep each phase "
        f"at or under the bound. Declare each phase's scope in its `frs:` list; "
        f"the lint counts those declared refs."
    )
    parts = [f"\n\n{header}\n{bound}\n"]

    window_lines = _render_window_budgets(providers or {})
    if window_lines:
        parts.append(
            "\nProvider window budget (size the whole run to fit within it):\n"
            + "\n".join(window_lines)
            + "\n"
        )

    manifests = list(iter_completed_manifests(run_root))
    if not manifests:
        parts.append(
            "\nNo completed run history is available in this repo yet — there are "
            "no measured per-phase costs to size against. Size phases "
            "conservatively, keep them at or under the size bound above, and lean "
            "on the PRD's own risk ordering; the next completed run seeds this "
            "block for future plans.\n"
        )
        return "".join(parts)

    stats = collect_step_type_stats(manifests)
    rows = sorted(
        (build_run_trend(m) for m in manifests), key=lambda r: r.run_id
    )
    n = len(manifests)
    parts.append(
        f"\nMeasured per-step-type cost/duration across {n} completed run(s) in "
        "this repo — ground phase sizing in these observed costs, not guesswork:\n\n"
        f"  {'step type':<20} {'n':>4} {'mean $':>9} {'median $':>9} "
        f"{'mean dur':>9} {'med dur':>9}\n"
    )
    for s in stats:
        parts.append(
            f"  {s.step_type:<20} {s.n_steps:>4} {_cost(s.mean_cost):>9} "
            f"{_cost(s.median_cost):>9} {_dur(s.mean_duration):>9} "
            f"{_dur(s.median_duration):>9}\n"
        )
    parts.append("\nPer-run cost/duration per phase (oldest first):\n")
    for r in rows:
        phases = f"{r.phases} phase(s)" if r.phases else "no numbered phases"
        parts.append(
            f"  {r.run_id:<26} {_cost(r.cost_per_phase)}/phase   "
            f"{_dur(r.duration_per_phase)}/phase   "
            f"({phases}, {_cost(r.total_cost)} / {_dur(r.total_duration)} total)\n"
        )
    return "".join(parts)

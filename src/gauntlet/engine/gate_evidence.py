"""Gate evidence shared by the driver notifier and the web console (#134, rec 10).

A gate notification should carry enough for the operator to decide from their
phone: the shape of the reviewed range (``git diff --stat``), the finding and
triage counts the gate shows, and the run's spend and elapsed time so far. This
module assembles that summary read-only from the run's persisted state — the
manifest, the pipeline snapshot, the ``show:`` artifacts — plus one read-only
git call over the object database (any tree of the repository answers
identically; nothing here touches a working tree).

It lives in ``engine/`` so the console (``web/gate.py``) can reuse the same
``show:``-list and artifact resolution without the engine importing ``web/``.
Every part is fail-soft: a missing artifact, an unparseable snapshot or an
orphaned SHA yields ``None`` for that part, never an exception — the short
notification always goes out.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gauntlet.engine import gitops
from gauntlet.engine.manifest import Manifest, StepRecord
from gauntlet.engine.pipeline import load_pipeline

logger = logging.getLogger(__name__)

FINDINGS_ARTIFACT = "findings.json"
TRIAGE_ARTIFACT = "triage.json"

SEVERITY_ORDER = ("blocking", "major", "minor", "nit")


def gate_show_list(run_dir: Path, step_id: str) -> list[str]:
    """The gate step's ``show:`` list, read from the run's snapshot pipeline.

    Reads the run's *own* ``pipeline.yaml`` (FR-4.2) so the gate is resolved
    against the pipeline the run actually committed to, not the repo's current
    one. A missing/unparseable snapshot yields an empty list.
    """
    try:
        pipeline, _ = load_pipeline(Path(run_dir) / "pipeline.yaml")
    except (FileNotFoundError, ValueError):
        return []
    for step in pipeline.all_steps():
        if step.id == step_id:
            return list(step.get("show", []) or [])
    return []


def _contained(path: Path, root: Path) -> Path | None:
    """``path`` resolved, iff it stays under ``root`` (FR-10.1 posture)."""
    try:
        root_r = Path(root).resolve()
        target = Path(path).resolve()
        target.relative_to(root_r)
    except (OSError, RuntimeError, ValueError):
        return None
    return target


def resolve_gate_artifact(
    run_dir: Path, slug_dir: Path, name: str
) -> tuple[Path | None, str | None]:
    """``show:`` name → (path, source): first ``<run_dir>/artifacts/``, then the
    slug dir. A name that is not a single safe segment, or resolves nowhere,
    yields ``(None, None)``."""
    if (
        not name
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or "\x00" in name
    ):
        return None, None
    cand = _contained(Path(run_dir) / "artifacts" / name, Path(run_dir))
    if cand is not None and cand.is_file():
        return cand, "artifacts"
    slug_cand = _contained(Path(slug_dir) / name, Path(slug_dir))
    if slug_cand is not None and slug_cand.is_file():
        return slug_cand, "slug"
    return None, None


def _read_json(path: Path | None) -> Any:
    if path is None:
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def findings_counts(findings: Any) -> dict[str, Any] | None:
    """``{total, by_severity}`` from a parsed ``findings.json`` (or ``None``)."""
    if not isinstance(findings, dict):
        return None
    items = [f for f in (findings.get("findings") or []) if isinstance(f, dict)]
    by: dict[str, int] = {}
    for f in items:
        sev = str(f.get("severity") or "unknown")
        by[sev] = by.get(sev, 0) + 1
    ordered = {s: by[s] for s in SEVERITY_ORDER if s in by}
    ordered.update({s: n for s, n in sorted(by.items()) if s not in ordered})
    return {"total": len(items), "by_severity": ordered}


def triage_counts(triage: Any) -> dict[str, Any] | None:
    """``{total, by_verdict, by_action}`` from a parsed ``triage.json``."""
    if not isinstance(triage, dict):
        return None
    items = [v for v in (triage.get("verdicts") or []) if isinstance(v, dict)]
    by_verdict: dict[str, int] = {}
    by_action: dict[str, int] = {}
    for v in items:
        verdict = str(v.get("verdict") or "unknown")
        action = str(v.get("action") or "unknown")
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
        by_action[action] = by_action.get(action, 0) + 1
    return {
        "total": len(items),
        "by_verdict": dict(sorted(by_verdict.items())),
        "by_action": dict(sorted(by_action.items())),
    }


def gate_findings_summary(
    run_dir: Path, slug_dir: Path, show: list[str]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """The (findings, triage) count blocks for the artifacts the gate shows —
    ``None`` for each the gate does not list or that is absent/unparseable."""
    findings = triage = None
    if FINDINGS_ARTIFACT in show:
        path, _ = resolve_gate_artifact(run_dir, slug_dir, FINDINGS_ARTIFACT)
        findings = findings_counts(_read_json(path))
    if TRIAGE_ARTIFACT in show:
        path, _ = resolve_gate_artifact(run_dir, slug_dir, TRIAGE_ARTIFACT)
        triage = triage_counts(_read_json(path))
    return findings, triage


def reviewed_range(
    repo: Path, man: Manifest, gate_rec: StepRecord, *, run_dir: Path | None = None
) -> tuple[str, str] | None:
    """``(base_sha, head_sha)`` of the range the gate ratifies, or ``None``.

    ``base`` is the upstream cycle's persisted ``StepRecord.base_sha`` (the
    transaction boundary stamped when the cycle entered), falling back to the
    nearest earlier step that recorded one, then to ``merge-base(base_branch,
    run branch)``. ``head`` is the run branch's tip — resolved from the shared
    refs, so the operator's checkout is never consulted or moved.
    """
    from gauntlet.engine import operator  # lazy: operator imports run (cycle guard)

    try:
        head = gitops.rev_parse(repo, man.branch).strip()
    except gitops.GitError:
        return None
    if not head:
        return None
    pipeline = None
    if run_dir is not None:
        try:
            pipeline, _ = load_pipeline(Path(run_dir) / "pipeline.yaml")
        except (FileNotFoundError, ValueError):
            pipeline = None
    base: str | None = None
    try:
        cycle_rec = operator._resolve_upstream_cycle(man, gate_rec, pipeline)
    except Exception:  # fail-soft: an odd pipeline shape must not sink the summary
        cycle_rec = None
    if cycle_rec is not None and cycle_rec.base_sha:
        base = cycle_rec.base_sha
    if base is None:
        # Nearest earlier step (manifest order) that stamped a boundary.
        idx = next(
            (i for i, r in enumerate(man.steps) if r is gate_rec), len(man.steps)
        )
        for rec in reversed(man.steps[:idx]):
            if rec.base_sha:
                base = rec.base_sha
                break
    if base is None:
        base = gitops.merge_base(repo, man.base_branch, head)
    if not base:
        return None
    return base, head


def diff_stat(repo: Path, base: str, head: str) -> str | None:
    """``git diff --stat base..head`` text, or ``None`` when git refuses (an
    orphaned SHA after a rewind, an unrelated history)."""
    try:
        return gitops.diff_stat_range(repo, base, head)
    except gitops.GitError:
        return None


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def run_elapsed_s(man: Manifest, now: datetime | None = None) -> float | None:
    """Wall-clock seconds from the earliest step start to the latest step end
    (or now while running); ``None`` if no step has started."""
    now = now or datetime.now(timezone.utc)
    starts = [t for t in (_parse_iso(s.started) for s in man.steps) if t is not None]
    if not starts:
        return None
    ends = [t for t in (_parse_iso(s.ended) for s in man.steps) if t is not None]
    end = max(ends) if ends else now
    if man.status == "running":
        end = now
    return max(0.0, (end - min(starts)).total_seconds())


def usage_block(man: Manifest) -> dict[str, Any]:
    u = man.totals
    return {
        "input_tokens": u.input_tokens or 0,
        "output_tokens": u.output_tokens or 0,
        "cached_input_tokens": u.cached_input_tokens or 0,
        "cost_usd": u.cost_usd,
    }


def gate_summary(
    man: Manifest,
    *,
    run_dir: Path,
    slug_dir: Path,
    repo: Path,
    gate_rec: StepRecord | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The rich gate-reached summary (#134, rec 10), every part fail-soft:

    ``{range: {base, head} | None, diff_stat: str | None, findings: {...} |
    None, triage: {...} | None, usage: {...}, elapsed_s: float | None,
    gate: <step id>}``.
    """
    from gauntlet.engine.notify import current_record

    rec = gate_rec or current_record(man)
    summary: dict[str, Any] = {
        "gate": rec.id if rec is not None else None,
        "range": None,
        "diff_stat": None,
        "findings": None,
        "triage": None,
        "usage": usage_block(man),
        "elapsed_s": run_elapsed_s(man, now),
    }
    if rec is None:
        return summary
    try:
        rng = reviewed_range(repo, man, rec, run_dir=run_dir)
    except Exception:
        logger.warning("gate summary: reviewed range failed", exc_info=True)
        rng = None
    if rng is not None:
        base, head = rng
        summary["range"] = {"base": base, "head": head}
        summary["diff_stat"] = diff_stat(repo, base, head)
    try:
        show = gate_show_list(run_dir, rec.id)
        summary["findings"], summary["triage"] = gate_findings_summary(
            run_dir, slug_dir, show
        )
    except Exception:
        logger.warning("gate summary: findings counts failed", exc_info=True)
    return summary


__all__ = [
    "gate_show_list",
    "resolve_gate_artifact",
    "findings_counts",
    "triage_counts",
    "gate_findings_summary",
    "reviewed_range",
    "diff_stat",
    "run_elapsed_s",
    "usage_block",
    "gate_summary",
]

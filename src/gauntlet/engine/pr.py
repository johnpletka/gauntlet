"""PR description drafting at final-gate pass (FR-9.8).

When a run reaches DONE, Gauntlet *drafts* (never opens) a PR description into
``runs/<slug>/PR.md``: the PRD summary, the per-phase commit list including fix
rounds, links to the run transcripts, and the final per-finding verdicts from
the last confirm pass. Opening the PR and pushing stay human actions in v1
(PRD §2.2 non-goals); this is a committable artifact a human reviews, edits, and
uses — not an automated GitHub action.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _prd_summary(prd_text: str) -> str:
    """First heading + first prose paragraph of the PRD, for the PR preamble."""
    lines = prd_text.splitlines()
    title = next((ln for ln in lines if ln.startswith("#")), "").lstrip("# ").strip()
    para: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s or s.startswith("#") or s.startswith(">") or s.startswith("---"):
            if para:
                break
            continue
        para.append(s)
        if len(" ".join(para)) > 400:
            break
    summary = " ".join(para)
    if title and summary:
        return f"**{title}** — {summary}"
    return title or summary or "_(PRD summary unavailable)_"


def _commits_by_phase(manifest: Any) -> list[tuple[str, list[Any]]]:
    """Group commit records by their phase (P1, PRD, …), preserving order."""
    groups: dict[str, list[Any]] = {}
    order: list[str] = []
    for c in manifest.commits:
        head = c.phase.split(".")[0] if c.phase else "(unlabelled)"
        if head not in groups:
            groups[head] = []
            order.append(head)
        groups[head].append(c)
    return [(h, groups[h]) for h in order]


def _final_verdicts(run_dir: Path) -> list[dict[str, Any]]:
    confirm_path = run_dir / "artifacts" / "confirm.json"
    if not confirm_path.exists():
        return []
    try:
        data = json.loads(confirm_path.read_text())
    except (ValueError, OSError):
        return []
    return data.get("verdicts") or []


def render_pr(
    manifest: Any, *, prd_text: str, plan_text: str, run_dir: Path
) -> str:
    lines = [
        f"# PR draft — `{manifest.slug}`",
        "",
        "> Drafted by Gauntlet at the final gate (FR-9.8). **Not opened, not "
        "pushed** — opening the PR and pushing remain human actions (PRD §2.2). "
        "Edit freely before use.",
        "",
        f"- branch: `{manifest.branch}` (base `{manifest.base_branch}`)",
        f"- run: `{manifest.run_id}` — status **{manifest.status}**",
        f"- pipeline: `{manifest.pipeline.name}` v{manifest.pipeline.version}",
        "",
        "## Summary",
        "",
        _prd_summary(prd_text),
        "",
        "## Phases & commits",
        "",
    ]
    groups = _commits_by_phase(manifest)
    if groups:
        for phase, commits in groups:
            lines.append(f"### {phase}")
            for c in commits:
                lines.append(f"- `{c.sha[:10]}` **{c.phase}** (step `{c.step_id}`)")
            lines.append("")
    else:
        lines += ["_(no commits recorded)_", ""]

    verdicts = _final_verdicts(run_dir)
    lines += ["## Final per-finding verdicts (last confirm pass)", ""]
    if verdicts:
        for v in verdicts:
            lines.append(
                f"- `{v.get('finding_id', '?')}`: **{v.get('verdict', '?')}** — "
                f"{v.get('notes', '')}".rstrip(" —")
            )
    else:
        lines.append("_(no confirm verdicts recorded; see run transcripts)_")
    lines += [
        "",
        "## Transcripts",
        "",
        f"Full review→triage→fix→confirm record: [`{manifest.run_id}/RUN.md`]"
        f"({manifest.run_id}/RUN.md).",
        "",
    ]
    if plan_text.strip():
        lines += ["_Plan: see `plan.md` in this directory._", ""]
    return "\n".join(lines)


def write_pr_draft(
    artifact_root: Path, run_dir: Path, manifest: Any, writer: Any
) -> Path:
    """Render and write ``runs/<slug>/PR.md`` (FR-9.8). Returns the path."""
    prd_text = _read(artifact_root / "prd.md")
    plan_text = _read(artifact_root / "plan.md")
    content = render_pr(manifest, prd_text=prd_text, plan_text=plan_text, run_dir=run_dir)
    pr_path = artifact_root / "PR.md"
    writer.write_text(pr_path, content)
    return pr_path


def _read(path: Path) -> str:
    return path.read_text() if path.exists() else ""

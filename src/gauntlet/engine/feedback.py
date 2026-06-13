"""Human feedback capture (FR-6.1): ``retro/feedback.md`` + machine mirror.

``gauntlet feedback <run>`` asks the human, at run end or any time later, for an
outcome rating, what the reviewers missed, which triage verdicts were wrong
(false ``legitimate`` / false ``bikeshedding``), and freeform notes. The canonical
record is ``retro/feedback.md`` (PRD §7 layout, human-readable); a parallel
``feedback.json`` is written for the proposal-synthesis pass and the triage-corpus
feeding (FR-6.5) so those steps read structured data, not a markdown re-parse
(data over inference, §2).

Capture is split from prompting: :func:`write_feedback` is pure I/O over a
:class:`FeedbackData`, so the CLI's interactive prompts and the tests share one
code path.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from gauntlet.logging.redact import RedactingWriter

FEEDBACK_MD = "feedback.md"
FEEDBACK_JSON = "feedback.json"

# Triager verdict enum (mirrors schemas/triage.json) — a human correction names
# the verdict the triager *should* have returned.
VERDICTS = ("legitimate", "bikeshedding", "premature_optimization", "not_applicable")


class TriageCorrection(BaseModel):
    """A triage verdict the human marked wrong (FR-6.1 → FR-6.5 corpus seed)."""

    finding_id: str
    correct_verdict: str  # what triage SHOULD have said (one of VERDICTS)
    note: str = ""


class FeedbackData(BaseModel):
    """Structured human feedback for a run (FR-6.1)."""

    run_id: str = ""
    outcome_rating: str = ""  # freeform or e.g. good|mixed|poor
    reviewer_misses: str = ""  # what the reviewers missed
    triage_corrections: list[TriageCorrection] = Field(default_factory=list)
    notes: str = ""


def render_feedback_md(data: FeedbackData) -> str:
    lines = [
        f"# Run feedback — {data.run_id or '(run)'}",
        "",
        "## Outcome rating",
        "",
        data.outcome_rating.strip() or "(none)",
        "",
        "## What the reviewers missed",
        "",
        data.reviewer_misses.strip() or "(none)",
        "",
        "## Triage corrections (false legitimate / false bikeshedding)",
        "",
    ]
    if data.triage_corrections:
        for c in data.triage_corrections:
            suffix = f" — {c.note}" if c.note else ""
            lines.append(f"- `{c.finding_id}` → **{c.correct_verdict}**{suffix}")
    else:
        lines.append("(none)")
    lines += ["", "## Notes", "", data.notes.strip() or "(none)", ""]
    return "\n".join(lines) + "\n"


def feedback_dir(run_dir: Path) -> Path:
    return run_dir / "retro"


def write_feedback(run_dir: Path, data: FeedbackData, writer: RedactingWriter) -> Path:
    """Persist feedback.md (canonical) + feedback.json (machine mirror). Returns md path."""
    d = feedback_dir(run_dir)
    md_path = d / FEEDBACK_MD
    writer.write_text(md_path, render_feedback_md(data))
    writer.write_text(
        d / FEEDBACK_JSON,
        json.dumps(data.model_dump(), indent=2, ensure_ascii=False),
    )
    return md_path


def read_feedback(run_dir: Path) -> FeedbackData | None:
    """Load structured feedback for a run, or ``None`` if none was captured."""
    json_path = feedback_dir(run_dir) / FEEDBACK_JSON
    if json_path.exists():
        return FeedbackData.model_validate_json(json_path.read_text())
    return None


def feedback_markdown(run_dir: Path) -> str | None:
    """Raw feedback.md text for the run summary, or ``None``."""
    md_path = feedback_dir(run_dir) / FEEDBACK_MD
    return md_path.read_text() if md_path.exists() else None

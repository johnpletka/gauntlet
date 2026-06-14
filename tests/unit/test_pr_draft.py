"""PR.md drafting at final-gate pass (P5, FR-9.8)."""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.engine.manifest import CommitRecord, Manifest, PipelineRef
from gauntlet.engine.pr import render_pr, write_pr_draft
from gauntlet.logging.redact import RedactingWriter

PRD = """# PRD: Invoice export

Export approved invoices to the accounting system as CSV, nightly.

## Requirements
- FR-1 ...
"""

PLAN = "# Plan\n\n```gauntlet-phases\n- id: P1\n  title: x\n  goal: y\n```\n"


def _manifest() -> Manifest:
    man = Manifest(
        run_id="run-2026", slug="invoice-export",
        branch="gauntlet/invoice-export", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:h"),
    )
    man.commits = [
        CommitRecord(step_id="prd-cycle", phase="PRD.1", sha="a" * 40),
        CommitRecord(step_id="plan-cycle", phase="PLAN", sha="b" * 40),
        CommitRecord(step_id="phase-commit", phase="P1", sha="c" * 40),
        CommitRecord(step_id="impl-cycle", phase="P1.1", sha="d" * 40),
    ]
    man.status = "done"
    return man


def test_render_pr_has_required_sections(tmp_path):
    run_dir = tmp_path / "run-2026"
    text = render_pr(_manifest(), prd_text=PRD, plan_text=PLAN, run_dir=run_dir)
    assert "# PR draft" in text
    assert "Not opened, not pushed" in text          # FR-9.8 / §2.2
    assert "Invoice export" in text                  # PRD summary
    assert "export approved invoices" in text.lower()
    # per-phase commit list including fix rounds, grouped by phase
    assert "### P1" in text and "### PRD" in text and "### PLAN" in text
    assert "P1.1" in text                            # fix round listed
    assert "RUN.md" in text                          # transcript link


def test_final_verdicts_pulled_from_last_confirm(tmp_path):
    run_dir = tmp_path / "run-2026"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "confirm.json").write_text(json.dumps({
        "verdicts": [{"finding_id": "F-001", "verdict": "resolved",
                      "notes": "the diff fixes it"}],
        "new_findings": [], "summary": "",
    }))
    text = render_pr(_manifest(), prd_text=PRD, plan_text=PLAN, run_dir=run_dir)
    assert "F-001" in text and "resolved" in text


def test_write_pr_draft_writes_to_slug_dir(tmp_path):
    slug_dir = tmp_path / "runs" / "invoice-export"
    run_dir = slug_dir / "run-2026"
    run_dir.mkdir(parents=True)
    (slug_dir / "prd.md").write_text(PRD)
    (slug_dir / "plan.md").write_text(PLAN)
    path = write_pr_draft(slug_dir, run_dir, _manifest(), RedactingWriter())
    assert path == slug_dir / "PR.md"
    assert path.exists()
    assert "Invoice export" in path.read_text()


def test_render_pr_survives_missing_artifacts(tmp_path):
    # No prd/plan text, no confirm.json — still produces a usable draft.
    text = render_pr(_manifest(), prd_text="", plan_text="", run_dir=tmp_path)
    assert "# PR draft" in text
    assert "no confirm verdicts recorded" in text

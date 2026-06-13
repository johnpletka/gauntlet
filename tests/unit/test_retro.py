"""retrospective step + proposal generation (P7, FR-6.2/6.3).

Drives the retrospective step through the Orchestrator with injected fakes:
each agent self-critiques, the cheap proposer synthesises path-contained diffs
into retro/proposals/, and the human feedback (when captured) reaches the
synthesis prompt. Mirrors the offline test pattern in test_cycle.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from gauntlet.engine import manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.feedback import FeedbackData, TriageCorrection
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline
from gauntlet.engine import proposals as P
from gauntlet.logging.redact import RedactingWriter

from conftest import FakeAdapter

REPO = Path(__file__).resolve().parents[2]

BASE_CONFIG = {
    "base_branch": "main",
    "agents": {
        "builder": {"adapter": "claude-code"},
        "reviewer": {"adapter": "codex"},
        "triage": {"adapter": "api", "model": "mini"},
    },
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _retro_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@gauntlet.local")
    _git(repo, "config", "commit.gpgsign", "false")
    shutil.copytree(REPO / "schemas", repo / "schemas")
    shutil.copytree(REPO / "prompts", repo / "prompts")
    (repo / "README.md").write_text("readme\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "branch", "-M", "main")
    return repo


def _capture_diff(repo: Path, rel: str, new_content: str) -> str:
    path = repo / rel
    orig = path.read_text()
    path.write_text(new_content)
    diff = _git(repo, "diff", "--", rel)
    path.write_text(orig)
    _git(repo, "checkout", "--", rel)
    return diff


def _run_retro(repo: Path, adapters: dict, *, feedback=None):
    step = {
        "id": "retrospective", "type": "retrospective",
        "agents": ["builder", "reviewer"], "proposer": "triage",
        "retro_prompt": "prompts/retro.md",
        "synthesis_prompt": "prompts/proposal-synthesis.md",
    }
    pipeline = Pipeline.model_validate(
        {"name": "demo", "version": 1, "stages": [{"id": "retro", "steps": [step]}]}
    )
    cfg = RunConfig.model_validate(BASE_CONFIG)
    run_dir = repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    if feedback is not None:
        from gauntlet.engine.feedback import write_feedback
        write_feedback(run_dir, feedback, RedactingWriter())
    man = Manifest(run_id="run-1", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    man.steps.append(StepRecord(
        id="impl-cycle", type="adversarial_cycle", notes="converged in 1",
        metrics={"rounds": 1, "findings_total": 2, "accepted_total": 1,
                 "verdict_counts": {"legitimate": 1, "bikeshedding": 1},
                 "confirm_counts": {"resolved": 1}},
    ))
    orch = Orchestrator(
        repo_root=repo, run_dir=run_dir, artifact_root=repo,
        config=cfg, pipeline=pipeline, manifest=man,
        adapter_factory=lambda n: adapters[n],
    )
    return orch.drive(), man, run_dir


def _adapters(repo: Path):
    good = _capture_diff(repo, "prompts/triage.md",
                         (repo / "prompts/triage.md").read_text() + "\nAn extra rubric line.\n")
    bad = _capture_diff(repo, "README.md", "readme changed\n")
    proposals = {"proposals": [
        {"slug": "sharpen-rubric", "target_path": "prompts/triage.md",
         "rationale": "triager kept calling real defects bikeshedding", "diff": good},
        {"slug": "touch-src", "target_path": "README.md",
         "rationale": "should be rejected", "diff": bad},
    ]}
    return {
        "builder": FakeAdapter(text="builder self-critique: I misread the scope rule."),
        "reviewer": FakeAdapter(text="reviewer self-critique: my F-002 was bikeshedding."),
        "triage": FakeAdapter(text="{}", structured=proposals),
    }


def test_retro_runs_and_generates_proposals(tmp_path: Path):
    repo = _retro_repo(tmp_path)
    status, man, run_dir = _run_retro(repo, _adapters(repo))
    assert status == M.RUN_DONE
    assert man.record("retrospective").status == M.DONE

    # each agent's self-critique was written (FR-6.2)
    assert (run_dir / "retro" / "retro-builder.md").read_text().startswith("builder self")
    assert (run_dir / "retro" / "retro-reviewer.md").exists()

    # proposals materialized: one valid+pending, one path-escape invalid (F-001)
    props = P.list_proposals(run_dir / "retro" / "proposals")
    assert len(props) == 2
    by_slug = {p.slug: p for p in props}
    assert by_slug["sharpen-rubric"].valid and by_slug["sharpen-rubric"].status == P.PENDING
    assert not by_slug["touch-src"].valid and by_slug["touch-src"].status == P.INVALID

    # outcome counts persisted for --trend (FR-6.6)
    metrics = man.record("retrospective").metrics
    assert metrics["proposals_generated"] == 2
    assert metrics["proposals_valid"] == 1
    assert metrics["retro_agents"] == 2


def test_feedback_reaches_synthesis_prompt(tmp_path: Path):
    repo = _retro_repo(tmp_path)
    feedback = FeedbackData(
        run_id="run-1", outcome_rating="mixed",
        reviewer_misses="missed the loader off-by-one",
        triage_corrections=[TriageCorrection(finding_id="F-002",
                            correct_verdict="legitimate", note="was real")],
        notes="SENTINEL-FEEDBACK-NOTE",
    )
    status, _, run_dir = _run_retro(repo, _adapters(repo), feedback=feedback)
    assert status == M.RUN_DONE
    synth_prompt = (run_dir / "steps" / "retrospective" / "synthesis" / "prompt.md").read_text()
    # the human feedback flowed into the synthesis pass (FR-6.2 → FR-6.3)
    assert "SENTINEL-FEEDBACK-NOTE" in synth_prompt
    assert "F-002" in synth_prompt
    # and the run summary carried the prior cycle's notes + metrics
    assert "converged in 1" in synth_prompt


def test_retro_survives_proposer_failure(tmp_path: Path):
    # a synthesis hiccup must not strand an otherwise-complete run (best-effort).
    repo = _retro_repo(tmp_path)

    class Boom(FakeAdapter):
        def run(self, *a, **k):
            raise RuntimeError("synthesis exploded")

    adapters = {
        "builder": FakeAdapter(text="b"),
        "reviewer": FakeAdapter(text="r"),
        "triage": Boom(),
    }
    status, man, run_dir = _run_retro(repo, adapters)
    assert status == M.RUN_DONE
    assert "proposal synthesis skipped" in man.record("retrospective").notes

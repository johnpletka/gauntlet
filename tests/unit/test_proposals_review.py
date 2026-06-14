"""`gauntlet proposals review` + report --trend wiring through RunManager (P7).

Exercises the governed-apply flow end-to-end: a pending proposal in a run dir is
approved (applied + committed) or rejected, the dirty-tree guard fails closed,
trend reads cross-run metrics, and the manifest records the policy.yaml hash
(FR-6 acceptance).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import proposals as P
from gauntlet.engine.config import RunConfig
from gauntlet.engine.feedback import FeedbackData, TriageCorrection
from gauntlet.engine.pipeline import Pipeline
from gauntlet.engine.run import RunManager
from gauntlet.logging.redact import RedactingWriter

from conftest import FakeAdapter

CFG = """\
base_branch: main
branch_prefix: "gauntlet/"
run_root: runs
agents:
  triage: {adapter: api, model: mini}
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@gauntlet.local")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "prompts").mkdir()
    (repo / "prompts" / "triage.md").write_text("rubric one\nrubric two\n")
    (repo / "policy.yaml").write_text("deny: []\n")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CFG)
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


def _seed_proposal(repo: Path, *, slug="x", rel="prompts/triage.md", new=None) -> Path:
    """Place a pending, valid proposal in a run dir and return that run dir."""
    run_dir = repo / "runs" / "demo" / "run-2026-06-13T00-00-00"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ".gitignore").write_text("*\n")  # the engine's self-ignore
    man = {
        "run_id": run_dir.name, "slug": "demo", "branch": "gauntlet/demo",
        "base_branch": "main",
        "pipeline": {"name": "standard", "version": 1, "hash": "h"},
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))
    diff = _capture_diff(repo, rel, new or "rubric one IMPROVED\nrubric two\n")
    P.materialize_proposals(
        repo, run_dir / "retro" / "proposals",
        [{"slug": slug, "target_path": rel, "rationale": "sharpen", "diff": diff}],
        source_run=run_dir.name, writer=RedactingWriter(),
    )
    return run_dir


def test_review_approve_applies_and_commits(tmp_path: Path):
    repo = _repo(tmp_path)
    _seed_proposal(repo)
    mgr = RunManager(repo)
    results = mgr.review_proposals(
        "demo", decide=lambda p: ("approve", ""), timestamp="2026-06-13"
    )
    assert results == [{"proposal": "001-x", "action": "applied", "sha": results[0]["sha"]}]
    assert "IMPROVED" in (repo / "prompts/triage.md").read_text()
    assert "sharpen" in (repo / "prompts/CHANGELOG.md").read_text()
    # the commit holds exactly the asset + changelog, nothing from the run dir
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert set(files) == {"prompts/triage.md", "prompts/CHANGELOG.md"}
    # tree clean (excludes the gitignored run dir); proposal flipped to applied
    assert _git(repo, "status", "--porcelain").strip() == ""


def test_review_reject_records_and_leaves_tree_untouched(tmp_path: Path):
    repo = _repo(tmp_path)
    run_dir = _seed_proposal(repo)
    mgr = RunManager(repo)
    results = mgr.review_proposals(
        "demo", decide=lambda p: ("reject", "not now"), timestamp="2026-06-13"
    )
    assert results == [{"proposal": "001-x", "action": "rejected"}]
    assert "IMPROVED" not in (repo / "prompts/triage.md").read_text()
    prop = P.list_proposals(run_dir / "retro" / "proposals")[0]
    assert prop.status == P.REJECTED


def test_review_fails_closed_on_dirty_tree(tmp_path: Path):
    repo = _repo(tmp_path)
    _seed_proposal(repo)
    (repo / "README.md").write_text("dirtied\n")  # uncommitted real change
    mgr = RunManager(repo)
    with pytest.raises(P.ProposalError):
        mgr.review_proposals("demo", decide=lambda p: ("approve", ""), timestamp="2026-06-13")


def test_manifest_records_policy_hash(tmp_path: Path):
    repo = _repo(tmp_path)
    mgr = RunManager(repo)
    pipeline = Pipeline.model_validate({
        "name": "standard", "version": 1,
        "stages": [{"id": "s", "steps": [
            {"id": "impl", "type": "agent_task", "agent": "triage",
             "prompt": "prompts/triage.md", "repo_write": False},
        ]}],
    })
    hashes = mgr._prompt_hashes(pipeline)
    # FR-6 acceptance: policy.yaml is a versioned, retro-tunable asset, hashed so
    # an approved policy proposal provably changes the next run's manifest.
    assert "policy.yaml" in hashes
    assert hashes["policy.yaml"].startswith("sha256:")


def test_feedback_after_run_regenerates_proposals(tmp_path: Path):
    # F-001: feedback captured AFTER the run must be able to drive proposal
    # generation (FR-6.1). A completed run with no feedback yet → enter feedback
    # → regenerate → a pending proposal appears reflecting that feedback.
    repo = _repo(tmp_path)
    run_dir = repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")  # engine self-ignore
    man = {
        "run_id": "run-1", "slug": "demo", "branch": "gauntlet/demo",
        "base_branch": "main",
        "pipeline": {"name": "standard", "version": 1, "hash": "h"},
        "status": "RUN_DONE",
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))
    (run_dir / "pipeline.yaml").write_text(
        "name: standard\nversion: 1\nstages:\n"
        "  - id: retro\n    steps:\n"
        "      - {id: retrospective, type: retrospective, "
        "agents: [builder, reviewer], proposer: triage}\n"
    )
    # self-critiques the original retro left behind
    (run_dir / "retro").mkdir()
    (run_dir / "retro" / "retro-builder.md").write_text("builder critique")
    (run_dir / "retro" / "retro-reviewer.md").write_text("reviewer critique")

    proposals_dir = run_dir / "retro" / "proposals"
    assert not P.list_proposals(proposals_dir)  # nothing yet

    mgr = RunManager(repo)
    # capture a triage correction in feedback (the FR-6.5 corpus seed case)
    mgr.save_feedback(
        "demo",
        FeedbackData(
            outcome_rating="mixed", reviewer_misses="missed the off-by-one",
            triage_corrections=[TriageCorrection(
                finding_id="F-002", correct_verdict="legitimate", note="was real")],
            notes="SENTINEL-LATE-FEEDBACK",
        ),
        run_dir=run_dir,
    )

    # the synthesiser proposes sharpening the rubric from that feedback
    diff = _capture_diff(repo, "prompts/triage.md", "rubric one SHARPENED\nrubric two\n")
    fake = FakeAdapter(text="{}", structured={"proposals": [
        {"slug": "sharpen-rubric", "target_path": "prompts/triage.md",
         "rationale": "human marked F-002 a false bikeshedding", "diff": diff}]})
    generated = mgr.regenerate_proposals(
        "demo", run_dir=run_dir, adapter_factory=lambda n: fake
    )

    # a pending, applyable proposal now exists, driven by the late feedback
    assert len(generated) == 1 and generated[0].valid
    pending = [p for p in P.list_proposals(proposals_dir)
               if p.status == P.PENDING and p.valid]
    assert len(pending) == 1 and pending[0].slug == "sharpen-rubric"
    # the feedback actually reached the synthesis prompt
    synth_prompt = (run_dir / "steps" / "retrospective" / "synthesis" / "prompt.md").read_text()
    assert "SENTINEL-LATE-FEEDBACK" in synth_prompt


def test_trend_reads_cross_run_metrics(tmp_path: Path):
    repo = _repo(tmp_path)
    run_dir = repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    man = {
        "run_id": "run-1", "slug": "demo", "branch": "b", "base_branch": "main",
        "pipeline": {"name": "standard", "version": 1, "hash": "h"},
        "steps": [{"id": "c", "type": "adversarial_cycle",
                   "metrics": {"rounds": 1, "findings_total": 2,
                               "verdict_counts": {"legitimate": 2},
                               "confirm_counts": {"resolved": 2}}}],
        "commits": [{"step_id": "c", "phase": "P1", "sha": "a" * 40}],
        "totals": {"input_tokens": 1, "output_tokens": 1, "cost_usd": 1.0},
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))
    rows = RunManager(repo).trend("demo")
    assert len(rows) == 1
    assert rows[0].pct_legitimate == pytest.approx(100.0)
    assert rows[0].findings_per_round == pytest.approx(2.0)

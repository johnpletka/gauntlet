"""§9 proposal-mode governance triggers (P6-A5, FR-5.1).

Panel-shrink and verifier-revert fire from the P1/P5 corpus metrics as
deterministic, ratifiable proposals — never a config self-mutation. Covers the
transforms, the trigger windows, and end-to-end materialization (path-contained +
`git apply`-checked) against the real shipped pipeline.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gauntlet.engine import governance as gov
from gauntlet.engine import manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.logging.redact import RedactingWriter

from conftest import git

REPO = Path(__file__).resolve().parents[2]


def _diff_lines(diff: str) -> tuple[list[str], list[str]]:
    """(added, removed) content lines of a unified diff, excluding the ---/+++ headers."""
    plus = [l for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    minus = [l for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    return plus, minus


# --- deterministic transforms ------------------------------------------------
PANEL_TEXT = (
    "      - {id: c, type: adversarial_cycle,\n"
    "         reviewers: [{profile: reviewer, lens: correctness},\n"
    "                     {profile: gemini, lens: spec-coverage}],\n"
    "         verifier: verifier,\n"
    "         max_rounds: 2}\n"
)


def test_remove_reviewer_drops_member():
    new = gov.remove_reviewer(PANEL_TEXT, "gemini")
    assert new is not None
    assert "gemini" not in new
    assert "reviewer, lens: correctness" in new  # the other member survives


def test_remove_reviewer_absent_returns_none():
    assert gov.remove_reviewer(PANEL_TEXT, "nobody") is None


def test_remove_verifier_drops_line():
    new = gov.remove_verifier(PANEL_TEXT)
    assert new is not None and "verifier:" not in new


def test_remove_verifier_absent_returns_none():
    assert gov.remove_verifier("no verifier here\n") is None


# --- corpus metric extraction ------------------------------------------------
def _ens_run(run_id: str, members: dict[str, int]) -> dict:
    """A manifest dict with one cycle step carrying per-member unique_legit."""
    by_member = {
        f"{p}::lens": {"profile": p, "lens": "lens", "unique_legit": n}
        for p, n in members.items()
    }
    return {"run_id": run_id, "steps": [
        {"id": "impl-cycle", "metrics": {"ensemble": {"unique_legit_by_member": by_member}}}
    ]}


def _verifier_run(run_id: str, legit: int) -> dict:
    return {"run_id": run_id, "steps": [
        {"id": "impl-cycle", "metrics": {"verifier": {"legit_findings": legit}}}
    ]}


def test_panel_shrink_fires_on_two_below_threshold(tmp_path: Path):
    _pipeline_repo(tmp_path)
    corpus = [
        _ens_run("run-a", {"reviewer": 9, "gemini": 1}),  # gemini 10%
        _ens_run("run-b", {"reviewer": 8, "gemini": 1}),  # gemini ~11%
    ]
    items = gov.panel_shrink_items(corpus, tmp_path, ".", "standard")
    assert len(items) == 1
    assert items[0]["slug"] == "shrink-panel-gemini"
    assert "run-a" in items[0]["rationale"] and "run-b" in items[0]["rationale"]
    plus, minus = _diff_lines(items[0]["diff"])
    assert any("gemini" in l for l in minus)          # removed on the - side
    assert not any("gemini" in l for l in plus)       # never on the + side
    assert any("reviewer, lens: correctness" in l for l in plus)  # the peer survives


def test_panel_shrink_silent_when_above_threshold(tmp_path: Path):
    _pipeline_repo(tmp_path)
    corpus = [
        _ens_run("run-a", {"reviewer": 6, "gemini": 4}),  # gemini 40%
        _ens_run("run-b", {"reviewer": 6, "gemini": 4}),
    ]
    assert gov.panel_shrink_items(corpus, tmp_path, ".", "standard") == []


def test_panel_shrink_needs_two_below_runs(tmp_path: Path):
    _pipeline_repo(tmp_path)
    corpus = [
        _ens_run("run-a", {"reviewer": 9, "gemini": 1}),  # below
        _ens_run("run-b", {"reviewer": 5, "gemini": 5}),  # not below
    ]
    assert gov.panel_shrink_items(corpus, tmp_path, ".", "standard") == []


def test_verifier_revert_fires_below_threshold(tmp_path: Path):
    _pipeline_repo(tmp_path)
    corpus = [_verifier_run("run-a", 0), _verifier_run("run-b", 0), _verifier_run("run-c", 1)]
    item = gov.verifier_revert_item(corpus, tmp_path, ".", "standard")
    assert item is not None and item["slug"] == "revert-verifier-to-opt-in"
    plus, minus = _diff_lines(item["diff"])
    assert any("verifier:" in l for l in minus)
    assert not any("verifier:" in l for l in plus)


def test_verifier_revert_silent_at_threshold(tmp_path: Path):
    _pipeline_repo(tmp_path)
    corpus = [_verifier_run("run-a", 1), _verifier_run("run-b", 1), _verifier_run("run-c", 1)]
    assert gov.verifier_revert_item(corpus, tmp_path, ".", "standard") is None


def test_verifier_revert_needs_three_runs(tmp_path: Path):
    _pipeline_repo(tmp_path)
    corpus = [_verifier_run("run-a", 0), _verifier_run("run-b", 0)]
    assert gov.verifier_revert_item(corpus, tmp_path, ".", "standard") is None


# --- end-to-end materialization (git apply-checked) --------------------------
def _pipeline_repo(tmp_path: Path) -> Path:
    """A git repo carrying the real shipped pipeline + schemas so a governance
    diff validates against genuine bytes."""
    if not (tmp_path / ".git").exists():
        git(tmp_path, "init", "-q")
        git(tmp_path, "config", "user.name", "Fixture")
        git(tmp_path, "config", "user.email", "fx@gauntlet.local")
        git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "pipelines").mkdir(exist_ok=True)
    shutil.copy(REPO / "pipelines" / "standard.yaml", tmp_path / "pipelines" / "standard.yaml")
    if not (tmp_path / "schemas").exists():
        shutil.copytree(REPO / "schemas", tmp_path / "schemas")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "pipeline")
    git(tmp_path, "branch", "-M", "main")
    return tmp_path


def _ctx(repo: Path, current_manifest: M.Manifest) -> SimpleNamespace:
    run_dir = repo / "runs" / "fam" / current_manifest.run_id
    return SimpleNamespace(
        repo_root=repo,
        run_dir=run_dir,
        config=RunConfig.model_validate({"asset_root": ".", "run_root": "runs"}),
        manifest=current_manifest,
        writer=RedactingWriter(),
    )


def _write_corpus_manifest(repo: Path, slug: str, run: dict) -> None:
    d = repo / "runs" / slug / run["run_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps(run))


def test_build_governance_proposals_materializes_ratifiable(tmp_path: Path):
    """P6-A5 end to end: a below-yield 2-run corpus + a below-signal 3-verifier
    corpus produce valid (git apply-checked, path-contained) PENDING proposals,
    and the pipeline file is NOT mutated (no config self-mutation)."""
    repo = _pipeline_repo(tmp_path)
    slug = "fam"
    # Two comparison runs with gemini below 25%.
    _write_corpus_manifest(repo, slug, _ens_run("run-2026-01-01T00-00-00", {"reviewer": 9, "gemini": 1}))
    _write_corpus_manifest(repo, slug, _ens_run("run-2026-01-02T00-00-00", {"reviewer": 9, "gemini": 1}))
    # Three verifier runs below the behavioral-signal threshold.
    _write_corpus_manifest(repo, slug, _verifier_run("run-2026-01-03T00-00-00", 0))
    _write_corpus_manifest(repo, slug, _verifier_run("run-2026-01-04T00-00-00", 0))
    _write_corpus_manifest(repo, slug, _verifier_run("run-2026-01-05T00-00-00", 0))

    man = M.Manifest(
        run_id="run-2026-01-06T00-00-00", slug=slug, branch="b", base_branch="main",
        pipeline=M.PipelineRef(name="standard", version=1, hash="h"),
    )
    ctx = _ctx(repo, man)
    before = (repo / "pipelines" / "standard.yaml").read_text()
    proposals = gov.build_governance_proposals(ctx)

    slugs = {p.slug for p in proposals}
    assert "shrink-panel-gemini" in slugs
    assert "revert-verifier-to-opt-in" in slugs
    assert all(p.valid and p.status == "pending" for p in proposals)
    # No config self-mutation: the shipped pipeline is byte-unchanged.
    assert (repo / "pipelines" / "standard.yaml").read_text() == before
    # The proposals are written under the run's retro/proposals dir.
    assert (ctx.run_dir / "retro" / "proposals").exists()

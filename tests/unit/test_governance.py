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


# --- block-style transforms (F-001/F-002 regression) -------------------------
# A ratified reformat (or an adopter's own pipeline) may render the panel as a
# block sequence and the verifier as a block mapping. A trigger that silently
# emits nothing against block style would be a fail-open governance gap.
PANEL_BLOCK_TEXT = (
    "      - id: c\n"
    "        type: adversarial_cycle\n"
    "        reviewers:\n"
    "          - profile: reviewer\n"
    "            lens: correctness\n"
    "          - profile: gemini\n"
    "            lens: spec-coverage\n"
    "        verifier:\n"
    "          profile: verifier\n"
    "          lens: behavioral\n"
    "        max_rounds: 2\n"
)


def test_remove_reviewer_drops_block_member():
    new = gov.remove_reviewer(PANEL_BLOCK_TEXT, "gemini")
    assert new is not None
    assert "gemini" not in new
    assert "spec-coverage" not in new           # gemini's whole item is gone
    assert "profile: reviewer" in new           # the peer survives
    assert "lens: correctness" in new
    assert "profile: verifier" in new           # unrelated block untouched


def test_remove_reviewer_block_absent_returns_none():
    assert gov.remove_reviewer(PANEL_BLOCK_TEXT, "nobody") is None


def test_remove_reviewer_block_flow_items():
    text = (
        "        reviewers:\n"
        "          - {profile: reviewer, lens: correctness}\n"
        "          - {profile: gemini, lens: spec-coverage}\n"
    )
    new = gov.remove_reviewer(text, "gemini")
    assert new is not None and "gemini" not in new
    assert "profile: reviewer" in new


def test_remove_verifier_drops_block_mapping():
    new = gov.remove_verifier(PANEL_BLOCK_TEXT)
    assert new is not None
    assert "verifier:" not in new
    assert "lens: behavioral" not in new        # the nested mapping is gone too
    assert "profile: reviewer" in new           # reviewers block untouched
    assert "max_rounds: 2" in new


# --- corpus metric extraction ------------------------------------------------
def _ens_run(run_id: str, members: dict[str, int], raised: int = 10) -> dict:
    """A manifest dict with one cycle step carrying per-member unique_legit."""
    by_member = {
        f"{p}::lens": {"profile": p, "lens": "lens", "unique_legit": n,
                       "raised": raised}
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


def test_panel_shrink_fires_on_full_overlap(tmp_path: Path):
    # PR #59 review F-004 follow-through: unique_legit is SOLE-SOURCE, so a
    # panel whose members fully overlap records 0 unique for everyone while
    # still raising findings — the exact §1.3 failure case. Zero total with
    # raised > 0 is "below any threshold", not "cannot judge".
    _pipeline_repo(tmp_path)
    corpus = [
        _ens_run("run-a", {"reviewer": 0, "gemini": 0}),
        _ens_run("run-b", {"reviewer": 0, "gemini": 0}),
    ]
    items = gov.panel_shrink_items(corpus, tmp_path, ".", "standard")
    assert {i["slug"] for i in items} >= {"shrink-panel-gemini"}


def test_panel_shrink_silent_when_panel_raised_nothing(tmp_path: Path):
    # Zero unique AND zero raised: no signal either way — no proposal.
    _pipeline_repo(tmp_path)
    corpus = [
        _ens_run("run-a", {"reviewer": 0, "gemini": 0}, raised=0),
        _ens_run("run-b", {"reviewer": 0, "gemini": 0}, raised=0),
    ]
    assert gov.panel_shrink_items(corpus, tmp_path, ".", "standard") == []


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


# A block-style rendering of the shipped cycle step: the panel is a block
# sequence and the verifier a block mapping. The §9 triggers must produce valid
# (git apply-checked) proposals against it exactly as they do the flow style.
_BLOCK_PIPELINE = """name: standard
version: 1

stages:
  - id: phases
    foreach: plan.phases
    steps:
      - id: impl-cycle
        type: adversarial_cycle
        mode: code_review
        triager: triage
        fixer: builder
        reviewers:
          - profile: reviewer
            lens: correctness
          - profile: gemini
            lens: spec-coverage
        verifier:
          profile: verifier
          lens: behavioral
        escalation_agent: escalation
        max_rounds: 2
        review_prompt: prompts/review-code.md
"""


def _block_pipeline_repo(tmp_path: Path) -> Path:
    """A git repo whose ``standard.yaml`` uses block-style reviewers + verifier."""
    repo = _pipeline_repo(tmp_path)
    (repo / "pipelines" / "standard.yaml").write_text(_BLOCK_PIPELINE)
    git(repo, "commit", "-qam", "block-style pipeline")
    return repo


def test_governance_fires_against_block_style_pipeline(tmp_path: Path):
    """F-001/F-002 regression: the panel-shrink and verifier-revert triggers
    produce valid, git apply-checked proposals against a block-style pipeline —
    the format the shipped triggers previously silently failed to touch."""
    repo = _block_pipeline_repo(tmp_path)
    slug = "fam"
    _write_corpus_manifest(repo, slug, _ens_run("run-2026-01-01T00-00-00", {"reviewer": 9, "gemini": 1}))
    _write_corpus_manifest(repo, slug, _ens_run("run-2026-01-02T00-00-00", {"reviewer": 9, "gemini": 1}))
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

    by_slug = {p.slug: p for p in proposals}
    assert "shrink-panel-gemini" in by_slug
    assert "revert-verifier-to-opt-in" in by_slug
    # Both must be VALID — i.e. their diffs applied cleanly to the block-style
    # bytes (the previous regex-only transforms emitted nothing here at all).
    assert by_slug["shrink-panel-gemini"].valid
    assert by_slug["revert-verifier-to-opt-in"].valid
    assert all(p.status == "pending" for p in proposals)
    # No config self-mutation.
    assert (repo / "pipelines" / "standard.yaml").read_text() == before

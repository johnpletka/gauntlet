"""Improvement proposals (P7, FR-6.3/6.4/6.5): path-containment + governed apply.

Covers: the path-containment allowlist (review F-001), diff target parsing,
proposal file parse/render round-trip, materialize (valid + invalid), the
governed apply (git apply + CHANGELOG accumulation + commit), the no-self-apply
guard, reject, and the manifest prompt/policy hash (FR-6 acceptance).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import proposals as P
from gauntlet.engine.gitops import Identity
from gauntlet.engine.pipeline import content_hash
from gauntlet.logging.redact import RedactingWriter

REPO = Path(__file__).resolve().parents[2]
IDENTITY = Identity(name="Gauntlet Retro", email="retro@gauntlet.local")


# --- path containment (the security control) ---------------------------------
@pytest.mark.parametrize("path,ok", [
    ("prompts/triage.md", True),
    ("prompts/triage-corpus.jsonl", True),
    ("pipelines/standard.yaml", True),
    ("schemas/findings.json", True),
    ("policy.yaml", True),
    ("src/gauntlet/cli.py", False),       # outside the allowlist
    ("../etc/passwd", False),             # traversal
    ("/etc/passwd", False),               # absolute
    ("prompts/../src/x.py", False),       # traversal through an allowed prefix
    ("README.md", False),
])
def test_path_allowed(path, ok):
    assert P.path_allowed(path) is ok


def test_diff_target_paths_and_containment():
    diff = (
        "diff --git a/prompts/triage.md b/prompts/triage.md\n"
        "--- a/prompts/triage.md\n+++ b/prompts/triage.md\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )
    assert P.diff_target_paths(diff) == ["prompts/triage.md"]
    ok, offending = P.path_containment(diff)
    assert ok and not offending


def test_containment_rejects_escape():
    diff = (
        "--- a/src/gauntlet/cli.py\n+++ b/src/gauntlet/cli.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    ok, offending = P.path_containment(diff)
    assert not ok
    assert "src/gauntlet/cli.py" in offending


# --- fixture repo + diff generation ------------------------------------------
def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


@pytest.fixture
def asset_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@gauntlet.local")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "prompts").mkdir()
    (repo / "prompts" / "triage.md").write_text("rubric line one\nrubric line two\n")
    (repo / "prompts" / "triage-corpus.jsonl").write_text(
        '{"id": "ex-1", "label": {"verdict": "legitimate"}}\n'
    )
    (repo / "policy.yaml").write_text("deny: []\n")
    (repo / "README.md").write_text("readme\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    _git(repo, "branch", "-M", "main")
    return repo


def _capture_diff(repo: Path, rel: str, new_content: str) -> str:
    """A git-applyable diff turning the committed file into ``new_content``."""
    path = repo / rel
    orig = path.read_text()
    path.write_text(new_content)
    diff = _git(repo, "diff", "--", rel)
    path.write_text(orig)
    _git(repo, "checkout", "--", rel)
    return diff


# --- parse / render round-trip -----------------------------------------------
def test_proposal_render_parse_round_trip(tmp_path: Path):
    diff = "--- a/prompts/triage.md\n+++ b/prompts/triage.md\n@@ -1 +1 @@\n-a\n+b\n"
    p = P.Proposal(number=3, slug="fix-rubric", status=P.PENDING,
                   source_run="run-x", targets=["prompts/triage.md"],
                   rationale="the rubric misfires on nits", diff=diff)
    path = tmp_path / "003-fix-rubric.md"
    path.write_text(P.render_proposal(p))
    back = P.parse_proposal(path)
    assert back.number == 3 and back.slug == "fix-rubric"
    assert back.status == P.PENDING and back.source_run == "run-x"
    assert back.targets == ["prompts/triage.md"]
    assert "rubric misfires" in back.rationale
    assert back.diff.strip() == diff.strip()


# --- materialize (valid + invalid) -------------------------------------------
def test_materialize_flags_valid_and_invalid(asset_repo: Path):
    good = _capture_diff(asset_repo, "prompts/triage.md",
                         "rubric line one CHANGED\nrubric line two\n")
    bad = _capture_diff(asset_repo, "README.md", "readme CHANGED\n")  # outside allowlist
    proposals_dir = asset_repo / "runs" / "demo" / "run-1" / "retro" / "proposals"
    items = [
        {"slug": "good", "target_path": "prompts/triage.md", "rationale": "ok", "diff": good},
        {"slug": "bad", "target_path": "README.md", "rationale": "nope", "diff": bad},
    ]
    written = P.materialize_proposals(asset_repo, proposals_dir, items,
                                      source_run="run-1", writer=RedactingWriter())
    by_slug = {p.slug: p for p in written}
    assert by_slug["good"].valid and by_slug["good"].status == P.PENDING
    assert not by_slug["bad"].valid and by_slug["bad"].status == P.INVALID
    assert "allowlist" in by_slug["bad"].invalid_reason
    # both written to disk, numbered sequentially (data over inference)
    assert len(list(proposals_dir.glob("*.md"))) == 2


def test_materialize_rejects_multi_file_diff(asset_repo: Path):
    # F-005: even when every touched path is allowlisted, a diff editing more
    # than one file violates the single-file proposal contract — one approval
    # must never apply multiple asset changes under a single rationale.
    one = _capture_diff(asset_repo, "prompts/triage.md",
                        "rubric line one EDIT\nrubric line two\n")
    two = _capture_diff(asset_repo, "policy.yaml", "deny: [rm]\n")
    multi = one + two  # both hunks target allowlisted files
    proposals_dir = asset_repo / "runs" / "demo" / "run-1" / "retro" / "proposals"
    [proposal] = P.materialize_proposals(
        asset_repo, proposals_dir,
        [{"slug": "multi", "target_path": "prompts/triage.md",
          "rationale": "touches two files", "diff": multi}],
        source_run="run-1", writer=RedactingWriter(),
    )
    assert not proposal.valid and proposal.status == P.INVALID
    assert "exactly one file" in proposal.invalid_reason
    # both touched paths are recorded for the human (data over inference)
    assert set(proposal.targets) == {"prompts/triage.md", "policy.yaml"}


# --- governed apply (FR-6.4/6.5) ---------------------------------------------
def test_apply_proposal_patches_changelog_and_commits(asset_repo: Path):
    diff = _capture_diff(asset_repo, "prompts/triage.md",
                         "rubric line one IMPROVED\nrubric line two\n")
    before_hash = content_hash((asset_repo / "prompts/triage.md").read_text())
    proposals_dir = asset_repo / "runs" / "demo" / "run-1" / "retro" / "proposals"
    [proposal] = P.materialize_proposals(
        asset_repo, proposals_dir,
        [{"slug": "improve", "target_path": "prompts/triage.md",
          "rationale": "sharpen the rubric", "diff": diff}],
        source_run="run-1", writer=RedactingWriter(),
    )
    changelog = asset_repo / "prompts" / "CHANGELOG.md"
    sha = P.apply_proposal(asset_repo, proposal, identity=IDENTITY,
                           changelog_path=changelog, timestamp="2026-06-13")

    # the asset changed → next run's hash differs (FR-6 acceptance)
    after_hash = content_hash((asset_repo / "prompts/triage.md").read_text())
    assert "IMPROVED" in (asset_repo / "prompts/triage.md").read_text()
    assert before_hash != after_hash
    # CHANGELOG accumulated (FR-6.5)
    assert "sharpen the rubric" in changelog.read_text()
    # committed with the proposal as the body; worktree clean afterwards
    body = _git(asset_repo, "log", "-1", "--format=%B", sha)
    assert "proposal 001 improve" in body.lower()
    assert "sharpen the rubric" in body
    # exactly the asset + changelog were committed; the run dir (untracked here)
    # was NOT swept in — commit_paths stages only the named paths.
    files = _git(asset_repo, "show", "--name-only", "--format=", "HEAD").split()
    assert set(files) == {"prompts/triage.md", "prompts/CHANGELOG.md"}
    status = _git(asset_repo, "status", "--porcelain")
    assert "prompts/" not in status  # committed assets are no longer dirty
    # status flipped to applied (no re-apply possible)
    assert P.parse_proposal(proposal.path).status == P.APPLIED


def test_no_self_apply_guard_refuses_non_pending(asset_repo: Path):
    diff = _capture_diff(asset_repo, "policy.yaml", "deny: []\nallow: [echo]\n")
    [proposal] = P.materialize_proposals(
        asset_repo, asset_repo / "p",
        [{"slug": "policy", "target_path": "policy.yaml", "rationale": "r", "diff": diff}],
        source_run="run-1", writer=RedactingWriter(),
    )
    proposal.status = P.APPLIED  # already applied
    with pytest.raises(P.ProposalError):
        P.apply_proposal(asset_repo, proposal, identity=IDENTITY,
                         changelog_path=asset_repo / "prompts/CHANGELOG.md",
                         timestamp="2026-06-13")


def test_apply_refuses_invalid_proposal(asset_repo: Path):
    bad = _capture_diff(asset_repo, "README.md", "readme x\n")
    [proposal] = P.materialize_proposals(
        asset_repo, asset_repo / "p",
        [{"slug": "bad", "target_path": "README.md", "rationale": "r", "diff": bad}],
        source_run="run-1", writer=RedactingWriter(),
    )
    assert proposal.status == P.INVALID
    with pytest.raises(P.ProposalError):
        P.apply_proposal(asset_repo, proposal, identity=IDENTITY,
                         changelog_path=asset_repo / "prompts/CHANGELOG.md",
                         timestamp="2026-06-13")


def test_corpus_feeding_appends_human_corrected_case(asset_repo: Path):
    # FR-6.5: a human-corrected triage case is fed to the few-shot corpus via the
    # SAME governed-apply mechanism (the corpus is an allowlisted prompts/ asset).
    corpus = "prompts/triage-corpus.jsonl"
    new = (
        (asset_repo / corpus).read_text()
        + '{"id": "ex-2", "label": {"verdict": "legitimate"}, '
          '"note": "human corrected a false bikeshedding"}\n'
    )
    diff = _capture_diff(asset_repo, corpus, new)
    [proposal] = P.materialize_proposals(
        asset_repo, asset_repo / "p",
        [{"slug": "corpus-fix", "target_path": corpus,
          "rationale": "learn from the corrected verdict", "diff": diff}],
        source_run="run-1", writer=RedactingWriter(),
    )
    assert proposal.valid
    P.apply_proposal(asset_repo, proposal, identity=IDENTITY,
                     changelog_path=asset_repo / "prompts/CHANGELOG.md",
                     timestamp="2026-06-13")
    assert "ex-2" in (asset_repo / corpus).read_text()


def test_reject_records_status(asset_repo: Path):
    diff = _capture_diff(asset_repo, "prompts/triage.md", "rubric line one Z\nrubric line two\n")
    [proposal] = P.materialize_proposals(
        asset_repo, asset_repo / "p",
        [{"slug": "x", "target_path": "prompts/triage.md", "rationale": "r", "diff": diff}],
        source_run="run-1", writer=RedactingWriter(),
    )
    P.reject_proposal(proposal, "not worth it")
    assert P.parse_proposal(proposal.path).status == P.REJECTED
    assert "not worth it" in P.parse_proposal(proposal.path).invalid_reason

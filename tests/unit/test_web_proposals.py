"""Read-only improvement-proposals view (FR-6.4 / §2.2 "not an editor").

Retro improvement proposals are written under
``run_dir/retro/proposals/NNN-<slug>.md`` (the same canonical source the CLI's
``gauntlet proposals review`` reads via ``engine.proposals.list_proposals``). The
console surfaces them read-only: rationale + diff, with a note that apply stays a
CLI verb. No apply/reject/approve controls — that would make the console an
editor (§2.2). Drives the new ``GET /runs/{slug}/proposals`` over a
``TestClient`` and asserts the page lists proposals + diffs, exposes NO
state-changing control, and contains ``slug``/``run_id``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.proposals import Proposal, render_proposal
from gauntlet.web.service import TOKEN_HEADER, create_app
from gauntlet.web.store import RunStore, UnsafePath

TOKEN = "proposals-token"

_DIFF = (
    "--- a/prompts/builder.md\n"
    "+++ b/prompts/builder.md\n"
    "@@ -1,1 +1,1 @@\n"
    "-old line\n"
    "+new line\n"
)


def _auth() -> dict[str, str]:
    return {TOKEN_HEADER: TOKEN}


def _write_run(repo: Path, slug: str, run_id: str) -> Path:
    run_dir = repo / "runs" / slug / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    man = Manifest(
        run_id=run_id,
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:x"),
        status="done",
        current_step=None,
        steps=[StepRecord(id="retrospective", type="retrospective", status="done")],
    )
    man.write_atomic(run_dir / "manifest.json")
    (repo / "runs" / slug / "active-run.txt").write_text(run_id)
    return run_dir


def _write_proposal(run_dir: Path, number: int, slug: str) -> None:
    proposals_dir = run_dir / "retro" / "proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)
    p = Proposal(
        number=number,
        slug=slug,
        status="pending",
        source_run=run_dir.name,
        targets=["prompts/builder.md"],
        rationale="Tighten the builder prompt to forbid scope creep.",
        diff=_DIFF,
    )
    (proposals_dir / f"{p.name}.md").write_text(render_proposal(p))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    return repo


@pytest.fixture
def client(repo: Path) -> TestClient:
    return TestClient(create_app(RunStore(repo, RunConfig()), token=TOKEN))


def _store(repo: Path) -> RunStore:
    return RunStore(repo, RunConfig())


def test_proposals_skips_symlinked_files(repo: Path):
    # An agent-authored symlink in the proposals dir must NOT be followed out of
    # containment (review F-003 / FR-10.1): `parse_proposal` does a bare
    # `read_text()`, so a symlinked `*.md` could otherwise render any
    # server-readable file in the console. The view rejects symlinks outright.
    run_dir = _write_run(repo, "demo", "run-1")
    _write_proposal(run_dir, 1, "legit-proposal")
    secret = repo / "secret.txt"
    secret.write_text("TOP SECRET — must never render in the console\n")
    proposals_dir = run_dir / "retro" / "proposals"
    (proposals_dir / "999-evil.md").symlink_to(secret)

    props = _store(repo).proposals("demo", run_id="run-1")

    assert [p["slug"] for p in props] == ["legit-proposal"]  # real one listed
    assert all("evil" not in p["name"] for p in props)  # symlink skipped
    blob = " ".join(p["rationale"] + p["diff"] for p in props)
    assert "SECRET" not in blob  # the symlink target's content never surfaces


# --------------------------------------------------------------------------- #
# store method
# --------------------------------------------------------------------------- #
def test_store_proposals_reads_canonical_source(repo: Path):
    run_dir = _write_run(repo, "demo", "run-1")
    _write_proposal(run_dir, 1, "tighten-builder")
    props = _store(repo).proposals("demo")
    assert len(props) == 1
    p = props[0]
    assert p["slug"] == "tighten-builder"
    assert p["target_path"] == "prompts/builder.md"
    assert "scope creep" in p["rationale"]
    assert "new line" in p["diff"]


def test_store_proposals_absent_dir_is_empty(repo: Path):
    _write_run(repo, "demo", "run-1")  # no retro/proposals dir
    assert _store(repo).proposals("demo") == []


def test_store_proposals_slug_traversal_rejected(repo: Path):
    _write_run(repo, "demo", "run-1")
    with pytest.raises(UnsafePath):
        _store(repo).proposals("../secret")
    with pytest.raises(UnsafePath):
        _store(repo).proposals("demo", run_id="../../etc")


# --------------------------------------------------------------------------- #
# HTML page
# --------------------------------------------------------------------------- #
def test_proposals_page_lists_diffs(client: TestClient, repo: Path):
    run_dir = _write_run(repo, "demo", "run-1")
    _write_proposal(run_dir, 1, "tighten-builder")
    _write_proposal(run_dir, 2, "fix-pipeline")
    html = client.get(
        "/runs/demo/proposals", headers=_auth(), params={"run_id": "run-1"}
    ).text
    assert "tighten-builder" in html and "fix-pipeline" in html
    assert "prompts/builder.md" in html
    assert "scope creep" in html
    assert "new line" in html  # the diff body
    # The CLI-verb guidance is present (control stays a CLI verb, §2.2).
    assert "gauntlet proposals review --slug demo" in html


def test_proposals_page_exposes_no_state_changing_control(client: TestClient, repo: Path):
    run_dir = _write_run(repo, "demo", "run-1")
    _write_proposal(run_dir, 1, "tighten-builder")
    html = client.get("/runs/demo/proposals", headers=_auth()).text
    # Strictly read-only (§2.2): no apply/reject/approve affordances, no forms,
    # no POST-driving control hooks.
    assert "<form" not in html
    assert "data-approve" not in html and "data-reject" not in html
    assert "data-apply" not in html
    for verb in ("Apply", "Reject", "Approve"):
        assert verb not in html


def test_proposals_page_empty(client: TestClient, repo: Path):
    _write_run(repo, "demo", "run-1")
    html = client.get("/runs/demo/proposals", headers=_auth()).text
    assert "no proposals" in html.lower()


def test_run_detail_links_proposals_when_present(client: TestClient, repo: Path):
    run_dir = _write_run(repo, "demo", "run-1")
    _write_proposal(run_dir, 1, "tighten-builder")
    html = client.get("/runs/demo", headers=_auth()).text
    assert "/runs/demo/proposals" in html
    assert "proposals (1)" in html.lower()


def test_run_detail_omits_proposals_link_when_absent(client: TestClient, repo: Path):
    _write_run(repo, "demo", "run-1")  # no proposals
    html = client.get("/runs/demo", headers=_auth()).text
    assert "/runs/demo/proposals" not in html


def test_proposals_slug_traversal_rejected_http(client: TestClient, repo: Path):
    _write_run(repo, "demo", "run-1")
    assert client.get("/runs/%2e%2e/proposals", headers=_auth()).status_code == 400
    resp = client.get(
        "/runs/demo/proposals", headers=_auth(), params={"run_id": "../../etc"}
    )
    assert resp.status_code == 400

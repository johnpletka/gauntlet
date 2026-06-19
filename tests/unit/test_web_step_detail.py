"""Step transcript drill-down HTML page (FR-3.1).

The JSON step API existed since P1; this closes the FR-3.1 gap that the step-tree
rows were plain text with no step-detail HTML page. Drives the new
``GET /runs/{slug}/steps/{step}`` over a ``TestClient`` against a fixture run-dir
tree, asserting: a step row links to its detail page, transcript.md renders as
markdown, prompt.md/events.jsonl are selectable, a cycle step surfaces its nested
rounds, and the ``step``/``artifact``/``run_id`` segments reject traversal
(mirroring the F-006 containment tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import (
    Manifest,
    PipelineRef,
    StepRecord,
    UsageTotals,
)
from gauntlet.web.service import TOKEN_HEADER, create_app
from gauntlet.web.store import RunNotFound, RunStore, UnsafePath

TOKEN = "step-detail-token"


def _auth() -> dict[str, str]:
    return {TOKEN_HEADER: TOKEN}


def _write_run(repo: Path, slug: str, run_id: str, steps, *, current=None) -> Path:
    run_dir = repo / "runs" / slug / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    man = Manifest(
        run_id=run_id,
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:x"),
        status="running",
        current_step=current,
        steps=steps,
    )
    man.write_atomic(run_dir / "manifest.json")
    (repo / "runs" / slug / "active-run.txt").write_text(run_id)
    return run_dir


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


def _agent_step_run(repo: Path) -> Path:
    run_dir = _write_run(
        repo,
        "demo",
        "run-1",
        [
            StepRecord(
                id="implement",
                type="agent_task",
                status="done",
                agent="builder",
                iteration="0",
                started="2026-02-02T00:00:00+00:00",
                ended="2026-02-02T00:05:00+00:00",
                usage=UsageTotals(cost_usd=0.42),
                notes="did the thing",
            )
        ],
        current="implement",
    )
    # A foreach fan-out writes its artifacts under `steps/<id>.<iteration>` — the
    # real engine layout — while the manifest record id stays `<id>` (review
    # F-002). The earlier fixture wrongly used `steps/implement`, masking the bug.
    step_dir = run_dir / "steps" / "implement.0"
    step_dir.mkdir(parents=True)
    (step_dir / "prompt.md").write_text("# Prompt\n\nbuild it please\n")
    (step_dir / "transcript.md").write_text(
        "# Transcript\n\nsome **bold** progress\n\n- did a\n- did b\n"
    )
    (step_dir / "events.jsonl").write_text('{"event": "start"}\n{"event": "end"}\n')
    return run_dir


# --------------------------------------------------------------------------- #
# the row link + transcript rendering
# --------------------------------------------------------------------------- #
def test_step_row_links_to_detail_page(client: TestClient, repo: Path):
    _agent_step_run(repo)
    html = client.get("/runs/demo", headers=_auth()).text
    # The step-tree row is now a link into the drill-down page (FR-3.1).
    assert '/runs/demo/steps/implement?run_id=run-1' in html
    assert "iteration=0" in html


def test_transcript_renders_as_markdown_by_default(client: TestClient, repo: Path):
    _agent_step_run(repo)
    html = client.get(
        "/runs/demo/steps/implement",
        headers=_auth(),
        params={"run_id": "run-1", "iteration": "0"},
    ).text
    # transcript.md is the default artifact and renders AS markdown, not raw text.
    assert '<div class="markdown">' in html
    assert "<strong>bold</strong>" in html
    assert "<li>did a</li>" in html
    assert "# Transcript" not in html  # not dumped raw
    # Metadata surfaced.
    assert "builder" in html and "done" in html


def test_select_prompt_and_events_artifacts(client: TestClient, repo: Path):
    _agent_step_run(repo)
    # prompt.md → markdown.
    prompt = client.get(
        "/runs/demo/steps/implement",
        headers=_auth(),
        params={"run_id": "run-1", "iteration": "0", "artifact": "prompt.md"},
    ).text
    assert "build it please" in prompt and '<div class="markdown">' in prompt
    # events.jsonl → raw <pre> (not markdown).
    events = client.get(
        "/runs/demo/steps/implement",
        headers=_auth(),
        params={"run_id": "run-1", "iteration": "0", "artifact": "events.jsonl"},
    ).text
    assert '<pre class="artifact">' in events
    assert "&#34;event&#34;: &#34;start&#34;" in events or '"event": "start"' in events


def test_drilldown_opens_the_requested_iteration(client: TestClient, repo: Path):
    # The transcript for iteration N must come from `steps/<id>.<N>`, not a bare
    # `steps/<id>` (review F-002). Two iterations with distinct transcripts prove
    # the correct on-disk dir is opened per `?iteration=`.
    run_dir = _write_run(
        repo,
        "demo",
        "run-1",
        [
            StepRecord(
                id="implement", type="agent_task", status="done",
                agent="builder", iteration="0",
            ),
            StepRecord(
                id="implement", type="agent_task", status="running",
                agent="builder", iteration="2",
            ),
        ],
        current="implement",
    )
    for it, marker in (("0", "FIRST iteration body"), ("2", "THIRD iteration body")):
        d = run_dir / "steps" / f"implement.{it}"
        d.mkdir(parents=True)
        (d / "transcript.md").write_text(marker + "\n")
    html = client.get(
        "/runs/demo/steps/implement",
        headers=_auth(),
        params={"run_id": "run-1", "iteration": "2"},
    ).text
    assert "THIRD iteration body" in html
    assert "FIRST iteration body" not in html


# --------------------------------------------------------------------------- #
# cycle rounds surfaced
# --------------------------------------------------------------------------- #
def test_cycle_step_surfaces_nested_rounds(client: TestClient, repo: Path):
    run_dir = _write_run(
        repo,
        "demo",
        "run-1",
        [StepRecord(id="prd-cycle", type="adversarial_cycle", status="done")],
        current=None,
    )
    base = run_dir / "steps" / "prd-cycle"
    review = base / "r1-review"
    review.mkdir(parents=True)
    (review / "findings.json").write_text('{"findings": []}')
    (review / "transcript.md").write_text("review transcript")
    triage = base / "r1-triage" / "F-001"
    triage.mkdir(parents=True)
    html = client.get(
        "/runs/demo/steps/prd-cycle", headers=_auth(), params={"run_id": "run-1"}
    ).text
    assert "r1-review" in html and "r1-triage" in html
    assert "findings.json" in html


# --------------------------------------------------------------------------- #
# containment (mirrors F-006 traversal tests)
# --------------------------------------------------------------------------- #
def test_step_segment_traversal_rejected_http(client: TestClient, repo: Path):
    _agent_step_run(repo)
    # `%2e%2e` reaches the handler as a literal ".." segment (the client would
    # normalise a bare `..` away); the store guard rejects it (mirrors F-006).
    resp = client.get(
        "/runs/demo/steps/%2e%2e",
        headers=_auth(),
        params={"run_id": "run-1"},
    )
    assert resp.status_code == 400


def test_step_segment_traversal_rejected_store_level(repo: Path):
    _agent_step_run(repo)
    store = _store(repo)
    with pytest.raises(UnsafePath):
        store.step_detail("demo", "../manifest.json", "run-1")


def test_artifact_traversal_rejected_http(client: TestClient, repo: Path):
    _agent_step_run(repo)
    # An ?artifact= naming a path outside the allowlist is refused, not opened.
    resp = client.get(
        "/runs/demo/steps/implement",
        headers=_auth(),
        params={"run_id": "run-1", "iteration": "0", "artifact": "../../manifest.json"},
    )
    # `..` segment → UnsafePath → 400.
    assert resp.status_code == 400


def test_run_id_traversal_rejected_http(client: TestClient, repo: Path):
    _agent_step_run(repo)
    resp = client.get(
        "/runs/demo/steps/implement",
        headers=_auth(),
        params={"run_id": "../../etc"},
    )
    assert resp.status_code == 400


def test_read_step_artifact_rejects_non_artifact(repo: Path):
    _agent_step_run(repo)
    store = _store(repo)
    # A name that is not one of the step's reported artifacts → RunNotFound (404),
    # so the page can only surface artifacts the read model already knows about.
    # The store takes the on-disk leaf (`<id>.<iteration>` for a foreach step).
    with pytest.raises(RunNotFound):
        store.read_step_artifact("demo", "implement.0", "manifest.json", run_id="run-1")
    # A traversal segment → UnsafePath.
    with pytest.raises(UnsafePath):
        store.read_step_artifact("demo", "implement.0", "../x", run_id="run-1")


def test_unknown_step_404(client: TestClient, repo: Path):
    _agent_step_run(repo)
    resp = client.get(
        "/runs/demo/steps/nope", headers=_auth(), params={"run_id": "run-1"}
    )
    assert resp.status_code == 404

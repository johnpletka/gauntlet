"""Console read model (P1): RunStore discovery, run_id selection, containment.

Drives the FastAPI console (`web/service.py`) with a `TestClient` over a fixture
run-dir tree built on disk, mirroring `test_judge_service.py`. Validates the P1
assumption: the on-disk manifest + artifact layout is a sufficient read model
with zero engine changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import (
    CommitRecord,
    Manifest,
    PipelineRef,
    StepRecord,
    UsageTotals,
)
from gauntlet.web.runner import NonLoopbackHostError, assert_loopback
from gauntlet.web.service import TOKEN_HEADER, create_app
from gauntlet.web.store import RunNotFound, RunStore, UnsafePath

TOKEN = "test-web-token-secret"


def _usage(cost: float | None) -> UsageTotals:
    return UsageTotals(input_tokens=100, output_tokens=50, cost_usd=cost)


def _write_manifest(
    slug_dir: Path,
    run_id: str,
    *,
    status: str,
    steps: list[StepRecord],
    current_step: str | None = None,
    commits: list[CommitRecord] | None = None,
    totals: UsageTotals | None = None,
    warnings: list[str] | None = None,
    active: bool = False,
) -> Path:
    run_dir = slug_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    man = Manifest(
        run_id=run_id,
        slug=slug_dir.name,
        branch=f"gauntlet/{slug_dir.name}",
        base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:deadbeef"),
        status=status,
        current_step=current_step,
        steps=steps,
        commits=commits or [],
        totals=totals or UsageTotals(),
        warnings=warnings or [],
    )
    man.write_atomic(run_dir / "manifest.json")
    if active:
        (slug_dir / "active-run.txt").write_text(run_id)
    return run_dir


def _write_cycle_artifacts(run_dir: Path) -> None:
    """A realistic adversarial_cycle step dir with one round + nested triage."""
    base = run_dir / "steps" / "prd-cycle"
    review = base / "r1-review"
    review.mkdir(parents=True)
    (review / "prompt.md").write_text("review prompt")
    (review / "transcript.md").write_text("review transcript")
    (review / "findings.json").write_text('{"findings": []}')
    triage = base / "r1-triage"
    (triage / "F-001").mkdir(parents=True)
    (triage / "F-002").mkdir(parents=True)
    confirm = base / "r1-confirm"
    confirm.mkdir(parents=True)
    (confirm / "confirm.json").write_text('{"verdicts": []}')


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    repo = tmp_path / "repo"
    runs = repo / "runs"

    # slug "alpha": a historical run + a latest run (active pointer set), where
    # the latest has an adversarial_cycle step with nested rounds.
    alpha = runs / "alpha"
    _write_manifest(
        alpha,
        "run-2026-01-01T00-00-00",
        status="done",
        steps=[
            StepRecord(
                id="prd-cycle", type="adversarial_cycle", status="done",
                started="2026-01-01T00:00:00+00:00", ended="2026-01-01T00:10:00+00:00",
            ),
        ],
        current_step=None,
        totals=_usage(1.23),
    )
    latest = _write_manifest(
        alpha,
        "run-2026-02-02T00-00-00",
        status="running",
        steps=[
            StepRecord(
                id="prd-cycle", type="adversarial_cycle", status="done",
                agent=None, base_sha="abc123",
                started="2026-02-02T00:00:00+00:00", ended="2026-02-02T00:13:00+00:00",
                usage=_usage(3.41),
            ),
            StepRecord(
                id="implement", type="agent_task", status="running", agent="builder",
                iteration="0", started="2026-02-02T00:13:00+00:00",
                notes="working",
            ),
        ],
        current_step="implement",
        commits=[CommitRecord(step_id="prd-cycle", phase="P1", sha="aaa")],
        totals=_usage(3.41),
        warnings=["FR-9.8 something"],
        active=True,
    )
    _write_cycle_artifacts(latest)

    # slug "beta": a single parked run (a human gate).
    beta = runs / "beta"
    _write_manifest(
        beta,
        "run-2026-03-03T00-00-00",
        status="parked",
        steps=[
            StepRecord(
                id="prd-approve", type="human_gate", status="parked",
                notes="awaiting approval",
                started="2026-03-03T00:00:00+00:00",
            ),
        ],
        current_step="prd-approve",
        totals=_usage(None),
        active=True,
    )

    # slug "empty": a slug dir with no runs at all (FR-1.1 edge: nothing to show).
    (runs / "empty").mkdir(parents=True)
    (runs / "empty" / "prd.md").write_text("# draft")

    return RunStore(repo, RunConfig())


@pytest.fixture
def client(store: RunStore) -> TestClient:
    return TestClient(create_app(store, token=TOKEN))


def _auth() -> dict[str, str]:
    return {TOKEN_HEADER: TOKEN}


# --- auth / loopback guards --------------------------------------------------


def test_healthz_unauthenticated(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_api_runs_requires_token(client: TestClient):
    assert client.get("/api/runs").status_code == 401


def test_api_runs_rejects_wrong_token(client: TestClient):
    resp = client.get("/api/runs", headers={TOKEN_HEADER: "wrong"})
    assert resp.status_code == 401


def test_query_param_token_rejected(client: TestClient):
    # P7 (FR-10.4): the ?token= bootstrap is retired — the token must never ride
    # in a URL. A query token no longer authenticates; the API header does.
    assert client.get("/api/runs", params={"token": TOKEN}).status_code == 401
    assert client.get("/api/runs", headers=_auth()).status_code == 200


def test_assert_loopback_guard():
    for ok in ("127.0.0.1", "localhost", "::1"):
        assert_loopback(ok)  # no raise
    for bad in ("0.0.0.0", "10.0.0.5", "example.com"):
        with pytest.raises(NonLoopbackHostError):
            assert_loopback(bad)


# --- /api/runs list ----------------------------------------------------------


def test_api_runs_shape(client: TestClient):
    rows = client.get("/api/runs", headers=_auth()).json()
    by_slug = {r["slug"]: r for r in rows}
    # "empty" has no run → omitted; alpha + beta present.
    assert set(by_slug) == {"alpha", "beta"}

    alpha = by_slug["alpha"]
    expected_fields = {
        "slug", "run_id", "status", "current_step", "current_step_status",
        "current_step_notes", "started", "ended", "totals", "branch",
        "base_branch", "owned", "attached", "n_steps", "n_done",
        "warnings_count", "updated",
    }
    assert expected_fields <= set(alpha)
    # alpha's latest/active run is the running one, not the historical done one.
    assert alpha["run_id"] == "run-2026-02-02T00-00-00"
    assert alpha["status"] == "running"
    assert alpha["current_step"] == "implement"
    assert alpha["current_step_status"] == "running"
    assert alpha["current_step_notes"] == "working"
    assert alpha["owned"] is False
    assert alpha["attached"] is False
    assert alpha["n_steps"] == 2
    assert alpha["n_done"] == 1
    assert alpha["warnings_count"] == 1
    assert alpha["totals"]["cost_usd"] == 3.41


def test_api_runs_sorted_recent_first(client: TestClient):
    rows = client.get("/api/runs", headers=_auth()).json()
    updated = [r["updated"] for r in rows]
    assert updated == sorted(updated, reverse=True)


# --- list search / status filter / sort (FR-1.2) -----------------------------


def _slugs(client: TestClient, **params) -> list[str]:
    rows = client.get("/api/runs", headers=_auth(), params=params).json()
    return [r["slug"] for r in rows]


def test_api_runs_status_filter(client: TestClient):
    assert _slugs(client, status="running") == ["alpha"]
    assert _slugs(client, status="parked") == ["beta"]
    assert _slugs(client, status="done") == []  # alpha's *latest* is running


def test_api_runs_free_text_search(client: TestClient):
    # q matches slug...
    assert _slugs(client, q="beta") == ["beta"]
    # ...and branch (gauntlet/alpha) — only alpha has it.
    assert _slugs(client, q="gauntlet/alpha") == ["alpha"]
    assert _slugs(client, q="nomatch") == []


def test_api_runs_slug_substring_filter(client: TestClient):
    assert _slugs(client, slug="alph") == ["alpha"]


def test_api_runs_sort_by_slug(client: TestClient):
    assert _slugs(client, sort="slug") == ["alpha", "beta"]


# --- full-history browser + cost report (FR-2.4) -----------------------------


def test_api_history_lists_all_runs_newest_first(client: TestClient):
    body = client.get("/api/runs/alpha/history", headers=_auth()).json()
    runs = body["runs"]
    assert [r["run_id"] for r in runs] == [
        "run-2026-02-02T00-00-00",
        "run-2026-01-01T00-00-00",
    ]
    # the active/latest run is flagged.
    active = {r["run_id"]: r["active"] for r in runs}
    assert active["run-2026-02-02T00-00-00"] is True
    assert active["run-2026-01-01T00-00-00"] is False
    # statuses are carried through.
    assert runs[1]["status"] == "done"


def test_api_history_empty_slug(client: TestClient):
    assert client.get("/api/runs/empty/history", headers=_auth()).json()["runs"] == []


def test_api_report_renders_breakdown(client: TestClient):
    body = client.get("/api/runs/alpha/report", headers=_auth()).json()
    assert body["slug"] == "alpha"
    # The report text is the same the CLI prints — non-empty and run-scoped.
    assert isinstance(body["report"], str) and body["report"].strip()


def test_api_report_honors_run_id(client: TestClient):
    # FR-2.4: a historical run is addressable; an unknown id 404s.
    ok = client.get(
        "/api/runs/alpha/report", headers=_auth(),
        params={"run_id": "run-2026-01-01T00-00-00"},
    )
    assert ok.status_code == 200
    missing = client.get(
        "/api/runs/alpha/report", headers=_auth(), params={"run_id": "run-nope"}
    )
    assert missing.status_code == 404


def test_history_page_renders(client: TestClient):
    html = client.get("/runs/alpha/history", headers=_auth()).text
    assert "Run history" in html
    assert "run-2026-01-01T00-00-00" in html


def test_report_page_renders(client: TestClient):
    html = client.get("/runs/alpha/report", headers=_auth()).text
    assert "Cost report" in html


# --- /api/runs/{slug} detail + run_id selection (FR-2.4) ---------------------


def test_api_run_defaults_to_latest_active(client: TestClient):
    body = client.get("/api/runs/alpha", headers=_auth()).json()
    assert body["run_id"] == "run-2026-02-02T00-00-00"
    assert body["status"] == "running"
    # full manifest shape
    assert [s["id"] for s in body["steps"]] == ["prd-cycle", "implement"]
    assert body["commits"][0]["sha"] == "aaa"
    assert "totals" in body and "agent_usage" in body and "warnings" in body
    assert body["owned"] is False


def test_api_run_run_id_selects_historical(client: TestClient):
    body = client.get(
        "/api/runs/alpha",
        headers=_auth(),
        params={"run_id": "run-2026-01-01T00-00-00"},
    ).json()
    assert body["run_id"] == "run-2026-01-01T00-00-00"
    assert body["status"] == "done"


def test_api_run_unknown_run_id_404(client: TestClient):
    resp = client.get(
        "/api/runs/alpha", headers=_auth(), params={"run_id": "run-9999"}
    )
    assert resp.status_code == 404


def test_api_run_unknown_slug_404(client: TestClient):
    assert client.get("/api/runs/nope", headers=_auth()).status_code == 404


def test_api_run_slug_with_no_runs_404(client: TestClient):
    assert client.get("/api/runs/empty", headers=_auth()).status_code == 404


# --- /api/runs/{slug}/steps/{step} cycle rounds ------------------------------


def test_step_detail_renders_cycle_rounds(client: TestClient):
    body = client.get(
        "/api/runs/alpha/steps/prd-cycle", headers=_auth()
    ).json()
    assert body["type"] == "adversarial_cycle"
    assert body["status"] == "done"
    round_names = {r["name"] for r in body["rounds"]}
    assert {"r1-review", "r1-triage", "r1-confirm"} <= round_names
    review = next(r for r in body["rounds"] if r["name"] == "r1-review")
    artifact_names = {a["name"] for a in review["artifacts"]}
    assert {"prompt.md", "transcript.md", "findings.json"} <= artifact_names
    assert all(a["size"] > 0 for a in review["artifacts"])
    # Each round artifact carries its step-relative path (the ?artifact= value),
    # so a nested file is viewable, not just listed (containment-bounded reader).
    assert any(a["path"] == "r1-review/findings.json" for a in review["artifacts"])
    # Per-finding triage dirs are now nested rounds (recursive), not opaque names.
    triage = next(r for r in body["rounds"] if r["name"] == "r1-triage")
    assert {"F-001", "F-002"} <= {sub["name"] for sub in triage["rounds"]}


def test_step_detail_unknown_step_404(client: TestClient):
    resp = client.get("/api/runs/alpha/steps/nope", headers=_auth())
    assert resp.status_code == 404


# --- path containment (FR-10.1) ----------------------------------------------


def test_slug_traversal_rejected_http(client: TestClient):
    # `%2e%2e` reaches the handler as a literal ".." segment (the client would
    # normalise a bare `..` away before sending); the store guard rejects it.
    assert client.get("/api/runs/%2e%2e", headers=_auth()).status_code == 400


def test_run_id_traversal_rejected_http(client: TestClient):
    resp = client.get(
        "/api/runs/alpha", headers=_auth(), params={"run_id": "../../etc"}
    )
    assert resp.status_code == 400


def test_traversal_rejected_store_level(store: RunStore):
    # Direct store calls: the slug, run_id and step segments are all guarded.
    with pytest.raises(UnsafePath):
        store.manifest("../secret")
    with pytest.raises(UnsafePath):
        store.manifest("alpha", "../../etc")
    with pytest.raises(UnsafePath):
        store.step_detail("alpha", "../manifest.json")
    with pytest.raises(UnsafePath):
        store.step_detail("alpha", "nested/evil")


def test_store_run_not_found_raises(store: RunStore):
    with pytest.raises(RunNotFound):
        store.manifest("alpha", "run-does-not-exist")


def test_run_root_escaping_repo_rejected_at_construction(tmp_path: Path):
    # Belt-and-suspenders (F-001): even if a config slips an escaping run_root
    # past validation (here simulated by bypassing the field validator), the
    # store fails closed at construction rather than browsing outside the repo.
    cfg = RunConfig()
    cfg.run_root = "../outside"  # direct assignment skips the field validator
    with pytest.raises(UnsafePath):
        RunStore(tmp_path / "repo", cfg)


# --- config resolution: missing falls back, malformed fails closed (F-002) ---


def test_from_repo_missing_config_falls_back_to_defaults(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    store = RunStore.from_repo(repo)
    assert store.config.run_root == "runs"


def test_from_repo_malformed_yaml_fails_closed(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".gauntlet").mkdir(parents=True)
    (repo / ".gauntlet" / "config.yaml").write_text("run_root: [unterminated\n")
    with pytest.raises(Exception):
        RunStore.from_repo(repo)


def test_from_repo_invalid_run_root_fails_closed(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".gauntlet").mkdir(parents=True)
    (repo / ".gauntlet" / "config.yaml").write_text("run_root: ../escape\n")
    with pytest.raises(Exception):
        RunStore.from_repo(repo)


# --- server-rendered pages ---------------------------------------------------


def test_html_run_list_page(client: TestClient):
    resp = client.get("/", headers=_auth())
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "alpha" in resp.text and "beta" in resp.text


def test_html_run_detail_page(client: TestClient):
    resp = client.get("/runs/alpha", headers=_auth())
    assert resp.status_code == 200
    assert "implement" in resp.text
    assert "prd-cycle" in resp.text


def test_html_pages_redirect_to_login(client: TestClient):
    # P7 (FR-10.4): an unauthenticated browser page navigation is bounced to the
    # /login form (303), not a bare 401 — the API/SSE/partial routes still 401.
    for path in ("/", "/runs/alpha"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/login?next=")
    # the assembled `next` round-trips the original path
    loc = client.get("/runs/alpha", follow_redirects=False).headers["location"]
    assert "next=%2Fruns%2Falpha" in loc


def test_api_routes_still_401_unauthenticated(client: TestClient):
    # API/SSE/partial routes fail closed loudly (no redirect) when unauthenticated.
    assert client.get("/api/runs").status_code == 401
    assert client.get("/partials/runs").status_code == 401

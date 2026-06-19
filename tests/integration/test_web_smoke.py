"""End-to-end console smoke: launch → gate → approve → done through the UI.

P7 (§8): drives the *whole* console control loop over real ``gauntlet`` CLI-verb
children, through the production cookie+CSRF auth path — a launch POST starts an
owned run, it parks at a ``human_gate``, an approve POST drives it to ``done`` —
proving control = sanctioned CLI verbs end-to-end (M4) with no invariant
bypassed. Marked ``integration`` (it spawns real subprocesses); excluded from the
default unit run, executed locally before a review handoff.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.run import RunManager
from gauntlet.web.auth import CSRF_HEADER
from gauntlet.web.service import create_app
from gauntlet.web.store import RunNotFound, RunStore
from gauntlet.web.supervisor import JobSupervisor

pytestmark = pytest.mark.integration

TOKEN = "smoke-token"

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

# Shell-only, no-creds pipeline that parks at a human gate, then finishes — so
# the UI can drive launch → park → approve → done without any agent/judge.
GATED_PIPELINE = """
name: gated
version: 1
stages:
  - id: phase
    steps:
      - {id: setup, type: shell, run: "true"}
      - {id: gate, type: human_gate}
      - {id: finish, type: shell, run: "true"}
"""


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


def _build_repo(repo: Path) -> JobSupervisor:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@gauntlet.local")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("fixture\n")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    (repo / "pipelines").mkdir()
    (repo / "pipelines" / "gated.yaml").write_text(GATED_PIPELINE)
    # A minimal fast-path policy so the managed judge starts healthily (the smoke
    # keeps the judge in the loop, proving control = CLI verbs bypasses nothing,
    # M4). Shell steps invoke no agent hooks, so no classification fires.
    (repo / "policy.yaml").write_text("version: 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "-M", "main")
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# PRD\n\nHuman-authored.\n")
    return JobSupervisor(repo)


def _poll(fn, *, timeout: float = 90.0, interval: float = 0.5):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s (last={last!r})")


def _status(store: RunStore) -> str | None:
    try:
        return store.manifest("demo").status
    except RunNotFound:
        return None


def test_launch_gate_approve_done_through_ui(tmp_path):
    sup = _build_repo(tmp_path / "repo")
    store = RunStore.from_repo(sup.repo_root, supervisor=sup)
    app = create_app(store, token=TOKEN, supervisor=sup)

    with TestClient(app) as client:
        # 1) Log in (cookie exchange) and read the session CSRF token.
        assert client.post(
            "/login", data={"token": TOKEN, "next": "/"}, follow_redirects=False
        ).status_code == 303
        csrf = re.search(
            r'name="csrf-token" content="([^"]*)"', client.get("/").text
        ).group(1)
        hdr = {CSRF_HEADER: csrf}

        # 2) Launch a run as an owned subprocess via the UI (cookie + CSRF POST).
        resp = client.post(
            "/api/runs", json={"slug": "demo", "pipeline": "gated"}, headers=hdr
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["owned"] is True

        # 3) It parks at the human gate AND the launch driver releases the
        #    worktree lock as it parks (FR-12.1), so the console can drive forward.
        _poll(
            lambda: _status(store) == "parked"
            and store.worktree_lock() is None
        )
        man = store.manifest("demo")
        assert man.current_step == "gate"

        # 4) Approve via the UI → drives the rest of the run to done.
        assert client.post(
            "/api/runs/demo/approve", json={}, headers=hdr
        ).status_code == 200
        _poll(lambda: _status(store) == "done")

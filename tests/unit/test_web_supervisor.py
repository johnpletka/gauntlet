"""JobSupervisor + RunProcess + control endpoints (P3, FR-6/FR-1.4/FR-10.5).

Subprocess-lifecycle tests mirror ``test_resume_crash.py`` (``Popen`` + signals):
launching a real ``gauntlet run`` child, asserting the pre-allocated
``run_dir/.serve/`` log + ``job.json`` (with a well-formed ProcessIdentity), a
running→done observation, process-group reap, and the crash-before-manifest
"failed launch" classification (no phantom owned run). The HTTP control surface
(``POST /api/runs`` / ``…/abort``) is driven over a ``TestClient`` with a fake
supervisor so argv/locking are asserted without spawning.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.manifest import DONE as STEP_DONE
from gauntlet.engine.manifest import RUN_DONE, Manifest
from gauntlet.web.jobproc import JOB_FILENAME, SERVE_DIRNAME, JobRecord
from gauntlet.web.service import TOKEN_HEADER, create_app
from gauntlet.web.store import RunStore
from gauntlet.web.supervisor import JobSupervisor, LockInfo

from conftest import git

TOKEN = "supervisor-test-token"

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

# Shell-only pipelines: reach DONE with no creds, no agent, no judge.
SLEEP_PIPELINE = """
name: simple
version: 1
stages:
  - id: phase
    steps:
      - {id: wait, type: shell, run: "sleep 1"}
"""

LONG_PIPELINE = """
name: long
version: 1
stages:
  - id: phase
    steps:
      - {id: wait, type: shell, run: "sleep 60"}
"""


def _build_repo(repo: Path, *, pipelines: dict[str, str], slug: str = "demo",
                author: bool = True) -> JobSupervisor:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@gauntlet.local")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("fixture\n")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    (repo / "pipelines").mkdir()
    for name, text in pipelines.items():
        (repo / "pipelines" / f"{name}.yaml").write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "-M", "main")
    sup = JobSupervisor(repo)
    # Author (or leave stubbed) the PRD via the same engine path the CLI uses.
    from gauntlet.engine.run import RunManager

    mgr = RunManager(repo)
    mgr.new(slug)
    if author:
        mgr.layout(slug).prd_path.write_text("# PRD\n\nReal human-authored PRD.\n")
    return sup


def _job_record(run_dir: Path) -> JobRecord | None:
    jp = run_dir / SERVE_DIRNAME / JOB_FILENAME
    return JobRecord.from_json(jp.read_text()) if jp.exists() else None


def test_launch_writes_log_and_job_then_drives_to_done(tmp_path):
    sup = _build_repo(tmp_path / "repo", pipelines={"simple": SLEEP_PIPELINE})
    rp = sup.launch_run("demo", pipeline="simple", no_judge=True)

    # The handshake: argv carries the pre-allocated --run-id (FR-6.1a).
    assert "--run-id" in rp.argv()
    assert rp.run_id in rp.argv()
    assert "--no-judge" in rp.argv()

    run_dir = rp.run_dir
    # Captured log + job.json land under the pre-allocated run_dir/.serve/ from
    # the first byte (FR-6.1a / FR-6.3).
    assert (run_dir / SERVE_DIRNAME / "run.log").exists()
    rec = _job_record(run_dir)
    assert rec is not None
    assert rec.verb == "run" and rec.run_id == rp.run_id
    # proc_identity is the structured FR-6.4 record on supported platforms.
    if rec.proc_identity is not None:
        assert {"platform", "value", "unit"} <= set(rec.proc_identity)
        assert isinstance(rec.proc_identity["value"], int)

    rp.wait(timeout=60)
    assert rp.returncode == 0
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == RUN_DONE
    assert man.record("wait").status == STEP_DONE
    rp.stop()  # idempotent on an already-exited child


def test_stop_reaps_the_process_group(tmp_path):
    sup = _build_repo(tmp_path / "repo", pipelines={"long": LONG_PIPELINE})
    rp = sup.launch_run("demo", pipeline="long", no_judge=True)
    # Give the child a moment to become the session leader of its group.
    deadline = time.monotonic() + 10
    while rp.pgid is None and time.monotonic() < deadline:
        time.sleep(0.02)
    pgid = rp.pgid
    assert pgid is not None
    assert rp.poll() is None  # still running (sleep 60)

    rp.stop()
    assert rp.poll() is not None  # reaped
    # The whole process group is gone (no orphaned grandchildren).
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_crash_before_manifest_is_failed_launch(tmp_path):
    # A slug whose PRD is still the stub: the child fails the entry contract
    # before any run dir / manifest is written (FR-6.1a pre-manifest window).
    sup = _build_repo(
        tmp_path / "repo", pipelines={"simple": SLEEP_PIPELINE}, author=False
    )
    rp = sup.launch_run("demo", pipeline="simple", no_judge=True)
    rp.wait(timeout=60)
    assert rp.returncode != 0  # entry contract refused

    run_dir = rp.run_dir
    # The captured bootstrap log is readable, so the failure is diagnosable.
    log = (run_dir / SERVE_DIRNAME / "run.log").read_text()
    assert log.strip() != ""
    # No manifest was written → it is a failed launch, not an owned run.
    assert not (run_dir / "manifest.json").exists()
    jobs = sup.jobs()
    job = next(j for j in jobs if j.run_id == rp.run_id)
    assert job.classify() == "failed_launch"
    # And it never appears as a phantom owned run in the read model.
    store = RunStore.from_repo(sup.repo_root, supervisor=sup)
    assert all(row.slug != "demo" or row.run_id != rp.run_id for row in store.list_rows())


def test_abort_stops_live_driver_and_marks_aborted(tmp_path):
    sup = _build_repo(tmp_path / "repo", pipelines={"long": LONG_PIPELINE})
    rp = sup.launch_run("demo", pipeline="long", no_judge=True)
    # Wait until the manifest exists (the run is genuinely live and recorded).
    run_dir = rp.run_dir
    deadline = time.monotonic() + 30
    while not (run_dir / "manifest.json").exists() and time.monotonic() < deadline:
        if rp.poll() is not None:
            raise RuntimeError("child exited before writing a manifest")
        time.sleep(0.05)
    assert (run_dir / "manifest.json").exists()

    abort_rp = sup.abort("demo")
    assert "abort" in abort_rp.argv()
    assert rp.poll() is not None  # the long-running driver was reaped
    assert Manifest.load(run_dir / "manifest.json").status == "aborted"


def test_owned_and_external_badges(tmp_path):
    """Store rows reflect supervisor ownership + the worktree-lock holder."""
    sup = _build_repo(tmp_path / "repo", pipelines={"simple": SLEEP_PIPELINE})
    rp = sup.launch_run("demo", pipeline="simple", no_judge=True)
    rp.wait(timeout=60)
    store = RunStore.from_repo(sup.repo_root, supervisor=sup)
    row = next(r for r in store.list_rows() if r.slug == "demo")
    assert row.owned is True  # has .serve/job.json
    # The child has exited, so it is not attached and not external.
    assert row.attached is False
    assert row.external is False


# ---- HTTP control surface (fake supervisor: argv/lock without spawning) -----


class _FakeProc:
    def __init__(self, slug, run_id):
        self.slug, self.run_id = slug, run_id
        self.pid = 4242
        self.log_path = Path("/tmp") / run_id / ".serve" / "run.log"

    def argv(self):
        return ["py", "-m", "gauntlet", "abort", self.slug]


class _FakeSupervisor:
    def __init__(self):
        self.launched: list[dict] = []
        self.aborted: list[str] = []
        self._lock: LockInfo | None = None

    def launch_run(self, slug, *, pipeline=None, no_judge=False):
        self.launched.append({"slug": slug, "pipeline": pipeline, "no_judge": no_judge})
        return _FakeProc(slug, "run-preallocated")

    def abort(self, slug):
        self.aborted.append(slug)
        return _FakeProc(slug, "run-x")

    def driving_lock(self):
        return self._lock

    def is_owned(self, slug, run_id):
        return False

    def is_attached(self, slug, run_id):
        return False


def _client(store, supervisor=None):
    app = create_app(store, token=TOKEN, supervisor=supervisor)
    return TestClient(app)


def _bare_store(tmp_path) -> RunStore:
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    return RunStore(repo, _config())


def _config():
    from gauntlet.engine.config import RunConfig

    return RunConfig()


def test_post_runs_launches_via_supervisor(tmp_path):
    sup = _FakeSupervisor()
    client = _client(_bare_store(tmp_path), supervisor=sup)
    resp = client.post(
        "/api/runs",
        json={"slug": "demo", "pipeline": "standard", "no_judge": True},
        headers={TOKEN_HEADER: TOKEN},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run-preallocated" and body["owned"] is True
    assert sup.launched == [{"slug": "demo", "pipeline": "standard", "no_judge": True}]


def test_post_runs_409_when_worktree_locked(tmp_path):
    sup = _FakeSupervisor()
    sup._lock = LockInfo(slug="other", run_id="run-y", pid=999, live=True)
    client = _client(_bare_store(tmp_path), supervisor=sup)
    resp = client.post("/api/runs", json={"slug": "demo"}, headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 409
    assert "being driven by other" in resp.json()["detail"]
    assert sup.launched == []  # fail closed: never launched


def test_post_runs_503_without_supervisor(tmp_path):
    client = _client(_bare_store(tmp_path), supervisor=None)
    resp = client.post("/api/runs", json={"slug": "demo"}, headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 503


def test_post_abort_invokes_supervisor(tmp_path):
    sup = _FakeSupervisor()
    client = _client(_bare_store(tmp_path), supervisor=sup)
    resp = client.post("/api/runs/demo/abort", headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 200
    assert sup.aborted == ["demo"]


def test_control_endpoints_require_token(tmp_path):
    sup = _FakeSupervisor()
    client = _client(_bare_store(tmp_path), supervisor=sup)
    assert client.post("/api/runs", json={"slug": "demo"}).status_code == 401
    assert client.post("/api/runs/demo/abort").status_code == 401
    assert sup.launched == [] and sup.aborted == []

"""kill -9 / resume crash test (P3 headline assumption, G8, FR-8.2, review F-003).

Runs the cycle in a LOOP — it must not flake. Each iteration spawns a child run
that dies mid-step (SIGKILL after a partial worktree edit), then resumes in this
process and asserts the engine recovered with exactly one set of effects: no lost
work, no duplicated commit, a consistent (never torn) manifest.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gauntlet.adapters.base import AdapterCapabilities, AgentResult
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.manifest import Manifest
from gauntlet.engine.run import RunManager

from conftest import git

CHILD = Path(__file__).parent / "_crash_child.py"

CONFIG = """
base_branch: main
run_root: runs
interrupted_step: {policy}
agents:
  builder: {{adapter: claude-code}}
"""

CRASH_PIPELINE = """
name: crash
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, message: "P1: crash phase\\n\\nthe body."}
"""


class RecoverAdapter:
    """Resume-time builder: writes the real file deterministically (idempotent)."""

    name = "recover"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        (Path(cwd) / "feature.py").write_text("RECOVERED — final content\n")
        return AgentResult(text="recovered", session_id="r", exit_code=0)


def _build_repo(tmp: Path, policy: str) -> tuple[Path, RunManager]:
    repo = tmp
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@gauntlet.local")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("crash fixture\n")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG.format(policy=policy))
    (repo / "pipelines").mkdir()
    (repo / "pipelines" / "crash.yaml").write_text(CRASH_PIPELINE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "-M", "main")
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# PRD\n\nReal human-authored PRD.\n")
    return repo, mgr


def _spawn_and_kill(repo: Path, kill_delay: float) -> None:
    ready = repo / ".crash_ready"
    if ready.exists():
        ready.unlink()
    proc = subprocess.Popen([sys.executable, str(CHILD), str(repo), "demo"])
    # Wait until the agent is mid-step (sentinel dropped), then a small extra
    # delay so the kill can also land during/after the manifest write.
    deadline = time.monotonic() + 30
    while not ready.exists() and time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"child exited early ({proc.returncode})")
        time.sleep(0.01)
    assert ready.exists(), "child never reached the mid-step sentinel"
    time.sleep(kill_delay)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)


@pytest.mark.parametrize("iteration", range(6))
def test_kill9_resume_recovers_in_a_loop(tmp_path, iteration):
    repo, mgr = _build_repo(tmp_path / f"repo{iteration}", policy="reset_to_base")
    # vary the kill timing across iterations to land at different points
    _spawn_and_kill(repo, kill_delay=[0.0, 0.02, 0.05, 0.1, 0.0, 0.03][iteration])

    # The manifest must always be loadable (atomic write => never torn).
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status in (M.RUN_RUNNING, M.RUN_PARKED)

    status = mgr.resume("demo", use_judge=False, adapter_factory=lambda n: RecoverAdapter())
    assert status == M.RUN_DONE

    # Exactly one commit, containing the recovered file; clean tree; no dupes.
    final = mgr.status("demo")
    assert [c.phase for c in final.commits] == ["P1"]
    assert gitops.commit_subject(repo, "HEAD") == "P1: crash phase"
    assert (repo / "feature.py").read_text() == "RECOVERED — final content\n"
    # work tree is clean; only the engine's own run bookkeeping is untracked
    assert gitops.is_clean(repo, exclude=["runs"])
    log = gitops._run(repo, "log", "--format=%s")
    assert log.count("P1: crash phase") == 1  # not double-committed


def test_kill9_resume_parks_under_park_policy(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo_park", policy="park")
    _spawn_and_kill(repo, kill_delay=0.05)

    status = mgr.resume("demo", use_judge=False, adapter_factory=lambda n: RecoverAdapter())
    # Default policy parks the run for a human rather than re-running over the
    # dirty mid-edit tree (review F-003).
    assert status == M.RUN_PARKED
    assert mgr.status("demo").record("implement").status == M.INTERRUPTED
    # the partial work is still present (parked, not discarded) for inspection
    assert (repo / "feature.py").read_text().startswith("PARTIAL")

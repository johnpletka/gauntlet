"""P3 contract: engine-managed judge lifecycle + a real pipeline (plan P3).

Two layers:
1. The engine starts/stops the judge around a run and injects per-run env
   (FR-7.1, supersedes BOOTSTRAP-NOTES #12) — exercised with a no-model
   shell+commit pipeline, so it runs without CLI/API creds.
2. A minimal real pipeline (agent_task on claude -> shell -> commit) on a
   fixture repo with the judge live and the PreToolUse hook wired — skipped
   when the claude CLI is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.judgeproc import _MANAGED_ENV_VARS
from gauntlet.engine.run import RunManager
from gauntlet.judge.service import TOKEN_ENV_VAR

pytestmark = [pytest.mark.integration]

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_BIN = shutil.which("gauntlet-judge-hook") or str(
    REPO_ROOT / ".venv" / "bin" / "gauntlet-judge-hook"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _fixture_repo(tmp_path: Path, config_yaml: str, pipeline_yaml: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@gauntlet.local")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(config_yaml)
    (repo / "pipelines").mkdir()
    (repo / "pipelines" / "mini.yaml").write_text(pipeline_yaml)
    # the engine-managed judge reads policy.yaml at the repo root
    shutil.copy2(REPO_ROOT / "policy.yaml", repo / "policy.yaml")
    (repo / "README.md").write_text("contract fixture\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    _git(repo, "branch", "-M", "main")
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# PRD\n\nReal human-authored PRD.\n")
    return repo


NO_AGENT_CONFIG = """
base_branch: main
run_root: runs
agents: {}
"""

SHELL_COMMIT_PIPELINE = """
name: mini
version: 1
stages:
  - id: phase
    steps:
      - {id: make, type: shell, run: "true"}
      - {id: write, type: shell, run: "/bin/sh -c 'echo work > artifact.txt'"}
      - {id: commit, type: commit, message: "P1: engine-managed judge run\\n\\nbody."}
"""


def test_engine_manages_judge_lifecycle_around_a_run(tmp_path):
    """The judge starts before the run and stops after; env is per-run."""
    repo = _fixture_repo(tmp_path, NO_AGENT_CONFIG, SHELL_COMMIT_PIPELINE)
    assert TOKEN_ENV_VAR not in os.environ  # clean precondition
    mgr = RunManager(repo)
    status = mgr.start("demo", repo / "pipelines" / "mini.yaml", use_judge=True)
    assert status == M.RUN_DONE
    # the judge env is torn down after the run (no leakage) — every managed var,
    # including the per-step GAUNTLET_STEP_ID set by the orchestrator (F-009)
    for var in _MANAGED_ENV_VARS:
        assert var not in os.environ, f"{var} leaked into the parent env"
    assert gitops.commit_subject(repo, "HEAD") == "P1: engine-managed judge run"
    assert (repo / "artifact.txt").read_text().strip() == "work"


CLAUDE_CONFIG = """
base_branch: main
run_root: runs
agents:
  builder:
    adapter: claude-code
    model: haiku
    permission_mode: acceptEdits
    allowed_tools: [Bash, Read]
    # claude loads the repo's PreToolUse hook only with project setting sources
    # (pins.yaml); without it the engine-managed judge cannot gate the agent.
    base_flags: ["--setting-sources", "project"]
"""

# The agent runs a bare `echo` — a deterministic policy fast-path ALLOW (Bash
# read-inspect group) — so the tool call really routes through the live
# PreToolUse hook -> judge and is audited as an allow, with no dependence on the
# LLM classifier rung. (An in-repo *write*, e.g. `echo ... > f`, deliberately
# escalates to the classifier and is not fast-path allowed; policy.yaml is a
# governed file we do not tune for a test — see BOOTSTRAP-NOTES #16.) The shell
# step then produces the committed change, exercising agent_task -> shell ->
# commit end to end with the judge live.
CLAUDE_PIPELINE = """
name: mini
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: "Run exactly this one shell command using the Bash tool and report its output, then stop: echo GAUNTLET_AGENT_RAN"}
      - {id: prepare, type: shell, run: "/bin/sh -c 'echo gauntlet > hello.txt'"}
      - {id: tests, type: shell, run: "grep -q gauntlet hello.txt"}
      - {id: commit, type: commit, message: "P1: pipeline writes hello\\n\\nagent_task->shell->commit through the live judge."}
"""


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_real_pipeline_agent_shell_commit_with_live_judge(tmp_path):
    repo = _fixture_repo(tmp_path, CLAUDE_CONFIG, CLAUDE_PIPELINE)
    # wire the PreToolUse hook into the fixture repo (init does this in P6)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": HOOK_BIN, "timeout": 15}
                            ],
                        }
                    ]
                }
            }
        )
    )
    mgr = RunManager(repo)
    status = mgr.start(
        "demo", repo / "pipelines" / "mini.yaml", use_judge=True,
        extra_context={},
    )
    assert status == M.RUN_DONE, mgr.status("demo").model_dump()
    assert (repo / "hello.txt").exists()
    assert gitops.commit_subject(repo, "HEAD") == "P1: pipeline writes hello"
    # the judge gated the agent's tool call live and allowed the benign echo
    audit = mgr.layout("demo").active_run_dir() / "judge-audit.jsonl"
    assert audit.exists()
    decisions = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    echos = [d for d in decisions if "GAUNTLET_AGENT_RAN" in str(d["tool_input"])]
    assert echos and echos[0]["decision"] == "allow"
    assert echos[0]["source"] == "fast-path"

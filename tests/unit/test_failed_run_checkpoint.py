"""A run that ends ``failed`` publishes its state to the run branch (#169).

Before this, the tracked ``manifest.json`` only advanced at engine checkpoints,
so a run that failed after its last one kept a committed record saying
``running`` — and every other checkout classified the finished run as
``orphaned`` with ``resume`` as its next action. The retrospective's fail-closed
synthesis is the failing step here because it is the one #169 hit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline
from gauntlet.engine.run import RunManager

from test_retro import BASE_CONFIG, _boom_adapters, _retro_repo

MANIFEST_REL = "runs/demo/run-1/manifest.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout


def _pipeline() -> Pipeline:
    return Pipeline.model_validate({"name": "demo", "version": 1, "stages": [
        {"id": "retro", "steps": [{
            "id": "retrospective", "type": "retrospective",
            "agents": ["builder", "reviewer"], "proposer": "triage",
        }]},
    ]})


def _drive_failing_run(repo: Path) -> Manifest:
    run_dir = repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    man = Manifest(run_id="run-1", slug="demo", branch="gauntlet/demo",
                   base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    adapters = _boom_adapters()
    orch = Orchestrator(
        repo_root=repo, run_dir=run_dir, artifact_root=repo,
        config=RunConfig.model_validate(BASE_CONFIG), pipeline=_pipeline(),
        manifest=man, adapter_factory=lambda n: adapters[n],
    )
    assert orch.drive() == M.RUN_FAILED
    return man


def test_failed_run_commits_its_state_on_the_run_branch(tmp_path: Path):
    repo = _retro_repo(tmp_path)
    _git(repo, "checkout", "-qb", "gauntlet/demo")
    man = _drive_failing_run(repo)

    assert _git(repo, "log", "-1", "--format=%s").strip() == (
        "gauntlet: run failed at retrospective"
    )
    committed = json.loads(_git(repo, "show", f"HEAD:{MANIFEST_REL}"))
    assert committed["status"] == M.RUN_FAILED
    assert committed["current_step"] == "retrospective"
    assert _git(repo, "show", "HEAD:runs/demo/run-1/RUN.md")
    # engine identity, bookkeeping paths only
    assert "gauntlet" in _git(repo, "log", "-1", "--format=%an").lower()
    assert set(_git(repo, "show", "--name-only", "--format=", "HEAD").split()) == {
        MANIFEST_REL, "runs/demo/run-1/RUN.md",
    }
    assert not man.warnings


def test_failed_run_state_lands_on_branch_without_touching_other_checkout(
    tmp_path: Path,
):
    """Same-tree approve/reject can drive while the operator's checkout is on
    another branch: the record goes onto the run branch through the object
    database, and the operator's HEAD stays where it was."""
    repo = _retro_repo(tmp_path)
    _git(repo, "branch", "gauntlet/demo")
    head_before = _git(repo, "rev-parse", "HEAD").strip()
    _drive_failing_run(repo)

    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert _git(repo, "rev-parse", "HEAD").strip() == head_before
    committed = json.loads(_git(repo, "show", f"gauntlet/demo:{MANIFEST_REL}"))
    assert committed["status"] == M.RUN_FAILED


def test_failed_run_publication_fault_is_a_warning_not_a_crash(tmp_path: Path):
    """No run branch to publish to: the run still lands `failed` locally and
    the missed publication is recorded, never raised."""
    repo = _retro_repo(tmp_path)
    man = _drive_failing_run(repo)
    assert man.status == M.RUN_FAILED
    assert any("not committed to the run branch" in w for w in man.warnings)


# --- the resume guidance names the retrospective's exit ----------------------
def test_terminal_retrospective_failure_points_at_response(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".gauntlet").mkdir(parents=True)
    (repo / ".gauntlet" / "config.yaml").write_text(
        "base_branch: main\nrun_root: runs\nagents:\n  builder: {adapter: claude-code}\n"
    )
    _git(tmp_path, "init", "-q", str(repo))
    mgr = RunManager(repo)
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   status=M.RUN_FAILED,
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    man.steps.append(M.StepRecord(
        id="retrospective", type="retrospective", status=M.FAILED, attempts=1,
        notes="proposal synthesis FAILED",
    ))
    with pytest.raises(ValueError) as exc:
        mgr._plan_response_action(man, None, _pipeline())
    msg = str(exc.value)
    assert 'gauntlet resume demo --response "<note>"' in msg
    assert "abort" not in msg

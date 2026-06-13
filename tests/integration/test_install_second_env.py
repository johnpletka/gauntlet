"""Second-environment install test (P6, FR-1 acceptance).

Validates the ≤ 3-command onboarding (`install` → `doctor` → `run`) in a clean,
throwaway `uv` environment — never touching system/global tooling (the plan's
ground rule: container/system tooling only with human sign-off; a disposable
`uv venv` is the default). Builds a wheel from this repo, installs it into an
isolated venv, and exercises the installed console script.

Marked `integration`: it shells out to `uv`, builds a wheel, and is excluded
from the default `uv run pytest`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration

# A minimal, human-authored toy PRD: enough to clear the entry contract
# (FR-10.1) so `gauntlet run` proceeds past the stub check into the pipeline.
TOY_PRD = """# PRD: Toy adder

## Problem statement
We want a tiny library function that adds two integers, to exercise the
Gauntlet onboarding path end to end.

## Requirements
- FR-1: `add(a, b)` returns the integer sum of `a` and `b`.
- FR-2: the function is covered by a unit test.
"""


def _uv(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.fixture(scope="module")
def installed_env(tmp_path_factory):
    """A throwaway venv with gauntlet installed from a freshly built wheel."""
    base = tmp_path_factory.mktemp("install")
    dist = base / "dist"

    build = _uv("build", "--wheel", "--out-dir", str(dist), cwd=REPO)
    assert build.returncode == 0, build.stderr
    wheels = list(dist.glob("gauntlet-*.whl"))
    assert wheels, "no wheel produced"

    venv = base / "venv"
    made = _uv("venv", str(venv))
    assert made.returncode == 0, made.stderr

    install = _uv("pip", "install", "--python", str(venv / "bin" / "python"), str(wheels[0]))
    assert install.returncode == 0, install.stderr

    return venv / "bin" / "gauntlet"


def _run(
    gauntlet: Path,
    *args: str,
    cwd: Path | None = None,
    env: dict | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(gauntlet), *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_console_script_installed(installed_env):
    out = _run(installed_env, "version")
    assert out.returncode == 0, out.stderr
    assert "gauntlet" in out.stdout


def test_init_scaffolds_a_runnable_repo(installed_env, tmp_path):
    # Command 1 of 3 (after install): scaffold the workflow into a fresh repo.
    out = _run(installed_env, "init", cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    for rel in (".gauntlet/config.yaml", "pipelines/standard.yaml", "policy.yaml",
                ".claude/settings.json", ".codex/hooks.json", ".gitignore"):
        assert (tmp_path / rel).exists(), rel

    # The scaffolded config + pipeline load and validate with the installed code.
    from gauntlet.engine.config import RunConfig
    from gauntlet.engine.pipeline import load_pipeline
    from gauntlet.engine.validate import validate_pipeline

    config = RunConfig.load(tmp_path / ".gauntlet/config.yaml")
    pipeline, _ = load_pipeline(tmp_path / "pipelines/standard.yaml")
    assert validate_pipeline(pipeline, config).ok()


def test_doctor_runs_and_reports_checks(installed_env, tmp_path):
    # Command 2 of 3: doctor validates the scaffolded environment. Exit code may
    # be non-zero in a credential-less CI box (that is doctor doing its job); we
    # assert it runs and emits the expected actionable checks.
    _run(installed_env, "init", cwd=tmp_path)
    out = _run(installed_env, "doctor", cwd=tmp_path)
    combined = out.stdout + out.stderr
    for name in ("gauntlet:", "claude", "codex", "claude-hook", "judge", "api-keys"):
        assert name in combined, f"missing check {name!r} in doctor output"


def test_run_starts_the_default_pipeline(installed_env, tmp_path):
    """Command 3 of 3: the installed default workflow actually starts (FR-1 / F-002).

    The earlier tests prove install + init + a loadable scaffold; this proves the
    last leg of clone→running-pipeline. A credential-less box cannot *complete* a
    run (the first step calls the reviewer CLI), so we force that first agent call
    to fail fast and free by running with a PATH that omits the agent CLIs, and
    assert the orchestrator was reached: a snapshotted run directory is written by
    `start()` only after the entry contract, pipeline load/validate, and branch
    creation all succeed.
    """
    repo = tmp_path / "proj"
    repo.mkdir()

    # A clean repo carrying the committed Gauntlet assets.
    assert _run(installed_env, "init", cwd=repo).returncode == 0
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    _git(repo, "config", "user.email", "toy@example.com")
    _git(repo, "config", "user.name", "Toy Tester")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "scaffold gauntlet assets")

    # `gauntlet new` scaffolds a stub PRD; the entry contract (FR-10.1) must
    # refuse to start a run on it — proving the run command is wired end to end.
    _run(installed_env, "new", "toy", cwd=repo)
    refused = _run(installed_env, "run", "toy", "--no-judge", cwd=repo)
    assert refused.returncode != 0
    assert "stub" in (refused.stdout + refused.stderr).lower()

    # A real, human-authored PRD clears the entry contract. The run then starts
    # the default pipeline; force the first agent step to fail fast (no agent CLI
    # on PATH, so no live call / cost) and confirm the orchestrator was reached.
    (repo / "runs/toy/prd.md").write_text(TOY_PRD)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "author toy PRD")

    git_dir = str(Path(shutil.which("git")).parent)
    no_agents = {**os.environ, "PATH": os.pathsep.join([git_dir, "/usr/bin", "/bin"])}
    try:
        _run(installed_env, "run", "toy", "--no-judge", cwd=repo,
             env=no_agents, timeout=120)
    except subprocess.TimeoutExpired:
        pass  # the run started; an agent CLI was reachable and we let it run long

    run_dirs = list((repo / "runs/toy").glob("run-*"))
    assert run_dirs, "no run dir created — the default pipeline never started"
    assert (run_dirs[0] / "pipeline.yaml").exists()
    snapshot = (run_dirs[0] / "pipeline.yaml").read_text()
    assert "adversarial_cycle" in snapshot  # the scaffolded standard pipeline

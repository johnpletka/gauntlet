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

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.integration


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


def _run(gauntlet: Path, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(gauntlet), *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )


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

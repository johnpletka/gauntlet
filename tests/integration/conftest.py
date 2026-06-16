"""Shared fixtures for the contract suite.

Plan P1 test constraints (review F-002 — the judge does not exist yet, so
these are the compensating control):
- smoke prompts are tool-less text round-trips;
- codex runs `--sandbox read-only` and claude runs with no tools allowed,
  except where a write-mode flag is itself under test;
- write-mode tests run only in disposable fixture repos under a temp dir,
  never against this repo.
"""

import os
import subprocess

import pytest

from gauntlet.engine.judgeproc import _MANAGED_ENV_VARS


@pytest.fixture(autouse=True)
def _sanitize_gauntlet_env():
    """Clear engine-managed GAUNTLET_* vars for each integration test, restoring
    them after.

    The suite assumes a clean process environment: several tests assert
    `GAUNTLET_JUDGE_TOKEN not in os.environ` as a precondition, and the
    engine/hook key judge gating on GAUNTLET_RUN_ID. An operator who exports
    GAUNTLET_JUDGE_TOKEN (or other GAUNTLET_* vars) globally — e.g. in
    ~/.zshenv — would otherwise leak them into os.environ and trip those
    preconditions or alter behavior. Tests that need these vars set them
    explicitly on the subprocess env they build, so clearing the inherited
    values here is safe.
    """
    saved = {v: os.environ.pop(v, None) for v in _MANAGED_ENV_VARS}
    try:
        yield
    finally:
        for v, val in saved.items():
            if val is None:
                os.environ.pop(v, None)
            else:
                os.environ[v] = val


@pytest.fixture
def fixture_repo(tmp_path):
    """Disposable git repo under a temp dir for write-mode flag tests."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text("disposable fixture repo for gauntlet P1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gauntlet Test",
            "-c",
            "user.email=test@gauntlet.local",
            "commit",
            "-qm",
            "init",
        ],
        cwd=repo,
        check=True,
    )
    return repo

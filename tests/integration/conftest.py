"""Shared fixtures for the contract suite.

Plan P1 test constraints (review F-002 — the judge does not exist yet, so
these are the compensating control):
- smoke prompts are tool-less text round-trips;
- codex runs `--sandbox read-only` and claude runs with no tools allowed,
  except where a write-mode flag is itself under test;
- write-mode tests run only in disposable fixture repos under a temp dir,
  never against this repo.
"""

import subprocess

import pytest


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

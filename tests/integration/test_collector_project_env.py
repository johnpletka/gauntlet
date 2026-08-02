"""Disposable collector execution against a self-contained project command.

This is deliberately independent of live model convergence. It proves that a
project can supply pytest explicitly through its launcher, that collection in a
detached disposable worktree resolves that environment, and that no ambient
Gauntlet virtualenv is needed or modified.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import collectors, verify
from gauntlet.engine.config import RunConfig

pytestmark = pytest.mark.integration


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_disposable_collection_uses_explicit_project_pytest(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Collector Fixture")
    _git(repo, "config", "user.email", "collector@gauntlet.local")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "pyproject.toml").write_text(
        "[project]\n"
        "name = 'collector-fixture'\n"
        "version = '0.0.0'\n"
        "requires-python = '>=3.10'\n"
    )
    (repo / "test_sample.py").write_text(
        "def test_disposable_node():\n"
        "    assert True\n"
    )
    (repo / ".gitignore").write_text(".venv/\n.pytest_cache/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed collector fixture")

    config = RunConfig.model_validate(
        {"test_command": "uv run --with pytest pytest -q"}
    )
    collector = collectors.get_collector("pytest")
    command = collectors.resolve_command(collector, config)
    copy = verify.make_disposable_copy(repo, parent_dir=tmp_path)
    try:
        ids = collector.enumerate(
            worktree=copy.path,
            judge_env={},
            command=command,
        )
    finally:
        verify.discard_disposable_copy(repo, copy)

    assert ids == {"test_sample.py::test_disposable_node"}
    assert not (repo / ".venv").exists()

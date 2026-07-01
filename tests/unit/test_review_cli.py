"""P2 — `gauntlet review` CLI wiring (exit codes + happy-path echo).

The command is a thin adapter over :class:`ReviewLifecycle` (exercised
exhaustively offline in ``test_review_lifecycle.py``). These tests pin the CLI
contract itself: usage errors map to exit 2, fail-closed halts to exit 1, and a
clean resolve exits 0 and echoes the resolved boundary. Under ``CliRunner`` no
TTY is attached, so a manual ``-m`` intent needs ``--approved-intent`` (the
non-interactive FR-2.5 ratification form).
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from gauntlet.cli import app
from gauntlet.engine import gitops

from conftest import git

runner = CliRunner()

CONFIG = "base_branch: main\nrun_root: runs\n"


def _repo_with_fix(fixture_repo: Path) -> None:
    (fixture_repo / ".gauntlet").mkdir(exist_ok=True)
    (fixture_repo / ".gauntlet" / "config.yaml").write_text(CONFIG)
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "add config")
    git(fixture_repo, "checkout", "-q", "-b", "fix")
    (fixture_repo / "fix.py").write_text("print('fixed')\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "the fix")


def _env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GAUNTLET_USER_EMAIL", "john.pletka@gmail.com")


def test_pr_with_positional_is_usage_error(fixture_repo, tmp_path, monkeypatch):
    _repo_with_fix(fixture_repo)
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(fixture_repo)
    result = runner.invoke(app, ["review", "fix", "--pr", "123"])
    assert result.exit_code == 2


def test_rounds_over_limit_is_usage_error(fixture_repo, tmp_path, monkeypatch):
    _repo_with_fix(fixture_repo)
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(fixture_repo)
    result = runner.invoke(app, ["review", "fix", "--code-only", "--rounds", "11"])
    # Usage-class error => exit 2 (the message content is asserted at the
    # lifecycle level; CliRunner routes err=True output to stderr, not stdout).
    assert result.exit_code == 2


def test_message_without_approval_fails_closed_exit_1(
    fixture_repo, tmp_path, monkeypatch
):
    _repo_with_fix(fixture_repo)
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(fixture_repo)
    # Non-interactive (CliRunner), non-independent intent, no --approved-intent.
    result = runner.invoke(app, ["review", "fix", "-m", "the widget crashes"])
    assert result.exit_code == 1


def test_happy_path_resolves_and_echoes_boundary(fixture_repo, tmp_path, monkeypatch):
    _repo_with_fix(fixture_repo)
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(fixture_repo)
    result = runner.invoke(
        app, ["review", "fix", "-m", "the widget crashes", "--approved-intent"]
    )
    assert result.exit_code == 0, result.stdout
    assert "review resolved for branch 'fix'" in result.stdout
    assert "pre-cycle boundary" in result.stdout
    # Zero repo footprint: only the committed config/fix touched the repo.
    assert git(fixture_repo, "status", "--porcelain").strip() == ""


def test_code_only_resolves(fixture_repo, tmp_path, monkeypatch):
    _repo_with_fix(fixture_repo)
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(fixture_repo)
    result = runner.invoke(app, ["review", "fix", "--code-only"])
    assert result.exit_code == 0, result.stdout
    assert "code-only" in result.stdout

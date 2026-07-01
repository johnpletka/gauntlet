"""`gauntlet review` CLI wiring (exit codes + fail-closed dispatch).

The command is a thin adapter over :class:`ReviewLifecycle` (resolution,
exercised offline in ``test_review_lifecycle.py``) and the P3 cycle driver
(`drive_review`, exercised offline in ``test_review_cycle.py``). These tests pin
the CLI contract itself: usage errors map to exit 2 and fail-closed halts to
exit 1. Under ``CliRunner`` no TTY is attached, so a manual ``-m`` intent needs
``--approved-intent`` (the non-interactive FR-2.5 ratification form).

A *successful* end-to-end drive spawns the run-scoped judge and the real
reviewer/triager/fixer adapters, so it is integration-only; the offline cycle
execution + summary is covered at the ``drive_review`` layer. Here we assert the
CLI dispatches into the drive and fails closed cleanly (a clean exit 1 with
guidance, never a raw traceback) when the environment can't support it — e.g.
the review pipeline asset is absent (an un-``init``-ed repo).
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


def test_drive_fails_closed_when_pipeline_asset_missing(
    fixture_repo, tmp_path, monkeypatch
):
    # The command now resolves then DRIVES the cycle (P3). `_repo_with_fix` is an
    # un-init-ed repo (no pipelines/review.yaml), so the drive must fail closed
    # with actionable guidance and a clean exit 1 — never a raw traceback, and
    # never the old resolve-and-stop echo. The intent resolves fine first
    # (approved), so this exercises the drive dispatch specifically.
    _repo_with_fix(fixture_repo)
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(fixture_repo)
    result = runner.invoke(
        app, ["review", "fix", "-m", "the widget crashes", "--approved-intent"]
    )
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "review pipeline asset" in result.output
    # Zero repo footprint even on the fail-closed path.
    assert git(fixture_repo, "status", "--porcelain").strip() == ""


def test_code_only_drives_and_fails_closed_without_assets(
    fixture_repo, tmp_path, monkeypatch
):
    # --code-only needs no intent, so it too reaches the drive and fails closed on
    # the absent pipeline asset (clean exit 1), confirming the dispatch is
    # independent of intent resolution.
    _repo_with_fix(fixture_repo)
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(fixture_repo)
    result = runner.invoke(app, ["review", "fix", "--code-only"])
    assert result.exit_code == 1
    assert "review pipeline asset" in result.output

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

import os
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


def test_resume_routes_a_parked_review_to_review_resume(
    fixture_repo, tmp_path, monkeypatch
):
    # A lightweight `gauntlet review` run keeps its state out-of-repo, not in
    # run_root, so the heavyweight RunManager.resume can't find it. The PRD's
    # documented recovery for a parked review is `gauntlet resume --response`
    # (FR-3.2), so the generic `resume` command must locate the review run by slug
    # and dispatch to the review resume path. resume_review is stubbed so the
    # routing is asserted without spawning real agents/judge.
    from gauntlet.engine import manifest as M
    from gauntlet.engine import review as review_mod
    from gauntlet.engine.config import RunConfig

    _repo_with_fix(fixture_repo)
    _env(monkeypatch, tmp_path)
    monkeypatch.chdir(fixture_repo)

    cfg = RunConfig.load(fixture_repo / ".gauntlet/config.yaml")
    slug = review_mod.review_slug("fix")
    repo_id = review_mod.derive_repo_id(fixture_repo)
    state_dir = review_mod.resolve_state_dir(
        fixture_repo, cfg, repo_id=repo_id, slug=slug, environ=os.environ,
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    # A minimal *bound, parked* review run at that state dir (load_review_run
    # requires manifest + pipeline.yaml present, a non-empty pipeline hash, and a
    # non-terminal status).
    man = M.Manifest(
        run_id=slug, slug=slug, branch="fix", base_branch="main",
        pipeline=M.PipelineRef(name="review", version=1, hash="deadbeef"),
        status=M.RUN_PARKED,
        steps=[M.StepRecord(id="review-cycle", type="adversarial_cycle",
                            status=M.PARKED,
                            parked_reason=M.PARKED_REASON_CYCLE_ESCALATION)],
        intent=M.IntentRecord(source=M.INTENT_SOURCE_MESSAGE,
                              provenance=M.PROVENANCE_AUTHOR_SESSION_SUMMARY,
                              independent=False),
    )
    man.write_atomic(state_dir / "manifest.json")
    (state_dir / "pipeline.yaml").write_text("name: review\nversion: 1\n")

    calls: dict = {}

    def fake_resume(repo_root, config, sdir, *, response=None, use_judge=True, **kw):
        calls["state_dir"] = sdir
        calls["response"] = response
        return review_mod.ReviewOutcome(
            status=M.RUN_DONE, parked=False, commits=[],
            summary=review_mod.ReviewSummary(residual_risk=[], declined=[]),
            state_dir=sdir, cycle_notes="",
        )

    monkeypatch.setattr(review_mod, "resume_review", fake_resume)

    result = runner.invoke(app, ["resume", slug, "--response", "ok, ship it"])
    assert result.exit_code == 0, result.output
    # Routed to the review resume path with the review's out-of-repo state dir.
    assert calls["state_dir"] == state_dir
    assert calls["response"] == "ok, ship it"
    assert "review done" in result.output

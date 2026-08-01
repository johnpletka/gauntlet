"""`resume` never ends on a bare "run status: parked" (#65): a park that did
zero agent work surfaces the interrupted step's dirty verdict from its notes."""

from __future__ import annotations

from typer.testing import CliRunner

from gauntlet.cli import app
from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.run import RunManager

runner = CliRunner()

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

_NOTES = (
    "interrupted mid-edit: worktree dirty vs base SHA deadbeef00; parked for a "
    "human (F-003, interrupted_step=park). Dirty verdict — commits in "
    "deadbeef00..cafebabe11:\nabc1234 Builder <b@g.local> — P2 wip: arm the thing"
)


def _seed_parked_run(root) -> None:
    (root / ".gauntlet").mkdir()
    (root / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    slug_dir = root / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    man = Manifest(
        run_id="run-1",
        slug="demo",
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_PARKED,
        steps=[StepRecord(
            id="implement", type="agent_task", status=M.INTERRUPTED,
            halt_reason=M.HALT_REASON_SIGNAL_KILL, notes=_NOTES,
        )],
    )
    man.write_atomic(run_dir / "manifest.json")
    (slug_dir / "active-run.txt").write_text("run-1")


def test_parked_resume_prints_the_dirty_verdict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_parked_run(tmp_path)
    monkeypatch.setattr(RunManager, "resume", lambda self, slug, **kw: M.RUN_PARKED)
    result = runner.invoke(app, ["resume", "demo"])
    assert result.exit_code == 0
    # Status AND its explanation travel together on STDOUT (PR #76 review):
    # the park is an outcome, not an error, and asserting the stdout stream
    # specifically keeps this contract stable across click versions (whose
    # CliRunner has changed how `.output` mixes stderr).
    assert "run status: parked" in result.stdout
    # The WHY travels with the status — never a bare park (#65).
    assert "interrupted mid-edit" in result.stdout
    assert "P2 wip: arm the thing" in result.stdout


def test_reset_interrupted_flag_reaches_the_manager(tmp_path, monkeypatch):
    # #72: the CLI flag is plumbing, not policy — assert it arrives verbatim.
    monkeypatch.chdir(tmp_path)
    _seed_parked_run(tmp_path)
    seen: dict = {}

    def fake_resume(self, slug, **kw):
        seen.update(kw, slug=slug)
        return M.RUN_DONE

    monkeypatch.setattr(RunManager, "resume", fake_resume)
    result = runner.invoke(app, ["resume", "demo", "--reset-interrupted"])
    assert result.exit_code == 0
    assert seen["slug"] == "demo"
    assert seen["reset_interrupted"] is True


def test_non_parked_resume_stays_quiet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _seed_parked_run(tmp_path)
    monkeypatch.setattr(RunManager, "resume", lambda self, slug, **kw: M.RUN_DONE)
    result = runner.invoke(app, ["resume", "demo"])
    assert result.exit_code == 0
    assert "run status: done" in result.stdout
    assert "interrupted mid-edit" not in result.output

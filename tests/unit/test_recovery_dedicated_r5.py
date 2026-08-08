"""#90 regression: R5 fires through a DEDICATED-worktree run, CLI-visible.

The live shape that regressed while every manager-layer pin stayed green — a
§10.8 matrix run (adopter layout, ``run_root: .gauntlet/runs``, dedicated
worktree, prior ``--response`` history) parked ``artifact_invalid`` and two
consecutive unchanged ``gauntlet resume`` invocations exited 0. Two engine
defects composed into that silence:

* the fingerprint was computed against the OPERATOR's checkout (capture runs
  outside ``_worktree_paths_or_park``), whose planes the A1 invariance pins
  byte-identical across a dedicated drive — and whose excludes mis-rooted
  against the adopter run_root could throw, silently disabling the guard;
* each resume's verb-own engine bookkeeping commit (``gauntlet: response …``)
  moved the raw run-branch SHA, so the digest read as "progress" every time.

These tests pin the CLI exit code through the dedicated layout — the exact
combination the manager-layer P5 pins missed.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from conftest import git

from gauntlet.cli import app
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine import recovery_exec as RX
from gauntlet.engine import worktree as WT
from gauntlet.engine.manifest import (
    HumanResponse,
    Manifest,
    PipelineRef,
    StepRecord,
)
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.run import RunManager

runner = CliRunner()

CONFIG = """
base_branch: main
run_root: .gauntlet/runs
interrupted_step: park
agents:
  builder: {adapter: claude-code}
"""

FOREACH_PIPELINE = """
name: demo
version: 1
stages:
  - id: phases
    foreach: plan.phases
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""

# The literal incident shape: a gauntlet-phases block that is not valid YAML.
BAD_PLAN = "# Plan\n\n```gauntlet-phases\n- id: P1\n  title: [broken\n```\n"


def _seed_dedicated(repo: Path) -> tuple[RunManager, Path]:
    """An adopter-layout run in a real dedicated worktree, with a consumed
    ``--response`` already on the record (the bookkeeping-commit trigger)."""
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed config")
    # The run branch exists but is NOT checked out in the operator's tree —
    # that is what makes the run dedicated rather than same_tree.
    git(repo, "branch", "gauntlet/demo")

    slug_dir = repo / ".gauntlet" / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")
    (run_dir / "pipeline.yaml").write_text(FOREACH_PIPELINE)
    (slug_dir / ".gitignore").write_text(".gitignore\nactive-run.txt\n")
    (slug_dir / "active-run.txt").write_text("run-1")
    (slug_dir / "plan.md").write_text(BAD_PLAN)
    _, phash = load_pipeline(run_dir / "pipeline.yaml")
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash=phash),
        status=M.RUN_RUNNING, worktree_mode=WT.MODE_DEDICATED,
        steps=[StepRecord(
            id="implement", type="agent_task",
            human_responses=[HumanResponse(
                response_id="implement-resp-1", response_text="carry on",
                timestamp="2026-08-08T00:00:00+00:00", user="op@example.com",
                response_attempt=1, state="consumed",
            )],
        )],
    )
    man.write_atomic(run_dir / "manifest.json")
    WT.ensure(repo, repo, slug="demo", run_id="run-1", branch="gauntlet/demo")
    return RunManager(repo), run_dir


def test_unchanged_artifact_invalid_repeat_exits_nonzero_dedicated_cli(
    fixture_repo, monkeypatch
):
    mgr, run_dir = _seed_dedicated(fixture_repo)

    first = mgr.resume("demo", use_judge=False)
    assert first == M.RUN_PARKED
    parked = Manifest.load(run_dir / "manifest.json")
    assert parked.status == M.RUN_PARKED
    rec = parked.record("implement")
    assert rec is not None
    assert rec.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID
    # The run genuinely resolved dedicated: its branch lives in a registered
    # run worktree, not the operator's checkout (the axis #90's pins missed).
    entry = WT.observe(fixture_repo, "gauntlet/demo",
                       main_root=fixture_repo)
    assert entry is not None

    # Without touching plan.md: the CLI repeat must exit NONZERO naming the
    # unchanged fingerprint — never `run status: parked` + exit 0 twice in a
    # row (the incident transcript).
    monkeypatch.chdir(fixture_repo)
    for attempt in (1, 2):
        result = runner.invoke(app, ["resume", "demo", "--no-judge"])
        assert result.exit_code != 0, (
            f"unchanged repeat #{attempt} exited 0:\n{result.output}"
        )
        assert "sha256:" in result.output

    # The park's own recommended recovery still works: hand-fix + plain resume.
    (fixture_repo / ".gauntlet" / "runs" / "demo" / "plan.md").write_text(
        "# Plan\n\n## P1 — Build it\nBuild.\n\n"
        "```gauntlet-phases\n"
        "- id: P1\n  title: Build it\n  goal: Build.\n"
        "  acceptance:\n    - id: P1-A1\n      clause: Builds.\n"
        "```\n"
    )
    from conftest import FakeAdapter

    adapter = FakeAdapter(writes={"out.py": "done\n"})
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_DONE


def test_bookkeeping_commits_do_not_read_as_progress(fixture_repo):
    """The git plane of the R5 fingerprint skips engine bookkeeping commits:
    a `gauntlet: response …` checkpoint landed by the verb itself must not
    move the digest, while a substantive commit must."""
    repo = fixture_repo
    git(repo, "checkout", "-qb", "gauntlet/demo")
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="h"),
        status=M.RUN_PARKED,
    )
    before = RX.build_progress_fingerprint(repo, manifest=man)

    (repo / "noise.txt").write_text("bookkeeping\n")
    git(repo, "add", "-f", "noise.txt")
    git(repo, "commit", "-qm", "gauntlet: response implement-resp-1 consumed")
    after_bookkeeping = RX.build_progress_fingerprint(
        repo, manifest=man, excludes=["noise.txt"]
    )
    assert after_bookkeeping.run_branch_sha == before.run_branch_sha
    assert after_bookkeeping.digest == before.digest

    (repo / "work.py").write_text("real work\n")
    git(repo, "add", "work.py")
    git(repo, "commit", "-qm", "P1.1: apply review fixes")
    after_work = RX.build_progress_fingerprint(
        repo, manifest=man, excludes=["noise.txt"]
    )
    assert after_work.run_branch_sha != before.run_branch_sha
    assert after_work.digest != before.digest

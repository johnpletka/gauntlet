"""Intra-phase checkpoint commits + checkpoint-aware recovery (P9, FR-11.1/11.2).

Covers the git-history contract (the phase always ends on a `P<N>:` commit —
empty marker, residual, or squash), the `checkpoint_commits` config knob, the
recovery rewind-to-latest-checkpoint path, and the implement-phase prompt
instruction.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gauntlet.adapters.base import AdapterCapabilities, AgentResult
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.execution import DONE, StepContext
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline, Step
from gauntlet.engine.steptypes import handle_commit
from gauntlet.logging.redact import RedactingWriter

from conftest import git


def _orch(repo, text, *, config=None, adapters=None):
    cfg = RunConfig.model_validate(
        config or {"agents": {"builder": {"adapter": "claude-code"}}}
    )
    pipeline = Pipeline.model_validate(yaml.safe_load(text))
    ar = repo / "runs" / "demo"
    rd = ar / "run-1"
    man = Manifest(
        run_id="r", slug="demo", branch="b", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="h"),
    )
    return Orchestrator(
        repo_root=repo, run_dir=rd, artifact_root=ar, config=cfg,
        pipeline=pipeline, manifest=man,
        adapter_factory=(lambda n: adapters[n]) if adapters else None,
    )


def _wip(repo, subject: str, rel: str, content: str) -> str:
    (repo / rel).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", subject)
    return gitops.head_sha(repo)


_COMMIT_PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, message: "P9: phase\\n\\nthe body."}
"""


# --- config knob (FR-11.1) ---------------------------------------------------
def test_checkpoint_commits_defaults_to_keep():
    assert RunConfig.model_validate({}).checkpoint_commits == "keep"


def test_checkpoint_commits_rejects_unknown_value():
    with pytest.raises(ValueError, match="checkpoint_commits must be one of"):
        RunConfig.model_validate({"checkpoint_commits": "amend"})


# --- git-history contract: keep, no residual → empty PN: marker --------------
def test_keep_empty_marker_over_checkpoints(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    last_wip = _wip(fixture_repo, "P9 wip: cli wiring", "b.py", "b\n")

    orch = _orch(fixture_repo, _COMMIT_PIPELINE)  # default: keep
    assert orch.drive() == M.RUN_DONE

    head = gitops.head_sha(fixture_repo)
    # Handoff always lands on a P9: commit, never a wip: commit.
    assert gitops.commit_subject(fixture_repo, head) == "P9: phase"
    # It is an empty marker sitting directly on the last wip commit.
    assert gitops.commit_parent(fixture_repo, head) == last_wip
    assert gitops.diff_range_empty(fixture_repo, last_wip, head)
    # Body lists the milestones so the marker summarizes the phase.
    msg = gitops.commit_message(fixture_repo, head)
    assert "P9 wip: model layer" in msg and "P9 wip: cli wiring" in msg
    # The reviewed range base..<PN:> is the cumulative wip diff.
    diff = gitops.range_diff(fixture_repo, base, head)
    assert "a.py" in diff and "b.py" in diff


# --- git-history contract: keep, residual → PN: captures the remainder -------
def test_keep_residual_commit_over_checkpoints(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    last_wip = _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    (fixture_repo / "c.py").write_text("c\n")  # residual, uncommitted

    orch = _orch(fixture_repo, _COMMIT_PIPELINE)  # default: keep
    assert orch.drive() == M.RUN_DONE

    head = gitops.head_sha(fixture_repo)
    assert gitops.commit_subject(fixture_repo, head) == "P9: phase"
    # The PN: commit sits on top of the wip commit (both preserved).
    assert gitops.commit_parent(fixture_repo, head) == last_wip
    assert not gitops.diff_range_empty(fixture_repo, last_wip, head)  # captured c.py
    diff = gitops.range_diff(fixture_repo, base, head)
    assert "a.py" in diff and "c.py" in diff
    assert "P9 wip: model layer" in gitops.commit_message(fixture_repo, head)


# --- git-history contract: squash → one non-empty PN: commit -----------------
def test_squash_collapses_checkpoints_into_one_commit(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    _wip(fixture_repo, "P9 wip: one", "a.py", "a\n")
    _wip(fixture_repo, "P9 wip: two", "b.py", "b\n")
    (fixture_repo / "c.py").write_text("c\n")  # residual folds into the squash

    cfg = {
        "agents": {"builder": {"adapter": "claude-code"}},
        "checkpoint_commits": "squash",
    }
    orch = _orch(fixture_repo, _COMMIT_PIPELINE, config=cfg)
    assert orch.drive() == M.RUN_DONE

    head = gitops.head_sha(fixture_repo)
    assert gitops.commit_subject(fixture_repo, head) == "P9: phase"
    # Exactly one commit since base — the wip commits collapsed.
    log = gitops.log_range(fixture_repo, base, head)
    assert len(log.splitlines()) == 1
    assert gitops.commit_parent(fixture_repo, head) == base
    # Non-empty: it carries every file of the phase.
    diff = gitops.range_diff(fixture_repo, base, head)
    assert "a.py" in diff and "b.py" in diff and "c.py" in diff
    # Body lists the squashed milestones (they are otherwise lost from history).
    msg = gitops.commit_message(fixture_repo, head)
    assert "Squashed checkpoint milestones" in msg
    assert "P9 wip: one" in msg and "P9 wip: two" in msg


# --- FR-11.2 recovery: rewind to the latest checkpoint, not base_sha ----------
class _FileWriter:
    """A re-run builder that writes ONE file (idempotent), leaving others alone."""

    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def __init__(self, rel: str, content: str) -> None:
        self.rel, self.content = rel, content
        self.calls: list[str] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.calls.append(prompt)
        (Path(cwd) / self.rel).write_text(self.content)
        return AgentResult(text="done", session_id="s", exit_code=0)


_RECOVERY_PIPELINE = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, message: "P3: phase\\n\\nthe body."}
"""


def test_recovery_rewinds_to_latest_checkpoint_preserving_milestones(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    # A completed milestone lands as a checkpoint commit...
    wip = _wip(fixture_repo, "P3 wip: model layer", "model.py", "MILESTONE\n")
    # ...then the builder makes further, uncommitted edits and is killed:
    (fixture_repo / "model.py").write_text("DIRTY — mid-edit\n")  # tracked, dirty
    (fixture_repo / "scratch.py").write_text("partial\n")  # untracked, dirty

    cfg = {
        "agents": {"builder": {"adapter": "claude-code"}},
        "interrupted_step": "reset_to_base",
    }
    builder = _FileWriter("feature.py", "RECOVERED\n")
    orch = _orch(
        fixture_repo, _RECOVERY_PIPELINE, config=cfg, adapters={"builder": builder}
    )
    # Simulate the killed mid-edit implement step: RUNNING with a base SHA that
    # predates the checkpoint.
    orch.manifest.upsert(
        StepRecord(id="implement", type="agent_task", agent="builder",
                   status=M.RUNNING, base_sha=base)
    )

    assert orch.drive() == M.RUN_DONE

    # Recovery rewound to the checkpoint, NOT base_sha: the milestone file
    # survived (restored from the wip commit), the mid-edit dirt was discarded.
    assert (fixture_repo / "model.py").read_text() == "MILESTONE\n"
    assert not (fixture_repo / "scratch.py").exists()  # untracked dirt cleaned
    # The re-run produced its own work on top of the preserved milestone.
    assert (fixture_repo / "feature.py").read_text() == "RECOVERED\n"

    rec = orch.manifest.record("implement")
    assert rec.resumed_from_checkpoint == "P3 wip: model layer"
    # The re-run prompt names the checkpoint it resumes from.
    assert builder.calls and "P3 wip: model layer" in builder.calls[0]

    # A pre-rewind recovery snapshot preserves the discarded dirty work (P3).
    refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/recovery/")
    assert "refs/gauntlet/recovery/" in refs

    # The final phase commit builds on the preserved checkpoint.
    head = gitops.head_sha(fixture_repo)
    assert gitops.is_ancestor(fixture_repo, wip, head)
    diff = gitops.range_diff(fixture_repo, base, head)
    assert "model.py" in diff and "feature.py" in diff


_RECOVERY_SQUASH_PIPELINE = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, phase: P3, message: "P3: phase\\n\\nthe body."}
"""


def _files_in_commit(repo, sha) -> list[str]:
    out = gitops._run(repo, "show", "--name-only", "--format=", sha)
    return [line for line in out.splitlines() if line.strip()]


def test_recovery_then_squash_collapses_preserved_checkpoints(fixture_repo):
    """After a checkpoint-preserving recovery leaves an engine bookkeeping commit
    atop the wip, the phase-end SQUASH still collapses the preserved checkpoints
    into one clean `P<N>:` commit with a milestone trailer (review F-002), and no
    engine bookkeeping state pollutes it."""
    base = gitops.head_sha(fixture_repo)
    _wip(fixture_repo, "P3 wip: model layer", "model.py", "MILESTONE\n")
    (fixture_repo / "model.py").write_text("DIRTY — mid-edit\n")  # killed mid-edit

    cfg = {
        "agents": {"builder": {"adapter": "claude-code"}},
        "interrupted_step": "reset_to_base",
        "checkpoint_commits": "squash",
    }
    builder = _FileWriter("feature.py", "RECOVERED\n")
    orch = _orch(
        fixture_repo, _RECOVERY_SQUASH_PIPELINE, config=cfg,
        adapters={"builder": builder},
    )
    orch.manifest.upsert(
        StepRecord(id="implement", type="agent_task", agent="builder",
                   status=M.RUNNING, base_sha=base)
    )
    assert orch.drive() == M.RUN_DONE

    head = gitops.head_sha(fixture_repo)
    assert gitops.commit_subject(fixture_repo, head) == "P3: phase"
    # The squash collapsed EVERYTHING (wip + recovery engine commit) into one
    # commit sitting directly on the phase base — the engine commit is gone.
    log = gitops.log_range(fixture_repo, base, head)
    assert len(log.splitlines()) == 1
    assert gitops.commit_parent(fixture_repo, head) == base
    # Non-empty: it carries the preserved milestone AND the re-run's work.
    diff = gitops.range_diff(fixture_repo, base, head)
    assert "model.py" in diff and "feature.py" in diff
    # The preserved milestone is listed in the body (otherwise lost from history).
    msg = gitops.commit_message(fixture_repo, head)
    assert "Squashed checkpoint milestones" in msg
    assert "P3 wip: model layer" in msg
    # No engine bookkeeping pollutes the collapsed phase commit (review F-002).
    committed = _files_in_commit(fixture_repo, head)
    assert not any("manifest.json" in f or "run-1" in f for f in committed)
    assert set(committed) == {"model.py", "feature.py"}


def _engine_commit(repo, subject: str) -> str:
    """A `gauntlet:` bookkeeping commit that force-tracks a run-dir file, exactly
    as the FR-11.2 rewind / response-checkpoint commits do."""
    rundir = repo / "runs" / "demo" / "run-1"
    rundir.mkdir(parents=True, exist_ok=True)
    (rundir / "manifest.json").write_text("{}\n")
    git(repo, "add", "-f", "runs/demo/run-1/manifest.json")
    git(
        repo, "-c", "user.name=Gauntlet Engine",
        "-c", "user.email=engine@gauntlet.local", "commit", "-qm", subject,
    )
    return gitops.head_sha(repo)


_SQUASH_SPAN_PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, phase: P3, message: "P3: phase\\n\\nthe body."}
"""


def test_squash_spanning_engine_commit_excludes_bookkeeping(fixture_repo):
    """A SQUASH whose range spans an engine bookkeeping commit (as left by a
    checkpoint-preserving recovery) collapses the phase's checkpoints without
    dragging the force-tracked manifest into the phase commit (review F-002)."""
    base = gitops.head_sha(fixture_repo)
    _wip(fixture_repo, "P3 wip: model layer", "model.py", "MILESTONE\n")
    _engine_commit(fixture_repo, "gauntlet: response r pending")  # sits above wip
    (fixture_repo / "feature.py").write_text("RESIDUAL\n")  # re-run residual

    cfg = {
        "agents": {"builder": {"adapter": "claude-code"}},
        "checkpoint_commits": "squash",
    }
    orch = _orch(fixture_repo, _SQUASH_SPAN_PIPELINE, config=cfg)
    assert orch.drive() == M.RUN_DONE

    head = gitops.head_sha(fixture_repo)
    assert gitops.commit_subject(fixture_repo, head) == "P3: phase"
    # One commit on base — the wip AND the intervening engine commit collapsed.
    assert len(gitops.log_range(fixture_repo, base, head).splitlines()) == 1
    assert gitops.commit_parent(fixture_repo, head) == base
    # The phase commit carries only implementation — never the engine manifest.
    committed = _files_in_commit(fixture_repo, head)
    assert set(committed) == {"model.py", "feature.py"}
    assert not any("manifest.json" in f for f in committed)
    # Milestone trailer preserved.
    assert "P3 wip: model layer" in gitops.commit_message(fixture_repo, head)


def test_recovery_then_keep_marker_lists_preserved_milestones(fixture_repo):
    """After recovery, a KEEP-mode phase-end commit still finds the preserved
    checkpoint beneath the engine bookkeeping commit and lists its milestone in
    the `P<N>:` body (review F-002)."""
    base = gitops.head_sha(fixture_repo)
    wip = _wip(fixture_repo, "P3 wip: model layer", "model.py", "MILESTONE\n")
    (fixture_repo / "model.py").write_text("DIRTY — mid-edit\n")

    cfg = {
        "agents": {"builder": {"adapter": "claude-code"}},
        "interrupted_step": "reset_to_base",
        # checkpoint_commits defaults to keep
    }
    builder = _FileWriter("feature.py", "RECOVERED\n")
    orch = _orch(
        fixture_repo, _RECOVERY_PIPELINE, config=cfg, adapters={"builder": builder}
    )
    orch.manifest.upsert(
        StepRecord(id="implement", type="agent_task", agent="builder",
                   status=M.RUNNING, base_sha=base)
    )
    assert orch.drive() == M.RUN_DONE

    head = gitops.head_sha(fixture_repo)
    assert gitops.commit_subject(fixture_repo, head) == "P3: phase"
    assert gitops.is_ancestor(fixture_repo, wip, head)  # milestone preserved
    # The KEEP marker's body lists the milestone found beneath the engine commit.
    assert "P3 wip: model layer" in gitops.commit_message(fixture_repo, head)
    diff = gitops.range_diff(fixture_repo, base, head)
    assert "model.py" in diff and "feature.py" in diff


_WRONG_PHASE_PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, phase: P9, message: "P9: phase\\n\\nthe body."}
"""


def test_commit_step_fails_closed_on_wrong_phase_checkpoint(fixture_repo):
    """A wrong-phase `P<N> wip:` in this phase's trailing run makes the commit
    step fail closed instead of squashing it into the wrong phase (review F-001)."""
    _wip(fixture_repo, "P9 wip: real", "a.py", "a\n")
    _wip(fixture_repo, "P8 wip: mistyped", "b.py", "b\n")  # wrong phase at HEAD

    orch = _orch(fixture_repo, _WRONG_PHASE_PIPELINE)
    assert orch.drive() == M.RUN_FAILED
    rec = orch.manifest.record("commit")
    assert rec.status == M.FAILED
    assert "failed closed" in (rec.notes or "")


def test_recovery_falls_back_to_base_when_no_checkpoint(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    # No checkpoint commit — just a dirty mid-edit tree.
    (fixture_repo / "model.py").write_text("PARTIAL — mid-edit\n")

    cfg = {
        "agents": {"builder": {"adapter": "claude-code"}},
        "interrupted_step": "reset_to_base",
    }
    builder = _FileWriter("feature.py", "RECOVERED\n")
    orch = _orch(
        fixture_repo, _RECOVERY_PIPELINE, config=cfg, adapters={"builder": builder}
    )
    orch.manifest.upsert(
        StepRecord(id="implement", type="agent_task", agent="builder",
                   status=M.RUNNING, base_sha=base)
    )
    assert orch.drive() == M.RUN_DONE

    rec = orch.manifest.record("implement")
    assert rec.resumed_from_checkpoint is None  # fell back to base_sha
    assert not (fixture_repo / "model.py").exists()  # mid-edit discarded to base
    assert (fixture_repo / "feature.py").read_text() == "RECOVERED\n"


# --- FR-11.1 prompt instruction ----------------------------------------------
def test_implement_prompt_instructs_wip_checkpoints():
    prompt = (
        Path(__file__).parents[2] / "prompts" / "implement-phase.md"
    ).read_text()
    assert "P<N> wip:" in prompt
    # It must still keep the final PN: commit as the pipeline's job.
    assert "Do **not** make the final `P<N>:` phase commit" in prompt


# --- #124: reconcile a phase commit reachable from HEAD but behind base_sha ---
def _ctx_for_commit(repo, base_sha, *, config=None):
    """A minimal StepContext for driving handle_commit directly, with an
    explicit record.base_sha (the orchestrator normally sets it to HEAD-at-start;
    here we simulate a base re-anchored past the phase commit by resume adoption)."""
    ar = repo / "runs" / "demo"
    (ar / "run-1").mkdir(parents=True, exist_ok=True)
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    cfg = config or {"agents": {"builder": {"adapter": "claude-code"}}}
    return StepContext(
        repo_root=repo, run_dir=ar / "run-1", artifact_root=ar,
        config=RunConfig.model_validate(cfg),
        pipeline=Pipeline.model_validate({"name": "demo", "version": 1, "stages": []}),
        manifest=man,
        record=StepRecord(id="commit", type="commit", base_sha=base_sha),
        writer=RedactingWriter(),
    )


def test_reconciles_phase_commit_reachable_from_head_behind_base(fixture_repo):
    # #124: an operator committed the phase work as a `P9:` commit (e.g. FR-9.3
    # clean-handoff pre-commit of human evidence) and `resume` ADOPTED it, then
    # layered engine bookkeeping commits on top and re-anchored the commit step's
    # base_sha PAST the P9: commit. HEAD is a bookkeeping commit, base is a later
    # one still, and the worktree is clean — so the HEAD match and a base..HEAD
    # scan both miss the P9: commit. The step must walk back from HEAD (bounded
    # to the run branch's own commits), find the phase-unique P9: commit, and
    # adopt it rather than failing on an empty tree.
    git(fixture_repo, "checkout", "-qb", "b")  # the run branch, off base `main`
    (fixture_repo / "work.py").write_text("phase work\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "P9: the phase\n\nbody")
    phase_commit = gitops.head_sha(fixture_repo)
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: response consumed")
    base_after_phase = gitops.head_sha(fixture_repo)  # base re-anchored here (past P9:)
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: bookkeeping flush")
    head = gitops.head_sha(fixture_repo)
    assert head != base_after_phase and gitops.is_clean(fixture_repo)

    step = Step.model_validate({"id": "commit", "type": "commit", "phase": "P9",
                                "message": "P9: the phase\n\nbody"})
    ctx = _ctx_for_commit(fixture_repo, base_after_phase)
    result = handle_commit(step, ctx)

    assert result.status == DONE
    assert result.commit_sha == phase_commit  # adopted the existing P9: commit
    assert result.commit_phase == "P9"
    assert "reachable from HEAD" in (result.notes or "")
    # No new commit was made — HEAD is unchanged, tree still clean.
    assert gitops.head_sha(fixture_repo) == head
    # The record's base is repaired to the adopted commit's parent, so review
    # consumers diff `base..commit` FORWARD over the phase's changes instead of
    # a reversed/empty range against the re-anchored base.
    assert ctx.record.base_sha == gitops.commit_parent(fixture_repo, phase_commit)


def test_clean_worktree_with_no_phase_commit_still_fails(fixture_repo):
    # The negative control: a genuinely empty phase (no `P9:` commit anywhere)
    # with a clean worktree must STILL fail loud — the #124 reconciliation only
    # adopts a real phase commit, it does not paper over a phase that did nothing.
    git(fixture_repo, "checkout", "-qb", "b")
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: response consumed")
    base = gitops.head_sha(fixture_repo)
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: bookkeeping flush")
    step = Step.model_validate({"id": "commit", "type": "commit", "phase": "P9",
                                "message": "P9: the phase\n\nbody"})
    result = handle_commit(step, _ctx_for_commit(fixture_repo, base))
    assert result.status == M.FAILED
    assert "nothing to commit" in (result.notes or "")


def test_foreign_phase_commit_in_base_history_is_not_adopted(fixture_repo):
    # A previous run/PRD left a `P9:` commit in the BASE branch's history. The
    # current phase produced nothing: the walk must not cross the branch point
    # and adopt that foreign commit as this phase's deliverable — the loud
    # FAILED is the correct outcome.
    (fixture_repo / "old.py").write_text("previous run's work\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "P9: an earlier run's phase")
    git(fixture_repo, "checkout", "-qb", "b")  # run branch forks AFTER old P9:
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: response consumed")
    base = gitops.head_sha(fixture_repo)
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: bookkeeping flush")
    step = Step.model_validate({"id": "commit", "type": "commit", "phase": "P9",
                                "message": "P9: the phase\n\nbody"})
    result = handle_commit(step, _ctx_for_commit(fixture_repo, base))
    assert result.status == M.FAILED
    assert "nothing to commit" in (result.notes or "")


def test_missing_base_branch_fails_closed_not_adopting(fixture_repo):
    # If the walk cannot be bounded (the base branch ref is gone), adoption is
    # skipped entirely — an unbounded walk is never an acceptable fallback.
    git(fixture_repo, "checkout", "-qb", "b")
    (fixture_repo / "work.py").write_text("phase work\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "P9: the phase\n\nbody")
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: response consumed")
    base = gitops.head_sha(fixture_repo)
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: bookkeeping flush")
    git(fixture_repo, "branch", "-qD", "main")
    step = Step.model_validate({"id": "commit", "type": "commit", "phase": "P9",
                                "message": "P9: the phase\n\nbody"})
    result = handle_commit(step, _ctx_for_commit(fixture_repo, base))
    assert result.status == M.FAILED
    assert "nothing to commit" in (result.notes or "")


def test_keep_mode_checkpoints_win_over_adoption(fixture_repo):
    # KEEP mode with `P9 wip:` checkpoints at the tip AND an adoptable `P9:`
    # commit behind base: the empty `P9:` marker carrying the checkpoint trailer
    # must still be created at the tip — adoption of the older commit would
    # leave the checkpointed work uncovered by any handoff commit.
    git(fixture_repo, "checkout", "-qb", "b")
    (fixture_repo / "work.py").write_text("phase work\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "P9: the phase\n\nbody")
    adopted_candidate = gitops.head_sha(fixture_repo)
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: response consumed")
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "more.py").write_text("checkpointed work\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "P9 wip: checkpointed milestone")
    step = Step.model_validate({"id": "commit", "type": "commit", "phase": "P9",
                                "message": "P9: the phase\n\nbody"})
    result = handle_commit(step, _ctx_for_commit(fixture_repo, base))
    assert result.status == DONE
    assert result.commit_sha != adopted_candidate  # NOT the old commit
    assert result.commit_sha == gitops.head_sha(fixture_repo)  # marker at tip
    subject = gitops.commit_subject(fixture_repo, result.commit_sha)
    assert subject.startswith("P9:")


# --- #134: marker commit over checkpoints buried beneath adopted commits -----
_P9_STEP = {"id": "commit", "type": "commit", "phase": "P9",
            "message": "P9: the phase\n\nbody"}


def _adopted(repo, subject: str, rel: str, content: str) -> str:
    """A plain (non-wip, non-bookkeeping) commit at the tip, as left by an
    operator pre-commit that `resume` adopted (re-anchoring base_sha to it)."""
    return _wip(repo, subject, rel, content)


def test_marker_lands_over_checkpoints_beneath_adopted_commit(fixture_repo):
    # #134 reproduction: the builder checkpointed the phase as `P9 wip:` commits,
    # an operator commit landed above them and `resume` adopted it, re-anchoring
    # the commit step's base_sha AT the adopted tip. Trailing-run discovery from
    # HEAD stops at the adopted commit (a "gap") and finds nothing; the tree is
    # clean; before this fix the step failed "nothing to commit" and the
    # operator hand-made the empty `P9:` marker. The engine now takes that path.
    git(fixture_repo, "checkout", "-qb", "b")
    first_wip = _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    _wip(fixture_repo, "P9 wip: cli wiring", "b.py", "b\n")
    adopted = _adopted(fixture_repo, "stage human evidence", "evidence.md", "e\n")
    assert gitops.wip_checkpoints(fixture_repo, phase="P9") == []  # the trap
    ctx = _ctx_for_commit(fixture_repo, adopted)  # base re-anchored at the tip
    result = handle_commit(Step.model_validate(_P9_STEP), ctx)

    assert result.status == DONE
    head = gitops.head_sha(fixture_repo)
    assert result.commit_sha == head and result.commit_phase == "P9"
    assert gitops.commit_subject(fixture_repo, head) == "P9: the phase"
    # An empty marker directly on the adopted commit — nothing rewritten.
    assert gitops.commit_parent(fixture_repo, head) == adopted
    assert gitops.diff_range_empty(fixture_repo, adopted, head)
    msg = gitops.commit_message(fixture_repo, head)
    assert "P9 wip: model layer" in msg and "P9 wip: cli wiring" in msg
    assert "2 checkpoint(s) beneath 1 adopted commit(s)" in (result.notes or "")
    # base_sha is repaired to the parent of the OLDEST checkpoint so the review
    # range diff is the cumulative phase diff (checkpoints + adopted commit).
    assert ctx.record.base_sha == gitops.commit_parent(fixture_repo, first_wip)
    diff = gitops.range_diff(fixture_repo, ctx.record.base_sha, head)
    assert "a.py" in diff and "b.py" in diff and "evidence.md" in diff


def test_marker_lands_beneath_adopted_fix_round_commit(fixture_repo):
    # The adopted commit above the checkpoints carries a phase-shaped header
    # (`P9.1:`) — not this phase's `P9:` commit, so the #124 adoption must not
    # take it, and the checkpoints beneath it still get their marker. base_sha
    # sits ABOVE the checkpoints but below HEAD (bookkeeping on top).
    git(fixture_repo, "checkout", "-qb", "b")
    first_wip = _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    fix = _adopted(fixture_repo, "P9.1: Address review — tweak", "a.py", "a2\n")
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "gauntlet: bookkeeping flush")
    ctx = _ctx_for_commit(fixture_repo, fix)
    result = handle_commit(Step.model_validate(_P9_STEP), ctx)
    assert result.status == DONE
    assert result.commit_sha == gitops.head_sha(fixture_repo)
    assert result.commit_sha != fix
    assert gitops.commit_subject(fixture_repo, result.commit_sha) == "P9: the phase"
    assert "1 checkpoint(s) beneath 1 adopted commit(s)" in (result.notes or "")
    assert ctx.record.base_sha == gitops.commit_parent(fixture_repo, first_wip)


def test_marker_keeps_base_when_it_is_already_below_checkpoints(fixture_repo):
    # base_sha was NOT re-anchored (it still sits below the checkpoints); an
    # adopted commit above them alone hides the trailing run. The marker lands
    # and the already-correct base is left alone.
    git(fixture_repo, "checkout", "-qb", "b")
    base = gitops.head_sha(fixture_repo)
    _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    _adopted(fixture_repo, "stage human evidence", "evidence.md", "e\n")
    ctx = _ctx_for_commit(fixture_repo, base)
    result = handle_commit(Step.model_validate(_P9_STEP), ctx)
    assert result.status == DONE
    assert gitops.commit_subject(fixture_repo, result.commit_sha) == "P9: the phase"
    assert ctx.record.base_sha == base


def test_marker_walk_stops_at_previous_phase_commit(fixture_repo):
    # The walk back to the phase's true start stops at the previous phase's
    # `P8:` commit: P8's own kept checkpoints beneath it are never counted, and
    # base_sha is re-anchored exactly at that boundary.
    git(fixture_repo, "checkout", "-qb", "b")
    _wip(fixture_repo, "P8 wip: earlier", "z.py", "z\n")
    git(fixture_repo, "commit", "-q", "--allow-empty", "-m", "P8: prior phase\n\nbody")
    boundary = gitops.head_sha(fixture_repo)
    _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    adopted = _adopted(fixture_repo, "stage human evidence", "evidence.md", "e\n")
    ctx = _ctx_for_commit(fixture_repo, adopted)
    result = handle_commit(Step.model_validate(_P9_STEP), ctx)
    assert result.status == DONE
    assert "1 checkpoint(s) beneath 1 adopted commit(s)" in (result.notes or "")
    msg = gitops.commit_message(fixture_repo, result.commit_sha)
    assert "P9 wip: model layer" in msg and "P8 wip" not in msg
    assert ctx.record.base_sha == boundary


def test_marker_fallback_fails_closed_on_wrong_phase_checkpoint(fixture_repo):
    # A wrong-phase `P8 wip:` inside the walked range (no `P8:` boundary shields
    # it) fails closed exactly like the trailing-run discovery does.
    git(fixture_repo, "checkout", "-qb", "b")
    _wip(fixture_repo, "P8 wip: mistyped", "z.py", "z\n")
    _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    adopted = _adopted(fixture_repo, "stage human evidence", "evidence.md", "e\n")
    result = handle_commit(Step.model_validate(_P9_STEP), _ctx_for_commit(fixture_repo, adopted))
    assert result.status == M.FAILED
    assert "failed closed" in (result.notes or "")
    assert gitops.head_sha(fixture_repo) == adopted  # nothing committed


def test_clean_tree_with_adopted_commit_but_no_checkpoints_still_fails(fixture_repo):
    # Negative control: adopted commits above the base but no `P9 wip:`
    # checkpoint anywhere in the run's history — still the loud FAILED. The
    # fallback lands a marker over checkpoints, never over nothing.
    git(fixture_repo, "checkout", "-qb", "b")
    adopted = _adopted(fixture_repo, "stage human evidence", "evidence.md", "e\n")
    result = handle_commit(Step.model_validate(_P9_STEP), _ctx_for_commit(fixture_repo, adopted))
    assert result.status == M.FAILED
    assert "nothing to commit" in (result.notes or "")
    assert gitops.head_sha(fixture_repo) == adopted


def test_foreign_run_checkpoints_in_base_history_are_not_counted(fixture_repo):
    # An earlier run/PRD left `P9 wip:` checkpoints in the BASE branch's
    # history. The walk is bounded to the run's own commits (`HEAD ^main`), so
    # those are never counted as this phase's checkpoints — FAILED is correct.
    _wip(fixture_repo, "P9 wip: an earlier run's milestone", "old.py", "o\n")
    git(fixture_repo, "checkout", "-qb", "b")  # run branch forks AFTER them
    adopted = _adopted(fixture_repo, "stage human evidence", "evidence.md", "e\n")
    result = handle_commit(Step.model_validate(_P9_STEP), _ctx_for_commit(fixture_repo, adopted))
    assert result.status == M.FAILED
    assert "nothing to commit" in (result.notes or "")
    assert gitops.head_sha(fixture_repo) == adopted


def test_marker_fallback_skipped_when_base_branch_missing(fixture_repo):
    # Without the base branch ref the walk cannot be bounded — no fallback, the
    # step fails closed as before rather than counting an unbounded history.
    git(fixture_repo, "checkout", "-qb", "b")
    _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    adopted = _adopted(fixture_repo, "stage human evidence", "evidence.md", "e\n")
    git(fixture_repo, "branch", "-qD", "main")
    result = handle_commit(Step.model_validate(_P9_STEP), _ctx_for_commit(fixture_repo, adopted))
    assert result.status == M.FAILED
    assert "nothing to commit" in (result.notes or "")


def test_marker_fallback_never_squashes_adopted_commits(fixture_repo):
    # SQUASH mode collapses only the TRAILING run it owns. Checkpoints found
    # beneath adopted commits get the empty marker: the adopted commits are not
    # this step's to rewrite.
    git(fixture_repo, "checkout", "-qb", "b")
    _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    adopted = _adopted(fixture_repo, "stage human evidence", "evidence.md", "e\n")
    cfg = {"agents": {"builder": {"adapter": "claude-code"}},
           "checkpoint_commits": "squash"}
    ctx = _ctx_for_commit(fixture_repo, adopted, config=cfg)
    result = handle_commit(Step.model_validate(_P9_STEP), ctx)
    assert result.status == DONE
    assert gitops.commit_parent(fixture_repo, result.commit_sha) == adopted
    assert gitops.diff_range_empty(fixture_repo, adopted, result.commit_sha)
    assert "empty P<N>: marker" in (result.notes or "")

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
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline

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

    # A pre-rewind backup ref preserves the discarded dirty work.
    refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/backup/")
    assert "refs/gauntlet/backup/" in refs

    # The final phase commit builds on the preserved checkpoint.
    head = gitops.head_sha(fixture_repo)
    assert gitops.is_ancestor(fixture_repo, wip, head)
    diff = gitops.range_diff(fixture_repo, base, head)
    assert "model.py" in diff and "feature.py" in diff


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

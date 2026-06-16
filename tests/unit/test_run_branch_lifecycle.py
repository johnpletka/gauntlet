"""Run-branch lifecycle: base:current, stale-branch guard, clean, finish.

These cover the branch-management changes that make the integration-branch
workflow safe and low-friction (proposals/run-branch-lifecycle.md):

* ``base_branch: current`` resolves to the checked-out branch and is recorded.
* ``start()`` fails closed on a stale/unmerged run branch instead of silently
  rewinding the worktree (the bug that erased a repo's scaffolding).
* ``clean`` deletes only a merged run branch (``--force`` overrides).
* ``finish`` merges a done run into its base, then deletes the branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.run import (
    BaseBranchError,
    FinishError,
    RunBranchNotMergedError,
    RunBranchStateError,
    RunManager,
    StaleRunBranchError,
    WorktreeDirtyError,
)

from conftest import FakeAdapter, git

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
  triage: {adapter: api, model: haiku}
"""

CONFIG_BASE_CURRENT = CONFIG_YAML.replace("base_branch: main", "base_branch: current")

LINEAR = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, message: "P1: implement\\n\\nthe body."}
"""

GATED = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [prd.md]}
"""


def _prepare(repo: Path, config: str = CONFIG_YAML) -> RunManager:
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(config)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _write_pipeline(repo: Path, text: str = LINEAR) -> Path:
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / "p.yaml"
    path.write_text(text)
    return path


def _author_prd(mgr: RunManager, slug: str) -> None:
    mgr.new(slug)
    mgr.layout(slug).prd_path.write_text("# Real PRD\n\nA genuine human-authored PRD.\n")


def _run_linear(mgr: RunManager, repo: Path, slug: str) -> str:
    _author_prd(mgr, slug)
    path = _write_pipeline(repo)
    return mgr.start(slug, path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))


# --- base_branch: current ----------------------------------------------------
def test_base_current_branches_from_checked_out_branch(fixture_repo):
    mgr = _prepare(fixture_repo, CONFIG_BASE_CURRENT)
    git(fixture_repo, "checkout", "-q", "-b", "feat/work")
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    # the run branch was created off feat/work, and the manifest records the
    # RESOLVED name (never the 'current' sentinel)
    man = mgr.status("demo")
    assert man.base_branch == "feat/work"
    assert gitops.is_ancestor(fixture_repo, "feat/work", "gauntlet/demo")


def test_base_current_refuses_detached_head(fixture_repo):
    from gauntlet.engine.run import EntryContractError

    mgr = _prepare(fixture_repo, CONFIG_BASE_CURRENT)
    git(fixture_repo, "checkout", "-q", "--detach")
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo)
    with pytest.raises(EntryContractError, match="detached"):
        mgr.start("demo", path, use_judge=False)


# --- stale-branch guard ------------------------------------------------------
def test_start_refuses_stale_unmerged_run_branch(fixture_repo):
    # The reported bug: a pre-existing gauntlet/<slug> at an OLDER base than the
    # current one. Adopting it silently rewinds the worktree. Now: refuse.
    mgr = _prepare(fixture_repo)
    # create a stale gauntlet/demo at the old root commit, with a divergent commit
    old = gitops.head_sha(fixture_repo)
    git(fixture_repo, "checkout", "-q", "-b", "gauntlet/demo", old)
    (fixture_repo / "stale.py").write_text("stale\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "-c", "user.name=H", "-c", "user.email=h@h",
        "commit", "-qm", "stale divergent commit")
    git(fixture_repo, "checkout", "-q", "main")
    # advance main so the stale branch is not an ancestor of base
    (fixture_repo / "onmain.py").write_text("main\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "-c", "user.name=H", "-c", "user.email=h@h",
        "commit", "-qm", "advance main")

    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo)
    with pytest.raises(StaleRunBranchError, match="not in base"):
        mgr.start("demo", path, use_judge=False)
    # the worktree was NOT rewound onto the stale branch
    assert gitops.current_branch(fixture_repo) == "main"


def test_start_discards_spent_merged_branch_and_recreates(fixture_repo):
    # A gauntlet/<slug> fully merged into base is spent -> discard + recreate.
    mgr = _prepare(fixture_repo)
    base = gitops.head_sha(fixture_repo)
    # gauntlet/demo points at base (trivially merged: equal == ancestor)
    git(fixture_repo, "branch", "gauntlet/demo", base)
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"
    assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: implement"


# --- clean -------------------------------------------------------------------
def test_clean_refuses_unmerged_branch(fixture_repo):
    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    # run branch is ahead of main (unmerged) -> clean refuses
    git(fixture_repo, "checkout", "-q", "main")
    with pytest.raises(RunBranchNotMergedError, match="not fully merged"):
        mgr.clean("demo")
    assert gitops.branch_exists(fixture_repo, "gauntlet/demo")


def test_clean_force_deletes_unmerged_branch(fixture_repo):
    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    git(fixture_repo, "checkout", "-q", "main")
    out = mgr.clean("demo", force=True)
    assert "deleted" in out and "forced" in out
    assert not gitops.branch_exists(fixture_repo, "gauntlet/demo")


def test_clean_deletes_merged_branch_and_keeps_run_dir(fixture_repo):
    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    # merge the run into main, then clean is safe
    git(fixture_repo, "checkout", "-q", "main")
    git(fixture_repo, "merge", "--no-ff", "-m", "land", "gauntlet/demo")
    layout = mgr.layout("demo")
    assert layout.prd_path.exists()  # committed audit trail present
    out = mgr.clean("demo")
    assert "deleted 'gauntlet/demo'" in out
    assert not gitops.branch_exists(fixture_repo, "gauntlet/demo")
    # the run record (prd.md) is preserved; only the pointer is cleared
    assert layout.prd_path.exists()
    assert not layout.active_pointer.exists()


# --- finish ------------------------------------------------------------------
def test_finish_merges_done_run_into_base_and_deletes_branch(fixture_repo):
    mgr = _prepare(fixture_repo, CONFIG_BASE_CURRENT)
    git(fixture_repo, "checkout", "-q", "-b", "feat/work")
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    out = mgr.finish("demo")
    assert "merged 'gauntlet/demo' into 'feat/work'" in out
    # base now contains the run's commit; the run branch + pointer are gone
    assert gitops.current_branch(fixture_repo) == "feat/work"
    assert "P1: implement" in gitops.log_range(fixture_repo, "main", "feat/work")
    assert not gitops.branch_exists(fixture_repo, "gauntlet/demo")
    assert not mgr.layout("demo").active_pointer.exists()


def test_finish_refuses_when_run_not_done(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    with pytest.raises(FinishError, match="not done"):
        mgr.finish("demo")


def test_finish_refuses_dirty_worktree(fixture_repo):
    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    (fixture_repo / "dirt.py").write_text("uncommitted\n")
    with pytest.raises(FinishError, match="dirty"):
        mgr.finish("demo")


# --- PR review fixes ---------------------------------------------------------
def test_resume_refuses_when_run_branch_missing(fixture_repo):
    # F-1: resume must not recreate the run branch from base (that drops the
    # manifest's recorded commits). A missing branch fails closed.
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED  # parked, resumable
    git(fixture_repo, "checkout", "-q", "main")
    git(fixture_repo, "branch", "-D", "gauntlet/demo")
    with pytest.raises(RunBranchStateError, match="missing"):
        mgr.resume("demo", use_judge=False)


def test_resume_refuses_when_branch_reset_behind_manifest(fixture_repo):
    # F-1: a run branch reset behind its recorded commits is divergent -> refuse
    # rather than silently resume a branch missing recorded work.
    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    # drop the recorded P1 commit off the branch tip
    git(fixture_repo, "reset", "-q", "--hard", "HEAD~1")
    with pytest.raises(RunBranchStateError, match="missing the manifest"):
        mgr.resume("demo", use_judge=False)


def test_clean_refuses_dirty_worktree_on_branch(fixture_repo):
    # F-2: clean must not carry uncommitted changes onto the base when it steps
    # off the run branch. Dirty tree fails closed — even under --force.
    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == M.RUN_DONE
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"
    (fixture_repo / "uncommitted.py").write_text("work in progress\n")
    with pytest.raises(WorktreeDirtyError, match="dirty"):
        mgr.clean("demo", force=True)
    # nothing was deleted or moved
    assert gitops.branch_exists(fixture_repo, "gauntlet/demo")
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"


def test_start_refuses_base_resolving_to_run_branch(fixture_repo):
    # F-3: base:current while sitting on a gauntlet/* branch resolves the base to
    # a machine-owned run branch -> fail closed (would later wedge `finish`).
    mgr = _prepare(fixture_repo, CONFIG_BASE_CURRENT)
    git(fixture_repo, "checkout", "-q", "-b", "gauntlet/demo")
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo)
    with pytest.raises(BaseBranchError, match="run branch"):
        mgr.start("demo", path, use_judge=False)


def test_finish_aborts_on_conflict_and_returns_to_branch(fixture_repo):
    # Make base and the run branch edit the same file divergently so the merge
    # conflicts; finish must abort cleanly and leave the human on the run branch.
    mgr = _prepare(fixture_repo, CONFIG_BASE_CURRENT)
    git(fixture_repo, "checkout", "-q", "-b", "feat/work")
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo)
    mgr.start("demo", path, use_judge=False,
              adapter_factory=lambda n: FakeAdapter(writes={"clash.py": "from-run\n"}))
    # diverge feat/work on the same path AFTER the run branched
    git(fixture_repo, "checkout", "-q", "feat/work")
    (fixture_repo / "clash.py").write_text("from-base\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "-c", "user.name=H", "-c", "user.email=h@h",
        "commit", "-qm", "base edits clash.py")
    git(fixture_repo, "checkout", "-q", "gauntlet/demo")
    with pytest.raises(FinishError, match="conflicts"):
        mgr.finish("demo")
    # merge aborted, branch intact, human back on the run branch
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"
    assert gitops.branch_exists(fixture_repo, "gauntlet/demo")
    assert gitops.is_clean(fixture_repo, exclude=[".gauntlet/runs"])

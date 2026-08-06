"""Real verbs driven in `dedicated` mode (P7c-1.1, review F-009).

The gap this closes, stated plainly: the P7c-1 suite created run worktrees by
calling `worktree.ensure` directly, and every existing verb test ran under the
shipped `same_tree` default. So nothing exercised `RunManager.start` / `resume`
/ `approve` / `rollback` / `finish` with a dedicated tree — and two blocking
defects landed underneath that gap:

* the Orchestrator was never handed `work_root`, so the agent's cwd, the shell
  steps and every engine commit ran in the OPERATOR's checkout (F-001);
* `_rollback_locked` resolved bookkeeping paths against the operator checkout
  while passing the work tree, so a dedicated rollback raised
  `StateDirNotContained` before reaching the executor (F-002).

Both are invisible to any test that does not drive a real verb end to end in
dedicated mode. These do. The autouse `operator_checkout_invariance` fixture in
conftest.py asserts A1 over every one of them for free.
"""

from __future__ import annotations

import pytest

from gauntlet.engine import gitops
from gauntlet.engine import manifest as M
from gauntlet.engine import worktree as WT
from gauntlet.engine.run import RunManager

from conftest import FakeAdapter, git

CONFIG_DEDICATED = """
base_branch: main
run_root: runs
worktree:
  mode: dedicated
agents:
  builder: {adapter: claude-code}
"""

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
      - {id: after, type: shell, run: "true"}
"""


def _prepare(repo) -> RunManager:
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_DEDICATED)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _pipeline(repo, text: str):
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / "p.yaml"
    path.write_text(text)
    git(repo, "add", "pipelines")
    git(repo, "commit", "-qm", "add pipeline")
    return path


def _author_prd(mgr: RunManager, slug: str) -> None:
    mgr.new(slug)
    mgr.layout(slug).prd_path.write_text("# Real PRD\n\nA genuine PRD.\n")


def _run_worktree(mgr: RunManager, branch: str):
    return WT.observe(
        mgr.operator_root, branch, common_dir=mgr._git_common_dir()
    )


# --- F-001: the drive actually happens in the run's tree ---------------------


def test_start_drives_the_run_worktree_not_the_operator_checkout(fixture_repo):
    """The F-001 regression, asserted on all three planes it broke.

    Agent cwd, the engine's commit, and the operator's checkout — each was
    wrong before, and each is checked here rather than inferring from one.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    adapter = FakeAdapter(writes={"feature.py": "code\n"})

    before_branch = gitops.current_branch(fixture_repo)
    before_head = gitops.head_sha(fixture_repo)
    status = mgr.start("demo", path, use_judge=False,
                       adapter_factory=lambda n: adapter)
    assert status == M.RUN_DONE

    entry = _run_worktree(mgr, "gauntlet/demo")
    assert entry is not None, "a dedicated run must register its own worktree"

    # 1. the agent wrote into the RUN tree, not the operator's
    assert (entry.path / "feature.py").exists()
    assert not (fixture_repo / "feature.py").exists()

    # 2. the engine's phase commit landed on the run branch, reachable from the
    #    run tree's HEAD — and the operator's HEAD never moved
    assert gitops.commit_subject(fixture_repo, "refs/heads/gauntlet/demo") == (
        "P1: implement"
    )
    assert gitops.current_branch(fixture_repo) == before_branch
    assert gitops.head_sha(fixture_repo) == before_head

    # 3. the run's own state stayed in the operator's checkout (spike §4.4)
    man = mgr.status("demo")
    assert man.status == M.RUN_DONE
    assert (mgr.layout("demo").run_dir(man.run_id) / "manifest.json").exists()
    assert man.worktree_mode == WT.MODE_DEDICATED


def test_start_records_the_born_mode_and_locks_the_tree(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    mgr.start("demo", path, use_judge=False,
              adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))
    entry = _run_worktree(mgr, "gauntlet/demo")
    assert WT.parse_lock_reason(entry.locked) is not None, (
        "the anti-prune marker must be held for the life of the run (§8.3)"
    )


def test_a_dirty_operator_checkout_does_not_block_a_dedicated_start(fixture_repo):
    """The #61 guard protects against `checkout -b` carrying dirt onto the run
    branch. A dedicated start checks nothing out in the operator's tree, so the
    guard has nothing to protect and must not block (spike §9.4)."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    (fixture_repo / "operator-scratch.txt").write_text("my own uncommitted work\n")
    status = mgr.start("demo", path, use_judge=False,
                       adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))
    assert status == M.RUN_DONE
    assert (fixture_repo / "operator-scratch.txt").read_text() == (
        "my own uncommitted work\n"
    ), "the operator's uncommitted work is untouched"


# --- gate / approve / resume in dedicated mode -------------------------------


def test_gate_park_then_approve_drives_the_run_worktree(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    entry = _run_worktree(mgr, "gauntlet/demo")
    assert entry is not None
    assert mgr.approve("demo", notes="ok", use_judge=False) == M.RUN_DONE
    assert mgr.status("demo").record("after").status == M.DONE


def test_resume_recreates_a_missing_worktree_and_verifies_head(fixture_repo):
    """Acceptance A3 through the PRODUCTION path (review F-008).

    Previously only the unit test called `WT.recreate`, so the head check never
    ran in a real run. Here the tree is destroyed between verbs and `resume`
    must rebuild it from the branch plus journal state.
    """
    import subprocess

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    entry = _run_worktree(mgr, "gauntlet/demo")
    recorded = gitops.head_sha(entry.path)

    subprocess.run(["rm", "-rf", str(entry.path)], check=True)
    state = WT.describe(fixture_repo, mode=WT.MODE_DEDICATED, branch="gauntlet/demo")
    assert state.missing, "registered-and-absent is the §11 row-2 signal"

    assert mgr.approve("demo", notes="ok", use_judge=False) == M.RUN_DONE
    again = _run_worktree(mgr, "gauntlet/demo")
    assert again is not None and again.path.is_dir()
    assert gitops.head_sha(again.path) == recorded


# --- F-002: rollback reaches the executor in dedicated mode ------------------


TWO_PHASE = """
name: p
version: 1
stages:
  - id: p1
    steps:
      - {id: a, type: agent_task, agent: builder, prompt_text: go}
      - {id: c1, type: commit, message: "P1: phase one\\n\\nbody one."}
  - id: p2
    steps:
      - {id: b, type: agent_task, agent: builder, prompt_text: go}
      - {id: c2, type: commit, message: "P2: phase two\\n\\nbody two."}
"""


def test_rollback_completes_in_dedicated_mode(fixture_repo):
    """F-002: this raised StateDirNotContained before reaching the executor."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, TWO_PHASE)
    # A distinct file per call, so each phase's commit is non-empty (the same
    # pattern test_run_lifecycle uses for its two-phase fixture).
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=factory) == M.RUN_DONE
    man = mgr.status("demo")
    assert [c.phase for c in man.commits] == ["P1", "P2"]

    target = mgr.rollback("demo", 1)
    assert target
    after = mgr.status("demo")
    assert [c.phase for c in after.commits] == ["P1"], (
        "rollback must rewind the manifest to the phase-1 boundary"
    )
    entry = _run_worktree(mgr, "gauntlet/demo")
    assert gitops.commit_subject(entry.path, "HEAD") == "P1: phase one", (
        "and the RUN tree must be the one that was rewound"
    )


# --- F-011: finish refuses on run-tree dirt, then tears down in order --------


def test_finish_refuses_when_the_run_worktree_is_dirty(fixture_repo):
    from gauntlet.engine.run import FinishError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    entry = _run_worktree(mgr, "gauntlet/demo")
    (entry.path / "uncommitted.txt").write_text("work the builder left\n")

    with pytest.raises(FinishError) as exc:
        mgr.finish("demo")
    assert str(entry.path) in str(exc.value), "the refusal must name the tree"
    assert "git -C" in str(exc.value), "and give an executable inspect command"
    assert (entry.path / "uncommitted.txt").exists(), "nothing was destroyed"


@pytest.mark.operator_tree_verb  # `finish` merges into the operator's base by design (§9.4)
def test_finish_releases_the_worktree_then_deletes_the_branch(fixture_repo):
    """REWRITTEN at P7d: an IDENTICAL untracked duplicate no longer refuses.

    This test used to assert that `finish` refuses outright on the governed-
    artifact merge collision, and it was right about P7c-1.1's behaviour. P7d
    proves that behaviour wrong as a default: under `dedicated` the operator's
    checkout is the authoring surface, so an untracked `runs/<slug>/prd.md`
    byte-identical to the copy the run branch committed is the NORMAL end state
    of every dedicated run — and refusing there made a manual step mandatory on
    the happy path.

    The assertion is not weakened, it is moved to the property that actually
    matters: the operator's bytes survive. After the finish the path is TRACKED
    on the base with the same content it had before, which is the whole
    justification for clearing it (the merge restores it in the same breath).
    `test_finish_refuses_a_divergent_untracked_duplicate` keeps the refusal
    pinned for the case where the bytes disagree.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    assert _run_worktree(mgr, "gauntlet/demo") is not None
    prd = fixture_repo / "runs" / "demo" / "prd.md"
    authored = prd.read_bytes()
    assert not gitops.is_tracked(fixture_repo, "runs/demo/prd.md"), (
        "precondition: the authoring copy is untracked in the operator checkout"
    )

    out = mgr.finish("demo")

    assert "replaced your untracked duplicate" in out, (
        "the operator must be TOLD which files were cleared — the P7c-1.1 "
        "objection was to doing it SILENTLY, not to doing it"
    )
    assert prd.read_bytes() == authored, "the operator's bytes are unchanged"
    assert gitops.is_tracked(fixture_repo, "runs/demo/prd.md"), (
        "and the path is now the merged TRACKED copy, not an untracked duplicate"
    )
    assert not gitops.branch_exists(fixture_repo, "gauntlet/demo"), (
        "PROBLEM D / E2-D: the branch delete only succeeds after the release"
    )
    assert _run_worktree(mgr, "gauntlet/demo") is None


@pytest.mark.operator_tree_verb  # `finish` merges into the operator's base by design (§9.4)
def test_finish_refuses_a_divergent_untracked_duplicate(fixture_repo):
    """The other half of P7d's split: proof earns the replacement, nothing else.

    An operator who edits their authoring copy after the run's last sync has a
    real disagreement with what the run branch committed about an APPROVED
    artifact. R9 says the engine never silently adopts either side, so this
    still refuses — and the refusal must be legible as being about divergence,
    not about the mere existence of the file.
    """
    from gauntlet.engine.run import FinishError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    prd = fixture_repo / "runs" / "demo" / "prd.md"
    prd.write_text(prd.read_text() + "\nan edit made after the run's last sync\n")
    edited = prd.read_bytes()

    with pytest.raises(FinishError, match="NOT the same object"):
        mgr.finish("demo")

    assert prd.read_bytes() == edited, "the divergent file is never touched"
    assert gitops.branch_exists(fixture_repo, "gauntlet/demo"), (
        "and the refusal is early — before the release destroys anything"
    )
    assert _run_worktree(mgr, "gauntlet/demo") is not None


@pytest.mark.operator_tree_verb  # `finish` merges into the operator's base by design (§9.4)
def test_finish_is_not_blocked_by_the_run_trees_own_synced_artifact(fixture_repo):
    """The second P7c-2 deferral: `finish`'s run-tree check gets its excludes.

    A dedicated run reaching `done` has the synced governed artifact sitting
    untracked in its OWN tree (that is what `_sync_governed_artifacts` puts
    there) plus the engine's write-only export. With no exclusion set, the
    run-tree dirtiness guard — whose stated purpose is protecting a BUILDER's
    uncommitted work — counted both as exactly that and refused.

    Asserted through the verb rather than by inspecting the exclusion list, so
    a future refactor that keeps the list and stops passing it still fails.

    The artifact used is `plan.md` rather than `prd.md` because the state the
    deferral names needs a synced artifact the run branch has NOT committed —
    P7c-2 said as much ("in practice a real pipeline's commit step stages the
    artifact first, which is why P7c-1.1's dedicated-finish test passes"). A
    pipeline that produces a plan after the last commit leaves exactly this.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    entry = _run_worktree(mgr, "gauntlet/demo")
    assert entry is not None
    # The authoritative copy in the operator's checkout, and the sync's copy in
    # the run tree — untracked on the branch, byte-identical, which is what
    # `_sync_governed_artifacts` leaves behind on every mutating contact.
    plan_bytes = b"# Plan\n\nA governed artifact synced after the last commit.\n"
    (fixture_repo / "runs" / "demo" / "plan.md").write_bytes(plan_bytes)
    synced = entry.path / "runs" / "demo" / "plan.md"
    synced.parent.mkdir(parents=True, exist_ok=True)
    synced.write_bytes(plan_bytes)
    assert gitops.status_porcelain(entry.path, untracked_all=True), (
        "precondition: the run tree is dirty by the unexcluded reading"
    )

    mgr.finish("demo")  # must not raise

    assert not gitops.branch_exists(fixture_repo, "gauntlet/demo")


@pytest.mark.operator_tree_verb  # `finish` merges into the operator's base by design (§9.4)
def test_finish_still_refuses_an_artifact_edited_inside_the_run_tree(fixture_repo):
    """The exclusion is earned by bytes at `finish` too, not by category.

    `finish` is a NEW caller of the shared `_run_tree_excludes` derivation, so
    review F-003's lesson has to hold at this call site independently: a
    governed artifact whose run-tree copy DIVERGED from the authority is a real
    edit, is not excluded, and must still stop the verb — because the release
    that follows would otherwise sweep it into a recovery ref nobody asked for.
    """
    from gauntlet.engine.run import FinishError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    entry = _run_worktree(mgr, "gauntlet/demo")
    assert entry is not None
    (fixture_repo / "runs" / "demo" / "plan.md").write_bytes(b"# Plan\n\nauthority\n")
    synced = entry.path / "runs" / "demo" / "plan.md"
    synced.parent.mkdir(parents=True, exist_ok=True)
    synced.write_bytes(b"# Plan\n\nEDITED IN THE RUN TREE\n")

    with pytest.raises(FinishError, match="RUN WORKTREE has uncommitted"):
        mgr.finish("demo")

    assert synced.read_bytes() == b"# Plan\n\nEDITED IN THE RUN TREE\n", (
        "the edit is preserved, not snapshotted-and-removed"
    )


# --- P7d review fixes --------------------------------------------------------


@pytest.mark.operator_tree_verb  # `finish` merges into the operator's base by design (§9.4)
def test_finish_snapshots_a_governed_artifact_staged_in_the_run_tree(fixture_repo):
    """Review F-001: byte equality is a claim about the FILE, not the index.

    A linked worktree has its own private index that dies with it. If the run
    tree stages version B while its working copy holds an authority-identical C,
    the worktree-only comparison called the path redundant, excluded it from
    BOTH the dirtiness refusal and `WT.release`'s snapshot decision, and the
    teardown took B with it — the staged/unstaged-differ row the plan's §7
    matrix names, and the one state the file's own bytes cannot reveal.
    """
    from gauntlet.engine.run import FinishError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    entry = _run_worktree(mgr, "gauntlet/demo")
    assert entry is not None

    authority = b"# Plan\n\nauthority bytes\n"
    (fixture_repo / "runs" / "demo" / "plan.md").write_bytes(authority)
    synced = entry.path / "runs" / "demo" / "plan.md"
    synced.parent.mkdir(parents=True, exist_ok=True)
    # Stage version B...
    synced.write_bytes(b"# Plan\n\nSTAGED VERSION B - ONLY IN THE INDEX\n")
    git(entry.path, "add", "runs/demo/plan.md")
    # ...then leave an authority-identical C in the working tree.
    synced.write_bytes(authority)

    with pytest.raises(FinishError, match="RUN WORKTREE has uncommitted"):
        mgr.finish("demo")

    assert synced.exists(), "the tree was not torn down"
    staged = git(entry.path, "show", ":runs/demo/plan.md")
    assert "STAGED VERSION B" in staged, "the index version is still recoverable"


@pytest.mark.operator_tree_verb  # `finish` merges into the operator's base by design (§9.4)
def test_finish_refuses_a_duplicate_whose_file_mode_differs(fixture_repo):
    """Review F-002: the executable bit is its own state plane (plan §7).

    A `100755` local file and a `100644` blob compare byte-equal and are
    different git objects. Replacing one with the other would silently drop the
    mode, so mode inequality makes the collision divergent.
    """
    import os

    from gauntlet.engine.run import FinishError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    prd = fixture_repo / "runs" / "demo" / "prd.md"
    assert prd.read_bytes() == git(
        fixture_repo, "show", "gauntlet/demo:runs/demo/prd.md"
    ).encode(), "precondition: the bytes match, so only the mode can differ"
    os.chmod(prd, 0o755)

    with pytest.raises(FinishError, match="NOT the same object"):
        mgr.finish("demo")

    assert prd.stat().st_mode & 0o111, "the operator's executable bit survives"


@pytest.mark.operator_tree_verb  # `finish` merges into the operator's base by design (§9.4)
def test_finish_restores_the_set_aside_artifact_when_the_landing_fails(fixture_repo):
    """Review F-002: move-then-restore, never unlink — R2 in the landing path.

    An unlink is unrecoverable the moment anything downstream fails. The file is
    moved aside, and every failure path must put it back exactly as found.
    Driven through a real failure (a conflicting commit on the base) rather than
    a patched helper, so the restore is proven where it actually has to happen.
    """
    from gauntlet.engine.run import FinishError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    prd = fixture_repo / "runs" / "demo" / "prd.md"
    authored = prd.read_bytes()
    # A conflicting commit on the base at the same path the merge will write.
    git(fixture_repo, "checkout", "-q", "main")
    (fixture_repo / "feature.py").write_text("a conflicting base version\n")
    git(fixture_repo, "add", "feature.py")
    git(fixture_repo, "commit", "-qm", "base touches the same path")

    with pytest.raises(FinishError, match="conflicts"):
        mgr.finish("demo")

    assert prd.read_bytes() == authored, "put back exactly as found"
    assert not list(fixture_repo.glob("runs/demo/*.gauntlet-finish-backup")), (
        "and no set-aside copy is stranded"
    )


def test_clean_refuses_while_a_driver_is_live(fixture_repo, monkeypatch):
    """Review F-005: `clean` destroys a tree AND a branch, under no lock at all.

    P7c gave `clean` a worktree to release without giving it the drive lock, so
    a concurrent driver could lose its working directory and its branch
    mid-step. The worktree-global tree guard never covered this either — `clean`
    never acquired it — so this is a gap, not a regression from the guard.
    """
    from gauntlet.engine import operator as op
    from gauntlet.engine.run import WorktreeLockError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    entry = _run_worktree(mgr, "gauntlet/demo")
    assert entry is not None

    monkeypatch.setattr(
        RunManager, "_migration_liveness", lambda self, s, d: op.LIVENESS_ALIVE
    )
    with pytest.raises(WorktreeLockError, match="refusing clean"):
        mgr.clean("demo", force=True)

    assert entry.path.is_dir(), "the live run's tree is untouched"
    assert gitops.branch_exists(fixture_repo, "gauntlet/demo")


def test_clean_refuses_under_an_indeterminate_driver(fixture_repo, monkeypatch):
    """The half that is easy to get wrong: `indeterminate` fails closed too.

    "I could not read the lock" must be treated as `alive`, never as gone —
    the same asymmetry `recover`, `_reap_orphaned_judge` and the migration gate
    take. Paired with the live case so a predicate that always refuses cannot
    satisfy both.
    """
    from gauntlet.engine import operator as op
    from gauntlet.engine.run import WorktreeLockError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED

    monkeypatch.setattr(
        RunManager,
        "_migration_liveness",
        lambda self, s, d: op.LIVENESS_INDETERMINATE,
    )
    with pytest.raises(WorktreeLockError, match="INDETERMINATE"):
        mgr.clean("demo", force=True)

    assert gitops.branch_exists(fixture_repo, "gauntlet/demo")


@pytest.mark.operator_tree_verb  # `clean` deletes the run branch from the operator's checkout (§9.4)
def test_clean_still_works_when_the_driver_is_gone(fixture_repo):
    """The other direction: the refusal must not become a wedge.

    A parked run with no live driver is the normal `clean` case and must stay
    executable, or F-005's fix trades a data-loss bug for an unusable verb.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    entry = _run_worktree(mgr, "gauntlet/demo")

    mgr.clean("demo", force=True)

    assert not gitops.branch_exists(fixture_repo, "gauntlet/demo")
    assert not entry.path.is_dir()


@pytest.mark.operator_tree_verb  # `finish` merges into the operator's base by design (§9.4)
def test_finish_already_merged_never_strands_the_set_aside_artifact(fixture_repo):
    """The quarantine's exit path must ask the filesystem, not assume.

    On the already-merged path `finish` deletes the branch WITHOUT merging, and
    it checks the base out only when the operator is standing on the run branch.
    So a "discard the set-aside copy, git restored it" step is wrong here: when
    no checkout runs, nothing restores it, and discarding would delete the
    operator's artifact outright.

    The state is built deliberately rather than incidentally: the operator ends
    up on a THIRD branch, forked before the artifact existed, so
    `runs/<slug>/prd.md` is untracked in their index while the base tracks it.
    That is the only shape that both produces a quarantine and skips the
    checkout — and a test that never reaches the quarantine proves nothing,
    which is why the assertion on "set aside" is here rather than implied.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, LINEAR)
    fork_point = gitops.head_sha(fixture_repo)
    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"feature.py": "code\n"})) == M.RUN_DONE
    prd = fixture_repo / "runs" / "demo" / "prd.md"
    authored = prd.read_bytes()

    # Land it by hand, so `finish` takes the already-merged path. The untracked
    # duplicate has to go first — git refuses the merge over it, which is the
    # very collision this suite exists for.
    prd.unlink()
    git(fixture_repo, "merge", "-q", "--no-ff", "-m", "landed", "gauntlet/demo")
    assert gitops.is_tracked(fixture_repo, "runs/demo/prd.md"), "the base now has it"
    # ...then stand somewhere the artifact is untracked.
    git(fixture_repo, "checkout", "-q", "-b", "side", fork_point)
    assert not gitops.is_tracked(fixture_repo, "runs/demo/prd.md"), (
        "precondition: untracked here, tracked on the base — a real collision"
    )
    prd.write_bytes(authored)

    out = mgr.finish("demo")

    assert "already merged" in out, out
    assert "set aside" in out, (
        "the quarantine must actually have fired, or this proves nothing: " + out
    )
    assert prd.read_bytes() == authored, "the operator's artifact was NOT stranded"
    assert not list(fixture_repo.glob("runs/demo/*.gauntlet-finish-backup")), (
        "and no set-aside copy is left behind"
    )

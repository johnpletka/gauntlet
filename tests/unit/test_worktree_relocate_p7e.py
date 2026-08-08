"""Relocating a run worktree off the pre-P7e root (P7e, `P7d-gate-blocker.md`).

Spike §6.2 put every run worktree under the git common dir. The `claude` CLI —
which drives every `builder` and `verifier` step — refuses to write any path
carrying a literal `.git` segment, so runs there fail, and fail *non-uniformly*:
`Write`, `Edit` and `printf > file` are refused while `tee` and `cat > file` are
not, so whether a run survives depends on which form the model improvises. The
maintainer ratified sub-option 1A on 2026-08-06.

The population this file defends is **adopters who opted into `dedicated` before
that decision**. Their trees are at the old root. Spike §10's rule is absolute —
a pre-existing run is never auto-migrated and never wedged — and there is a
third thing it must never do, which is specific to this case: it must never
silently resolve `same_tree`, because that would drop the run onto the
operator's own checkout. That is the failure these tests exist to prevent, and
it is a *quiet* failure, which is why it is tested rather than argued.

Nothing is mocked. A legacy-root tree is built with real git at the real old
path, because every claim here is a claim about what the engine observes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gauntlet.engine import gitops
from gauntlet.engine import worktree as WT
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.run import RunManager

from conftest import git


CONFIG = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""


def _mgr(repo: Path) -> RunManager:
    """A manager on a repo scaffolded the way an adopter's would be.

    The config is COMMITTED, not just written: several checks here refuse on a
    dirty operator checkout, and an untracked fixture file would make them
    refuse for a reason that has nothing to do with what is being tested.
    """
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _manifest(slug="demo", run_id="run-1") -> Manifest:
    return Manifest(
        run_id=run_id,
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="p", version="1", hash="h"),
    )


def _legacy_tree(repo: Path, slug: str = "demo", run_id: str = "run-1") -> Path:
    """Build a run worktree exactly where a P7c/P7d engine would have put it.

    Deliberately constructed from the legacy derivation rather than by calling
    the engine: the engine can no longer create one, and reproducing the shape
    by hand is what makes this a test of *discovery* rather than of round-trip.
    """
    common = gitops.git_common_dir(repo)
    target = WT.legacy_worktrees_root(common) / slug / run_id
    target.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "branch", f"gauntlet/{slug}")
    gitops.add_worktree_branch(repo, target, f"gauntlet/{slug}")
    gitops.lock_worktree(repo, target, reason=WT.lock_reason(slug, run_id))
    return target


# --- discovery: the old root must not read as "no worktree" ------------------


def test_a_legacy_root_tree_is_observed_as_legacy_not_as_absent(fixture_repo):
    """The distinction the whole case rests on.

    `observe` scopes to the CURRENT derived root, so a legacy tree is correctly
    invisible to it. If that were the only question the engine asked, "tree at
    the old path" and "no tree at all" would be the same answer — and the second
    resolves `same_tree`, i.e. the operator's checkout.
    """
    legacy = _legacy_tree(fixture_repo)
    main_root = gitops.main_worktree_root(fixture_repo)
    common = gitops.git_common_dir(fixture_repo)

    assert WT.observe(fixture_repo, "gauntlet/demo", main_root=main_root) is None
    entry = WT.legacy_observe(fixture_repo, "gauntlet/demo", common_dir=common)
    assert entry is not None and entry.path.resolve() == legacy.resolve()
    assert not WT.is_inside_worktrees_root(legacy, main_root)


def test_a_legacy_run_never_resolves_same_tree(fixture_repo):
    """The quiet failure this file exists to prevent.

    Resolver rule 1 (a registered worktree under the derived root) cannot see a
    legacy tree. Rule 2 — an unreleased `WorktreeAdopted` in the journal — is
    what keeps the answer `dedicated`, so the run is never silently handed the
    operator's checkout to drive.
    """
    _legacy_tree(fixture_repo)
    mgr = _mgr(fixture_repo)
    man = _manifest()
    run_dir = fixture_repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    # `created=True` because the event is only journalled on a TRANSITION —
    # adopting an already-healthy tree on every verb is the steady state, not an
    # event. This reproduces the adoption a P7c/P7d engine wrote when it first
    # built the tree at the old root.
    assert mgr._record_worktree_adopted(
        run_dir,
        WT.RunWorktree(
            path=_legacy_path(fixture_repo), branch="gauntlet/demo", created=True
        ),
        slug="demo", run_id="run-1",
    )
    assert mgr._effective_worktree_mode(man) == WT.MODE_DEDICATED


def _legacy_path(repo: Path) -> Path:
    return WT.legacy_worktrees_root(gitops.git_common_dir(repo)) / "demo" / "run-1"


# --- the refusal: actionable, and never `--same-tree` ------------------------


def test_ensure_refuses_a_legacy_tree_and_names_the_relocation(fixture_repo):
    """Fail closed toward RELOCATION, not toward the operator's checkout.

    Left to itself `ensure` would try `worktree add` at the new path, hit git's
    one-branch-one-worktree refusal, and surface `WorktreeUnavailable` — whose
    advertised action is `gauntlet resume <slug> --same-tree`. That action is
    correct for its own case and catastrophic for this one: taking it would
    drive the run in the operator's tree, which is precisely what P7 exists to
    prevent. So the legacy case is detected first and named.
    """
    legacy = _legacy_tree(fixture_repo)
    main_root = gitops.main_worktree_root(fixture_repo)
    common = gitops.git_common_dir(fixture_repo)

    with pytest.raises(WT.WorktreeUnavailable) as exc:
        WT.ensure(
            fixture_repo, main_root, slug="demo", run_id="run-1",
            branch="gauntlet/demo", common_dir=common,
        )
    msg = str(exc.value)
    assert str(legacy) in msg, "must name the tree it found"
    assert "migrate-worktree demo" in msg, "must name the verb that fixes it"
    assert "--same-tree" not in msg, (
        "offering the same-tree fallback here would send the operator to drive "
        "the run in their own checkout — the one outcome this case must avoid"
    )
    # And nothing was touched.
    assert legacy.is_dir()
    assert not WT.worktrees_root(main_root).joinpath("demo").exists()


def test_the_legacy_tree_does_not_block_migration_eligibility(fixture_repo):
    """The precondition check must not refuse the very tree it is asked to move.

    `_migration_precondition_blocker` refuses when the run branch is checked out
    somewhere that is not engine-owned, because git would refuse the `add`
    (E2-A). A legacy tree IS engine-owned; treating it as foreign would refuse
    the relocation and leave the run with no action at all — §10's "never
    wedged", failed by the commit that exists to fix it.
    """
    _legacy_tree(fixture_repo)
    mgr = _mgr(fixture_repo)
    blocker = mgr._migration_precondition_blocker(_manifest())
    assert blocker is None, blocker


# --- the action: relocation preserves the run --------------------------------


def test_migrate_relocates_the_tree_and_preserves_committed_work(fixture_repo):
    """The transaction: release the old tree, recreate at the new root.

    A release-and-recreate rather than a directory move, because the run's
    authoritative state never lived in the tree (§4.4) and the branch ref is
    shared — so recreating from the branch reconstructs everything that matters,
    without needing `git worktree repair` to fix the admin entry's gitdir
    pointer.
    """
    legacy = _legacy_tree(fixture_repo)
    (legacy / "feature.py").write_text("work\n")
    git(legacy, "add", "-A")
    git(legacy, "commit", "-qm", "P1: work")
    recorded = gitops.head_sha(legacy)

    mgr = _mgr(fixture_repo)
    main_root = gitops.main_worktree_root(fixture_repo)
    old = mgr._relocate_legacy_worktree(
        mgr.layout("demo"), fixture_repo / "runs" / "demo" / "run-1",
        _manifest(), gitops.git_common_dir(fixture_repo),
    )
    assert old is not None and old.resolve() == legacy.resolve()
    assert not legacy.is_dir(), "the old tree is released, not left behind"

    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    assert wt.path == WT.run_worktree_path(main_root, "demo", "run-1")
    assert ".git" not in wt.path.parts
    assert gitops.head_sha(wt.path) == recorded
    assert (wt.path / "feature.py").read_text() == "work\n"


def test_a_dirty_legacy_tree_refuses_rather_than_snapshotting_it_away(fixture_repo):
    """R2 is satisfied by refusing, not by burying the work in a recovery ref.

    `WT.release` would snapshot the dirt into `refs/gauntlet/recovery/…` and
    proceed — durable, but it converts the operator's visible work-in-progress
    into a ref they have to know to look for. The clean-handoff invariant says
    the tree is normally clean, so a dirty one here means something is genuinely
    in flight and the operator should decide.
    """
    from gauntlet.engine.run import MigrateWorktreeRefused

    legacy = _legacy_tree(fixture_repo)
    (legacy / "WIP.txt").write_text("BUILDER-WIP-DO-NOT-LOSE\n")

    mgr = _mgr(fixture_repo)
    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr._relocate_legacy_worktree(
            mgr.layout("demo"), fixture_repo / "runs" / "demo" / "run-1",
            _manifest(), gitops.git_common_dir(fixture_repo),
        )
    msg = str(exc.value)
    assert "WIP.txt" in msg and str(legacy) in msg
    assert legacy.is_dir(), "a refusal must not have moved anything"
    assert (legacy / "WIP.txt").exists()


# --- the marker --------------------------------------------------------------


def test_the_root_marker_is_reestablished_and_never_shadows_the_tracked_config(
    fixture_repo,
):
    """`git clean -xdf` deletes the marker while sparing the tree (E11).

    Invisibility to `git status` was structural under `.git/` and is maintained
    here, so the maintenance has to happen on every drive or one ordinary
    operator keystroke leaves the repo permanently unclean.
    """
    main_root = gitops.main_worktree_root(fixture_repo)
    marker = WT.ensure_root_marker(main_root)
    assert marker == main_root / ".gauntlet" / "worktrees" / ".gitignore"
    assert marker.read_text() == "*\n"

    marker.unlink()
    assert WT.ensure_root_marker(main_root) is not None
    assert marker.read_text() == "*\n"

    # The adopter's tracked config lives one level up and must stay visible.
    (main_root / ".gauntlet" / "config.yaml").write_text("run_root: runs\n")
    assert not gitops.path_is_ignored(main_root, ".gauntlet/config.yaml")

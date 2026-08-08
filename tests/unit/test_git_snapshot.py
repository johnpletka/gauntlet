"""P2 — lossless Git recovery snapshots (recovery-redesign plan §4.4 / §6 P2).

Deterministic tests against real throwaway Git repositories, table-driven over
the P1 Git-state fixture matrix (``RecoveryGitFixture``). The properties under
test are the P2 acceptance criteria:

* snapshot creation is observational (R3): branch, HEAD, raw index bytes, and
  ``git status --porcelain=v2`` are untouched, across every fixture state;
* every independent state plane round-trips: staged B vs worktree C, tracked
  deletions, untracked files/directories, protected ignored paths, executable
  bits, symlink/regular transitions, and unmerged three-stage conflict indexes;
* an outside-target symlink is never followed at capture time nor written
  through at restore time;
* one durable ref (``refs/gauntlet/recovery/<run>/<snapshot>``) keeps every
  required object structurally reachable through Git's object graph;
* a failure before ref creation leaves zero mutations; a crash after ref
  creation leaves everything needed to reload and restore;
* restoration is exact and idempotent.

The pilot rewind-path ordering tests (snapshot-before-mutation on the
conflict-park clean restore) live with the pilot's harness in
``test_resume_disposition.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from conftest import RecoveryGitFixture, git

from gauntlet.engine import git_snapshot, gitops
from gauntlet.engine.git_snapshot import (
    SnapshotError,
    create_snapshot,
    load_snapshot,
    restore_protected,
    restore_snapshot,
    snapshot_ref,
)
from gauntlet.engine.gitops import GitError, TempIndexPathError


# --- harness -------------------------------------------------------------------


def _status(repo: Path) -> str:
    return git(repo, "status", "--porcelain=v2", "--untracked-files=all")


def _index_bytes(repo: Path) -> bytes:
    return gitops.git_index_path(repo).read_bytes()


def _index_stage_listing(repo: Path) -> str:
    """Semantic index equality witness (paths, modes, oids, stages)."""
    return git(repo, "ls-files", "--stage")


def _settle(repo: Path) -> None:
    """Let git finish any opportunistic index refresh before byte captures."""
    _status(repo)
    _status(repo)


def _snap(repo: Path, name: str, **kwargs) -> git_snapshot.GitRecoverySnapshot:
    kwargs.setdefault("run_id", "run-test")
    kwargs.setdefault("reason", "P2 test snapshot")
    kwargs.setdefault("snapshot_id", f"snap-{name}")
    kwargs.setdefault("created_at", "2026-06-24T00:00:00+00:00")
    return create_snapshot(repo, **kwargs)


def _wreck(repo: Path) -> None:
    """Destroy the dirty state the way a rewind would (reset + full clean)."""
    git(repo, "reset", "-q", "--hard", "HEAD")
    git(repo, "clean", "-fdxq")


# --- the P1-style Git-state matrix --------------------------------------------


def _s_clean(g: RecoveryGitFixture) -> None:
    pass


def _s_staged_only(g: RecoveryGitFixture) -> None:
    g.write("staged.txt", "B\n")
    g.stage("staged.txt")


def _s_unstaged_only(g: RecoveryGitFixture) -> None:
    g.write("mod.txt", "A\n")
    g.stage()
    g.commit()
    g.write("mod.txt", "A2\n")


def _s_staged_plus_unstaged(g: RecoveryGitFixture) -> None:
    g.write("dual.txt", "A\n")
    g.stage()
    g.commit()
    g.write("dual.txt", "B\n")
    g.stage("dual.txt")
    g.write("dual.txt", "C\n")


def _s_tracked_deletion(g: RecoveryGitFixture) -> None:
    g.write("gone.txt", "present\n")
    g.stage()
    g.commit()
    g.delete("gone.txt")


def _s_untracked_file(g: RecoveryGitFixture) -> None:
    g.write("new.txt", "untracked\n")


def _s_untracked_directory(g: RecoveryGitFixture) -> None:
    g.write("newdir/inner.txt", "inner\n")
    g.write("newdir/sub/deep.txt", "deep\n")


def _s_exec_bit(g: RecoveryGitFixture) -> None:
    g.write("tool.sh", "#!/bin/sh\n")
    g.stage()
    g.commit()
    g.write("tool.sh", "#!/bin/sh\n", executable=True)


def _s_regular_to_symlink(g: RecoveryGitFixture) -> None:
    g.write("morph", "regular\n")
    g.stage()
    g.commit()
    g.delete("morph")
    g.symlink("morph", "README.md")


def _s_symlink_to_regular(g: RecoveryGitFixture) -> None:
    g.symlink("morph2", "README.md")
    g.stage()
    g.commit()
    g.delete("morph2")
    g.write("morph2", "now regular\n")


def _s_conflict(g: RecoveryGitFixture) -> None:
    g.write("conflict.txt", "base\n")
    g.stage()
    g.commit("conflict base")
    g.create_branch("other")
    g.write("conflict.txt", "ours\n")
    g.stage()
    g.commit("ours side")
    g.switch("other")
    g.write("conflict.txt", "theirs\n")
    g.stage()
    g.commit("theirs side")
    g.switch("main")
    g.merge_conflict("other")


def _s_ignored_protected(g: RecoveryGitFixture) -> None:
    g.write(".gitignore", "PR.md\n")
    g.stage()
    g.commit("ignore PR.md")
    g.write("PR.md", "human notes\n")


def _v_staged_plus_unstaged(g: RecoveryGitFixture) -> None:
    assert (g.repo / "dual.txt").read_text() == "C\n"
    assert git(g.repo, "show", ":dual.txt") == "B\n"


def _v_tracked_deletion(g: RecoveryGitFixture) -> None:
    assert not (g.repo / "gone.txt").exists()


def _v_untracked_directory(g: RecoveryGitFixture) -> None:
    assert (g.repo / "newdir/inner.txt").read_text() == "inner\n"
    assert (g.repo / "newdir/sub/deep.txt").read_text() == "deep\n"


def _v_exec_bit(g: RecoveryGitFixture) -> None:
    assert (g.repo / "tool.sh").stat().st_mode & stat.S_IXUSR


def _v_regular_to_symlink(g: RecoveryGitFixture) -> None:
    path = g.repo / "morph"
    assert path.is_symlink()
    assert os.readlink(path) == "README.md"


def _v_symlink_to_regular(g: RecoveryGitFixture) -> None:
    path = g.repo / "morph2"
    assert not path.is_symlink()
    assert path.read_text() == "now regular\n"


def _v_conflict(g: RecoveryGitFixture) -> None:
    unmerged = git(g.repo, "ls-files", "--unmerged")
    assert len(unmerged.splitlines()) == 3  # stages 1..3 survive


def _v_ignored_protected(g: RecoveryGitFixture) -> None:
    assert (g.repo / "PR.md").read_text() == "human notes\n"


# (name, state builder, protected patterns, post-restore verifier)
STATES = [
    ("clean", _s_clean, None, None),
    ("staged_only", _s_staged_only, None, None),
    ("unstaged_only", _s_unstaged_only, None, None),
    ("staged_plus_unstaged_same_path", _s_staged_plus_unstaged, None,
     _v_staged_plus_unstaged),
    ("tracked_deletion", _s_tracked_deletion, None, _v_tracked_deletion),
    ("untracked_file", _s_untracked_file, None, None),
    ("untracked_directory", _s_untracked_directory, None,
     _v_untracked_directory),
    ("executable_bit", _s_exec_bit, None, _v_exec_bit),
    ("regular_to_symlink", _s_regular_to_symlink, None, _v_regular_to_symlink),
    ("symlink_to_regular", _s_symlink_to_regular, None, _v_symlink_to_regular),
    ("conflict_three_stage", _s_conflict, None, _v_conflict),
    ("ignored_protected", _s_ignored_protected, ["PR.md"],
     _v_ignored_protected),
]

_STATE_IDS = [name for name, *_ in STATES]


# --- R3: creation is observational --------------------------------------------


@pytest.mark.parametrize("name,build,protected,_verify", STATES, ids=_STATE_IDS)
def test_snapshot_creation_is_observational(recovery_git, name, build, protected, _verify):
    # Plan §6 P2 acceptance: creation leaves the checked-out branch, HEAD, the
    # REAL index bytes, and porcelain-v2 status byte-for-byte unchanged — for
    # every row of the Git-state matrix, including an unmerged conflict index.
    g = recovery_git
    build(g)
    repo = g.repo
    _settle(repo)
    before_branch = g.branch
    before_head = g.head_sha
    before_status = _status(repo)
    before_bytes = _index_bytes(repo)

    snapshot = _snap(repo, name, protected=list(protected or []))

    after_bytes = _index_bytes(repo)  # read BEFORE any further status refresh
    assert after_bytes == before_bytes
    assert g.branch == before_branch
    assert g.head_sha == before_head
    assert _status(repo) == before_status
    # The durable ref exists and names a commit whose parent is original HEAD.
    assert git(repo, "rev-parse", snapshot.ref).strip() == snapshot.snapshot_commit
    assert (
        git(repo, "rev-parse", f"{snapshot.ref}^").strip() == before_head
    )


# --- exact, idempotent restoration across the matrix --------------------------


@pytest.mark.parametrize("name,build,protected,verify", STATES, ids=_STATE_IDS)
def test_snapshot_roundtrip_restores_exact_state(recovery_git, name, build, protected, verify):
    # Wreck the worktree the way a rewind would (reset --hard + clean -fdx),
    # then restore: porcelain-v2 status and the semantic index listing must
    # come back exactly, and a SECOND restoration must change nothing.
    g = recovery_git
    build(g)
    repo = g.repo
    _settle(repo)
    before_status = _status(repo)
    before_stages = _index_stage_listing(repo)

    snapshot = _snap(repo, name, protected=list(protected or []))
    _wreck(repo)
    restore_snapshot(repo, snapshot)

    assert _status(repo) == before_status
    assert _index_stage_listing(repo) == before_stages
    if verify is not None:
        verify(g)

    restore_snapshot(repo, snapshot)  # idempotent: same state, no damage
    assert _status(repo) == before_status
    assert _index_stage_listing(repo) == before_stages
    if verify is not None:
        verify(g)


def test_staged_and_worktree_versions_are_captured_distinctly(recovery_git):
    # Plan §3: one tree cannot represent staged B and worktree C at the same
    # path — the snapshot must hold BOTH, in separate planes, and restore them
    # separately (index tree/raw bytes vs worktree tree).
    g = recovery_git
    _s_staged_plus_unstaged(g)
    repo = g.repo
    snapshot = _snap(repo, "dual")

    assert snapshot.index_tree is not None
    staged_oid = git(repo, "rev-parse", f"{snapshot.index_tree}:dual.txt").strip()
    worktree_oid = git(repo, "rev-parse", f"{snapshot.worktree_tree}:dual.txt").strip()
    assert git(repo, "cat-file", "blob", staged_oid) == "B\n"
    assert git(repo, "cat-file", "blob", worktree_oid) == "C\n"
    assert staged_oid != worktree_oid

    _wreck(repo)
    restore_snapshot(repo, snapshot)
    assert git(repo, "show", ":dual.txt") == "B\n"  # staged plane
    assert (repo / "dual.txt").read_text() == "C\n"  # worktree plane


# --- unmerged conflict index ---------------------------------------------------


def test_unmerged_index_snapshots_even_when_write_tree_fails(recovery_git):
    # ``git write-tree`` cannot represent conflict stages; the snapshot must
    # still succeed, carrying the raw index bytes as the authoritative record.
    g = recovery_git
    _s_conflict(g)
    repo = g.repo
    snapshot = _snap(repo, "conflict")
    assert snapshot.index_tree is None  # write-tree failed, by design
    assert snapshot.raw_index_oid is not None
    # The raw blob is byte-identical to the live conflicted index.
    assert gitops.cat_file_blob(repo, snapshot.raw_index_oid) == _index_bytes(repo)


def test_raw_conflict_index_is_restored_exactly(recovery_git):
    g = recovery_git
    _s_conflict(g)
    repo = g.repo
    _settle(repo)
    before_unmerged = git(repo, "ls-files", "--unmerged")
    before_status = _status(repo)
    assert before_unmerged  # a real three-stage conflict fixture

    snapshot = _snap(repo, "conflict-restore")
    git(repo, "reset", "-q", "--hard", "HEAD")  # destroys the conflict state
    assert git(repo, "ls-files", "--unmerged") == ""

    restore_snapshot(repo, snapshot)
    assert git(repo, "ls-files", "--unmerged") == before_unmerged
    assert _status(repo) == before_status


# --- symlink containment -------------------------------------------------------


def test_capture_records_outside_symlink_without_following(recovery_git, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    g = recovery_git
    g.symlink("evil", outside)
    repo = g.repo

    snapshot = _snap(repo, "outside-link")

    entry = git(repo, "ls-tree", snapshot.worktree_tree, "--", "evil")
    mode, _kind, rest = entry.split(maxsplit=2)
    assert mode == "120000"  # captured as a symlink entry, not target bytes
    oid = rest.split("\t")[0]
    assert gitops.cat_file_blob(repo, oid).decode() == str(outside)
    assert outside.read_text() == "secret\n"  # target never touched


def test_restore_never_writes_through_a_leaf_symlink(recovery_git, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    g = recovery_git
    g.write("victim.txt", "safe\n")
    g.stage()
    g.commit("victim baseline")
    repo = g.repo
    snapshot = _snap(repo, "leaf-link")

    # Between snapshot and restore, the path becomes a symlink to an outside
    # target. Restoration must unlink it and recreate the regular file — a
    # naive byte write would clobber the outside target through the link.
    (repo / "victim.txt").unlink()
    (repo / "victim.txt").symlink_to(outside)
    restore_snapshot(repo, snapshot)

    victim = repo / "victim.txt"
    assert not victim.is_symlink()
    assert victim.read_text() == "safe\n"
    assert outside.read_text() == "secret\n"  # never written through


def test_restore_never_traverses_a_symlinked_parent_directory(recovery_git, tmp_path):
    outside_dir = tmp_path / "outdir"
    outside_dir.mkdir()
    g = recovery_git
    g.write("sub/file.txt", "inner\n")
    g.stage()
    g.commit("nested baseline")
    repo = g.repo
    snapshot = _snap(repo, "parent-link")

    shutil.rmtree(repo / "sub")
    (repo / "sub").symlink_to(outside_dir)
    restore_snapshot(repo, snapshot)

    sub = repo / "sub"
    assert sub.is_dir() and not sub.is_symlink()
    assert (sub / "file.txt").read_text() == "inner\n"
    assert list(outside_dir.iterdir()) == []  # nothing materialized outside


# --- protected paths (scoped restoration) --------------------------------------


def test_untracked_protected_file_and_directory_restore_scoped(recovery_git):
    # The pilot's shape: a rewind (reset + clean) destroys untracked
    # human-owned paths; the scoped restore brings back exactly the protected
    # set — file and directory — from the durable snapshot.
    g = recovery_git
    g.write("PR.md", "human notes\n")
    g.write("docs/a.txt", "a\n")
    g.write("docs/sub/b.txt", "b\n")
    repo = g.repo
    snapshot = _snap(repo, "protected", protected=["PR.md", "docs"])
    assert set(snapshot.protected_paths) == {
        "PR.md", "docs/a.txt", "docs/sub/b.txt"
    }

    gitops.reset_hard(repo, g.head_sha)
    gitops.clean_untracked(repo)
    assert not (repo / "PR.md").exists()
    assert not (repo / "docs").exists()

    restore_protected(repo, snapshot)
    assert (repo / "PR.md").read_text() == "human notes\n"
    assert (repo / "docs/a.txt").read_text() == "a\n"
    assert (repo / "docs/sub/b.txt").read_text() == "b\n"

    restore_protected(repo, snapshot)  # idempotent
    assert (repo / "PR.md").read_text() == "human notes\n"


def test_protected_ignored_path_is_captured_and_retained(recovery_git):
    # A gitignored-but-protected path is invisible to status yet must be
    # force-included in the snapshot and restorable; a full restore must also
    # RETAIN (not delete) ignored files it never captured.
    g = recovery_git
    g.write(".gitignore", "PR.md\nscratch.log\n")
    g.stage()
    g.commit("ignore rules")
    g.write("PR.md", "human notes\n")
    g.write("scratch.log", "unprotected ignored\n")
    g.write("work.txt", "dirty\n")
    repo = g.repo

    snapshot = _snap(repo, "ignored", protected=["PR.md"])
    assert "PR.md" in snapshot.protected_paths
    tree_paths = git(
        repo, "ls-tree", "-r", "--name-only", snapshot.worktree_tree
    ).splitlines()
    assert "PR.md" in tree_paths
    assert "scratch.log" not in tree_paths  # unprotected ignored: not captured

    # Full restore over the live state: the unprotected ignored file survives
    # (it is not "extraneous" — ignored files are outside the captured plane).
    restore_snapshot(repo, snapshot)
    assert (repo / "scratch.log").read_text() == "unprotected ignored\n"
    assert (repo / "PR.md").read_text() == "human notes\n"

    # And after a rewind destroys the protected file, scoped restore returns it.
    (repo / "PR.md").unlink()
    restore_protected(repo, snapshot)
    assert (repo / "PR.md").read_text() == "human notes\n"


def test_protected_tracked_deletion_is_restored_after_reset(recovery_git):
    # A tracked protected file DELETED in the worktree: a reset --hard
    # resurrects it, so the snapshot must record the deletion and the scoped
    # restore must delete it again (the overlay tombstone semantics, durably).
    g = recovery_git
    g.write("PR.md", "draft\n")
    g.stage()
    g.commit("track PR.md")
    g.delete("PR.md")
    repo = g.repo

    snapshot = _snap(repo, "tombstone", protected=["PR.md"])
    assert "PR.md" in snapshot.protected_deletions

    gitops.reset_hard(repo, g.head_sha)  # resurrects the deleted file
    assert (repo / "PR.md").exists()
    restore_protected(repo, snapshot)
    assert not (repo / "PR.md").exists()
    restore_protected(repo, snapshot)  # idempotent
    assert not (repo / "PR.md").exists()


def test_excluded_engine_state_is_neither_captured_nor_deleted(recovery_git):
    # ``exclude`` (active engine state, e.g. the run root) stays out of the
    # snapshot AND out of the restore's extraneous-deletion pass — for BOTH
    # the untracked and the TRACKED case. The tracked case is review F-001:
    # a force-committed manifest is tracked, so the HEAD-seeded temp index
    # holds its committed baseline; if that entry reached the worktree tree,
    # a full restore would materialize the baseline OVER the live control
    # state (R8 violation). Excluded entries must be absent from the tree.
    g = recovery_git
    g.write("runs/demo/manifest.json", "committed baseline\n")
    g.stage()
    g.commit("track engine bookkeeping")
    g.write("runs/demo/manifest.json", "live control state v1\n")  # tracked+dirty
    g.write("runs/demo/scratch.txt", "untracked engine state\n")
    g.write("work.txt", "dirty\n")
    repo = g.repo
    snapshot = _snap(repo, "excluded", exclude=["runs"])

    tree_paths = git(
        repo, "ls-tree", "-r", "--name-only", snapshot.worktree_tree
    ).splitlines()
    assert "runs/demo/manifest.json" not in tree_paths
    assert "runs/demo/scratch.txt" not in tree_paths
    assert snapshot.excluded_paths == ("runs",)

    # The live engine advances between snapshot and restore; a full restore
    # must leave the advanced state completely untouched.
    g.write("runs/demo/manifest.json", "live control state v2\n")
    restore_snapshot(repo, snapshot)
    assert (repo / "runs/demo/manifest.json").read_text() == "live control state v2\n"
    assert (repo / "runs/demo/scratch.txt").read_text() == "untracked engine state\n"
    assert (repo / "work.txt").read_text() == "dirty\n"


def test_excluded_root_keeps_protected_carveout_in_tree(recovery_git):
    # Protected paths UNDER an excluded root (the pilot's runs/*/PR.md shape)
    # are the sanctioned carve-out: still captured even though their parent is
    # excluded active engine state.
    g = recovery_git
    g.write("runs/demo/manifest.json", "engine\n")
    g.write("runs/demo/PR.md", "human-owned\n")
    repo = g.repo
    snapshot = _snap(
        repo, "carveout", exclude=["runs"], protected=["runs/*/PR.md"]
    )
    tree_paths = git(
        repo, "ls-tree", "-r", "--name-only", snapshot.worktree_tree
    ).splitlines()
    assert "runs/demo/PR.md" in tree_paths
    assert "runs/demo/manifest.json" not in tree_paths
    assert snapshot.protected_paths == ("runs/demo/PR.md",)

    (repo / "runs/demo/PR.md").unlink()
    restore_protected(repo, snapshot)
    assert (repo / "runs/demo/PR.md").read_text() == "human-owned\n"
    assert (repo / "runs/demo/manifest.json").read_text() == "engine\n"


# --- durable ref reachability --------------------------------------------------


def test_snapshot_ref_keeps_every_required_object_reachable(recovery_git):
    # Structural reachability (deliverable 5): every object needed for exact
    # restoration — the raw-index blob, both trees, the metadata blob, the
    # per-plane content blobs, and the original HEAD and run-branch commits —
    # must be reachable from the ONE recovery ref through the object graph.
    g = recovery_git
    _s_staged_plus_unstaged(g)
    repo = g.repo
    g.create_branch("runb", "HEAD~1")  # a distinct run-branch tip
    runb_sha = git(repo, "rev-parse", "refs/heads/runb").strip()

    snapshot = _snap(repo, "reachable", run_branch="runb")
    assert snapshot.run_branch_sha == runb_sha

    reachable = {
        line.split()[0]
        for line in git(repo, "rev-list", "--objects", snapshot.ref).splitlines()
        if line
    }
    staged_oid = git(repo, "rev-parse", f"{snapshot.index_tree}:dual.txt").strip()
    worktree_oid = git(
        repo, "rev-parse", f"{snapshot.worktree_tree}:dual.txt"
    ).strip()
    metadata_oid = git(
        repo, "rev-parse", f"{snapshot.snapshot_commit}:metadata.json"
    ).strip()
    required = {
        snapshot.snapshot_commit,
        snapshot.worktree_tree,
        snapshot.index_tree,
        snapshot.raw_index_oid,
        snapshot.head_sha,
        runb_sha,
        staged_oid,
        worktree_oid,
        metadata_oid,
    }
    assert required <= reachable

    # The strong form: prune unreachable objects; everything required survives
    # purely because the recovery ref anchors it. (The raw-index blob in
    # particular is referenced by NOTHING else.)
    git(repo, "prune", "--expire=now")
    for oid in required:
        assert gitops.object_exists(repo, oid), f"{oid} pruned — not anchored"


# --- crash-window behaviour ----------------------------------------------------


@pytest.mark.parametrize("failing", ["mktree", "create_ref_exclusive"])
def test_failure_before_ref_creation_leaves_no_mutation(recovery_git, monkeypatch, failing):
    # A snapshot failure at any point before the ref exists must leave the
    # repository exactly as observed: same branch, HEAD, index bytes, status —
    # and no partial recovery ref (fail closed, R2/R3).
    g = recovery_git
    _s_staged_plus_unstaged(g)
    repo = g.repo
    _settle(repo)
    before = (g.branch, g.head_sha, _status(repo), _index_bytes(repo))

    def _boom(*args, **kwargs):
        raise GitError([failing], 128, "injected crash")

    monkeypatch.setattr(gitops, failing, _boom)
    with pytest.raises(GitError):
        _snap(repo, f"crash-{failing}")

    assert (g.branch, g.head_sha, _status(repo), _index_bytes(repo)) == before
    refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/gauntlet").strip()
    assert refs == ""


def test_crash_after_ref_creation_leaves_restorable_data(recovery_git):
    # Simulate losing the in-memory record right after the durable point: the
    # ref alone must reload a complete, validated snapshot that restores the
    # exact captured state.
    g = recovery_git
    _s_staged_plus_unstaged(g)
    g.delete("README.md")
    repo = g.repo
    _settle(repo)
    before_status = _status(repo)

    created = _snap(repo, "crashed", attempt_id="implement#3")
    ref = created.ref
    del created  # the process "died" here; only the ref survives

    loaded = load_snapshot(repo, ref)
    assert loaded.attempt_id == "implement#3"
    _wreck(repo)
    restore_snapshot(repo, loaded)
    assert _status(repo) == before_status
    assert (repo / "dual.txt").read_text() == "C\n"
    assert not (repo / "README.md").exists()


def test_loaded_snapshot_metadata_is_complete_and_equal(recovery_git):
    g = recovery_git
    _s_staged_plus_unstaged(g)
    repo = g.repo
    snapshot = _snap(
        repo, "meta", attempt_id="implement#1", run_branch="main",
        protected=[], exclude=[],
    )
    text = gitops.file_at_commit(repo, snapshot.snapshot_commit, "metadata.json")
    data = json.loads(text)
    assert data["schema_version"] == git_snapshot.SNAPSHOT_SCHEMA_VERSION
    assert data["snapshot_id"] == "snap-meta"
    assert data["run_id"] == "run-test"
    assert data["attempt_id"] == "implement#1"
    assert data["reason"] == "P2 test snapshot"
    assert data["created_at"] == "2026-06-24T00:00:00+00:00"
    assert data["checked_out_branch"] == "main"
    assert data["head_sha"] == snapshot.head_sha
    assert data["run_branch"] == "main"
    assert data["run_branch_sha"] == snapshot.head_sha
    assert data["raw_index_oid"] == snapshot.raw_index_oid
    assert data["index_tree"] == snapshot.index_tree
    assert data["worktree_tree"] == snapshot.worktree_tree
    assert data["ref"] == snapshot.ref
    assert "snapshot_commit" not in data  # it IS the commit carrying the data

    assert load_snapshot(repo, snapshot.ref) == snapshot


def test_load_refuses_a_moved_or_renamed_ref(recovery_git):
    g = recovery_git
    _s_staged_only(g)
    repo = g.repo
    snapshot = _snap(repo, "moved")
    moved = f"{git_snapshot.RECOVERY_REF_PREFIX}/run-test/elsewhere"
    gitops.create_ref(repo, moved, snapshot.snapshot_commit)
    with pytest.raises(SnapshotError, match="moved or renamed"):
        load_snapshot(repo, moved)


def test_duplicate_snapshot_ref_fails_closed(recovery_git):
    g = recovery_git
    _s_staged_only(g)
    repo = g.repo
    _snap(repo, "dup")
    with pytest.raises(SnapshotError, match="already exists"):
        _snap(repo, "dup")


def test_racing_snapshot_creator_cannot_displace_a_winner(recovery_git, monkeypatch):
    # Review F-003: ref creation must be create-only at the ref store, not
    # check-then-write. Deterministic race simulation: another creator lands
    # the SAME ref in the window between this creator's existence probe and
    # its ref creation (interposed inside commit_tree, which runs after the
    # probe and before create_ref_exclusive). The loser must error — and the
    # winner's snapshot must remain exactly where it was, never overwritten.
    g = recovery_git
    _s_staged_only(g)
    repo = g.repo
    ref = snapshot_ref("run-test", "snap-race")
    winner_sha = g.head_sha
    real_commit_tree = gitops.commit_tree

    def _interposed(repo_path, tree, parents, message, *, identity):
        gitops.create_ref(repo_path, ref, winner_sha)  # the racing winner
        return real_commit_tree(repo_path, tree, parents, message, identity=identity)

    monkeypatch.setattr(gitops, "commit_tree", _interposed)
    with pytest.raises(SnapshotError, match="already exists"):
        _snap(repo, "race")
    # The winner's ref is untouched — the loser did not displace it.
    assert git(repo, "rev-parse", ref).strip() == winner_sha


def test_create_ref_exclusive_is_create_only(recovery_git):
    repo = recovery_git.repo
    sha = recovery_git.head_sha
    gitops.create_ref_exclusive(repo, "refs/gauntlet/recovery/x/one", sha)
    assert git(repo, "rev-parse", "refs/gauntlet/recovery/x/one").strip() == sha
    with pytest.raises(GitError):
        gitops.create_ref_exclusive(repo, "refs/gauntlet/recovery/x/one", sha)


def test_snapshot_ref_components_are_validated():
    with pytest.raises(ValueError):
        snapshot_ref("run/../evil", "snap")
    with pytest.raises(ValueError):
        snapshot_ref("run", "snap name")
    with pytest.raises(ValueError):
        snapshot_ref("run", "snap.lock")
    assert snapshot_ref("run-1", "s.1") == "refs/gauntlet/recovery/run-1/s.1"


# --- the narrow Git command-layer extension ------------------------------------


def test_temp_index_path_containment(recovery_git, tmp_path):
    repo = recovery_git.repo
    # Relative paths, worktree-internal paths, and anything under the git dir
    # (including the REAL index) are refused before git ever runs.
    with pytest.raises(TempIndexPathError):
        gitops.validate_temp_index_path(repo, Path("relative-index"))
    with pytest.raises(TempIndexPathError):
        gitops.validate_temp_index_path(repo, repo / "scratch-index")
    with pytest.raises(TempIndexPathError):
        gitops.validate_temp_index_path(repo, gitops.git_index_path(repo))
    with pytest.raises(TempIndexPathError):
        gitops.validate_temp_index_path(repo, repo / ".git" / "sneaky-index")

    # A contained temp path works, and operations against it never touch the
    # real index: write-tree over a HEAD-seeded temp index equals HEAD's tree.
    _settle(repo)
    before_bytes = _index_bytes(repo)
    idx = tmp_path / "scratch" / "index"
    idx.parent.mkdir()
    gitops.run_with_temp_index(repo, idx, "read-tree", "HEAD")
    tree = gitops.run_with_temp_index(repo, idx, "write-tree").strip()
    assert tree == git(repo, "rev-parse", "HEAD^{tree}").strip()
    assert _index_bytes(repo) == before_bytes


def test_temp_index_containment_covers_the_shared_git_dir_from_a_linked_worktree(
    recovery_git, tmp_path
):
    """A linked worktree must not widen the temp-index containment rule (P7 §9.2).

    ``--absolute-git-dir`` inside a linked worktree names only that worktree's
    private admin dir, so a check built on it alone ACCEPTS a scratch path in
    the shared ``.git`` — where the real index, refs and objects live, and the
    very path this same call refuses from the main worktree. The guard must
    consider ``--git-common-dir`` too.
    """
    repo = recovery_git.repo
    linked = tmp_path / "linked-wt"
    gitops.add_worktree(repo, linked, "HEAD")
    try:
        common = gitops.git_common_dir(linked)
        private = Path(
            git(linked, "rev-parse", "--absolute-git-dir").strip()
        ).resolve()
        # Precondition: this fixture really is a linked worktree (the two dirs
        # differ). Without it the test would pass vacuously in a main worktree.
        assert private != common
        assert common == gitops.git_common_dir(repo)

        # The shared git dir is refused from BOTH trees — the asymmetry is gone.
        with pytest.raises(TempIndexPathError):
            gitops.validate_temp_index_path(linked, common / "sneaky-index")
        with pytest.raises(TempIndexPathError):
            gitops.validate_temp_index_path(repo, common / "sneaky-index")
        # The worktree's own admin dir and its tree stay refused as before.
        with pytest.raises(TempIndexPathError):
            gitops.validate_temp_index_path(linked, private / "sneaky-index")
        with pytest.raises(TempIndexPathError):
            gitops.validate_temp_index_path(linked, linked / "scratch-index")
        # A genuinely external path is still accepted from the linked worktree.
        gitops.validate_temp_index_path(linked, tmp_path / "outside-index")
    finally:
        gitops.remove_worktree(repo, linked)


def test_temp_index_symlinked_parent_is_resolved_before_containment(recovery_git, tmp_path):
    # A symlink outside the repo pointing INTO it must not smuggle the temp
    # index inside: containment is checked on the resolved location.
    repo = recovery_git.repo
    link_dir = tmp_path / "sneaky"
    link_dir.symlink_to(repo)
    with pytest.raises(TempIndexPathError):
        gitops.validate_temp_index_path(repo, link_dir / "index")


# --- fault injection across restoration boundaries (plan §7) --------------------
#
# Plan §7 lists "before and after worktree/index restoration" among the
# transaction fault injection points. At this library boundary the §7 global
# acceptance property specializes to: a restoration killed at ANY internal
# boundary leaves the durable snapshot ref fully loadable and writes nothing
# outside the repository, and re-running the restoration — the library-level
# replay — converges to the exact captured state, idempotently. (The executor
# transaction's protected-restore step has its own boundary rows in
# test_recovery_executor.py; these rows cover the full restoration verb.)


class _RestoreBoom(RuntimeError):
    pass


def _build_restore_rich_state(g: RecoveryGitFixture, outside: Path) -> None:
    """Every restoration code path in one state: staged-B-vs-worktree-C
    divergence, a tracked deletion, an executable bit, an untracked directory,
    a gitignored protected file, a protected deletion, and an untracked
    symlink to an outside target (the containment witness)."""
    g.write("dual.txt", "A\n")
    g.write("gone.txt", "present\n")
    g.write("tool.sh", "#!/bin/sh\n")
    g.write("PR.md", "committed notes\n")
    g.write(".gitignore", "NOTES.md\n")
    g.stage()
    g.commit("rich baseline")
    g.write("dual.txt", "B\n")
    g.stage("dual.txt")
    g.write("dual.txt", "C\n")
    g.delete("gone.txt")
    g.write("tool.sh", "#!/bin/sh\n", executable=True)
    g.delete("PR.md")  # protected deletion vs HEAD
    g.write("NOTES.md", "human notes\n")  # gitignored, protected
    g.write("newdir/inner.txt", "inner\n")
    g.symlink("evil", outside)


def _assert_rich_state(g: RecoveryGitFixture, outside: Path) -> None:
    repo = g.repo
    assert (repo / "dual.txt").read_text() == "C\n"  # worktree plane
    assert git(repo, "show", ":dual.txt") == "B\n"  # staged plane
    assert not (repo / "gone.txt").exists()
    assert (repo / "tool.sh").stat().st_mode & stat.S_IXUSR
    assert not (repo / "PR.md").exists()  # protected deletion reproduced
    assert (repo / "NOTES.md").read_text() == "human notes\n"
    assert (repo / "newdir" / "inner.txt").read_text() == "inner\n"
    evil = repo / "evil"
    assert evil.is_symlink() and os.readlink(evil) == str(outside)
    assert outside.read_text() == "secret\n"  # never written through


def _fault_before_read_tree(monkeypatch):
    real = gitops.run_with_temp_index

    def exploding(work_root, index, *args, **kwargs):
        if "read-tree" in args:
            raise _RestoreBoom("killed before read-tree")
        return real(work_root, index, *args, **kwargs)

    monkeypatch.setattr(gitops, "run_with_temp_index", exploding)


def _fault_before_checkout_index(monkeypatch):
    real = gitops.run_with_temp_index

    def exploding(work_root, index, *args, **kwargs):
        if "checkout-index" in args:
            raise _RestoreBoom("killed before checkout-index")
        return real(work_root, index, *args, **kwargs)

    monkeypatch.setattr(gitops, "run_with_temp_index", exploding)


def _nth_call_fault(monkeypatch, name: str, nth: int):
    """Let ``nth - 1`` real calls of ``git_snapshot.<name>`` run, then explode
    — a kill landing partway through a multi-entry pass, not before it."""
    real = getattr(git_snapshot, name)
    calls = {"n": 0}

    def exploding(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= nth:
            raise _RestoreBoom(f"killed at {name} call {nth}")
        return real(*args, **kwargs)

    monkeypatch.setattr(git_snapshot, name, exploding)


def _fault_mid_extraneous_removal(monkeypatch):
    _nth_call_fault(monkeypatch, "_safe_unlink", 2)


def _fault_mid_destination_prep(monkeypatch):
    _nth_call_fault(monkeypatch, "_prepare_destination", 2)


def _fault_before_raw_index_restore(monkeypatch):
    _nth_call_fault(monkeypatch, "_restore_raw_index", 1)


def _fault_mid_raw_index_replace(monkeypatch):
    # The raw-index bytes are written to a temp file, then os.replace'd. A
    # kill between the two must not corrupt the index nor block a replay.
    def exploding(*args, **kwargs):
        raise _RestoreBoom("killed between temp write and replace")

    monkeypatch.setattr(os, "replace", exploding)


_RESTORE_FAULTS = [
    # (boundary, fault installer, expectation)
    ("before_read_tree", _fault_before_read_tree, "untouched"),
    ("mid_extraneous_removal", _fault_mid_extraneous_removal, "partial"),
    ("before_checkout_index", _fault_before_checkout_index, "partial"),
    ("mid_destination_prep", _fault_mid_destination_prep, "partial"),
    ("before_raw_index_restore", _fault_before_raw_index_restore, "partial"),
    ("mid_raw_index_replace", _fault_mid_raw_index_replace, "partial"),
]


@pytest.mark.parametrize(
    "boundary,install_fault,expectation",
    _RESTORE_FAULTS,
    ids=[row[0] for row in _RESTORE_FAULTS],
)
def test_restore_killed_at_each_boundary_replays_to_convergence(
    recovery_git, tmp_path, monkeypatch, boundary, install_fault, expectation
):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    g = recovery_git
    _build_restore_rich_state(g, outside)
    repo = g.repo
    _settle(repo)
    before_status = _status(repo)
    before_stages = _index_stage_listing(repo)

    snapshot = _snap(repo, f"fault-{boundary}", protected=["PR.md", "NOTES.md"])
    assert snapshot.protected_deletions == ("PR.md",)  # both planes captured
    assert "NOTES.md" in snapshot.protected_paths
    _wreck(repo)
    wrecked_status = _status(repo)

    install_fault(monkeypatch)
    with pytest.raises(_RestoreBoom):
        restore_snapshot(repo, snapshot)
    monkeypatch.undo()  # the "next process" runs the real code

    # Whatever partial worktree state the kill left, the durable snapshot is
    # untouched and fully loadable, and nothing left the repository.
    assert git(repo, "rev-parse", snapshot.ref).strip() == snapshot.snapshot_commit
    reloaded = load_snapshot(repo, snapshot.ref)
    assert reloaded.worktree_tree == snapshot.worktree_tree
    assert outside.read_text() == "secret\n"
    if expectation == "untouched":
        assert _status(repo) == wrecked_status  # crashed before any mutation

    # The replay — the same restoration, re-run — converges exactly...
    restore_snapshot(repo, snapshot)
    assert _status(repo) == before_status
    assert _index_stage_listing(repo) == before_stages
    _assert_rich_state(g, outside)

    # ...and repeating it converges again (idempotent, no damage).
    restore_snapshot(repo, snapshot)
    assert _status(repo) == before_status
    assert _index_stage_listing(repo) == before_stages
    _assert_rich_state(g, outside)

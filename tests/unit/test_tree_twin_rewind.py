"""#132 — orphaned rewind targets resolve to their reachable tree twin.

A sanctioned recovery reconciliation (fork preservation + a linear
``commit-tree`` restore) carries a commit's exact tree forward under a new
sha. Recorded rewind targets naming the orphaned sha must resolve to the
reachable twin instead of leaving the cycle terminal.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import gitops
from gauntlet.engine.recovery_exec import tree_equal_reachable_commit


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(r), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (r / "a.txt").write_text("base\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    return r


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_an_orphaned_commit_resolves_to_its_reachable_tree_twin(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    # Line B: real work, then orphan it by moving the branch elsewhere.
    (repo / "work.txt").write_text("completion\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "completion")
    orphaned = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "linear", base)
    # Line A: bookkeeping commit, then the linear commit-tree restore of B's tree.
    (repo / "marker.txt").write_text("bookkeeping\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "bookkeeping")
    twin = _git(repo, "commit-tree", f"{orphaned}^{{tree}}",
                "-p", "HEAD", "-m", "linear restore")
    _git(repo, "update-ref", "refs/heads/linear", twin)
    tip = twin

    assert not gitops.is_ancestor(repo, orphaned, tip)
    # The orphan is preserved: `main` still reaches it (the shape the
    # fork-preservation ref leaves behind), so it may resolve.
    assert gitops.refs_containing(repo, orphaned)
    assert tree_equal_reachable_commit(repo, orphaned, tip=tip) == twin


def test_an_unpreserved_orphan_never_resolves_even_with_a_twin(repo: Path) -> None:
    # Fail closed: tree equality alone must not launder a dangling commit
    # past the fork guard. Same twin shape as above, but no ref keeps the
    # orphan alive — no sanctioned reconciliation produced it.
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "work.txt").write_text("completion\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "completion")
    orphaned = _git(repo, "rev-parse", "HEAD")
    _git(repo, "reset", "-q", "--hard", base)  # main no longer reaches it
    assert not gitops.refs_containing(repo, orphaned)
    twin = _git(repo, "commit-tree", f"{orphaned}^{{tree}}",
                "-p", "HEAD", "-m", "linear restore")
    _git(repo, "update-ref", "refs/heads/main", twin)

    assert gitops.first_commit_with_tree(repo, twin, _git(repo, "rev-parse", f"{orphaned}^{{tree}}")) == twin
    assert tree_equal_reachable_commit(repo, orphaned, tip=twin) is None


def test_the_twin_walk_stops_at_the_newest_match(repo: Path) -> None:
    # Two reachable commits share the tree; the NEWEST one is the answer, and
    # the streaming walk returns it without needing the older one.
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "b.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add b")
    _git(repo, "rm", "-q", "b.txt")
    _git(repo, "commit", "-qm", "remove b again")  # tree == base's tree
    newest = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "rev-parse", f"{base}^{{tree}}")
    assert gitops.first_commit_with_tree(repo, newest, tree) == newest


def test_a_reachable_target_passes_through_unchanged(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "b.txt").write_text("more\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "more")
    tip = _git(repo, "rev-parse", "HEAD")
    assert tree_equal_reachable_commit(repo, base, tip=tip) == base


def test_no_twin_and_bad_shas_return_none(repo: Path) -> None:
    tip = _git(repo, "rev-parse", "HEAD")
    # A commit whose tree exists nowhere on the tip's line.
    _git(repo, "checkout", "-qb", "other")
    (repo / "unique.txt").write_text("nowhere else\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unique")
    stranger = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    assert tree_equal_reachable_commit(repo, stranger, tip=tip) is None
    assert tree_equal_reachable_commit(repo, None, tip=tip) is None
    assert tree_equal_reachable_commit(repo, "0" * 40, tip=tip) is None

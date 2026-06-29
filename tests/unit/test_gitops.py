"""Git wrapper helpers (FR-9), against throwaway fixture repos."""

from gauntlet.engine import gitops
from gauntlet.engine.gitops import Identity


def test_clean_and_dirty(fixture_repo):
    assert gitops.is_clean(fixture_repo)
    (fixture_repo / "x.py").write_text("hi")
    assert not gitops.is_clean(fixture_repo)


def test_is_clean_ignores_show_untracked_files_config(fixture_repo):
    """Safety checks must not depend on adopter-local git config (review).

    With ``status.showUntrackedFiles=no`` a bare ``git status --porcelain``
    omits untracked files entirely — reporting a false "clean" tree — which
    would silently bypass the FR-9.3 clean-handoff guard and FR-9.6 mutation
    detection. ``status_porcelain`` pins ``--untracked-files`` explicitly, so
    untracked work is still seen regardless of the config.
    """
    gitops._run(fixture_repo, "config", "status.showUntrackedFiles", "no")
    (fixture_repo / "stray.txt").write_text("untracked work\n")
    assert not gitops.is_clean(fixture_repo)
    assert "stray.txt" in gitops.status_porcelain(fixture_repo)
    assert "stray.txt" in gitops.status_porcelain(fixture_repo, untracked_all=True)


def test_commit_all_uses_identity(fixture_repo):
    (fixture_repo / "f.py").write_text("code")
    sha = gitops.commit_all(
        fixture_repo, "P1: add f\n\nbody", identity=Identity("Builder X", "bx@g.local")
    )
    assert gitops.commit_subject(fixture_repo, sha) == "P1: add f"
    author = gitops._run(fixture_repo, "log", "-1", "--format=%an <%ae>", sha).strip()
    assert author == "Builder X <bx@g.local>"


def test_commit_paths_excludes_pre_staged_files(fixture_repo):
    """commit_paths is pathspec-limited: a file already staged in the index when
    it runs is NOT swept into the commit (it stays staged, uncommitted)."""
    (fixture_repo / "unrelated.txt").write_text("operator's other work\n")
    gitops._run(fixture_repo, "add", "unrelated.txt")  # pre-staged, not ours
    (fixture_repo / "artifact.txt").write_text("the deliverable\n")
    sha = gitops.commit_paths(
        fixture_repo, "PLAN: author artifact\n\nbody", ["artifact.txt"],
        identity=Identity("Builder", "b@g.local"),
    )
    files = gitops._run(
        fixture_repo, "show", "--name-only", "--format=", sha
    ).split()
    assert files == ["artifact.txt"]  # ONLY the named path, never the pre-staged file
    # the pre-staged file is left exactly as it was — staged, uncommitted.
    assert "A  unrelated.txt" in gitops.status_porcelain(fixture_repo)


def test_is_dirty_vs_base(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    assert not gitops.is_dirty_vs(fixture_repo, base)
    (fixture_repo / "x.py").write_text("hi")
    assert gitops.is_dirty_vs(fixture_repo, base)


def test_branch_create_and_checkout(fixture_repo):
    gitops.checkout_or_create_branch(fixture_repo, "gauntlet/demo", "HEAD")
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"
    # idempotent re-checkout
    gitops.checkout_or_create_branch(fixture_repo, "gauntlet/demo", "HEAD")
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"


def test_is_ancestor(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "x.py").write_text("hi")
    head = gitops.commit_all(fixture_repo, "P1: x\n\nb", identity=Identity("a", "a@b.c"))
    assert gitops.is_ancestor(fixture_repo, base, head)
    assert not gitops.is_ancestor(fixture_repo, head, base)


def test_backup_and_clean_round_trip(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "untracked.py").write_text("partial")
    gitops.backup_dirty_worktree(fixture_repo, "refs/gauntlet/backup/test", "snap")
    gitops.reset_hard(fixture_repo, base)
    gitops.clean_untracked(fixture_repo)
    assert not (fixture_repo / "untracked.py").exists()
    refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/backup/")
    assert "refs/gauntlet/backup/test" in refs

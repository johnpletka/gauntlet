"""Git wrapper helpers (FR-9), against throwaway fixture repos."""

from gauntlet.engine import gitops
from gauntlet.engine.gitops import Identity


def test_clean_and_dirty(fixture_repo):
    assert gitops.is_clean(fixture_repo)
    (fixture_repo / "x.py").write_text("hi")
    assert not gitops.is_clean(fixture_repo)


def test_commit_all_uses_identity(fixture_repo):
    (fixture_repo / "f.py").write_text("code")
    sha = gitops.commit_all(
        fixture_repo, "P1: add f\n\nbody", identity=Identity("Builder X", "bx@g.local")
    )
    assert gitops.commit_subject(fixture_repo, sha) == "P1: add f"
    author = gitops._run(fixture_repo, "log", "-1", "--format=%an <%ae>", sha).strip()
    assert author == "Builder X <bx@g.local>"


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

"""Git wrapper helpers (FR-9), against throwaway fixture repos."""

import pytest

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


def test_path_is_untracked_distinguishes_tracked_states(fixture_repo):
    """Only a genuinely untracked file reports ``??``; tracked files never do.

    The review command uses this to exempt an in-repo ``--intent`` file from the
    clean checks ONLY when it is untracked user-owned dirt — a tracked file,
    clean or modified, must not be masked (FR-2.4/FR-9.2).
    """
    untracked = fixture_repo / "note.md"
    untracked.write_text("scratch\n")
    assert gitops.path_is_untracked(fixture_repo, "note.md")

    # Once committed it is tracked and clean => not untracked.
    gitops.commit_all(
        fixture_repo, "P1: add note\n\nbody", identity=Identity("B", "b@g.local")
    )
    assert not gitops.path_is_untracked(fixture_repo, "note.md")

    # Tracked + modified is still not "untracked" (it must trip the clean check).
    untracked.write_text("edited\n")
    assert not gitops.path_is_untracked(fixture_repo, "note.md")


def test_is_tracked_is_reliable_for_gitignored_paths(fixture_repo):
    """``is_tracked`` reports index membership, not the ``??`` porcelain state.

    A gitignored-but-untracked file emits no ``??`` line (ignored entries are
    hidden), so a "not untracked" test would misread it as tracked. ``is_tracked``
    uses ``ls-files`` so it stays correct there — the F-001 flush relies on this to
    avoid ``git add``-ing an ignored path without ``-f`` (the #33 clash).
    """
    run_dir = fixture_repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")  # run-dir self-ignore
    (run_dir / "manifest.json").write_text("{}\n")
    rel = "runs/demo/run-1/manifest.json"

    # Gitignored + untracked → NOT tracked (though path_is_untracked hides it).
    assert not gitops.is_tracked(fixture_repo, rel)
    assert not gitops.path_is_untracked(fixture_repo, rel)  # ignored → no `??`

    # Force past the ignore rule (the FR-2.2 response-checkpoint mechanism) → tracked.
    gitops._run(fixture_repo, "add", "-f", "--", rel)
    gitops._run(fixture_repo, "commit", "-qm", "track manifest")
    assert gitops.is_tracked(fixture_repo, rel)


def test_commit_tracked_bookkeeping_commits_only_tracked_dirty(fixture_repo):
    """F-001: re-commit already-tracked, dirty run bookkeeping — never force-add.

    Once a response checkpoint force-tracked the bookkeeping, later live updates
    dirty a raw ``git status``; this flush re-commits them so a handoff is clean.
    It commits ONLY tracked+dirty paths, is idempotent, and is a no-op (never an
    #33 error) when the bookkeeping is still untracked+ignored.
    """
    ident = Identity("Gauntlet Engine", "engine@gauntlet.local")
    run_dir = fixture_repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")
    man = run_dir / "manifest.json"
    runmd = run_dir / "RUN.md"
    man.write_text("{}\n")
    runmd.write_text("# run\n")
    paths = ["runs/demo/run-1/manifest.json", "runs/demo/run-1/RUN.md"]

    # Untracked + ignored: flushing is a no-op (must NOT force-track / must NOT
    # raise the "paths are ignored" error).
    assert gitops.commit_tracked_bookkeeping(
        fixture_repo, "gauntlet: flush", paths, identity=ident
    ) is None
    assert not gitops.is_tracked(fixture_repo, paths[0])

    # Track them (simulating a prior FR-2.2 response checkpoint).
    gitops._run(fixture_repo, "add", "-f", "--", *paths)
    gitops._run(fixture_repo, "commit", "-qm", "track bookkeeping")

    # Clean now → idempotent no-op.
    assert gitops.commit_tracked_bookkeeping(
        fixture_repo, "gauntlet: flush", paths, identity=ident
    ) is None

    # Live update dirties the tracked bookkeeping → flush commits ONLY those paths.
    man.write_text('{"totals": 1}\n')
    (fixture_repo / "impl.py").write_text("unrelated work\n")  # dirty non-bookkeeping
    sha = gitops.commit_tracked_bookkeeping(
        fixture_repo, "gauntlet: flush run bookkeeping", paths, identity=ident
    )
    assert sha is not None
    files = gitops._run(
        fixture_repo, "show", "--name-only", "--format=", sha
    ).split()
    assert files == ["runs/demo/run-1/manifest.json"]  # RUN.md unchanged, not committed
    # Engine-attributed, and the unrelated implementation work is untouched.
    assert gitops._run(fixture_repo, "log", "-1", "--format=%an|%ae", sha).strip() == (
        "Gauntlet Engine|engine@gauntlet.local"
    )
    assert gitops.path_is_untracked(fixture_repo, "impl.py")


def test_file_at_commit_reads_history_and_none_when_absent(fixture_repo):
    """file_at_commit reads a path's committed bytes (FR-3.3 deferral injection
    reads a prior phase's committed acceptance-map out of history), and returns
    None — not an error — for a path absent at that commit."""
    (fixture_repo / "a.json").write_text('{"phase": "P2"}')
    gitops._run(fixture_repo, "add", "a.json")
    sha = gitops.commit_all(
        fixture_repo, "P2: add a\n\nbody", identity=Identity("B", "b@g.local")
    )
    assert gitops.file_at_commit(fixture_repo, sha, "a.json") == '{"phase": "P2"}'
    # a path that does not exist at that commit -> None (git show exits non-zero)
    assert gitops.file_at_commit(fixture_repo, sha, "nope.json") is None


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


# --- intra-phase checkpoint discovery (harness-efficiency FR-11.1/11.2) -------
def _wip(repo, phase_subject: str, rel: str, content: str) -> str:
    """Write `rel` and commit it as a `P<N> wip:` checkpoint; return the SHA."""
    (repo / rel).write_text(content)
    return gitops.commit_all(
        repo, f"{phase_subject}\n\nbody", identity=Identity("B", "b@g.local")
    )


def test_wip_checkpoints_trailing_run_stops_at_non_checkpoint(fixture_repo):
    """Without a base, the TRAILING run of `P<N> wip:` commits is returned,
    newest first, stopping at the first non-checkpoint commit (FR-11.1)."""
    # A prior phase commit (non-checkpoint) then two checkpoints for this phase.
    gitops.commit_all(
        fixture_repo, "P8: prior phase\n\nbody", identity=Identity("B", "b@g.local"),
        allow_empty=True,
    )
    m1 = _wip(fixture_repo, "P9 wip: model layer", "a.py", "a\n")
    m2 = _wip(fixture_repo, "P9 wip: cli wiring", "b.py", "b\n")
    wips = gitops.wip_checkpoints(fixture_repo)
    assert [s for _sha, s in wips] == ["P9 wip: cli wiring", "P9 wip: model layer"]
    assert [sha for sha, _s in wips] == [m2, m1]  # newest first
    # squash base = parent of the oldest checkpoint = the prior P8: commit.
    assert gitops.commit_parent(fixture_repo, m1) == gitops.rev_parse(
        fixture_repo, "HEAD~2"
    )


def test_wip_checkpoints_since_base_are_descendants(fixture_repo):
    """With a base, every `P<N> wip:` commit in base..HEAD is returned newest
    first — the recovery rewind candidates (FR-11.2)."""
    base = gitops.head_sha(fixture_repo)
    m1 = _wip(fixture_repo, "P3 wip: one", "a.py", "a\n")
    m2 = _wip(fixture_repo, "P3 wip: two", "b.py", "b\n")
    wips = gitops.wip_checkpoints(fixture_repo, base=base)
    assert [sha for sha, _s in wips] == [m2, m1]
    # A commit at/under the base is excluded (base..HEAD is exclusive of base).
    assert base not in [sha for sha, _s in wips]


def test_wip_checkpoints_none_when_no_checkpoints(fixture_repo):
    (fixture_repo / "x.py").write_text("x\n")
    gitops.commit_all(
        fixture_repo, "P1: normal phase\n\nbody", identity=Identity("B", "b@g.local")
    )
    assert gitops.wip_checkpoints(fixture_repo) == []


def test_wip_checkpoints_scoped_to_phase_ignores_other_phases_in_range(fixture_repo):
    """With a base, a `phase` scope collects ONLY that phase's checkpoints, so a
    wrong-phase wip is never chosen as the recovery rewind target (review F-001)."""
    base = gitops.head_sha(fixture_repo)
    p3a = _wip(fixture_repo, "P3 wip: one", "a.py", "a\n")
    _wip(fixture_repo, "P4 wip: stray", "b.py", "b\n")  # wrong phase, newest
    scoped = gitops.wip_checkpoints(fixture_repo, base=base, phase="P3")
    assert [s for _sha, s in scoped] == ["P3 wip: one"]
    assert scoped[0][0] == p3a  # newest P3 checkpoint is the rewind target
    # Unscoped still sees both (legacy behaviour) — and would pick the stray.
    unscoped = gitops.wip_checkpoints(fixture_repo, base=base)
    assert [s for _sha, s in unscoped] == ["P4 wip: stray", "P3 wip: one"]


def test_wip_checkpoints_trailing_run_fails_closed_on_wrong_phase(fixture_repo):
    """A wrong-phase `P<N> wip:` inside this phase's trailing run fails closed
    rather than being squashed into the wrong phase (review F-001)."""
    gitops.commit_all(
        fixture_repo, "P8: prior\n\nbody", identity=Identity("B", "b@g.local"),
        allow_empty=True,
    )
    _wip(fixture_repo, "P9 wip: real", "a.py", "a\n")
    _wip(fixture_repo, "P8 wip: mistyped", "b.py", "b\n")  # newest, wrong phase
    with pytest.raises(gitops.WrongPhaseCheckpointError) as exc:
        gitops.wip_checkpoints(fixture_repo, phase="P9")
    assert exc.value.expected_phase == "P9"
    assert exc.value.found_subject == "P8 wip: mistyped"


def test_wip_checkpoints_trailing_run_walks_through_engine_commits(fixture_repo):
    """An engine bookkeeping (`gauntlet:`) commit between this phase's wip commits
    is transparent — the walk finds the checkpoints beneath it (review F-002)."""
    gitops.commit_all(
        fixture_repo, "P8: prior\n\nbody", identity=Identity("B", "b@g.local"),
        allow_empty=True,
    )
    m1 = _wip(fixture_repo, "P9 wip: one", "a.py", "a\n")
    m2 = _wip(fixture_repo, "P9 wip: two", "b.py", "b\n")
    # An engine bookkeeping commit lands on top (e.g. a recovery rewind commit).
    gitops.commit_all(
        fixture_repo, "gauntlet: response x consumed\n\nbody",
        identity=Identity("Gauntlet Engine", "engine@gauntlet.local"),
        allow_empty=True,
    )
    wips = gitops.wip_checkpoints(fixture_repo, phase="P9")
    # Both checkpoints are found despite the intervening engine commit at HEAD.
    assert [s for _sha, s in wips] == ["P9 wip: two", "P9 wip: one"]
    assert [sha for sha, _s in wips] == [m2, m1]


def test_unstage_removes_paths_from_index_leaving_worktree(fixture_repo):
    """`unstage` resets the named index entries to HEAD without touching disk."""
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "keep.py").write_text("keep\n")
    (fixture_repo / "drop.py").write_text("drop\n")
    gitops._run(fixture_repo, "add", "-A")
    gitops.unstage(fixture_repo, ["drop.py"])
    staged = gitops._run(fixture_repo, "diff", "--cached", "--name-only").split()
    assert staged == ["keep.py"]
    # drop.py is still on disk (only its index entry was reset to base = absent).
    assert (fixture_repo / "drop.py").read_text() == "drop\n"
    assert gitops.head_sha(fixture_repo) == base
    # No-ops are harmless (empty list, and paths absent from the index).
    gitops.unstage(fixture_repo, [])
    gitops.unstage(fixture_repo, ["never-tracked-dir"])


def test_reset_soft_keeps_worktree_and_stages_changes(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    _wip(fixture_repo, "P1 wip: one", "a.py", "a\n")
    _wip(fixture_repo, "P1 wip: two", "b.py", "b\n")
    gitops.reset_soft(fixture_repo, base)
    # HEAD moved back to base; both files still present and staged.
    assert gitops.head_sha(fixture_repo) == base
    assert (fixture_repo / "a.py").read_text() == "a\n"
    assert (fixture_repo / "b.py").read_text() == "b\n"
    staged = gitops._run(fixture_repo, "diff", "--cached", "--name-only").split()
    assert set(staged) == {"a.py", "b.py"}


# --- engine-bookkeeping tolerance in the dirty-base check (#62/#65) -----------
# The dirty-check exclusions (porcelain leg) vs the exact allowlist of paths an
# engine COMMIT may touch (the tolerance leg) — deliberately different sets:
# the exclusions also hide human-owned files (PR.md) the engine never commits
# (PR #76 review F-001).
_BK_EXCLUDES = ["runs/demo/run-1", "runs/*/PR.md"]
_BK_PATHS = ["runs/demo/run-1/manifest.json", "runs/demo/run-1/RUN.md"]


def _bookkeeping_commit(repo, subject="gauntlet: response r-1 pending", *,
                        identity=None) -> str:
    """Land an engine-shaped bookkeeping commit (force-tracked manifest only)."""
    run_dir = repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    man = run_dir / "manifest.json"
    prior = man.read_text() if man.exists() else ""
    man.write_text(prior + f"# {subject}\n")
    sha = gitops.commit_run_bookkeeping(
        repo, subject, ["runs/demo/run-1/manifest.json"],
        identity=identity or gitops.ENGINE_IDENTITY,
    )
    assert sha is not None
    return sha


def test_is_dirty_vs_tolerates_engine_bookkeeping_advance(fixture_repo):
    """#62/#65: HEAD ahead of base by ONLY engine bookkeeping commits is clean.

    The engine itself advances HEAD during a drive (response checkpoints,
    run-bookkeeping flushes); reading its own commits as mid-edit dirt made
    every plain resume of an interrupted step re-park forever.
    """
    base = gitops.head_sha(fixture_repo)
    _bookkeeping_commit(fixture_repo, "gauntlet: response implement-resp-1 pending")
    _bookkeeping_commit(
        fixture_repo, "gauntlet: flush run bookkeeping before P2 round-1 review handoff"
    )
    assert not gitops.is_dirty_vs(
        fixture_repo, base, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS
    )
    assert gitops.advance_is_engine_bookkeeping(fixture_repo, base, bookkeeping=_BK_PATHS)
    # Without a bookkeeping allowlist the tolerance is OFF (fail closed): a
    # HEAD past the base reads dirty exactly as before.
    assert gitops.is_dirty_vs(fixture_repo, base)
    # Uncommitted real work still reads dirty regardless of the tolerance.
    (fixture_repo / "partial.py").write_text("half written")
    assert gitops.is_dirty_vs(fixture_repo, base, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS)


def test_bookkeeping_tolerance_requires_engine_author(fixture_repo):
    # A `gauntlet:` subject under a non-engine author is NOT bookkeeping — the
    # convention requires BOTH markers (fail closed).
    base = gitops.head_sha(fixture_repo)
    _bookkeeping_commit(
        fixture_repo, "gauntlet: response r-1 pending",
        identity=Identity("Impostor", "impostor@example.com"),
    )
    assert gitops.is_dirty_vs(fixture_repo, base, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS)
    assert not gitops.advance_is_engine_bookkeeping(
        fixture_repo, base, bookkeeping=_BK_PATHS
    )


def test_bookkeeping_tolerance_requires_engine_subject(fixture_repo):
    # ENGINE_IDENTITY without the `gauntlet:` subject prefix is not bookkeeping.
    base = gitops.head_sha(fixture_repo)
    _bookkeeping_commit(
        fixture_repo, "flush run bookkeeping", identity=gitops.ENGINE_IDENTITY
    )
    assert gitops.is_dirty_vs(fixture_repo, base, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS)


def test_bookkeeping_tolerance_tree_diff_is_authoritative(fixture_repo):
    # An engine-marked commit that moves IMPLEMENTATION (the shape
    # rewind_impl_preserving_bookkeeping builds) must still read dirty: the
    # markers are a cheap precondition, the tree diff decides.
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "impl.py").write_text("moved\n")
    gitops.commit_paths(
        fixture_repo, "gauntlet: rewind implementation to abcdef1234 for re-run (x)",
        ["impl.py"], identity=gitops.ENGINE_IDENTITY,
    )
    assert gitops.is_dirty_vs(fixture_repo, base, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS)
    assert not gitops.advance_is_engine_bookkeeping(
        fixture_repo, base, bookkeeping=_BK_PATHS
    )


def test_bookkeeping_tolerance_refuses_behind_and_forked(fixture_repo):
    (fixture_repo / "a.py").write_text("a\n")
    ahead = gitops.commit_all(
        fixture_repo, "P1: a\n\nbody", identity=Identity("B", "b@g.local")
    )
    # HEAD strictly BEHIND the recorded base: never bookkeeping drift.
    gitops.reset_hard(fixture_repo, f"{ahead}~1")
    assert gitops.is_dirty_vs(fixture_repo, ahead, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS)
    # Genuine fork off the same parent: also refused.
    (fixture_repo / "b.py").write_text("b\n")
    gitops.commit_all(fixture_repo, "P1: b\n\nbody", identity=Identity("B", "b@g.local"))
    assert gitops.is_dirty_vs(fixture_repo, ahead, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS)


def test_bookkeeping_tolerance_is_an_allowlist_not_the_exclusions(fixture_repo):
    """PR #76 review F-001: PR.md sits in the dirty-check EXCLUSIONS (human-
    owned, hidden from porcelain and engine commits) but is never engine-
    committed — an engine-shaped commit touching it must NOT classify as
    bookkeeping, or rollback would hard-reset a human-owned change away."""
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "runs" / "demo").mkdir(parents=True)
    (fixture_repo / "runs" / "demo" / "PR.md").write_text("human-owned draft\n")
    gitops.commit_run_bookkeeping(
        fixture_repo, "gauntlet: response r-1 pending", ["runs/demo/PR.md"],
        identity=gitops.ENGINE_IDENTITY,
    )
    assert not gitops.advance_is_engine_bookkeeping(
        fixture_repo, base, bookkeeping=_BK_PATHS
    )
    assert gitops.is_dirty_vs(
        fixture_repo, base, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS
    )


def test_bookkeeping_tolerance_mixed_range_is_dirty(fixture_repo):
    # One real commit anywhere in the range poisons the whole tolerance.
    base = gitops.head_sha(fixture_repo)
    _bookkeeping_commit(fixture_repo)
    (fixture_repo / "wip.py").write_text("real work\n")
    gitops.commit_all(
        fixture_repo, "P2 wip: real work\n\nbody",
        identity=Identity("Builder", "b@g.local"), exclude=_BK_EXCLUDES,
    )
    assert gitops.is_dirty_vs(fixture_repo, base, exclude=_BK_EXCLUDES, bookkeeping=_BK_PATHS)


def test_dirty_paths_does_not_eat_the_first_paths_leading_character(fixture_repo):
    """A worktree-only first entry must report its WHOLE path (P7.1).

    The defect this pins is a one-character corruption that only ever hit the
    FIRST reported entry, which is why it survived the whole unit suite and
    surfaced only in a live run. ``status_porcelain`` ``.strip()``s its report,
    so a leading ``" M"`` (modified, unstaged — the commonest status of all)
    loses its leading space; the old ``line[3:]`` slicing then ate the path's
    first character too, turning ``runs/toy/prd.md`` into ``uns/toy/prd.md``.

    Asserted on a TRACKED, MODIFIED, UNSTAGED file so the status really is
    ``" M"`` — the precondition the bug needs. A staged or untracked file
    reports ``"M "``/``"??"`` with no leading space and would pass vacuously.
    """
    tracked = fixture_repo / "tracked.txt"
    tracked.write_text("seed\n")
    gitops._run(fixture_repo, "add", "tracked.txt")
    gitops.commit_all(
        fixture_repo, "seed: add tracked file\n\nBody.\n",
        identity=Identity("T", "t@example.com"),
    )
    tracked.write_text("modified, unstaged\n")

    # the precondition, asserted rather than assumed
    raw = gitops._run(
        fixture_repo, "status", "--porcelain", "--untracked-files=all"
    )
    assert raw.startswith(" M tracked.txt"), repr(raw)

    assert gitops.dirty_paths(fixture_repo) == ["tracked.txt"]


def test_dirty_paths_reports_every_entry_and_survives_renames(fixture_repo):
    """The parse must be structural, not positional.

    Guards the two things ``-z`` buys over line-splitting: a rename entry
    carries a SECOND NUL field (its source) that must be skipped rather than
    reported as a path, and a path with a space must not be truncated at it.
    """
    src = fixture_repo / "old name.txt"
    src.write_text("body\n")
    gitops._run(fixture_repo, "add", "old name.txt")
    gitops.commit_all(
        fixture_repo, "seed: add renameable file\n\nBody.\n",
        identity=Identity("T", "t@example.com"),
    )
    gitops._run(fixture_repo, "mv", "old name.txt", "new name.txt")
    (fixture_repo / "untracked.txt").write_text("x\n")

    found = gitops.dirty_paths(fixture_repo)
    assert "new name.txt" in found, found       # space preserved, not truncated
    assert "old name.txt" not in found, found   # rename SOURCE field skipped
    assert "untracked.txt" in found, found


def test_dirty_paths_honours_exclude(fixture_repo):
    """``exclude`` still reaches git as a pathspec, as ``status_porcelain``'s does."""
    (fixture_repo / "runs").mkdir()
    (fixture_repo / "runs" / "bookkeeping.json").write_text("{}\n")
    (fixture_repo / "code.py").write_text("x = 1\n")

    assert gitops.dirty_paths(fixture_repo, exclude=["runs"]) == ["code.py"]


# --- commit-message byte hygiene (#105) ---------------------------------------


def test_commit_all_sanitizes_nul_byte_in_message(fixture_repo):
    """A drafted message carrying a literal 0x00 must still commit (#105).

    git hard-rejects a NUL in the log message (exit 128: "a NUL byte in commit
    log message not allowed"), which failed the commit step `adapter_error` and
    stranded a finished fix round uncommitted. The stdin choke point now
    replaces the NUL with the readable escape ``\\x00`` before the message
    reaches ``commit -F -``.
    """
    (fixture_repo / "f.py").write_text("code\n")
    message = (
        "P3.1: Address review — NUL handling\n"
        "\n"
        "Describes a literal \x00 byte in prose.\n"
    )
    sha = gitops.commit_all(
        fixture_repo, message, identity=Identity("Builder", "b@g.local")
    )
    stored = gitops._run(fixture_repo, "log", "-1", "--format=%B", sha)
    assert "\x00" not in stored
    assert "Describes a literal \\x00 byte in prose." in stored
    # Printable text — including the non-ASCII em dash — is untouched.
    assert gitops.commit_subject(fixture_repo, sha) == (
        "P3.1: Address review — NUL handling"
    )


def test_commit_all_normalizes_cr_to_lf(fixture_repo):
    """CRLF/CR in a drafted message normalize to LF at the same choke point."""
    (fixture_repo / "g.py").write_text("code\n")
    sha = gitops.commit_all(
        fixture_repo,
        "P1: crlf message\r\n\r\nBody line one.\r\nBody line two.\rEnd.\n",
        identity=Identity("Builder", "b@g.local"),
    )
    # Bytes, not text: _run's text mode would itself translate \r\n on read,
    # making a CR assertion vacuous.
    stored = gitops._run_bytes(fixture_repo, "log", "-1", "--format=%B", sha)
    assert b"\r" not in stored
    assert b"Body line one.\nBody line two.\nEnd." in stored


def test_commit_retries_once_with_flattened_redraft_on_git_rejection(
    fixture_repo, monkeypatch
):
    """Belt and braces (#105): a byte the sanitizer missed must not strand work.

    Simulated by disabling the first-pass sanitizer so a raw NUL genuinely
    reaches git and git genuinely rejects it (exit 128). The single retry must
    land the commit with the printable-ASCII redraft instead of failing the
    step with the finished changes uncommitted.
    """
    monkeypatch.setattr(gitops, "_sanitize_commit_message", lambda m: m)
    (fixture_repo / "h.py").write_text("code\n")
    sha = gitops.commit_all(
        fixture_repo,
        "P1: nul \x00 survived sanitize\n\nBody — with a dash.\n",
        identity=Identity("Builder", "b@g.local"),
    )
    stored = gitops._run_bytes(fixture_repo, "log", "-1", "--format=%B", sha)
    assert b"\x00" not in stored
    # Aggressive redraft: NUL and the non-ASCII dash both flattened to "?".
    assert b"P1: nul ? survived sanitize" in stored
    assert b"Body ? with a dash." in stored


def test_unrelated_git_failure_is_not_masked_by_the_redraft_retry(fixture_repo):
    """Only a log-message rejection triggers the redraft retry.

    A clean tree (nothing to commit) with a non-ASCII message must still raise
    GitError — not silently land a flattened surprise commit.
    """
    before = gitops.head_sha(fixture_repo)
    with pytest.raises(gitops.GitError):
        gitops.commit_all(
            fixture_repo,
            "P1: nothing staged — clean tree\n\nBody.\n",
            identity=Identity("Builder", "b@g.local"),
        )
    assert gitops.head_sha(fixture_repo) == before

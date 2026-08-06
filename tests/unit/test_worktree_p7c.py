"""The dedicated run worktree: lifecycle, acceptance, and the seam (P7c).

Acceptance A1 is asserted as an autouse PROPERTY in `conftest.py`, not here —
see `operator_checkout_invariance`. This file carries A2, A3, the spike §11
recovery rows, the mode-resolution boundary that keeps auto-migration
structurally impossible, and the export dir's authority contract.

Everything here runs against throwaway fixture repos with real git; nothing is
mocked, because every claim P7c makes is a claim about what git actually does.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine import gitops
from gauntlet.engine import worktree as WT
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.run import RunManager

from conftest import git


def _main(repo: Path) -> Path:
    """The anchor run-worktree paths derive from (P7e: the MAIN worktree).

    Was ``gitops.git_common_dir`` while the root lived under ``.git/``. Every
    call site that used it is asking "where does the engine put run worktrees?",
    which is now anchored at the main worktree — so the rename is the whole
    change at those sites, and their assertions are untouched.
    """
    return gitops.main_worktree_root(repo)


def _manifest(slug="demo", run_id="run-1", branch=None, mode=None) -> Manifest:
    return Manifest(
        run_id=run_id,
        slug=slug,
        branch=branch or f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="p", version="1", hash="h"),
        worktree_mode=mode,
    )


# --- the derived root (§6.2/§6.4) --------------------------------------------


def test_worktree_path_is_derived_under_the_main_worktree(fixture_repo):
    """P7e: the derived root is `<main-worktree>/.gauntlet/worktrees`, no knob.

    REWRITTEN at P7e, and the rename is the point. This asserted the ratified
    §6.2 root under the git common dir. That location is unusable: the `claude`
    CLI refuses to write any path carrying a literal `.git` segment, so every
    builder and verifier step in such a tree fails — non-uniformly across write
    mechanisms, so it fails *iff the model does not improvise a form the guard
    misses* (`proposals/P7d-gate-blocker.md` §2). The maintainer ratified
    sub-option 1A on 2026-08-06.

    The `.gauntlet/` prefix is load-bearing in a way the old `gauntlet/` was
    not: it is the same directory that holds the adopter's TRACKED
    `config.yaml`, which is why the self-ignoring marker goes one level down.
    """
    main_root = _main(fixture_repo)
    path = WT.run_worktree_path(main_root, "demo", "run-1")
    assert path == main_root / ".gauntlet" / "worktrees" / "demo" / "run-1"
    assert WT.is_inside_worktrees_root(path, main_root)
    assert ".git" not in path.parts, (
        "the path must carry no literal `.git` segment — that is the blocker "
        "P7e exists to fix, and it is a property of the PATH, not of the engine"
    )
    # There is no config knob for it — §6.4, deferral D2.
    assert not hasattr(RunConfig().worktree, "root")
    assert not hasattr(RunConfig().worktree, "path")


def test_the_derived_root_is_the_same_from_every_vantage_point(fixture_repo):
    """P7e: anchored at the MAIN worktree, not at the invoking checkout.

    The property §6.2 got for free from the shared git common dir, which had to
    be re-established deliberately once the root moved into the working tree.
    Without it, an operator driving from their own linked worktree would derive
    a second, disjoint set of run worktrees, and the §14.4 refusal — which only
    ever fires from *inside* a run worktree — could never match.
    """
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    operator_wt = fixture_repo.parent / "operator-own"
    git(fixture_repo, "branch", "side")
    gitops.add_worktree_branch(fixture_repo, operator_wt, "side")

    for vantage in (fixture_repo, operator_wt, wt.path):
        assert gitops.main_worktree_root(vantage) == main_root, vantage
        assert WT.is_inside_worktrees_root(wt.path, gitops.main_worktree_root(vantage))


def test_config_defaults_to_dedicated_and_refuses_an_unknown_mode():
    """P7g: `dedicated` is the default; `same_tree` stays selectable forever.

    This one line is the whole of P7g's production change, so it is asserted
    directly rather than inferred from a run's behaviour. It gates two claims at
    once: that acceptance A1/A2/A3 now hold for runs in general (a new run gets
    its own tree without anyone opting in), and that spike §16's "`same_tree` is
    not removed" survived the flip — it remains the mode of every legacy run and
    the documented fallback for an adopter layout that cannot host a worktree.
    """
    assert RunConfig().worktree.mode == WT.MODE_DEDICATED
    assert RunConfig(worktree={"mode": "same_tree"}).worktree.mode == "same_tree"
    with pytest.raises(Exception):
        RunConfig(worktree={"mode": "somewhere-else"})


def test_a_run_worktree_is_invisible_to_status_via_the_engine_owned_marker(
    fixture_repo,
):
    """P7e: invisibility is now MAINTAINED, not structural — so assert the mechanism.

    Under §6.2 the tree was inside `.git/` and git ignored it with no help from
    us. At the 1A root it is ordinary working-tree content, and what keeps it
    out of `git status` is the engine-owned self-ignoring marker. That marker is
    therefore part of the clean-handoff invariant (CLAUDE.md §1) rather than a
    tidiness detail, and it must never sit at `.gauntlet/.gitignore`, which
    would blanket-ignore the tracked `config.yaml` beside it.
    """
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(
        fixture_repo, main_root, slug="demo", run_id="run-1", branch="gauntlet/demo"
    )
    assert wt.path.is_dir()
    assert gitops.status_porcelain(fixture_repo, untracked_all=True) == ""

    marker = main_root / ".gauntlet" / "worktrees" / ".gitignore"
    assert marker.read_text() == "*\n"
    assert not (main_root / ".gauntlet" / ".gitignore").exists(), (
        "a `*` one level up would ignore the adopter's tracked config.yaml"
    )

    # Deleting the marker (what `git clean -xdf` does) makes the tree dirt; the
    # next drive must re-establish it rather than leave the repo permanently
    # unclean from one ordinary operator keystroke.
    marker.unlink()
    assert gitops.status_porcelain(fixture_repo, untracked_all=True) != ""
    WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
              branch="gauntlet/demo")
    assert gitops.status_porcelain(fixture_repo, untracked_all=True) == ""


def test_clean_xdff_destroys_the_tree_but_leaves_it_recoverable(fixture_repo):
    """P7e: the §6.1 disqualifier, re-measured and downgraded to recoverable.

    REWRITTEN at P7e. This asserted `clean -xdff` could not reach the run
    worktree, which was true under `.git/` and is FALSE at the 1A root — so the
    old assertion cannot simply be re-anchored, it has to change sides.

    §6.1 treated this as disqualifying because a destroyed tree meant a lost
    run. It no longer does. What `-xdff` produces is exactly spike §11 row 2 —
    registered-and-absent, with the branch ref and the journal (which never
    lived in the tree, §4.4) intact — and P7c built `recreate` for precisely
    that. So the test now asserts the RECOVERY rather than the immunity, which
    is the honest form of the trade the maintainer ratified.
    """
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    (wt.path / "feature.py").write_text("work\n")
    git(wt.path, "add", "-A")
    git(wt.path, "commit", "-qm", "P1: work")
    recorded = gitops.head_sha(wt.path)

    subprocess.run(["git", "-C", str(fixture_repo), "clean", "-xdff"], check=True,
                   capture_output=True)
    assert not wt.path.is_dir(), "measured at P7e (E11): -xdff DOES reach it now"
    # The two things recovery needs both survived, which is why this is a
    # recoverable event and not a lost run.
    assert gitops.rev_parse(fixture_repo, "gauntlet/demo") == recorded

    again = WT.recreate(fixture_repo, main_root, slug="demo", run_id="run-1",
                        branch="gauntlet/demo", expect_head=recorded)
    assert again.path.is_dir()
    assert gitops.head_sha(again.path) == recorded
    assert (again.path / "feature.py").read_text() == "work\n"


# --- acceptance A2: concurrent operations cannot target one worktree ---------


def test_a2_git_refuses_a_second_worktree_for_the_run_branch(fixture_repo):
    """A2 is supplied by git itself (spike E2-A), not by an advisory lock."""
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
              branch="gauntlet/demo")
    with pytest.raises(gitops.GitError) as exc:
        gitops.add_worktree_branch(
            fixture_repo, fixture_repo.parent / "second", "gauntlet/demo"
        )
    assert "already used by worktree" in str(exc.value)


def test_a2_ensure_refuses_to_adopt_a_worktree_at_a_foreign_path(fixture_repo):
    """§11 row 6: never `add -f`, never adopt an entry nothing explained."""
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    foreign = fixture_repo.parent / "hand-made"
    gitops.add_worktree_branch(fixture_repo, foreign, "gauntlet/demo")
    with pytest.raises(WT.WorktreeUnavailable) as exc:
        WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                  branch="gauntlet/demo")
    assert str(foreign) in str(exc.value)
    assert "--same-tree" in exc.value.action


def test_a2_mixed_mode_runs_still_exclude_each_other_on_the_tree_guard(
    fixture_repo, tmp_path
):
    """PROBLEM A: a `dedicated` run must not stop excluding a `same_tree` one.

    Spike §10 says P7c retires the worktree-global tree guard to read-only.
    Implemented literally at P7c that is a REGRESSION, because `same_tree` is
    still the default: a dedicated run's `finish` merges into the operator's
    base *in the operator's checkout*, which would yank a concurrently-driving
    same_tree run's tree out from under it. Git's one-branch-one-worktree rule
    does not help — different slugs, different branches, and the same_tree run
    is not in a linked worktree at all.

    So P7c keeps WRITING the guard in both modes and P7d retires it. This test
    is that decision, made executable.
    """
    (fixture_repo / ".gauntlet").mkdir(exist_ok=True)
    (fixture_repo / ".gauntlet" / "config.yaml").write_text(
        "worktree:\n  mode: dedicated\n"
    )
    mgr = RunManager(fixture_repo)
    assert mgr.configured_worktree_mode == WT.MODE_DEDICATED
    handle = mgr._acquire_worktree_lock("alpha", "run-1")
    try:
        # The guard is at its P7b path, written by a manager configured for
        # `dedicated` — so a second slug contends on the same object.
        assert mgr._tree_lock_path().exists()
        other = RunManager(fixture_repo)
        with pytest.raises(Exception) as exc:
            other._acquire_worktree_lock("beta", "run-2")
        assert "driven by alpha" in str(exc.value)
    finally:
        mgr._release_worktree_lock(handle)


# --- acceptance A3: recreate from refs plus journal state --------------------


def test_a3_missing_worktree_is_recreated_and_head_matches_the_journal(fixture_repo):
    """E4-B's shape, asserted at runtime rather than assumed.

    The tree is destroyed out from under the run; the branch ref and the
    journal survive because §4.4 never put the journal in the tree. The
    recreated HEAD must equal the journal head's `branch_sha` — "the tree came
    back" and "the tree came back CORRECT" are different assertions.
    """
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    (wt.path / "feature.py").write_text("work\n")
    git(wt.path, "add", "-A")
    git(wt.path, "commit", "-qm", "P1: work")
    recorded = gitops.head_sha(wt.path)

    subprocess.run(["rm", "-rf", str(wt.path)], check=True)
    # CORRECTION TO THE RATIFIED SPIKE. §4.2 and §11 row 2 both name
    # `prunable gitdir file points to non-existent location` as THE discovery
    # signal for "the tree is gone but the branch survives". Measured here, it
    # is not emitted for a run worktree — because §8.3 requires that tree to be
    # held under `git worktree lock` for the life of the run, and git does not
    # report a LOCKED worktree as prunable. The two ratified recommendations
    # are individually right and jointly incompatible; the lock is the
    # load-bearing one (it is the only thing stopping E8-C), so the detection
    # moves rather than the lock.
    #
    # The correct signal is registered-AND-not-present, which is what
    # `WorktreeState.missing` computes and what the recovery path keys on.
    entry = WT.observe(fixture_repo, "gauntlet/demo")
    assert entry is not None, "the branch's worktree stays REGISTERED"
    assert entry.locked is not None, "and stays locked, which is why not prunable"
    assert entry.prunable is None, (
        "spike §11 row 2's `prunable` signal is suppressed by the §8.3 lock"
    )
    state = WT.describe(fixture_repo, mode=WT.MODE_DEDICATED, branch="gauntlet/demo")
    assert state.missing, "registered-and-absent is the real row-2 signal"

    again = WT.recreate(fixture_repo, main_root, slug="demo", run_id="run-1",
                        branch="gauntlet/demo", expect_head=recorded)
    assert again.recreated
    assert gitops.head_sha(again.path) == recorded


def test_a3_recreate_refuses_when_the_branch_disagrees_with_the_journal(fixture_repo):
    """A recreate that lands on a different SHA is a reconcile, not a repair."""
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    subprocess.run(["rm", "-rf", str(wt.path)], check=True)
    with pytest.raises(WT.WorktreeUnavailable) as exc:
        WT.recreate(fixture_repo, main_root, slug="demo", run_id="run-1",
                    branch="gauntlet/demo", expect_head="0" * 40)
    assert "journal head records" in str(exc.value)


# --- spike §11 rows ----------------------------------------------------------


def test_row5_the_git_lock_is_what_stops_another_runs_prune(fixture_repo):
    """§11 row 5 / E8-C: prune is repository-wide; only the lock stops it."""
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    entry = WT.observe(fixture_repo, "gauntlet/demo")
    assert entry is not None and entry.locked is not None
    assert WT.parse_lock_reason(entry.locked) == ("demo", "run-1")

    subprocess.run(["rm", "-rf", str(wt.path)], check=True)
    gitops.prune_worktrees(fixture_repo, expire="now")  # another run's teardown
    assert WT.observe(fixture_repo, "gauntlet/demo") is not None, (
        "a locked run worktree must survive an unrelated prune (E8-C)"
    )


def test_row7_prune_always_passes_an_explicit_expiry(fixture_repo, monkeypatch):
    """§11 row 7: never rely on adopter-configurable gc.worktreePruneExpire."""
    seen: list[tuple] = []
    real = gitops._run

    def spy(repo, *args, **kw):
        seen.append(args)
        return real(repo, *args, **kw)

    monkeypatch.setattr(gitops, "_run", spy)
    gitops.prune_worktrees(fixture_repo)
    prunes = [a for a in seen if a[:2] == ("worktree", "prune")]
    assert prunes and all(
        any(x.startswith("--expire=") for x in a) for a in prunes
    ), f"every prune must pin an expiry; saw {prunes}"


def test_row10_a_dirty_worktree_is_snapshotted_before_any_force_removal(fixture_repo):
    """§11 row 10 / R2: never `--force` without a durable record."""
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    (wt.path / "uncommitted.txt").write_text("work nobody committed\n")

    ref = WT.release(fixture_repo, wt.path, slug="demo", run_id="run-1")
    assert ref, "a dirty teardown must produce a recovery snapshot ref"
    assert gitops.ref_is_valid_commit(fixture_repo, ref)
    assert not wt.path.exists()


def test_row3_release_then_delete_is_the_order_git_requires(fixture_repo):
    """PROBLEM D / E2-D: `branch -D` refuses while a worktree holds the branch."""
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    with pytest.raises(gitops.GitError) as exc:
        gitops.delete_branch(fixture_repo, "gauntlet/demo")
    assert "used by worktree" in str(exc.value)

    WT.release(fixture_repo, wt.path, slug="demo", run_id="run-1")
    gitops.delete_branch(fixture_repo, "gauntlet/demo")  # now it succeeds
    assert not gitops.branch_exists(fixture_repo, "gauntlet/demo")


def test_submodule_superproject_parks_rather_than_handing_over_an_empty_tree(
    fixture_repo, tmp_path
):
    """§7: `git status` reports CLEAN on a half-populated submodule tree."""
    sub = tmp_path / "sub"
    sub.mkdir()
    git(sub, "init", "-q")
    git(sub, "config", "user.name", "Fixture")
    git(sub, "config", "user.email", "f@example.invalid")
    (sub / "README.md").write_text("sub\n")
    git(sub, "add", "-A")
    git(sub, "commit", "-qm", "sub init")
    subprocess.run(
        ["git", "-C", str(fixture_repo), "-c", "protocol.file.allow=always",
         "submodule", "add", "-q", str(sub), "vendor/sub"],
        check=True, capture_output=True,
    )
    git(fixture_repo, "commit", "-qm", "add submodule")
    git(fixture_repo, "branch", "gauntlet/demo")
    with pytest.raises(WT.WorktreeUnavailable) as exc:
        WT.ensure(fixture_repo, _main(fixture_repo), slug="demo",
                  run_id="run-1", branch="gauntlet/demo")
    assert "uninitialized submodules" in str(exc.value)
    assert "submodule update --init" in str(exc.value)


# --- the seam: mode resolution never auto-migrates ---------------------------


def test_config_dedicated_does_not_move_a_run_born_same_tree(fixture_repo):
    """THE SEAM TEST (`proposals/P7c-split-seam.md` §2).

    An operator flips `worktree.mode: dedicated` on a repo that already has
    runs. Those runs must keep driving `same_tree`: moving them would be
    auto-migration, which spike §10 forbids, and it would arrive in the commit
    that never mentions migration. The rule is that mode resolves from evidence
    plus what the run recorded at birth — never from live config.
    """
    (fixture_repo / ".gauntlet").mkdir(exist_ok=True)
    (fixture_repo / ".gauntlet" / "config.yaml").write_text(
        "worktree:\n  mode: dedicated\n"
    )
    mgr = RunManager(fixture_repo)
    assert mgr.configured_worktree_mode == WT.MODE_DEDICATED

    legacy = _manifest(mode=None)          # a pre-P7c run: no recorded mode
    born_same = _manifest(mode=WT.MODE_SAME_TREE)
    assert mgr._effective_worktree_mode(legacy) == WT.MODE_SAME_TREE
    assert mgr._effective_worktree_mode(born_same) == WT.MODE_SAME_TREE


def test_mode_resolves_from_evidence_when_a_worktree_is_registered(fixture_repo):
    """Rule 1: the registered tree is ground truth, readable with a dead driver."""
    mgr = RunManager(fixture_repo, RunConfig())
    git(fixture_repo, "branch", "gauntlet/demo")
    man = _manifest(mode=WT.MODE_SAME_TREE)  # the record says same_tree...
    WT.ensure(fixture_repo, _main(fixture_repo), slug="demo", run_id="run-1",
              branch="gauntlet/demo")
    # ...but a worktree exists, so the evidence wins.
    assert mgr._effective_worktree_mode(man) == WT.MODE_DEDICATED


def test_mode_stays_dedicated_when_the_tree_is_missing_but_was_adopted(
    fixture_repo, monkeypatch
):
    """Rule 2: registered-but-gone must resolve `dedicated`, so it is recreated.

    Resolving §11 row 2 to `same_tree` would silently drop the run back into
    the operator's checkout at exactly the moment its own tree vanished — the
    worst possible time to change trees.
    """
    mgr = RunManager(fixture_repo, RunConfig())
    man = _manifest(mode=WT.MODE_DEDICATED)
    monkeypatch.setattr(WT, "observe", lambda *a, **k: None)  # nothing registered
    monkeypatch.setattr(
        RunManager, "_journal_says_adopted", lambda self, m: True
    )
    assert mgr._effective_worktree_mode(man) == WT.MODE_DEDICATED


def test_config_is_read_in_exactly_one_place():
    """`proposals/P7c-split-seam.md` §5: any second reader re-opens the hazard.

    A static check, deliberately: while the default is `same_tree` a stray
    `config.worktree.mode` read is invisible at runtime — it agrees with the
    resolved mode for every run on the machine — and only becomes a silent
    auto-migration once an operator flips the flag.
    """
    import ast

    src = Path(RunManager.__module__.replace(".", "/") + ".py")
    src = Path(__file__).resolve().parents[2] / "src" / src
    tree = ast.parse(src.read_text())
    readers = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "configured_worktree_mode"
        ):
            fn = getattr(node, "_fn", None)
            readers.append(fn)
    # Locate the enclosing functions by a second pass (ast has no parent links).
    enclosing = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "configured_worktree_mode"
                ):
                    enclosing.append(fn.name)
    assert set(enclosing) <= {"start", "configured_worktree_mode"}, (
        "`config.worktree.mode` must be read only when a NEW run is born "
        f"(`start`); found reads in: {sorted(set(enclosing))}. Every other "
        "caller must use `_effective_worktree_mode`, or flipping the config "
        "silently migrates existing runs."
    )


# --- the export dir and its authority contract -------------------------------


def test_export_dir_mirrors_the_state_layout_and_banners_the_copy(fixture_repo,
                                                                  tmp_path):
    """§4.4: two files, mirrored path, and the copy says what it is not."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "manifest.json").write_text(json.dumps({"run_id": "run-1"}))
    (state / "RUN.md").write_text("# Run\n")
    work = tmp_path / "work"
    work.mkdir()

    written = WT.write_bookkeeping_export(work, state, "runs", "demo", "run-1")
    dest = WT.export_dir(work, "runs", "demo", "run-1")
    assert dest == work / "runs" / "demo" / "run-1"
    assert {p.name for p in written} == {"manifest.json", "RUN.md"}
    assert (dest / "RUN.md").read_text().startswith("<!-- EXPORTED COPY")
    assert "journal" in (dest / "RUN.md").read_text()
    # The manifest export stays byte-identical: it IS the audit payload the
    # FR-2.2 checkpoint commit records, and a banner would corrupt the JSON.
    assert (dest / "manifest.json").read_text() == (state / "manifest.json").read_text()


def test_no_engine_module_loads_a_manifest_from_a_work_root():
    """The authority answer, enforced: the export has one writer, zero readers.

    `RunPaths.bookkeeping_root` deliberately points the bookkeeping builders at
    the export dir, so nothing stops a future reader from resolving a Manifest
    through a work root by mistake. This is the guard that keeps "which one is
    authoritative?" answerable by reading the code.
    """
    import ast

    engine = Path(gitops.__file__).parent
    offenders = []
    for src in sorted(engine.glob("*.py")):
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name not in ("load",):
                continue
            owner = getattr(getattr(fn, "value", None), "id", None)
            if owner != "Manifest":
                continue
            text = ast.unparse(node.args[0])
            if "work_root" in text or "bookkeeping_root" in text:
                offenders.append(f"{src.name}:{node.lineno}: Manifest.load({text})")
    assert not offenders, (
        "the run worktree's exported manifest is WRITE-ONLY — the journal in "
        "the operator's checkout is authoritative (R8) and its projection "
        "beside it is what every reader resolves. Loading a manifest from a "
        f"work root reads a copy that is stale between commits:\n  "
        + "\n  ".join(offenders)
    )


# --- the §14.4 refusal -------------------------------------------------------


def test_verbs_refuse_when_invoked_from_inside_a_run_worktree(fixture_repo,
                                                              monkeypatch):
    """§14.4: reading run state from in there answers from the branch tip."""
    import typer

    from gauntlet import cli

    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    with pytest.raises(typer.Exit):
        cli._refuse_inside_run_worktree(wt.path)
    # ...and stays silent for the operator's own checkout.
    cli._refuse_inside_run_worktree(fixture_repo)


def test_the_refusal_does_not_fire_for_an_adopters_own_linked_worktree(
    fixture_repo, tmp_path
):
    """§7: a plain worktree-of-worktree is not a run worktree and must work."""
    from gauntlet import cli

    git(fixture_repo, "branch", "feature/mine")
    mine = tmp_path / "my-worktree"
    gitops.add_worktree_branch(fixture_repo, mine, "feature/mine")
    cli._refuse_inside_run_worktree(mine)  # must not raise


# --- the §12.2 fault-injection seam ------------------------------------------


def test_lifecycle_reaches_every_named_crash_boundary_in_order(fixture_repo,
                                                               monkeypatch):
    """§12.2: five boundaries, each mapped to exactly one §11 recovery row.

    The seam itself is asserted here — that every point in ``_BOUNDARIES`` is
    actually reached, in the documented order, and from inside the critical
    section rather than around it. `tests/unit/_crash_child.py`'s ``wt:`` mode
    turns each into a real SIGKILL; this keeps the two from drifting, because a
    boundary that stopped firing would leave that kill mode silently inert.
    """
    main_root = _main(fixture_repo)
    git(fixture_repo, "branch", "gauntlet/demo")
    reached: list[str] = []
    monkeypatch.setattr(WT, "_boundary_hook", reached.append)

    wt = WT.ensure(fixture_repo, main_root, slug="demo", run_id="run-1",
                   branch="gauntlet/demo")
    assert reached == ["before_add", "after_add", "after_lock"]

    WT.release(fixture_repo, wt.path, slug="demo", run_id="run-1")
    assert reached[3:] == ["before_remove", "after_remove"]
    assert set(reached) == set(WT._BOUNDARIES), (
        "every declared boundary must be reachable, or its kill mode is inert"
    )


def test_the_operators_own_checkout_is_never_mistaken_for_a_run_worktree(
    fixture_repo,
):
    """REGRESSION: `git worktree list` includes the operator's MAIN checkout.

    Caught by the first full-suite run of this phase, as 175 failures with one
    root cause — and it is the exact failure mode P7 exists to prevent, invisible
    until the two modes coexist.

    In `same_tree` mode the run branch is checked out in the operator's own
    tree, and that tree is itself a registered worktree. An unscoped "is a
    worktree registered for this branch?" therefore answered YES for every
    legacy run, which (a) resolved every `same_tree` run as `dedicated` and
    (b) pointed teardown's `worktree remove` at the operator's checkout.

    Scoping to the engine's derived root is what makes the question mean "does
    this run have a tree of its OWN", which is what every caller was actually
    asking.
    """
    mgr = RunManager(fixture_repo, RunConfig())
    main_root = _main(fixture_repo)
    git(fixture_repo, "checkout", "-qb", "gauntlet/demo")  # the same_tree layout
    man = _manifest(mode=WT.MODE_SAME_TREE)

    assert gitops.worktree_for_branch(fixture_repo, "gauntlet/demo") is not None, (
        "precondition: git DOES register the operator's checkout for this branch"
    )
    assert WT.observe(fixture_repo, "gauntlet/demo", main_root=main_root) is None, (
        "scoped, the operator's checkout is not a run worktree"
    )
    assert mgr._effective_worktree_mode(man) == WT.MODE_SAME_TREE

    state = WT.describe(fixture_repo, mode=WT.MODE_SAME_TREE, branch="gauntlet/demo")
    assert state.path is None and not state.registered

    # And the teardown path finds nothing to remove, so the operator's checkout
    # is never a `worktree remove` target.
    mgr._release_run_worktree(fixture_repo, man)
    assert (fixture_repo / "README.md").exists()

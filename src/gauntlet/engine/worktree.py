"""The dedicated run worktree: derivation, lifecycle and discovery (P7c).

Spike §6.2 fixes the root at
``<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>`` — derived, with **no
config knob** (§6.4). Every property P7 needs was measured there: invisible to
``git status`` even with ``--ignored``, untouched by ``git clean -xdff``,
tolerated by ``fsck``/``gc``, on the same filesystem as the repo by
construction (so ``EXDEV`` is structurally unreachable for every atomic write),
and still a valid parent for the verifier's disposable copy.

**What lives here and what deliberately does not.** This module owns the
*tree*: creating it, marking it, discovering it, recreating it, tearing it
down. It owns none of the run's *state* — spike §4.4's whole point is that the
journal, the manifest projection, transcripts, the heartbeat and the recovery
intent stay in the operator's checkout, so destroying this tree destroys
nothing authoritative. The single exception is the two-file bookkeeping
**export** (:func:`write_bookkeeping_export`), which is written into the tree
and never read back — see its docstring for why that asymmetry is the design
rather than an oversight.

**Three layers of exclusion, and which one this module supplies.**

* the per-run driving lock and the retained worktree-global tree guard live in
  :mod:`gauntlet.engine.run` (P7b) — they answer "who is driving?";
* the repo-global git lock (:mod:`gauntlet.engine.repolock`) serializes the
  shared worktree administration dir — every ``add``/``remove``/``prune`` here
  runs inside it;
* ``git worktree lock --reason`` (:func:`gitops.lock_worktree`) is what THIS
  module holds for the life of a run worktree. It is a marker, not a mutex, and
  it is the *only* thing that stops another run's ``prune_worktrees`` from
  deleting this run's admin entry the moment its tree is momentarily missing
  (spike E8-C). Acceptance A2 — "concurrent different-run operations cannot
  target the same worktree" — is supplied for free above all of these by git's
  own one-branch-one-worktree rule (E2-A), because the run branch is per-slug.

**Fail closed, never fall back.** Every failure to create, lock or verify a run
worktree raises :class:`WorktreeUnavailable`, which the caller turns into a
park with reason ``worktree_unavailable`` offering ``gauntlet resume <slug>
--same-tree``. Silently falling back to the operator's checkout would do the
exact thing P7 exists to prevent, precisely when the machine is already in an
unexpected state (spike §13).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gauntlet.engine import git_snapshot, gitops, repolock

# `worktree.mode` values (spike §13). `same_tree` is the DEFAULT and stays the
# mode of every legacy run forever (§16); `dedicated` is opt-in until a dogfood
# run justifies P7d flipping it.
MODE_SAME_TREE = "same_tree"
MODE_DEDICATED = "dedicated"
MODES = (MODE_SAME_TREE, MODE_DEDICATED)

# The fixed path segments under the git common dir (§6.2). Not configurable:
# a knob would need a `resolve()`-based containment validator, and spike E9-C
# proves the existing string-based `_validate_repo_relative` is defeated by a
# symlink. Not shipping the knob avoids the whole class (§6.4, deferral D2).
WORKTREE_DIRNAME = "gauntlet"
WORKTREE_SUBDIR = "worktrees"

# The park reason a caller raises when the tree cannot be made ready. Mirrored
# in manifest.py's park-reason vocabulary and in schemas/status.json.
PARK_REASON_WORKTREE_UNAVAILABLE = "worktree_unavailable"

# Fault-injection seam for the P7c crash matrix (spike §12.2). `tests/unit/
# _crash_child.py` replaces this with a function that self-signals at a named
# point, so a real process death lands exactly at a worktree-lifecycle
# boundary. Production is a no-op; the points are named in `_BOUNDARIES`.
_BOUNDARIES = (
    "before_add",       # §11 rows 1/8 — creation crash / partial create
    "after_add",        # §11 row 6  — admin entry exists, no git lock yet
    "after_lock",       # §11 row 5  — locked, export dir not yet written
    "before_remove",    # §11 row 3  — teardown crash with the tree still there
    "after_remove",     # §11 row 2  — tree gone, WorktreeReleased not appended
)
_boundary_hook: Callable[[str], None] | None = None


def _boundary(point: str) -> None:
    if _boundary_hook is not None:
        _boundary_hook(point)


class WorktreeUnavailable(RuntimeError):
    """A run worktree could not be created, locked, populated or verified.

    Always fail-closed and always operator-actionable: ``action`` carries the
    exact command spike §13 requires the assessment to offer, and the message
    preserves git's own stderr rather than paraphrasing it, because the nine
    failure classes in §11 are distinguished by that text (``already exists``,
    ``already registered``, ``already used by worktree at``, ``could not create
    leading directories``).
    """

    def __init__(self, message: str, *, slug: str | None = None) -> None:
        super().__init__(message)
        self.slug = slug
        self.action = (
            f"gauntlet resume {slug} --same-tree" if slug else "gauntlet resume <slug> --same-tree"
        )


@dataclass(frozen=True)
class RunWorktree:
    """A run's dedicated tree, as observed.

    ``created`` distinguishes "this call made it" from "it was already there
    and healthy", which is what decides whether a ``WorktreeAdopted`` journal
    event is due. ``recreated`` marks the §11-row-2 path, where the branch ref
    and the journal — not the tree — were the surviving evidence.
    """

    path: Path
    branch: str
    created: bool = False
    recreated: bool = False


def worktrees_root(common_dir: Path) -> Path:
    """``<git-common-dir>/gauntlet/worktrees`` — the parent of every run tree."""
    return common_dir / WORKTREE_DIRNAME / WORKTREE_SUBDIR


def run_worktree_path(common_dir: Path, slug: str, run_id: str) -> Path:
    """The derived, non-configurable path for one run's tree (§6.2/§6.4).

    ``slug`` and ``run_id`` are already containment-checked by
    ``run.safe_run_segment`` at every entry point that can supply them, so this
    is a pure join. It is deliberately *derived* rather than persisted: a
    recorded path could drift from the layout the running engine expects, and
    the one incident that most needs this path — recreating a MISSING tree
    (acceptance A3) — must work from the branch ref and the journal alone.
    """
    return worktrees_root(common_dir) / slug / run_id


def is_inside_worktrees_root(path: Path, common_dir: Path) -> bool:
    """True when ``path`` resolves inside the engine-owned worktrees root.

    Both sides are ``resolve()``d before comparison: git records worktree paths
    exactly as given and does not resolve symlinks (spike E9-B), so a string
    comparison would be defeated by the same symlink that defeats
    ``_validate_repo_relative`` (E9-C).
    """
    try:
        path.resolve().relative_to(worktrees_root(common_dir).resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def lock_reason(slug: str, run_id: str) -> str:
    """The ``git worktree lock --reason`` text for a live run.

    Machine-readable on purpose: :func:`describe` parses it back out of
    ``worktree list --porcelain`` so an operator (and the recovery assessment)
    can tell an engine-held lock from one a human placed by hand.
    """
    return f"gauntlet run {slug}/{run_id} live"


def parse_lock_reason(reason: str | None) -> tuple[str, str] | None:
    """``(slug, run_id)`` from a reason :func:`lock_reason` produced, else None."""
    if not reason:
        return None
    parts = reason.split()
    if len(parts) < 3 or parts[0] != "gauntlet" or parts[1] != "run":
        return None
    slug, _, run_id = parts[2].partition("/")
    return (slug, run_id) if slug and run_id else None


# --- discovery ----------------------------------------------------------------


def observe(
    repo_root: Path, branch: str, *, common_dir: Path | None = None
) -> gitops.WorktreeEntry | None:
    """This run's OWN worktree for ``branch``, or ``None``. Read-only.

    Usable when the driver is dead, which is what makes it the evidence half of
    both the §10 migration-detection rule and the §11 recovery rows. Never
    raises for a repository with no worktrees; a git failure propagates as
    :class:`gitops.GitError` because "cannot read the worktree list" must not
    be indistinguishable from "there is no worktree" (fail closed).

    ``common_dir`` scopes the answer to trees under the engine's own derived
    root, and every caller that is asking "does this run have a dedicated
    worktree?" MUST pass it. Without it the answer is any worktree holding the
    branch — **including the operator's own main checkout**, which is exactly
    where a `same_tree` run's branch is checked out. Two things went wrong when
    this was unscoped, and both are the kind that only appear once the modes
    coexist:

    * every `same_tree` run resolved as `dedicated`, because its branch was
      "registered" — in the operator's checkout;
    * teardown would then have called ``worktree remove`` on that entry, i.e.
      on the operator's own checkout.

    An adopter's private worktree of the run branch is filtered out for the same
    reason: it is not this engine's tree, and adopting it silently is what §11
    row 6 forbids. When a foreign tree holds the branch, ``worktree add`` fails
    with git's own one-branch-one-worktree refusal (E2-A) naming the holder —
    which is a better message than anything reconstructed here.
    """
    entry = gitops.worktree_for_branch(repo_root, branch)
    if entry is None:
        return None
    if common_dir is not None and not is_inside_worktrees_root(entry.path, common_dir):
        return None
    return entry


@dataclass(frozen=True)
class WorktreeState:
    """The rendered worktree facts for one run — the `status --json` source.

    ``present`` is the on-disk answer, not the registry's: an entry can be
    registered and ``prunable`` at once (its tree deleted under it), which is
    exactly §11 row 2 and must not read as "present". ``missing`` is therefore
    ``registered and not present``, and it is the state that has a recreate
    action; an unregistered, absent tree for a `same_tree` run is simply not a
    worktree at all.
    """

    mode: str
    path: Path | None
    registered: bool
    present: bool
    locked_reason: str | None
    prunable: str | None
    head: str | None

    @property
    def missing(self) -> bool:
        return self.registered and not self.present


def describe(repo_root: Path, *, mode: str, branch: str) -> WorktreeState:
    """Observe a run's tree without mutating anything (the `status` source).

    A `same_tree` run yields ``registered=False, path=None`` — the honest
    answer, and what renders as ``worktree: null``. Deliberately tolerant of a
    git failure here: `status` is the verb an operator reaches for when things
    are already wrong, so an unreadable worktree list degrades to "not
    registered" rather than making the whole status payload unavailable. Every
    MUTATING path uses :func:`observe` instead, where the same failure
    propagates.
    """
    if mode != MODE_DEDICATED:
        return WorktreeState(
            mode=mode, path=None, registered=False, present=False,
            locked_reason=None, prunable=None, head=None,
        )
    try:
        entry = observe(
            repo_root, branch, common_dir=gitops.git_common_dir(repo_root)
        )
    except gitops.GitError:
        entry = None
    if entry is None:
        return WorktreeState(
            mode=mode, path=None, registered=False, present=False,
            locked_reason=None, prunable=None, head=None,
        )
    return WorktreeState(
        mode=mode,
        path=entry.path,
        registered=True,
        present=entry.path.is_dir(),
        locked_reason=entry.locked,
        prunable=entry.prunable,
        head=entry.head,
    )


# --- creation / adoption ------------------------------------------------------


def _verify_submodules(work_root: Path, slug: str) -> None:
    """Park rather than hand an agent a half-populated tree (spike §7).

    A worktree of a superproject checks out the submodule gitlink but leaves
    the directory EMPTY, and ``git status`` reports the tree CLEAN — a builder
    would see failing tests with nothing pointing at the cause. P7 ships the
    fail-closed park; doing ``submodule update --init`` automatically touches
    network and credential posture and is deferral D4.
    """
    missing = gitops.uninitialized_submodules(work_root)
    if not missing:
        return
    raise WorktreeUnavailable(
        f"run worktree {work_root} has uninitialized submodules: "
        f"{', '.join(missing)}. "
        "A worktree of a superproject checks out the gitlink but leaves the "
        "directory empty, and `git status` still reports the tree clean — so an "
        "agent would get failing tests with no signal pointing at the cause "
        f"(spike §7). Run `git -C {work_root} submodule update --init` and "
        "resume, or resume in the operator's checkout with --same-tree.",
        slug=slug,
    )


def ensure(
    repo_root: Path,
    common_dir: Path,
    *,
    slug: str,
    run_id: str,
    branch: str,
) -> RunWorktree:
    """Make this run's dedicated tree exist, be marked, and be usable.

    Idempotent and safe to call on every driving verb: a healthy existing tree
    is adopted unchanged. The order is fixed and each step is a §11 row:

    1. **discover** — a registered entry for ``branch`` that is still on disk is
       this run's tree; verify it sits at the derived path before adopting it,
       because an entry somewhere else means a human (or an older engine) put it
       there and adopting it silently would hide that;
    2. **recreate** — a registered but ``prunable`` entry is §11 row 2: the tree
       vanished (reboot, ``/tmp`` sweep, an operator's ``rm -rf``) while the
       branch ref and the journal survived. ``prune`` then ``add``; never
       ``add -f`` (§11 row 6), which would adopt an entry nothing has explained;
    3. **create** — ``worktree add <path> <branch>``. Refuses if the branch is
       checked out anywhere (E2-A), which is the correct fail-closed answer and
       is also acceptance A2 enforced by git rather than by us;
    4. **mark** — ``git worktree lock --reason`` for the life of the run;
    5. **verify** — submodules populated (§7), else park.

    Everything that touches the shared worktree administration dir runs inside
    the repo-global lock, because that dir has no per-worktree isolation to fall
    back on (spike E9-D: a lost ``worktree add`` race leaves debris behind).
    """
    target = run_worktree_path(common_dir, slug, run_id)
    try:
        with repolock.repo_lock(repo_root, reason=f"worktree:ensure:{slug}"):
            wt = _ensure_locked(
                repo_root, target, common_dir,
                slug=slug, run_id=run_id, branch=branch,
            )
    except repolock.RepoLockError as exc:
        raise WorktreeUnavailable(
            f"could not serialize worktree creation for {slug!r}: {exc}", slug=slug
        ) from exc
    _verify_submodules(wt.path, slug)
    return wt


def _ensure_locked(
    repo_root: Path, target: Path, common_dir: Path,
    *, slug: str, run_id: str, branch: str,
) -> RunWorktree:
    """:func:`ensure`'s body, with the repo-global lock already held.

    ``common_dir`` is threaded in rather than re-derived from ``target``:
    counting path components back up to the git dir is the kind of arithmetic
    that is silently wrong by one, and the failure mode is invisible — a
    mis-scoped ``observe`` returns ``None``, the stale-entry cleanup never
    runs, and ``add`` fails on an entry this function was supposed to clear.
    """
    entry = observe(repo_root, branch, common_dir=common_dir)
    recreated = False
    created = False
    if entry is not None and entry.path.resolve() != target.resolve():
        # An engine-owned tree for this branch at a DIFFERENT derived path —
        # a previous run-id of this slug, or a hand-moved tree. Adopting it
        # silently is what §11 row 6 forbids. A worktree OUTSIDE the engine's
        # root (the operator's own checkout, an adopter's private worktree) is
        # not this case: `observe` filters it out and git's own E2-A refusal
        # names the holder when `add` runs below.
        raise WorktreeUnavailable(
            f"branch {branch!r} is already checked out in a Gauntlet run "
            f"worktree at {entry.path}, which is not this run's derived path "
            f"{target}. Refusing to adopt a tree this engine did not create "
            "for THIS run, or to drive one run from two locations (spike §11 "
            "row 6 — `add -f` would hide exactly this). Remove that worktree "
            "(`gauntlet clean`), or resume with --same-tree.",
            slug=slug,
        )
    if entry is not None and entry.prunable is None and target.is_dir():
        return RunWorktree(path=target, branch=branch)
    if entry is not None:
        # Registered but gone (or registered with a stale admin entry): §11
        # rows 2 and 6. Unlock first — our own live-run lock blocks the prune
        # and the remove (E6-B) — then clear the entry before adding.
        _clear_registration(repo_root, target, entry)
        recreated = True
    _boundary("before_add")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        gitops.add_worktree_branch(repo_root, target, branch)
    except gitops.GitError as exc:
        raise WorktreeUnavailable(
            f"could not create the run worktree for {slug!r} at {target}: {exc}",
            slug=slug,
        ) from exc
    created = not recreated
    _boundary("after_add")
    try:
        gitops.lock_worktree(repo_root, target, reason=lock_reason(slug, run_id))
    except gitops.GitError as exc:
        # A tree that cannot be marked is a tree another run's prune may delete
        # mid-drive (E8-C). Undo rather than proceed unmarked.
        try:
            gitops.remove_worktree(repo_root, target)
        except gitops.GitError:
            pass
        raise WorktreeUnavailable(
            f"could not lock the run worktree for {slug!r} at {target}: {exc}. "
            "An unmarked run worktree can be pruned out from under a live run "
            "by any other run's cleanup (spike E8-C), so this fails closed.",
            slug=slug,
        ) from exc
    _boundary("after_lock")
    return RunWorktree(path=target, branch=branch, created=created, recreated=recreated)


def _clear_registration(
    repo_root: Path, target: Path, entry: gitops.WorktreeEntry
) -> None:
    """Drop a stale admin entry so a fresh ``add`` can succeed (§11 rows 2/6).

    Unlock first (our own live-run marker blocks both ``prune`` and ``remove``,
    E6-B), then remove a still-present tree, then prune with an EXPLICIT expiry
    — never relying on the adopter-configurable ``gc.worktreePruneExpire``
    (§11 row 7), which would leave a freshly-missing entry registered and turn
    the recreate into ``fatal: already registered``.
    """
    if entry.locked is not None:
        try:
            gitops.unlock_worktree(repo_root, entry.path)
        except gitops.GitError:
            pass
    if entry.path.is_dir():
        try:
            gitops.remove_worktree(repo_root, entry.path)
        except gitops.GitError:
            pass
    try:
        gitops.prune_worktrees(repo_root, expire="now")
    except gitops.GitError:
        pass


def recreate(
    repo_root: Path,
    common_dir: Path,
    *,
    slug: str,
    run_id: str,
    branch: str,
    expect_head: str | None = None,
) -> RunWorktree:
    """The §11-row-2 recovery action, as an explicit verb (acceptance A3).

    :func:`ensure` already recreates a missing tree as part of its normal path;
    this is the same thing reached deliberately from a recovery assessment, with
    one addition: when ``expect_head`` is supplied (the journal head's
    ``branch_sha``) the recreated tree's HEAD is verified against it, so "the
    tree came back" and "the tree came back CORRECT" are not the same
    assertion. Spike E4-B proves the reconstruction is exact; this is the check
    that keeps it proven at runtime rather than assumed.
    """
    wt = ensure(
        repo_root, common_dir, slug=slug, run_id=run_id, branch=branch
    )
    if expect_head:
        actual = gitops.head_sha(wt.path)
        if actual != expect_head:
            raise WorktreeUnavailable(
                f"recreated run worktree for {slug!r} is at {actual[:10]} but the "
                f"journal head records {expect_head[:10]} for branch {branch!r}; "
                "the branch ref and the journal disagree, so the tree was NOT "
                "reconstructed from the state this run recorded. Reconcile the "
                "branch (`gauntlet status`, `gauntlet rollback`) before driving.",
                slug=slug,
            )
    return RunWorktree(
        path=wt.path, branch=branch, created=wt.created, recreated=True
    )


# --- teardown -----------------------------------------------------------------


def release(
    repo_root: Path,
    path: Path,
    *,
    slug: str,
    run_id: str,
    snapshot_run_id: str | None = None,
    excludes: list[str] | None = None,
) -> str | None:
    """Unlock and remove a run worktree; returns a snapshot ref if one was taken.

    Ordering is not stylistic — it is what git enforces and what R2 requires:

    1. **snapshot if dirty** (§11 row 10). ``worktree remove`` refuses a tree
       with modified or untracked files, and the escape is ``--force``. R2 says
       durable state is never discarded without a recoverable record, so a dirty
       tree is captured into ``refs/gauntlet/recovery/...`` FIRST. A snapshot
       failure aborts the removal rather than forcing past it.
    2. **unlock** — our own ``worktree lock`` blocks ``remove`` (E6-B).
    3. **remove**, then **prune** with an explicit expiry.

    The branch is NOT deleted here. ``clean``/``finish`` do that, and they must
    do it AFTER this returns: with a live worktree on the branch, ``branch -D``
    hard-refuses (E2-D).
    """
    ref: str | None = None
    with repolock.repo_lock(repo_root, reason=f"worktree:release:{slug}"):
        if path.is_dir():
            ref = _snapshot_if_dirty(
                path, slug=slug, run_id=snapshot_run_id or run_id, excludes=excludes
            )
        entry = observe(repo_root, _branch_of(repo_root, path))
        if entry is not None and entry.locked is not None:
            try:
                gitops.unlock_worktree(repo_root, path)
            except gitops.GitError:
                pass
        _boundary("before_remove")
        if path.exists():
            try:
                gitops.remove_worktree(repo_root, path)
            except gitops.GitError as exc:
                raise WorktreeUnavailable(
                    f"could not remove the run worktree for {slug!r} at {path}: "
                    f"{exc}",
                    slug=slug,
                ) from exc
        try:
            gitops.prune_worktrees(repo_root, expire="now")
        except gitops.GitError:
            pass
        _boundary("after_remove")
    return ref


def _branch_of(repo_root: Path, path: Path) -> str:
    """The branch a registered worktree holds, or ``""`` when unregistered."""
    try:
        for entry in gitops.list_worktrees(repo_root):
            if entry.path.resolve() == path.resolve():
                return entry.branch or ""
    except (gitops.GitError, OSError, RuntimeError):
        pass
    return ""


def _snapshot_if_dirty(
    work_root: Path, *, slug: str, run_id: str, excludes: list[str] | None
) -> str | None:
    """R2 in git's own voice (§11 row 10): capture before any ``--force``.

    Returns the snapshot ref, or ``None`` when the tree was already clean (the
    common case — the central invariant is that the tree is clean at every
    handoff, so a dirty teardown means something went wrong and is exactly when
    the record is worth having).

    A snapshot failure RAISES: "we could not preserve it" must never become
    "so we deleted it anyway".
    """
    try:
        if gitops.is_clean(work_root, exclude=excludes or []):
            return None
    except gitops.GitError:
        pass  # cannot prove clean → treat as dirty and snapshot
    try:
        snap = git_snapshot.create_snapshot(
            work_root,
            run_id=run_id,
            reason="worktree-teardown",
            exclude=excludes,
        )
    except (git_snapshot.SnapshotError, gitops.GitError) as exc:
        raise WorktreeUnavailable(
            f"run worktree {work_root} for {slug!r} has uncommitted work and it "
            f"could not be snapshotted ({exc}); refusing to force-remove it. "
            "Commit or "
            "discard the work in that tree by hand, then retry (spike §11 row "
            "10 / R2 — a --force removal without a durable record is exactly "
            "what R2 forbids).",
            slug=slug,
        ) from exc
    return snap.ref


# --- the committed bookkeeping export ----------------------------------------


EXPORT_BANNER = (
    "<!-- EXPORTED COPY — NOT THE RUN'S STATE.\n"
    "     This file is written INTO the run worktree so the FR-2.2 bookkeeping\n"
    "     checkpoint commit has a path that exists in this branch's tree. The\n"
    "     authoritative record is the append-only journal in the operator's\n"
    "     checkout at <run_root>/<slug>/<run-id>/journal/ (R8); manifest.json\n"
    "     beside it there is that journal's live projection. Nothing in the\n"
    "     engine ever READS the copy you are looking at. -->\n"
)


def export_dir(work_root: Path, run_root: str, slug: str, run_id: str) -> Path:
    """``<work worktree>/<run_root>/<slug>/<run-id>`` — the export location.

    Mirrors the operator-checkout run-instance path exactly, so a human reading
    the run branch finds the bookkeeping where they expect it, and so the
    committed history of a `dedicated` run is byte-comparable with that of a
    `same_tree` one.
    """
    return work_root / run_root / slug / run_id


def write_bookkeeping_export(
    work_root: Path, state_root: Path, run_root: str, slug: str, run_id: str
) -> list[Path]:
    """Copy the two committable bookkeeping files into the run worktree.

    **Which manifest is authoritative, stated in the one place it matters.**
    The journal under ``state_root`` is authoritative (R8); ``state_root/
    manifest.json`` is its live projection; the file this function writes is a
    write-only EXPORT. The engine has exactly one writer (this function) and
    zero readers of the exported copy — every reader in the engine resolves
    through ``state_root``, which is why spike §4.4 could leave the whole
    reader population untouched.

    That makes a disagreement between the two harmless *to the engine* and
    visible *to a human*: between checkpoint commits the export is legitimately
    stale, because it is refreshed immediately before each commit rather than on
    every projection write. A stale export can therefore never influence a
    decision — only a human reading the branch — so the exported ``RUN.md``
    carries :data:`EXPORT_BANNER` naming the authoritative path, and
    ``tests/unit/test_worktree_p7c.py`` asserts no engine module loads a
    manifest from a work root.

    Returns the paths written (missing sources are skipped, not fabricated).
    """
    dest = export_dir(work_root, run_root, slug, run_id)
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in ("manifest.json", "RUN.md"):
        src = state_root / name
        if not src.exists():
            continue
        text = src.read_text()
        if name == "RUN.md":
            text = EXPORT_BANNER + text
        (dest / name).write_text(text)
        written.append(dest / name)
    return written


__all__ = [
    "EXPORT_BANNER",
    "MODES",
    "MODE_DEDICATED",
    "MODE_SAME_TREE",
    "PARK_REASON_WORKTREE_UNAVAILABLE",
    "RunWorktree",
    "WorktreeState",
    "WorktreeUnavailable",
    "describe",
    "ensure",
    "export_dir",
    "is_inside_worktrees_root",
    "lock_reason",
    "observe",
    "parse_lock_reason",
    "recreate",
    "release",
    "run_worktree_path",
    "worktrees_root",
    "write_bookkeeping_export",
]

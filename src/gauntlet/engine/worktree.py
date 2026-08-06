"""The dedicated run worktree: derivation, lifecycle and discovery (P7c/P7e).

**CORRECTION TO THE RATIFIED SPIKE (P7e).** Spike §6.2 fixed the root at
``<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>``. That location is not
usable: the `claude` CLI — which drives every `builder` and `verifier` step —
refuses to write any file whose path carries a literal ``.git`` **segment**,
and it does so non-uniformly across write mechanisms, so a run there fails only
*iff the model does not improvise a form the guard misses*. The full
measurement is `proposals/P7d-gate-blocker.md`; the maintainer ratified
sub-option **1A** on 2026-08-06 (see `proposals/P7-final-split.md` §1).

The root is therefore ``<main-worktree>/.gauntlet/worktrees/<slug>/<run-id>`` —
still derived, still with **no config knob** (§6.4, deferral D2).

Which of §6.2's measured properties survive, re-measured at the new location
rather than assumed (P7e, experiment E11):

* **agent-writable** — FIXED, and the reason the move happened. `Write`, `Edit`
  and a shell redirection all land here; all three were refused under ``.git/``.
* **invisible to ``git status``** — KEPT, but *mitigated rather than
  structural*: it depends on the self-ignoring marker this module re-establishes
  on every drive (:func:`ensure_root_marker`). Under ``.git/`` git ignored the
  tree with no help from us.
* **invisible to ``git status --ignored``** — **TRADED.** The tree now shows as
  ``!! .gauntlet/worktrees/``. Nothing in the engine reads `--ignored`
  (``gitops.is_clean`` does not pass it), so the FR-9.3 clean-handoff guard is
  unaffected; the loss is visible to a human who asks for ignored files.
* **untouched by ``git clean -xdff``** — **TRADED.** ``-xdf`` deletes the
  marker but skips the tree; ``-xdff`` deletes the tree. The second is a
  recoverable §11 row 2 event, *demonstrated* rather than argued: E11 produced
  the registered-and-``prunable`` shape with the branch ref intact, and a
  ``prune`` + ``add`` rebuilt the tree on its branch. Note that ``git worktree
  lock`` does NOT protect against ``clean`` — git's clean is not
  worktree-aware — so the lock is not a mitigation here.
* **same filesystem / inside the repository / no outer-repo contamination / no
  repo-identity map** — KEPT, all four measured at the new root. Same-device
  means ``EXDEV`` stays structurally unreachable for every atomic write; inside
  the repository means `RECOVERY-REDESIGN-PLAN.md` §7's global acceptance
  property survives **unamended**, which is what 1A buys over the two
  outside-the-repo options.
* **tolerated by ``fsck``/``gc``, and a valid parent for the verifier's
  disposable copy** — KEPT (§12.4 holds: only the parent changes).

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

# `worktree.mode` values (spike §13). `dedicated` is the DEFAULT since P7g, so
# acceptance A1/A2/A3 hold for runs in general. `same_tree` is not removed
# (§16): it stays the mode of every legacy run forever, and the documented,
# explicitly-selectable fallback for an adopter layout that cannot host a
# worktree.
MODE_SAME_TREE = "same_tree"
MODE_DEDICATED = "dedicated"
MODES = (MODE_SAME_TREE, MODE_DEDICATED)

# The fixed path segments under the MAIN worktree root (P7e; §6.2 as corrected).
# Not configurable: a knob would need a `resolve()`-based containment validator,
# and spike E9-C proves the existing string-based `_validate_repo_relative` is
# defeated by a symlink. Not shipping the knob avoids the whole class (§6.4,
# deferral D2).
#
# `.gauntlet/` is shared with the adopter's TRACKED `config.yaml`, which is why
# the self-ignoring marker goes one level down at `worktrees/.gitignore` and
# never at `.gauntlet/.gitignore` — the latter would blanket-ignore the very
# file `RunConfig.load` reads.
GAUNTLET_DIRNAME = ".gauntlet"
WORKTREE_SUBDIR = "worktrees"

# The pre-P7e root, retained for DETECTION ONLY (P7e). An adopter who opted
# into `dedicated` before this commit has a tree here; the engine must
# recognise it well enough to refuse actionably and to relocate it on request,
# and must never let it resolve as `same_tree` — that would drop the run back
# onto the operator's own checkout. Never written to.
LEGACY_WORKTREE_DIRNAME = "gauntlet"

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


def worktrees_root(main_root: Path) -> Path:
    """``<main-worktree>/.gauntlet/worktrees`` — the parent of every run tree.

    ``main_root`` is the MAIN worktree (``gitops.main_worktree_root``), never
    the invoking checkout. See that function for why: an operator driving from
    their own linked worktree must derive the same root as one driving from the
    main checkout, or `observe`'s scoping, the §14.4 refusal and
    ``_run_tree_excludes`` stop agreeing about which trees are the engine's.
    """
    return main_root / GAUNTLET_DIRNAME / WORKTREE_SUBDIR


def legacy_worktrees_root(common_dir: Path) -> Path:
    """``<git-common-dir>/gauntlet/worktrees`` — the pre-P7e root (detection only).

    Runs created by a P7c/P7d engine live under here. Nothing writes to it; it
    exists so :func:`legacy_observe` can tell "this run has a tree at the OLD
    derived path" apart from "this run has no tree", which are the same answer
    to every containment check built on :func:`worktrees_root` and would
    otherwise resolve the run to `same_tree`.
    """
    return common_dir / LEGACY_WORKTREE_DIRNAME / WORKTREE_SUBDIR


def ensure_root_marker(main_root: Path) -> Path | None:
    """Re-establish the self-ignoring marker at the worktrees root (P7e).

    The engine-owned ``<main>/.gauntlet/worktrees/.gitignore`` containing ``*``
    (which therefore ignores itself) is what keeps run worktrees out of the
    operator's ``git status`` — the clean-handoff invariant in CLAUDE.md §1 and
    the FR-9.3 guard. Under spike §6.2 no marker was needed, because ``.git/``
    is invisible to git by construction; that invisibility is the guarantee 1A
    downgrades from *structural* to *maintained*, and this is what maintains it.

    Called on **every** driving verb, not once at creation, because ``git clean
    -xdf`` deletes the marker while deliberately skipping the worktree itself
    (``Skipping repository …`` / ``Removing .gauntlet/worktrees/.gitignore``,
    measured at E11). Without re-establishment that single ordinary keystroke
    would leave the tree as permanent untracked dirt from the next command
    onward; with it, the breakage is transient. Idempotent, and mirrors
    :meth:`run.RunManager._ensure_run_root_gitignore`.

    Never writes ``.gauntlet/.gitignore``: ``.gauntlet/config.yaml`` is a
    tracked adopter file and a ``*`` one level up would blanket-ignore it.

    Returns the marker path, or ``None`` if it could not be written — a
    best-effort guarantee, because failing a drive over a cosmetic ignore rule
    would be a worse trade than a dirty ``git status``. The caller that cares
    (the FR-9.3 clean guard) reports the dirt on its own.
    """
    root = worktrees_root(main_root)
    marker = root / ".gitignore"
    try:
        root.mkdir(parents=True, exist_ok=True)
        if not marker.exists() or marker.read_text() != "*\n":
            marker.write_text("*\n")
    except OSError:
        return None
    return marker


def run_worktree_path(main_root: Path, slug: str, run_id: str) -> Path:
    """The derived, non-configurable path for one run's tree (§6.2/§6.4).

    ``slug`` and ``run_id`` are already containment-checked by
    ``run.safe_run_segment`` at every entry point that can supply them, so this
    is a pure join. It is deliberately *derived* rather than persisted: a
    recorded path could drift from the layout the running engine expects, and
    the one incident that most needs this path — recreating a MISSING tree
    (acceptance A3) — must work from the branch ref and the journal alone.
    """
    return worktrees_root(main_root) / slug / run_id


def is_inside_worktrees_root(path: Path, main_root: Path) -> bool:
    """True when ``path`` resolves inside the engine-owned worktrees root.

    Both sides are ``resolve()``d before comparison: git records worktree paths
    exactly as given and does not resolve symlinks (spike E9-B), so a string
    comparison would be defeated by the same symlink that defeats
    ``_validate_repo_relative`` (E9-C).

    **Why this is still sound after P7e** (design problem A). Under §6.2 the
    root was inside the git dir, so containment was a fact about a directory git
    itself owned. It is now an ordinary in-repo path that could in principle
    collide with an adopter's own files, which makes this a *validator* rather
    than a *derivation*. Three callers depend on it and each stays correct for a
    different reason:

    * **``observe``'s scoping** — asks "is this tree one the engine created for a
      run?". A false negative degrades to git's own one-branch-one-worktree
      refusal, which names the holder; a false positive would let teardown call
      ``worktree remove`` on a tree the engine does not own. The
      ``resolve()``-on-both-sides discipline is what prevents the false
      positive, and it is unchanged.
    * **the §14.4 "you are inside a run worktree" refusal** — now anchored at
      the MAIN worktree (``gitops.main_worktree_root``) rather than the invoking
      checkout, so it answers the same way from inside a run worktree as from
      the operator's. Anchoring at ``--show-toplevel`` would have made the
      refusal unreachable from precisely the place it exists to fire.
    * **``_run_tree_excludes``** — excludes the engine's own root from
      dirtiness checks. It is scoped to a path *under the repo* now, which is
      strictly narrower than the git dir it replaced.

    An adopter who keeps their own files at ``.gauntlet/worktrees/`` would
    collide; that path is engine-owned by the same convention that already owns
    ``.gauntlet/config.yaml``, and the marker written there makes the ownership
    visible on disk.
    """
    try:
        path.resolve().relative_to(worktrees_root(main_root).resolve())
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


def legacy_observe(
    repo_root: Path, branch: str, *, common_dir: Path
) -> gitops.WorktreeEntry | None:
    """This run's worktree at the PRE-P7e root, or ``None``. Read-only (P7e).

    The evidence half of the legacy-root case. A run created by a P7c/P7d engine
    has its tree under ``<git-common-dir>/gauntlet/worktrees/``, which every
    containment check built on :func:`worktrees_root` now rejects — so without
    this the engine cannot tell "the tree is at the old path" from "there is no
    tree", and those two must not share an answer:

    * ``ensure`` would try to ``worktree add`` at the new path and hit git's
      one-branch-one-worktree refusal, which is fail-closed but offers
      ``--same-tree`` — the one action that WOULD drop the run onto the
      operator's checkout;
    * a run whose journal lost its ``WorktreeAdopted`` backstop would resolve
      `same_tree` outright, which is the silent form of the same thing.

    Spike §10's rule is absolute — a pre-existing run is never auto-migrated and
    never wedged — so this feeds an actionable refusal and the explicit
    relocation, never an implicit move.
    """
    entry = gitops.worktree_for_branch(repo_root, branch)
    if entry is None:
        return None
    try:
        entry.path.resolve().relative_to(legacy_worktrees_root(common_dir).resolve())
    except (ValueError, OSError, RuntimeError):
        return None
    return entry


def observe(
    repo_root: Path, branch: str, *, main_root: Path | None = None
) -> gitops.WorktreeEntry | None:
    """This run's OWN worktree for ``branch``, or ``None``. Read-only.

    Usable when the driver is dead, which is what makes it the evidence half of
    both the §10 migration-detection rule and the §11 recovery rows. Never
    raises for a repository with no worktrees; a git failure propagates as
    :class:`gitops.GitError` because "cannot read the worktree list" must not
    be indistinguishable from "there is no worktree" (fail closed).

    ``main_root`` scopes the answer to trees under the engine's own derived
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
    if main_root is not None and not is_inside_worktrees_root(entry.path, main_root):
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
    answer for a run that drives the operator's checkout.

    An unreadable worktree list raises :class:`gitops.GitError` (F-013). It
    must NOT degrade to ``registered=False``: that is a positive claim ("this
    run has no worktree") manufactured from a failed observation, and the
    schema documents this object as observed facts. The caller
    (:func:`operator.compute_worktree_block`) turns the raise into
    ``worktree: null`` — "unknown" — which is the honest rendering and keeps
    `status` from contradicting a mutating verb that CAN read the list (R4).
    """
    if mode != MODE_DEDICATED:
        return WorktreeState(
            mode=mode, path=None, registered=False, present=False,
            locked_reason=None, prunable=None, head=None,
        )
    entry = observe(
        repo_root, branch, main_root=gitops.main_worktree_root(repo_root)
    )
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
    main_root: Path,
    *,
    slug: str,
    run_id: str,
    branch: str,
    common_dir: Path | None = None,
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

    ``common_dir`` is optional and used for ONE thing (P7e): recognising a tree
    still at the pre-P7e root, so that case gets a refusal naming the relocation
    instead of git's generic one offering ``--same-tree``. Omitting it is safe —
    the add still fails closed — just less actionable.
    """
    target = run_worktree_path(main_root, slug, run_id)
    if common_dir is not None:
        _refuse_legacy_root(repo_root, common_dir, slug=slug, branch=branch, target=target)
    # Re-established before the tree exists, so it is never briefly visible to
    # `git status` (the same ordering reason as `_ensure_run_dir_gitignore`).
    ensure_root_marker(main_root)
    try:
        with repolock.repo_lock(repo_root, reason=f"worktree:ensure:{slug}"):
            wt = _ensure_locked(
                repo_root, target, main_root,
                slug=slug, run_id=run_id, branch=branch,
            )
    except repolock.RepoLockError as exc:
        raise WorktreeUnavailable(
            f"could not serialize worktree creation for {slug!r}: {exc}", slug=slug
        ) from exc
    _verify_submodules(wt.path, slug)
    return wt


def _refuse_legacy_root(
    repo_root: Path, common_dir: Path, *, slug: str, branch: str, target: Path
) -> None:
    """Fail closed, actionably, on a run whose tree is at the pre-P7e root (P7e).

    Spike §10's rule is that a pre-existing run is never auto-migrated and never
    wedged. Relocating this tree implicitly would be the auto-migration half;
    letting ``ensure`` fall through to ``worktree add`` would be the wedged half
    in disguise, because git's refusal — correct as it is — carries the
    ``--same-tree`` action, and taking it would drive this run in the operator's
    own checkout. That is the one outcome the legacy case must never reach by
    default.

    So: name the old tree, name the new root, and name the verb that moves it.
    """
    entry = legacy_observe(repo_root, branch, common_dir=common_dir)
    if entry is None:
        return
    raise WorktreeUnavailable(
        f"run {slug!r} has its worktree at {entry.path}, which is the pre-P7e "
        f"location under the git directory. Gauntlet no longer drives runs "
        f"there: the `claude` CLI refuses to write any path containing a "
        f"`.git` segment, so a builder or verifier step in that tree fails — "
        f"sometimes silently, depending on which write form the model picks "
        f"(proposals/P7d-gate-blocker.md §2). This run's tree has NOT been "
        f"moved and nothing has been changed.\n"
        f"  Relocate it to {target} with:\n"
        f"      gauntlet migrate-worktree {slug}\n"
        f"  That is an explicit, journalled, copy-never-move transaction and it "
        f"refuses while a driver is live. Until then the run is not driven.",
        slug=slug,
    )


def _ensure_locked(
    repo_root: Path, target: Path, main_root: Path,
    *, slug: str, run_id: str, branch: str,
) -> RunWorktree:
    """:func:`ensure`'s body, with the repo-global lock already held.

    ``main_root`` is threaded in rather than re-derived from ``target``:
    counting path components back up to the git dir is the kind of arithmetic
    that is silently wrong by one, and the failure mode is invisible — a
    mis-scoped ``observe`` returns ``None``, the stale-entry cleanup never
    runs, and ``add`` fails on an entry this function was supposed to clear.
    """
    entry = observe(repo_root, branch, main_root=main_root)
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
        # F-004: a healthy tree is not enough — it must also still carry OUR
        # anti-prune lock. A crash between `worktree add` and `worktree lock`
        # (the `after_add` boundary) leaves exactly this shape: registered,
        # non-prunable, on disk, and UNLOCKED. Returning here without checking
        # adopted it permanently unlocked, so any other run's repository-wide
        # prune could delete it mid-drive — the one failure §8.3's lock exists
        # to prevent. Re-establishing the marker is the self-healing action.
        _require_lock(repo_root, target, slug=slug, run_id=run_id, entry=entry)
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
        _discard_worktree(repo_root, target, slug=slug, run_id=run_id)
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
        _discard_worktree(
            repo_root, entry.path, slug="<stale>", run_id="stale-registration"
        )
    try:
        gitops.prune_worktrees(repo_root, expire="now")
    except gitops.GitError:
        pass


def _require_lock(
    repo_root: Path,
    target: Path,
    *,
    slug: str,
    run_id: str,
    entry: gitops.WorktreeEntry,
) -> None:
    """Ensure an adopted tree carries THIS run's lock; fail closed otherwise.

    Three cases, and the middle one is the reason this exists (F-004):

    * already ours — nothing to do;
    * **unlocked** — a crash between ``add`` and ``lock``. Establish it. This is
      the self-healing half: the invariant is "a live run's tree is locked", and
      an interrupted lifecycle should converge back to it rather than run
      forever in a state that is silently prune-vulnerable;
    * **locked by someone else** — a different run/id, or a human's manual
      ``worktree lock``. Never steal it: a foreign marker is evidence we cannot
      explain, and overwriting it is the same class of mistake as ``add -f``.
    """
    expected = lock_reason(slug, run_id)
    current = entry.locked
    if current == expected:
        return
    if current is None:
        try:
            gitops.lock_worktree(repo_root, target, reason=expected)
            return
        except gitops.GitError as exc:
            raise WorktreeUnavailable(
                f"run worktree {target} for {slug!r} is unlocked (a crash "
                f"between `worktree add` and `worktree lock`) and the marker "
                f"could not be re-established: {exc}. Driving it unlocked would "
                "leave it deletable by any other run's prune (spike E8-C).",
                slug=slug,
            ) from exc
    raise WorktreeUnavailable(
        f"run worktree {target} for {slug!r} is locked by someone else: "
        f"{current!r} (expected {expected!r}). Refusing to steal a marker this "
        "engine did not place — it is evidence of another holder or a manual "
        "`git worktree lock`. Inspect it, then `git -C "
        f"{repo_root} worktree unlock {target}` if it is genuinely stale.",
        slug=slug,
    )


def _discard_worktree(
    repo_root: Path, path: Path, *, slug: str, run_id: str
) -> str | None:
    """The ONE forced-removal path: prove clean or snapshot, then remove (F-005).

    ``gitops.remove_worktree`` is unconditionally ``--force``. Two cleanup
    callers reached it directly — the failed-lock unwind and the stale-entry
    clear — so a tree with uncommitted work could be destroyed without the
    durable record R2 requires. The odds differ (a seconds-old tree is usually
    empty; a stale registered tree may hold real work) but R2 does not scale
    with odds, and one guarded helper is simpler than two justified exceptions.

    Returns the snapshot ref when one was taken. Never raises: both callers are
    already on an error/cleanup path. A tree that could not be preserved is
    LEFT IN PLACE rather than force-removed — leaking a worktree is recoverable
    (``gauntlet clean``), destroying uncommitted work is not.
    """
    ref: str | None = None
    try:
        ref = _snapshot_if_dirty(path, slug=slug, run_id=run_id, excludes=None)
    except WorktreeUnavailable:
        return None  # could not preserve → do not destroy
    except (gitops.GitError, OSError):
        return None
    try:
        gitops.remove_worktree(repo_root, path)
    except gitops.GitError:
        pass
    return ref


def recreate(
    repo_root: Path,
    main_root: Path,
    *,
    slug: str,
    run_id: str,
    branch: str,
    expect_head: str | None = None,
    common_dir: Path | None = None,
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
        repo_root, main_root, slug=slug, run_id=run_id, branch=branch,
        common_dir=common_dir,
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
    "GAUNTLET_DIRNAME",
    "LEGACY_WORKTREE_DIRNAME",
    "MODES",
    "MODE_DEDICATED",
    "MODE_SAME_TREE",
    "PARK_REASON_WORKTREE_UNAVAILABLE",
    "RunWorktree",
    "WorktreeState",
    "WorktreeUnavailable",
    "describe",
    "ensure",
    "ensure_root_marker",
    "export_dir",
    "is_inside_worktrees_root",
    "legacy_observe",
    "legacy_worktrees_root",
    "lock_reason",
    "observe",
    "parse_lock_reason",
    "recreate",
    "release",
    "run_worktree_path",
    "worktrees_root",
    "write_bookkeeping_export",
]

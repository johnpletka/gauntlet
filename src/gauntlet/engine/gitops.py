"""Thin git wrapper for the engine (FR-9, FR-8 transaction boundary).

The engine executes only human-committed configuration and git operations on
its own behalf — it never substitutes agent-authored text into a command line
(plan §0 trust model / review F-001). These helpers shell out to ``git`` with
explicit, fixed argv; the only model-derived value that reaches git is the
commit *message*, which is passed via a file/`-F`-style stdin path and is
treated as data (format-validated before it is used — see ``commit_format``).
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Intra-phase checkpoint-commit subject convention (harness-efficiency FR-11.1 /
# §6): ``P<N> wip: <milestone>``. Matched at fixed field position (the subject
# line), never against free-text prose — the checkpoint discovery below and the
# recovery rewind-target selection both key on exactly this shape.
_WIP_SUBJECT_RE = re.compile(r"^P\d+ wip:")

# Engine bookkeeping-commit subject convention: every orchestrator-owned commit
# (manifest/RUN.md response checkpoints, the FR-11.2 rewind commit) carries a
# fixed ``gauntlet:`` prefix and never touches tracked implementation. A
# checkpoint-preserving recovery can leave such a commit BETWEEN a phase's wip
# commits, so the trailing-checkpoint walk (commit step) treats it as
# transparent — walking through it to the wips beneath — rather than stopping at
# it (review F-002). Phase (``P<N>:``) and checkpoint (``P<N> wip:``) subjects
# never collide with this prefix.
_ENGINE_SUBJECT_RE = re.compile(r"^gauntlet: ")


def _wip_subject_re(phase: str | None) -> re.Pattern[str]:
    """Checkpoint-subject matcher, scoped to ``phase`` (e.g. ``P9``) when given.

    Unscoped (``phase is None``) it matches any ``P<N> wip:`` subject — the
    legacy behaviour. Scoped, it matches ONLY ``<phase> wip:`` so a wrong-phase
    checkpoint is never mistaken for this phase's (review F-001).
    """
    if phase is None:
        return _WIP_SUBJECT_RE
    return re.compile(rf"^{re.escape(phase)} wip:")


class WrongPhaseCheckpointError(RuntimeError):
    """A trailing checkpoint commit belongs to a different phase (fail closed).

    Raised by the commit step's scoped checkpoint discovery when this phase's
    trailing run of ``P<N> wip:`` commits contains a ``wip:`` commit for another
    phase — e.g. a mistyped ``P8 wip:`` landed during a P9 implement (review
    F-001). Squashing it into, or truncating the trailing run at, the wrong
    phase would corrupt the phase boundary, so the engine fails closed instead.
    """

    def __init__(self, expected_phase: str, found_subject: str) -> None:
        super().__init__(
            f"trailing checkpoint {found_subject!r} is not a {expected_phase} "
            "checkpoint; refusing to treat a wrong-phase wip commit as this "
            "phase's checkpoint (failing closed, FR-11.1)"
        )
        self.expected_phase = expected_phase
        self.found_subject = found_subject


class GitError(RuntimeError):
    """A git invocation failed. Carries argv + stderr for the manifest/log."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git {' '.join(argv)} failed (exit {returncode}): {stderr.strip()}"
        )
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


# --- root scope classification (P7a, spike §9) --------------------------------
#
# Every helper here takes a `repo` path, and that single parameter has silently
# meant three different things since the bootstrap. Once a run gets its own
# worktree (P7c) the three diverge, and passing the wrong one is invisible in a
# same-tree test: the operation succeeds, against the wrong tree.
#
# So the scope is DATA, not documentation. `tests/unit/test_root_scope.py`
# parses every `gitops.*` call in the engine and fails when a WORK-scoped helper
# is handed anything but a work-tree root — and fails when a helper is missing
# from this table, so the classification cannot silently rot as helpers are
# added.
#
#   WORK   — operates on or observes a WORKING TREE: its index, its HEAD, its
#            checked-out branch, its file contents. HEAD and the index are
#            per-worktree (proved in spike E1), so even a read like `head_sha`
#            is work-scoped: from the operator's checkout it answers about the
#            operator's branch, not the run's.
#   REPO   — a property of the REPOSITORY, identical from any worktree: refs,
#            the object database, the commit graph, diffs between two SHAs.
#   COMMON — the shared git dir / worktree administration itself.
ROOT_SCOPE_WORK = "work"
ROOT_SCOPE_REPO = "repo"
ROOT_SCOPE_COMMON = "common"

ROOT_SCOPE: dict[str, str] = {
    # --- WORK: the tree the agent edits and the engine commits in -------------
    "head_sha": ROOT_SCOPE_WORK,          # per-worktree HEAD
    "current_branch": ROOT_SCOPE_WORK,    # per-worktree HEAD
    "status_porcelain": ROOT_SCOPE_WORK,
    "is_clean": ROOT_SCOPE_WORK,
    "is_dirty_vs": ROOT_SCOPE_WORK,
    "dirty_paths_matching": ROOT_SCOPE_WORK,
    "dirty_paths": ROOT_SCOPE_WORK,
    "worktree_tree_hash": ROOT_SCOPE_WORK,
    "diff_head": ROOT_SCOPE_WORK,
    "diff_worktree_vs": ROOT_SCOPE_WORK,
    "commit_all": ROOT_SCOPE_WORK,
    "commit_paths": ROOT_SCOPE_WORK,
    "commit_run_bookkeeping": ROOT_SCOPE_WORK,
    "commit_tracked_bookkeeping": ROOT_SCOPE_WORK,
    "checkout_branch": ROOT_SCOPE_WORK,
    "checkout_or_create_branch": ROOT_SCOPE_WORK,
    "recreate_branch": ROOT_SCOPE_WORK,
    "merge_branch": ROOT_SCOPE_WORK,
    "merge_abort": ROOT_SCOPE_WORK,
    "reset_hard": ROOT_SCOPE_WORK,
    "reset_soft": ROOT_SCOPE_WORK,
    "unstage": ROOT_SCOPE_WORK,
    "clean_untracked": ROOT_SCOPE_WORK,
    "rewind_impl_preserving_bookkeeping": ROOT_SCOPE_WORK,
    "apply_patch": ROOT_SCOPE_WORK,
    "apply_patch_check": ROOT_SCOPE_WORK,
    "apply_patch_error": ROOT_SCOPE_WORK,
    "git_index_path": ROOT_SCOPE_WORK,    # per-worktree index (spike E1/E7)
    "is_tracked": ROOT_SCOPE_WORK,        # reads the index
    "path_is_ignored": ROOT_SCOPE_WORK,
    "path_is_untracked": ROOT_SCOPE_WORK,
    "wip_checkpoints": ROOT_SCOPE_WORK,   # walks from the tree's own HEAD
    "run_with_temp_index": ROOT_SCOPE_WORK,
    "validate_temp_index_path": ROOT_SCOPE_WORK,
    "show_toplevel": ROOT_SCOPE_WORK,
    # --- REPO: identical from any worktree -----------------------------------
    "is_git_repo": ROOT_SCOPE_REPO,
    "rev_parse": ROOT_SCOPE_REPO,
    "branch_exists": ROOT_SCOPE_REPO,
    "create_branch": ROOT_SCOPE_REPO,   # ref-store only; checks out nothing
    "delete_branch": ROOT_SCOPE_REPO,
    "tag_exists": ROOT_SCOPE_REPO,
    "ref_is_valid_commit": ROOT_SCOPE_REPO,
    "is_ancestor": ROOT_SCOPE_REPO,
    "merge_base": ROOT_SCOPE_REPO,
    "create_ref": ROOT_SCOPE_REPO,
    "create_ref_exclusive": ROOT_SCOPE_REPO,
    "delete_ref": ROOT_SCOPE_REPO,
    "hash_object_write": ROOT_SCOPE_REPO,
    "cat_file_blob": ROOT_SCOPE_REPO,
    "object_exists": ROOT_SCOPE_REPO,
    "mktree": ROOT_SCOPE_REPO,
    "commit_tree": ROOT_SCOPE_REPO,
    "commit_subject": ROOT_SCOPE_REPO,
    "commit_parent": ROOT_SCOPE_REPO,
    "commit_message": ROOT_SCOPE_REPO,
    "log_range": ROOT_SCOPE_REPO,
    "range_diff": ROOT_SCOPE_REPO,
    "range_diff_path": ROOT_SCOPE_REPO,
    "diff_range_empty": ROOT_SCOPE_REPO,
    "any_tracked_at": ROOT_SCOPE_REPO,
    "file_at_commit": ROOT_SCOPE_REPO,
    "file_bytes_at_commit": ROOT_SCOPE_REPO,
    "file_mode_at_commit": ROOT_SCOPE_REPO,
    "advance_is_engine_bookkeeping": ROOT_SCOPE_REPO,
    "remote_url": ROOT_SCOPE_REPO,
    "remote_default_branch": ROOT_SCOPE_REPO,
    # --- COMMON: the shared git dir / worktree administration ----------------
    #
    # Every entry below mutates or reads the ONE worktree administration dir the
    # whole repository shares, so the root passed in selects the *repository*,
    # never a tree — any worktree of the repo answers identically (spike E1/E8).
    # That is also why each of these runs inside the repo-global lock
    # (`repolock`): the admin dir has no per-worktree isolation to fall back on.
    "git_common_dir": ROOT_SCOPE_COMMON,
    "add_worktree": ROOT_SCOPE_COMMON,
    "add_worktree_branch": ROOT_SCOPE_COMMON,
    "remove_worktree": ROOT_SCOPE_COMMON,
    "prune_worktrees": ROOT_SCOPE_COMMON,
    "lock_worktree": ROOT_SCOPE_COMMON,
    "unlock_worktree": ROOT_SCOPE_COMMON,
    "repair_worktree": ROOT_SCOPE_COMMON,
    "list_worktrees": ROOT_SCOPE_COMMON,
    "worktree_for_branch": ROOT_SCOPE_COMMON,
    # Reads `worktree list --porcelain`, which every worktree of a repository
    # answers identically — that vantage-independence is the whole reason P7e
    # anchors the derived run-worktree root here rather than on `show_toplevel`,
    # which is WORK-scoped precisely because it does NOT have it.
    "main_worktree_root": ROOT_SCOPE_COMMON,
    # `submodule status` reads the SUPERPROJECT's index and the on-disk
    # submodule dirs, so it is genuinely a property of one tree — but the tree
    # it must be asked about is always the RUN worktree (that is the point of
    # spike §7: the superproject's own checkout has its submodules populated
    # while a fresh linked worktree does not), so it is work-scoped and the
    # static audit holds callers to naming a work root.
    "submodule_status": ROOT_SCOPE_WORK,
    "uninitialized_submodules": ROOT_SCOPE_WORK,
}


def _run(
    repo: Path, *args: str, stdin: str | None = None, _env: dict[str, str] | None = None
) -> str:
    argv = ["git", "-C", str(repo), *args]
    proc = subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        env=_env,
    )
    if proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc.stdout


def _run_bytes(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    """Binary-safe variant of :func:`_run` for object I/O (index bytes, blobs)."""
    argv = ["git", "-C", str(repo), *args]
    proc = subprocess.run(argv, input=stdin, capture_output=True)
    if proc.returncode != 0:
        raise GitError(
            list(args), proc.returncode, proc.stderr.decode("utf-8", "replace")
        )
    return proc.stdout


class TempIndexPathError(ValueError):
    """A temporary-index path failed the containment validation (fail closed)."""


def validate_temp_index_path(repo: Path, index_file: Path) -> None:
    """Validate a ``GIT_INDEX_FILE`` target before any git command may use it.

    The recovery-snapshot machinery (P2, plan §4.4) is the only sanctioned user
    of a substitute index. Containment rules, all fail-closed:

    * the path must be absolute — a relative path would resolve against git's
      cwd, not a location this validation inspected;
    * it must resolve OUTSIDE the repository worktree — a temp index inside the
      worktree would itself dirty the tree the snapshot must observe untouched
      (R3: snapshot creation is observational);
    * it must resolve OUTSIDE the git dir — nothing under ``.git`` (the real
      index, refs, packed objects) may ever be the scratch target. BOTH git
      dirs are checked: in a *linked* worktree ``--absolute-git-dir`` names only
      that worktree's private admin dir (``.git/worktrees/<name>``), while the
      real index, refs and object database live in the SHARED
      ``--git-common-dir``. Checking only the former accepts a scratch path
      inside the shared ``.git`` — the identical path this same function
      rejects when called against the main worktree, because there the shared
      dir happens to sit under the toplevel. The two dirs coincide in a main
      worktree, so this costs nothing there and closes the gap everywhere else
      (P7 spike §9.2, experiment E7).

    ``resolve()`` is applied to the parent directory (the leaf may not exist
    yet) so a symlinked temp path cannot smuggle the file into either tree.
    """
    if not index_file.is_absolute():
        raise TempIndexPathError(
            f"temporary index path must be absolute: {index_file}"
        )
    resolved = index_file.parent.resolve() / index_file.name
    top = Path(show_toplevel(repo)).resolve()
    git_dir = Path(_run(repo, "rev-parse", "--absolute-git-dir").strip()).resolve()
    common_dir = git_common_dir(repo)
    for forbidden in (top, git_dir, common_dir):
        if resolved == forbidden or forbidden in resolved.parents:
            raise TempIndexPathError(
                f"temporary index path {index_file} resolves inside {forbidden}; "
                "a substitute index must live outside the worktree and git dir"
            )


def run_with_temp_index(
    repo: Path, index_file: Path, *args: str, stdin: str | None = None
) -> str:
    """Run ONE git command against a validated substitute index file.

    This is the narrow environment extension P2 requires (plan §4.4): the only
    variable that can be injected is ``GIT_INDEX_FILE``, its value is a path
    the caller supplies and :func:`validate_temp_index_path` has contained, and
    the rest of the environment is inherited untouched. There is deliberately
    no generic "run git with env overrides" surface.
    """
    validate_temp_index_path(repo, index_file)
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}
    return _run(repo, *args, stdin=stdin, _env=env)


def git_common_dir(repo: Path) -> Path:
    """Absolute path of the SHARED git dir (``rev-parse --git-common-dir``).

    In a main worktree this is the same directory ``--absolute-git-dir``
    reports. In a *linked* worktree the two differ: ``--absolute-git-dir`` is
    the worktree's private admin dir under ``.git/worktrees/<name>``, and this
    is the shared dir holding the object database, the refs and the main
    index. Any containment rule about "the git dir" must consider both.

    ``--git-common-dir`` answers relatively (``.git``) when invoked from a main
    worktree's top level, so the result is joined onto ``repo`` when it is not
    already absolute — the same idiom :func:`git_index_path` uses, chosen over
    ``--path-format=absolute`` so no minimum git version is implied.
    """
    out = _run(repo, "rev-parse", "--git-common-dir").strip()
    path = Path(out)
    return (path if path.is_absolute() else (repo / path)).resolve()


def main_worktree_root(repo: Path) -> Path:
    """Absolute path of the repository's MAIN worktree, from any vantage point.

    The anchor the run-worktree root is derived from (P7e). It is deliberately
    not ``rev-parse --show-toplevel``: that answers *the checkout you are
    standing in*, so an operator driving from their own linked worktree — or
    anything running inside a run worktree — would derive a different root and
    the containment rules built on it (``worktree.is_inside_worktrees_root``,
    the §14.4 refusal, ``_run_tree_excludes``) would stop agreeing with each
    other. Spike §6.2 got that vantage-independence for free from the shared
    git common dir; anchoring at the main worktree is how it survives the move
    out of ``.git/``.

    ``git worktree list --porcelain`` reports the main worktree FIRST — before
    the linked worktrees, and independently of creation order or of which
    worktree the command runs from (measured at P7e from the main checkout, an
    adopter's linked worktree, and a run worktree). For a bare repository the
    first entry is the bare directory itself, which keeps spike §7's "P7 must
    not accidentally forbid a bare repo" true.

    Raises :class:`GitError` when the list is unreadable or empty rather than
    guessing: a wrong answer here relocates every run worktree.
    """
    entries = list_worktrees(repo)
    if not entries:
        raise GitError(
            f"`git worktree list` reported no worktrees for {repo}; cannot "
            "derive the main worktree root"
        )
    return entries[0].path.resolve()


def git_index_path(repo: Path) -> Path:
    """Absolute path of the REAL index file (``rev-parse --git-path index``).

    Read-only discovery for the raw-index snapshot: the snapshot hashes these
    bytes into a blob and never writes them back except during an explicit
    exact restoration.
    """
    out = _run(repo, "rev-parse", "--git-path", "index").strip()
    path = Path(out)
    return path if path.is_absolute() else (repo / path)


def hash_object_write(repo: Path, data: bytes) -> str:
    """Store ``data`` as a blob in the object database; return its object id."""
    return _run_bytes(
        repo, "hash-object", "-w", "--stdin", stdin=data
    ).decode().strip()


def cat_file_blob(repo: Path, oid: str) -> bytes:
    """The raw bytes of blob ``oid`` (binary-safe)."""
    return _run_bytes(repo, "cat-file", "blob", oid)


def object_exists(repo: Path, oid: str) -> bool:
    """True iff ``oid`` names an object present in the object database."""
    try:
        _run(repo, "cat-file", "-e", oid)
        return True
    except GitError:
        return False


def mktree(repo: Path, entries: list[str]) -> str:
    """Build a tree object from ``ls-tree``-format entry lines; return its id.

    Entry names in the recovery-snapshot wrapper tree are fixed engine strings
    (``metadata.json``, ``index.raw``, ``worktree`` …), never model- or
    user-derived, so the newline format is safe here.
    """
    return _run(repo, "mktree", stdin="".join(f"{e}\n" for e in entries)).strip()


def commit_tree(
    repo: Path,
    tree: str,
    parents: list[str],
    message: str,
    *,
    identity: Identity,
) -> str:
    """Create a commit object for ``tree`` with explicit parents and identity.

    Pure object creation: no ref moves, no checkout, no index or worktree
    change. The message arrives on stdin, never argv.
    """
    args = [
        "-c", f"user.name={identity.name}",
        "-c", f"user.email={identity.email}",
        "commit-tree", tree,
    ]
    for parent in parents:
        args += ["-p", parent]
    return _run(repo, *args, stdin=message).strip()


def is_git_repo(repo: Path) -> bool:
    try:
        _run(repo, "rev-parse", "--git-dir")
        return True
    except GitError:
        return False


def head_sha(repo: Path) -> str:
    return _run(repo, "rev-parse", "HEAD").strip()


def rev_parse(repo: Path, ref: str) -> str:
    return _run(repo, "rev-parse", "--verify", ref).strip()


def current_branch(repo: Path) -> str:
    return _run(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _exclude_pathspec(exclude: list[str] | None) -> list[str]:
    """Build a git pathspec that limits an operation to everything but ``exclude``.

    The engine passes its own run root here: that subtree is bookkeeping
    (manifests, run pointer, transcripts), never part of the work tree, so it
    must be invisible to status/add — otherwise it reads as perpetual "dirt",
    pollutes phase commits, and confuses the base-SHA transaction boundary.
    """
    if not exclude:
        return []
    spec = ["--", "."]
    for e in exclude:
        spec.append(f":(exclude){e}")
        spec.append(f":(exclude){e}/**")
    return spec


def status_porcelain(
    repo: Path,
    *,
    exclude: list[str] | None = None,
    untracked_all: bool = False,
    paths: list[str] | None = None,
) -> str:
    """Porcelain status; empty string means a clean worktree.

    The untracked-files mode is ALWAYS pinned explicitly (never left to git
    config). An adopter with ``status.showUntrackedFiles=no`` would otherwise
    make ``--porcelain`` omit untracked files entirely, so ``is_clean`` could
    report a clean tree while untracked work exists — silently bypassing the
    FR-9.3 clean-handoff invariant and FR-9.6 mutation detection (review:
    safety checks must not depend on adopter-local git config; fail closed,
    determinism over cleverness). The explicit ``--untracked-files`` flag
    overrides that config.

    ``untracked_all`` selects ``all`` over the default ``normal``. ``normal``
    collapses a fully-untracked directory into a single ``dir/`` entry — fine
    for a clean/dirty boolean, but lossy for any caller that compares the
    reported paths against a specific file. A nested run-artifact layout
    (``.gauntlet/runs/<slug>/prd.md``) collapses all the way up to
    ``.gauntlet/runs/`` before anything under it is tracked, so a path-equality
    check never sees the file. Callers that match on individual paths must pass
    ``untracked_all=True``.

    ``paths`` narrows the report to a repo-relative pathspec. It lets a caller
    ask about ONE path without string-matching git's own output, which is quoted
    and rename-aware and therefore parses wrong exactly where it matters least
    and breaks worst. Combines with ``exclude``; git applies both pathspecs.
    """
    mode = "all" if untracked_all else "normal"
    scope = ["--", *paths] if paths else []
    return _run(
        repo, "status", "--porcelain", f"--untracked-files={mode}",
        *_exclude_pathspec(exclude), *scope,
    ).strip()


def is_clean(repo: Path, *, exclude: list[str] | None = None) -> bool:
    return status_porcelain(repo, exclude=exclude) == ""


def dirty_paths(
    repo: Path,
    *,
    exclude: list[str] | None = None,
    untracked_all: bool = True,
) -> list[str]:
    """Repo-relative paths with any uncommitted state, parsed STRUCTURALLY.

    The safe way to answer "which paths are dirty?". Callers used to slice
    :func:`status_porcelain`'s text as ``line[3:]``, which is wrong in a way
    that hides: that function ``.strip()``s the whole report, so when the FIRST
    entry's status is worktree-only — ``" M"``, ``" D"``, ``" A"``, a leading
    SPACE, and much the most common kind — the leading space is eaten and
    ``[3:]`` then eats the first character of the path too. ``runs/toy/prd.md``
    arrives as ``uns/toy/prd.md``.

    That was not merely cosmetic. :func:`~gauntlet.engine.cycle._only_artifact_dirty`
    compares the parsed list against the artifact's own path, so a tracked
    artifact with unstaged edits — the exact state an artifact-mode cycle
    reviews — never matched, the FR-9.3 baseline commit silently declined, and
    the round-1 clean-handoff guard failed the run while naming a corrupted
    path. Only the first entry is affected, so it survived every test whose
    fixture happened to dirty something else first.

    Uses ``-z`` like :func:`dirty_paths_matching`: NUL-delimited, never quoted,
    no leading-status ambiguity, and rename/copy entries report the live
    (destination) path with their source field skipped.
    """
    mode = "all" if untracked_all else "normal"
    out = _run(
        repo, "status", "--porcelain", "-z", f"--untracked-files={mode}",
        *_exclude_pathspec(exclude),
    )
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        paths.append(rel)
        if "R" in status or "C" in status:
            i += 1  # skip the rename/copy source field
    return paths


def is_dirty_vs(
    repo: Path,
    base_sha: str,
    *,
    exclude: list[str] | None = None,
    bookkeeping: list[str] | None = None,
) -> bool:
    """True if the worktree (tracked + staged + untracked) differs from ``base_sha``.

    The engine's transaction boundary (review F-003) records a step's base SHA
    before any worktree-touching step. On resume it compares against that base:
    a difference means the killed step left partial edits.

    HEAD ahead of ``base_sha`` is NOT dirt when the advance is purely the
    engine's own bookkeeping (#62/#65): the engine itself moves HEAD past the
    recorded base during a drive with response checkpoints and run-bookkeeping
    flushes. A bare ``head != base`` here made every plain resume of an
    interrupted step re-park on the engine's own commits, forever.
    ``bookkeeping`` is the EXACT allowlist of paths engine commits may touch
    (``engine_bookkeeping_candidates``) — deliberately not the broader
    ``exclude`` list, which also hides human-owned files the engine never
    commits (PR #76 review F-001). ``None`` disables the tolerance entirely
    (fail closed). Real commits above the base — wip checkpoints, operator
    commits, anything not engine bookkeeping — still read as dirty; so does a
    HEAD behind or forked from the base.
    The same allowlist governs the UNCOMMITTED leg (issue #96): once a
    response checkpoint force-tracks ``manifest.json``/``RUN.md``, the
    engine's own live re-projection of those files sits modified between
    flush commits — engine bookkeeping, not agent side effects, even where
    the ``exclude`` pathspec does not happen to hide it. Only paths in the
    exact ``bookkeeping`` allowlist are tolerated; any other dirty path
    (structurally parsed — an untracked directory reports as ``dir/`` and
    never matches) still reads dirty, and ``bookkeeping=None`` keeps the
    prior fail-closed behaviour on this leg too.
    """
    if bookkeeping is None:
        if status_porcelain(repo, exclude=exclude) != "":
            return True
    else:
        allowed = set(bookkeeping)
        if any(
            path not in allowed
            for path in dirty_paths(repo, exclude=exclude, untracked_all=False)
        ):
            return True
    if head_sha(repo) == base_sha:
        return False
    if bookkeeping is None:
        return True
    return not advance_is_engine_bookkeeping(repo, base_sha, bookkeeping=bookkeeping)


def advance_is_engine_bookkeeping(
    repo: Path, base_sha: str, *, bookkeeping: list[str], tip: str | None = None
) -> bool:
    """True iff ``base_sha..HEAD`` is nothing but engine bookkeeping (#62/#65).

    ``bookkeeping`` is the exact allowlist of repo-relative paths an engine
    bookkeeping commit may touch (the run's ``manifest.json``/``RUN.md`` — see
    ``engine_bookkeeping_candidates``). Three legs, ALL required (fail closed):

    1. ``base_sha`` is an ancestor of HEAD — behind or genuinely forked is
       never bookkeeping drift.
    2. Every commit in the range carries BOTH engine markers: the
       ``ENGINE_IDENTITY`` author AND the ``gauntlet: `` subject prefix. Either
       marker alone is forgeable/ambiguous; requiring both keeps the commit
       convention honest.
    3. The AUTHORITATIVE leg: every path changed across ``base_sha..HEAD`` is
       in the ``bookkeeping`` allowlist. The markers are a cheap precondition,
       but a ``gauntlet:``-subject commit CAN carry other tree changes
       (``rewind_impl_preserving_bookkeeping`` builds one, and the markers are
       locally forgeable), so the tree diff — not the commit labels — decides.
       An allowlist, not the dirty-check exclusions: those exclusions also
       hide human-owned paths (every slug's ``PR.md``) that must never be
       classified as engine bookkeeping (PR #76 review F-001).

    ``tip`` names the range end explicitly (default: HEAD). The pre-checkout
    rollback validation (P3, plan §6: validate everything before checkout)
    classifies ``base..refs/heads/<run-branch>`` without touching the
    operator's checkout, so it must not read a bare HEAD.
    """
    head = tip if tip is not None else head_sha(repo)
    if head == base_sha:
        return True
    if not is_ancestor(repo, base_sha, head):
        return False
    out = _run(repo, "log", "--format=%H%x00%an%x00%ae%x00%s", f"{base_sha}..{head}")
    for line in out.splitlines():
        _sha, name, email, subject = line.split("\x00", 3)
        if name != ENGINE_IDENTITY.name or email != ENGINE_IDENTITY.email:
            return False
        if not _ENGINE_SUBJECT_RE.match(subject):
            return False
    changed = _run(repo, "diff", "--name-only", base_sha, head).splitlines()
    allowed = set(bookkeeping)
    return all(path in allowed for path in changed if path)


def dirty_paths_matching(repo: Path, patterns: list[str]) -> list[str]:
    """Paths matching ``patterns`` with any uncommitted index/worktree state.

    The read-only probe the rollback checkout guard uses for human-owned
    excluded files (``PR.md``): they are hidden from the generic dirty guard
    by policy, but a branch checkout can still refuse or clobber their
    uncommitted state, so the guard needs to *detect* them without capturing
    bytes (the durable preservation is the recovery snapshot's job now).
    ``-z`` porcelain so special characters never arrive quoted; rename
    entries report the live (destination) path.
    """
    if not patterns:
        return []
    out = _run(
        repo, "status", "--porcelain", "-z", "--untracked-files=all",
        "--", *patterns,
    )
    fields = out.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        status, rel = entry[:2], entry[3:]
        paths.append(rel)
        if "R" in status or "C" in status:
            i += 1  # skip the rename/copy source field
    return sorted(set(paths))


def worktree_tree_hash(repo: Path) -> str:
    """A content hash of the repo's HEAD tree — the mutation-guard witness for the
    P5 verifier (FR-2.1/FR-2.5).

    Returns HEAD's tree object id (``git rev-parse HEAD^{tree}``): a stable digest
    of the *committed* tree the verifier hands off. The verifier runs in a
    disposable copy and must not touch the real worktree, so the plan's "run
    worktree hash is unchanged after verification" (P5-A4) is asserted by
    capturing this before verification and confirming it after — together with the
    existing FR-9.6 mutation guard, which watches the real tree for uncommitted
    dirt."""
    return _run(repo, "rev-parse", "HEAD^{tree}").strip()


def add_worktree(repo: Path, path: Path, ref: str = "HEAD") -> None:
    """Create a detached git worktree of ``ref`` at ``path`` (FR-2.1 disposable
    copy). ``--detach`` avoids branch contention with the run branch; the copy is a
    faithful checkout of the post-handoff committed tree. Raises ``GitError`` on
    failure so the verifier sub-step fails closed (never proceeds without a copy)."""
    _run(repo, "worktree", "add", "--detach", "--force", str(path), ref)


def remove_worktree(repo: Path, path: Path) -> None:
    """Remove a disposable worktree created by :func:`add_worktree`. ``--force``
    because the sandboxed verifier will have left uncommitted edits in the copy
    (that is the point — it *executes* the deliverable)."""
    _run(repo, "worktree", "remove", "--force", str(path))


def prune_worktrees(repo: Path, *, expire: str = "now") -> None:
    """Prune stale worktree administrative entries.

    ``expire`` is passed EXPLICITLY on every call (spike §11 row 7). A bare
    ``git worktree prune`` consults ``gc.worktreePruneExpire``, which is
    adopter-configurable: a repo that sets it to ``3.days.ago`` would silently
    leave a freshly-missing entry registered, and the recreate path (§11 row 2)
    would then hit ``already registered worktree`` instead of succeeding. Pinning
    the value is the same fail-closed reasoning as ``status_porcelain``'s pinned
    ``--untracked-files``: never let an adopter's config change the engine's
    observed answer.

    Repository-wide by construction (spike E8-C): this removes the ``prunable``
    entry of EVERY worktree of the repository, not just the caller's. That is
    why a live run worktree is held under :func:`lock_worktree` for its whole
    life, and why every call here runs inside the repo-global lock.
    """
    _run(repo, "worktree", "prune", f"--expire={expire}")


def add_worktree_branch(repo: Path, path: Path, branch: str) -> None:
    """Check ``branch`` out into a NEW linked worktree at ``path`` (P7c, §6.2).

    Deliberately not :func:`add_worktree`: that one is the verifier's
    ``--detach --force`` disposable copy. A run worktree is the opposite on both
    counts — it is *attached* to the run branch (so git's own
    one-branch-one-worktree rule supplies acceptance A2 for free, spike E2-A),
    and it is never ``--force``d (spike §11 rows 1 and 6: ``add -f`` would
    silently adopt a registered admin entry the recovery assessment has not
    explained, and ``add`` refusing a non-empty path is information, not an
    obstacle).

    Raises :class:`GitError` on every refusal — branch already checked out
    elsewhere, path exists and is non-empty, stale admin entry, or a
    leading-directory failure — so the caller parks with git's own stderr
    preserved rather than guessing.
    """
    _run(repo, "worktree", "add", str(path), branch)


def lock_worktree(repo: Path, path: Path, *, reason: str) -> None:
    """``git worktree lock --reason`` — the git-native anti-prune marker (§8.3).

    The ONLY thing that stops another run's ``prune_worktrees`` from removing
    this run's admin entry while its tree is momentarily missing (spike E8-C),
    and it also blocks ``worktree remove --force`` (E6-B), so a teardown must
    :func:`unlock_worktree` first. Held for the LIFE of the run worktree, not
    for a critical section — it is a marker, not a mutex.
    """
    _run(repo, "worktree", "lock", "--reason", reason, str(path))


def unlock_worktree(repo: Path, path: Path) -> None:
    """``git worktree unlock``. Raises :class:`GitError` if it was not locked."""
    _run(repo, "worktree", "unlock", str(path))


def repair_worktree(repo: Path, path: Path) -> str:
    """``git worktree repair`` for a worktree whose paths moved (§11 row 9).

    Preferred over prune+recreate when the tree is intact but the pointer pair
    is broken (spike E6-F), because it preserves uncommitted work.
    """
    return _run(repo, "worktree", "repair", str(path))


@dataclass(frozen=True)
class WorktreeEntry:
    """One record from ``git worktree list --porcelain``.

    ``prunable`` carries git's own machine-readable reason string (e.g.
    ``gitdir file points to non-existent location``) — the discovery signal
    spike §4.2 identifies for "the tree is gone but the branch survives", which
    is what the recreate action (§11 row 2) keys on. ``locked`` carries the
    ``--reason`` text, so the holder of a lock is self-describing.
    """

    path: Path
    head: str | None
    branch: str | None  # short name; None for a detached or bare entry
    bare: bool
    detached: bool
    locked: str | None  # the lock reason ("" when locked with no reason)
    prunable: str | None  # git's reason string


def list_worktrees(repo: Path) -> list[WorktreeEntry]:
    """Parse ``git worktree list --porcelain`` into records.

    The porcelain format is one blank-line-separated stanza per worktree, whose
    first line is always ``worktree <path>``. ``locked``/``prunable`` appear as
    a bare keyword or ``<keyword> <reason>``; both forms are preserved (a bare
    keyword becomes ``""``, which is falsy-but-not-None, so "locked with no
    reason" stays distinguishable from "not locked").

    Paths are recorded AS GIT REPORTS THEM — git does not resolve symlinks here
    (spike E9-B) — so any containment or identity comparison against an entry
    must ``resolve()`` both sides itself.
    """
    out = _run(repo, "worktree", "list", "--porcelain")
    entries: list[WorktreeEntry] = []
    cur: dict[str, Any] = {}

    def flush() -> None:
        if not cur.get("path"):
            return
        branch_ref = cur.get("branch")
        entries.append(
            WorktreeEntry(
                path=Path(cur["path"]),
                head=cur.get("HEAD"),
                branch=(
                    branch_ref[len("refs/heads/"):]
                    if isinstance(branch_ref, str)
                    and branch_ref.startswith("refs/heads/")
                    else branch_ref
                ),
                bare=bool(cur.get("bare")),
                detached=bool(cur.get("detached")),
                locked=cur.get("locked"),
                prunable=cur.get("prunable"),
            )
        )
        cur.clear()

    for raw in out.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            flush()
            cur["path"] = value
        elif key in ("HEAD", "branch", "locked", "prunable"):
            cur[key] = value
        elif key in ("bare", "detached"):
            cur[key] = True
    flush()
    return entries


def worktree_for_branch(repo: Path, branch: str) -> WorktreeEntry | None:
    """The registered worktree holding ``branch``, or ``None``.

    The evidence half of the spike §10 detection rule ("a run is `same_tree` iff
    its journal carries no ``WorktreeAdopted`` event AND ``worktree list``
    registers no worktree for ``man.branch``") and the discovery half of §11
    rows 2 and 4. Read-only and available when the driver is dead, which is what
    makes it usable from a recovery assessment.
    """
    for entry in list_worktrees(repo):
        if entry.branch == branch:
            return entry
    return None


def submodule_status(repo: Path) -> list[tuple[str, str]]:
    """``git submodule status`` → ``[(state_prefix, path)]`` (spike §7).

    A worktree of a superproject checks out the submodule *gitlink* but leaves
    the directory EMPTY, and ``git status`` reports the tree CLEAN — so a
    builder would see failing tests with no signal pointing at the cause. The
    leading ``-`` in this command's output is the machine-readable
    "uninitialized" marker and is the only reliable detection.

    ``state_prefix`` is ``"-"`` (uninitialized), ``"+"`` (checked out at a
    different SHA than the index), ``"U"`` (merge conflicts) or ``""`` (in
    sync). A repository with no submodules yields an empty list.
    """
    out = _run(repo, "submodule", "status")
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        prefix = line[0] if line[0] in "-+U" else ""
        rest = line[1:] if prefix else line
        parts = rest.split()
        if len(parts) >= 2:
            rows.append((prefix, parts[1]))
    return rows


def uninitialized_submodules(repo: Path) -> list[str]:
    """Submodule paths reporting the ``-`` (uninitialized) marker (spike §7).

    Empty when the repository has no submodules, and empty when a
    ``submodule`` invocation is not meaningful here — a repository without the
    command's preconditions must not be misreported as having uninitialized
    submodules, which would park every run on a repo that has none.
    """
    try:
        return [path for prefix, path in submodule_status(repo) if prefix == "-"]
    except GitError:
        return []


def branch_exists(repo: Path, branch: str) -> bool:
    try:
        _run(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
        return True
    except GitError:
        return False


def checkout_or_create_branch(repo: Path, branch: str, base: str) -> None:
    """Check out ``branch``, creating it off ``base`` if it does not exist (FR-9.1)."""
    if branch_exists(repo, branch):
        _run(repo, "checkout", branch)
    else:
        _run(repo, "checkout", "-b", branch, base)


def checkout_branch(repo: Path, branch: str) -> None:
    """Check out an existing branch (no creation)."""
    _run(repo, "checkout", branch)


def recreate_branch(repo: Path, branch: str, start_point: str) -> None:
    """Reset ``branch`` to ``start_point`` and check it out (``checkout -B``).

    Used by the run-branch lifecycle guard to discard a *spent* run branch (one
    already merged into its base) and start it fresh. ``-B`` handles both the
    on-branch and off-branch cases. The caller MUST have verified the branch is
    fully merged first — ``-B`` moves the ref unconditionally, so calling it on
    unmerged work would orphan those commits.
    """
    _run(repo, "checkout", "-B", branch, start_point)


def create_branch(repo: Path, branch: str, start_point: str, *, force: bool = False) -> None:
    """Create (or with ``force``, reset) ``branch`` at ``start_point``.

    The no-checkout sibling of :func:`checkout_or_create_branch`, added for the
    dedicated-worktree start path (P7c): the run worktree does not exist yet
    when the branch is minted, so there is no tree to check it out into — and
    checking it out in the OPERATOR's tree is precisely what acceptance A1
    forbids. ``git branch`` touches only the ref store, so it is repo-scoped.

    ``force`` is the ``-f`` form used to recycle a *spent* run branch (one
    already merged into its base). Git refuses it outright if the branch is
    checked out in any worktree (spike E2-E), which is the correct fail-closed
    answer and is why the caller does not need a lock of its own.
    """
    args = ["branch"] + (["-f"] if force else []) + [branch, start_point]
    _run(repo, *args)


def delete_branch(repo: Path, branch: str) -> None:
    """Force-delete a branch ref (``branch -D``).

    Callers gate this with their own merged-ness check (``is_ancestor``) so the
    engine never relies on git's narrower ``-d`` notion of "merged" (merged into
    HEAD/upstream, not into the run's recorded base).
    """
    _run(repo, "branch", "-D", branch)


def merge_branch(repo: Path, branch: str, *, message: str) -> str:
    """Merge ``branch`` into the current branch with a merge commit (``--no-ff``).

    A human-territory action (``gauntlet finish``): it runs with the repo's own
    configured git identity, not an engine identity. Raises :class:`GitError` on
    conflict; the caller aborts the half-merge and fails closed. Returns the new
    HEAD SHA.
    """
    _run(repo, "merge", "--no-ff", "-m", message, branch)
    return head_sha(repo)


def merge_abort(repo: Path) -> None:
    """Abort an in-progress merge, restoring the pre-merge state."""
    _run(repo, "merge", "--abort")


@dataclass(frozen=True)
class Identity:
    name: str
    email: str


# Author/committer for engine-owned bookkeeping commits (FR-2.2): response
# checkpoints, recovery rewinds, and the cycle's fix-rerun rewind. Defined here
# (not in the orchestrator) so the cycle can label its bookkeeping commits with
# the same identity without importing the orchestrator.
ENGINE_IDENTITY = Identity(name="Gauntlet Engine", email="engine@gauntlet.local")


# --- commit-message byte hygiene (#105) ---------------------------------------
#
# The commit message is the one model-authored value that reaches git (on
# stdin, via ``-F -``), and git hard-rejects a NUL byte in a log message
# (exit 128: "a NUL byte in commit log message not allowed"). A drafted
# message containing a literal 0x00 failed the commit step `adapter_error`
# and stranded the finished fix round uncommitted (#105). Sanitize at this
# single choke point, before the message is piped to git: NUL becomes the
# readable escape ``\x00`` so the stored message still reads sensibly,
# CR/CRLF normalize to LF, and the remaining C0 controls (plus DEL) become
# readable escapes too — git tolerates those, but they scramble logs.
# TAB and LF pass through untouched.
_CTRL_ESCAPES = {
    i: f"\\x{i:02x}" for i in range(0x20) if i not in (0x09, 0x0A)
}
_CTRL_ESCAPES[0x7F] = "\\x7f"


def _sanitize_commit_message(message: str) -> str:
    """Replace control bytes a drafted message must never carry into git.

    CRLF/CR normalize to LF first; every other C0 control (and DEL) except
    TAB/LF is replaced with its readable ``\\xNN`` escape. Printable text —
    including non-ASCII — is untouched.
    """
    message = message.replace("\r\n", "\n").replace("\r", "\n")
    return message.translate(_CTRL_ESCAPES)


def _ascii_printable_message(message: str) -> str:
    """Aggressive last-resort redraft: printable ASCII + LF only (#105).

    Every other character becomes ``?``. Only used when git rejects a message
    that already passed :func:`_sanitize_commit_message` — landing the
    finished work with a flattened message beats failing the step with the
    work stranded uncommitted.
    """
    return "".join(
        ch if ch == "\n" or 0x20 <= ord(ch) <= 0x7E else "?" for ch in message
    )


def _commit_stdin(repo: Path, args: list[str], message: str) -> None:
    """Pipe a sanitized commit message to a fixed ``git commit -F -`` argv.

    Belt and braces (#105): if git still rejects the sanitized message
    (a byte sequence the sanitizer did not anticipate), retry ONCE with the
    aggressively-flattened redraft instead of failing the step with the
    completed work stranded. The retry fires only when the failure looks like
    a log-message rejection AND the redraft actually differs; every other
    failure (nothing staged, hooks, locks) re-raises unchanged.
    """
    msg = _sanitize_commit_message(message)
    try:
        _run(repo, *args, stdin=msg)
    except GitError as err:
        fallback = _ascii_printable_message(msg)
        rejected_message = err.returncode == 128 and (
            "log message" in err.stderr or "NUL" in err.stderr
        )
        if fallback == msg or not rejected_message:
            raise
        _run(repo, *args, stdin=fallback)


def commit_all(
    repo: Path,
    message: str,
    *,
    identity: Identity,
    allow_empty: bool = False,
    exclude: list[str] | None = None,
) -> str:
    """Stage everything and commit with an explicit author/committer identity.

    The message is passed on stdin (`-F -`) so no agent-authored text ever
    lands on the argv. ``exclude`` (the run root) is kept out of the commit so
    phase commits carry the work, not engine bookkeeping. Returns the SHA.
    """
    _run(repo, "add", "-A", *_exclude_pathspec(exclude))
    args = [
        "-c",
        f"user.name={identity.name}",
        "-c",
        f"user.email={identity.email}",
        "commit",
        "-F",
        "-",
    ]
    if allow_empty:
        args.append("--allow-empty")
    _commit_stdin(repo, args, message)
    return head_sha(repo)


def commit_paths(
    repo: Path, message: str, paths: list[str], *, identity: Identity
) -> str:
    """Stage exactly ``paths`` and commit with an explicit identity. Returns SHA.

    Unlike :func:`commit_all`, this never runs ``git add -A`` — the governed
    proposal apply (FR-6.4) commits precisely the allowlisted asset(s) it patched
    plus the CHANGELOG, so run bookkeeping can never be swept into the commit.
    The message is passed on stdin (``-F -``); no agent-authored text hits argv.

    The commit is **pathspec-limited** (``commit … -- <paths>``): it commits ONLY
    these paths even if other files were already staged in the index when this
    runs. Without the pathspec a bare ``git commit`` snapshots the whole index, so
    a pre-staged unrelated file would be swept in — silently breaking the
    isolation both callers rely on (the producer commit's clean-handoff guarantee,
    and the proposal apply's allowlist). Any such pre-staged file is left staged
    and uncommitted, exactly as it was.
    """
    _run(repo, "add", "--", *paths)
    args = [
        "-c", f"user.name={identity.name}",
        "-c", f"user.email={identity.email}",
        "commit", "-F", "-", "--", *paths,
    ]
    _commit_stdin(repo, args, message)
    return head_sha(repo)


def commit_run_bookkeeping(
    repo: Path, message: str, paths: list[str], *, identity: Identity
) -> str | None:
    """Force-stage gitignored run-bookkeeping paths and commit them alone.

    The live run dir is gitignored (its manifest/RUN.md must never dirty the
    worktree or pollute phase commits), so a response checkpoint (FR-2.2) has to
    ``add -f`` past that ignore rule. It then commits ONLY the named paths
    (path-limited ``commit``), so a dirty implementation tree can never smuggle
    agent edits into a bookkeeping commit. **Idempotent:** if the named paths
    carry no change vs HEAD, returns ``None`` and creates no empty commit — so
    crash recovery can call it to flush a not-yet-landed state whether or not the
    commit already happened. The message is passed on stdin (``-F -``); no
    agent-authored text reaches argv. Returns the new SHA, or ``None``.
    """
    _run(repo, "add", "-f", "--", *paths)
    # Scope the change check to OUR paths so unrelated staged/worktree state
    # never makes this look "dirty" (or get swept into the commit below).
    if not _run(repo, "diff", "--cached", "--name-only", "--", *paths).strip():
        return None
    args = [
        "-c", f"user.name={identity.name}",
        "-c", f"user.email={identity.email}",
        "commit", "-F", "-", "--", *paths,
    ]
    _commit_stdin(repo, args, message)
    return head_sha(repo)


def is_tracked(repo: Path, relpath: str) -> bool:
    """True iff git tracks ``relpath`` (it is in the index / a HEAD tree).

    Unlike :func:`path_is_untracked`, this is reliable for a **gitignored** path:
    a gitignored-but-untracked file yields no ``??`` porcelain line (ignored
    entries are hidden), so a "not untracked" test would misread it as tracked and
    then ``git add`` it without ``-f`` — the #33 pathspec clash. ``ls-files``
    reports only tracked paths, so an ignored-untracked run-dir file correctly
    returns False here.
    """
    return bool(_run(repo, "ls-files", "--", relpath).strip())


def commit_tracked_bookkeeping(
    repo: Path, message: str, paths: list[str], *, identity: Identity
) -> str | None:
    """Commit already-tracked, dirty run bookkeeping so the RAW worktree is clean
    when control passes to a reviewer (review F-001).

    A run that ever hit an FR-2.2 response checkpoint has its manifest.json/RUN.md
    force-committed (:func:`commit_run_bookkeeping`'s ``add -f``) and therefore
    TRACKED from then on — after which the run-dir ``*`` self-ignore no longer
    hides the engine's live updates, so every later review handoff shows them as
    uncommitted dirt in a bare ``git status`` (the reviewer's view), even though
    the engine's own ``--exclude``-scoped clean check stays green. Re-committing
    that tracked bookkeeping makes the two views agree so the handoff is genuinely
    clean (CLAUDE.md §1).

    NEVER force-adds (contrast :func:`commit_run_bookkeeping`): a run whose
    bookkeeping is still untracked is already invisible to ``git status`` via the
    self-ignore, so tracking it here would only defeat that, add commit noise, and
    collide with the ignore rule (#33). Commits ONLY the subset of ``paths`` git
    already tracks, and only when they are dirty. **Idempotent:** returns ``None``
    (no commit) when nothing tracked is dirty. The message is passed on stdin
    (``-F -``); no agent-authored text reaches argv. Returns the new SHA, or
    ``None``.
    """
    tracked = [p for p in paths if is_tracked(repo, p)]
    if not tracked:
        return None
    _run(repo, "add", "--", *tracked)  # tracked → no `-f`, no #33 clash
    # Scope the change check to OUR paths so unrelated staged/worktree state never
    # makes this look "dirty" (or get swept into the commit below).
    if not _run(repo, "diff", "--cached", "--name-only", "--", *tracked).strip():
        return None
    args = [
        "-c", f"user.name={identity.name}",
        "-c", f"user.email={identity.email}",
        "commit", "-F", "-", "--", *tracked,
    ]
    _commit_stdin(repo, args, message)
    return head_sha(repo)


def commit_subject(repo: Path, sha: str) -> str:
    return _run(repo, "log", "-1", "--format=%s", sha).strip()


def commit_parent(repo: Path, sha: str) -> str:
    """First-parent commit of ``sha`` (``<sha>^``); the squash base of a phase.

    Used to anchor a checkpoint squash (FR-11.1): the parent of the OLDEST
    ``P<N> wip:`` commit is where the collapsed ``P<N>:`` phase commit lands.
    """
    return _run(repo, "rev-parse", "--verify", f"{sha}^").strip()


def wip_checkpoints(
    repo: Path,
    *,
    base: str | None = None,
    tip: str = "HEAD",
    limit: int = 1000,
    phase: str | None = None,
) -> list[tuple[str, str]]:
    """Intra-phase checkpoint commits at ``tip`` as ``(sha, subject)``, newest first.

    A checkpoint is a commit whose subject matches ``P<N> wip:`` (§6 convention).
    When ``phase`` (e.g. ``"P9"``) is given, discovery is SCOPED to that phase's
    prefix (``<phase> wip:``): a wrong-phase checkpoint is never counted as this
    phase's (review F-001). Unscoped (``phase is None``) it matches any
    ``P<N> wip:`` — the legacy behaviour. Two modes, both fail-closed on subject
    shape (nothing else is ever treated as a checkpoint):

    * ``base`` given (recovery, FR-11.2): every (scoped) checkpoint in
      ``base..tip`` — i.e. every matching ``wip:`` commit that is a descendant of
      ``base`` reachable from ``tip`` — newest first. ``result[0]`` is the newest
      checkpoint the recovery rewind targets. Non-matching commits in the range
      are skipped, not a stop.
    * ``base`` absent (commit step, FR-11.1): the TRAILING run of (scoped)
      checkpoint commits at ``tip`` — walk back to the first real gap (the prior
      phase's ``P<N>:`` commit / the branch base). Engine bookkeeping commits
      (``gauntlet:`` subjects) are walked THROUGH, not treated as a gap, so a
      checkpoint preserved beneath a recovery rewind is still found (review
      F-002). When ``phase`` is scoped, a ``P<N> wip:`` commit for a DIFFERENT
      phase in the trailing run raises :class:`WrongPhaseCheckpointError` (fail
      closed, review F-001) rather than being squashed into the wrong phase. This
      is the set the phase-end commit collapses (squash) or lists (keep marker).
    """
    matcher = _wip_subject_re(phase)
    if base is not None:
        out = _run(repo, "log", "--format=%H%x00%s", f"{base}..{tip}")
        stop_at_gap = False
    else:
        out = _run(repo, "log", f"-{limit}", "--format=%H%x00%s", tip)
        stop_at_gap = True
    result: list[tuple[str, str]] = []
    for line in out.splitlines():
        sha, _, subject = line.partition("\x00")
        if matcher.match(subject):
            result.append((sha, subject))
        elif stop_at_gap:
            if phase is not None and _WIP_SUBJECT_RE.match(subject):
                # A `P<N> wip:` for another phase inside this phase's trailing run
                # (e.g. a mistyped `P8 wip:` during P9). Fail closed rather than
                # squash it into — or truncate the run at — the wrong phase.
                raise WrongPhaseCheckpointError(phase, subject)
            if _ENGINE_SUBJECT_RE.match(subject):
                # Engine bookkeeping commit (a response/rewind checkpoint) can sit
                # between this phase's wip commits after a checkpoint-preserving
                # recovery. It carries no implementation, so walk through it.
                continue
            break
    return result


def commit_message(repo: Path, sha: str) -> str:
    return _run(repo, "log", "-1", "--format=%B", sha).rstrip("\n")


def range_diff(repo: Path, base: str, head: str) -> str:
    """Diff for the confirm pass / review handoff (`base..head`)."""
    return _run(repo, "diff", f"{base}..{head}")


def any_tracked_at(repo: Path, sha: str, paths: list[str]) -> bool:
    """True iff any of ``paths`` exists in ``sha``'s tree.

    The guard for the bookkeeping-preserving rewind: a plain ``reset --hard``
    deletes a file from disk only when it is *tracked* at HEAD but absent from
    the target tree — untracked-on-both-sides files are never touched, so the
    plain reset stays the right (and cheaper) verb for them.
    """
    if not paths:
        return False
    return bool(_run(repo, "ls-tree", "--name-only", sha, "--", *paths).strip())


def file_at_commit(repo: Path, sha: str, relpath: str) -> str | None:
    """The contents of ``relpath`` as of ``sha``, or ``None`` if absent there.

    Reads a committed artifact out of history (``git show <sha>:<path>``) without
    touching the worktree — used to recover a prior phase's committed
    ``acceptance-map.json`` for deferral injection (FR-3.3), since the live file
    on disk is the *current* phase's map (each phase overwrites it). A path that
    does not exist at that commit is not an error: ``git show`` exits non-zero and
    this returns ``None`` so the caller simply has no map to read there.
    """
    try:
        return _run(repo, "show", f"{sha}:{relpath}")
    except GitError:
        return None


def file_mode_at_commit(repo: Path, sha: str, relpath: str) -> str | None:
    """The git mode of ``relpath`` at ``sha`` (e.g. ``100644``), or ``None``.

    The executable bit and the regular-file/symlink distinction are their own
    state plane (plan §7), so a caller proving "this file is a redundant copy of
    what is committed" has not proved it from the bytes alone: a `100755` local
    file and a `100644` blob compare byte-equal and are different objects.
    """
    try:
        out = _run(repo, "ls-tree", sha, "--", relpath).strip()
    except GitError:
        return None
    return out.split()[0] if out else None


def file_bytes_at_commit(repo: Path, sha: str, relpath: str) -> bytes | None:
    """:func:`file_at_commit`, but the RAW BYTES, or ``None`` if absent there.

    The text variant decodes, which is fine for reading an artifact but not for
    *proving two files are the same object*. `finish` uses this to establish
    that an operator's untracked file is byte-identical to the copy the merge is
    about to bring in before it will touch that file at all (P7d) — and a proof
    that survives a decode round-trip is not a proof of the bytes.
    """
    try:
        return _run_bytes(repo, "show", f"{sha}:{relpath}")
    except GitError:
        return None


def range_diff_path(repo: Path, base: str, head: str, relpath: str) -> str:
    """`base..head` diff scoped to a single path (harness-efficiency FR-1.2).

    Used by the artifact-mode re-review: rounds 2+ send the reviewer the diff of
    the artifact since the last reviewed version, not the full document."""
    return _run(repo, "diff", f"{base}..{head}", "--", relpath)


def log_range(repo: Path, base: str, head: str) -> str:
    """One line per commit in ``base..head``: sha, author, subject.

    The confirm pass embeds this so reviewer-attributed mutation commits
    (`PN.rX`) stay distinguishable from fixer commits inside the combined
    range diff (FR-9.6 / P4.r1 F-005)."""
    return _run(
        repo, "log", "--format=%h %an <%ae> — %s", f"{base}..{head}"
    ).strip()


def diff_head(repo: Path, *, exclude: list[str] | None = None) -> str:
    """Working-tree diff vs HEAD (the change a commit step is about to record)."""
    return _run(repo, "diff", "HEAD", *_exclude_pathspec(exclude))


def diff_worktree_vs(repo: Path, base: str, *, exclude: list[str] | None = None) -> str:
    """Working-tree diff vs an arbitrary ``base`` commit (harness-efficiency FR-11.1).

    When the phase already landed ``P<N> wip:`` checkpoint commits, the change a
    phase commit records is not ``diff HEAD`` (the tip may be clean) but the
    cumulative diff since the phase base. The commit-message drafter is handed
    this range so its body reflects the whole phase, not an empty residual tree.
    """
    return _run(repo, "diff", base, *_exclude_pathspec(exclude))


def merge_base(repo: Path, a: str, b: str) -> str | None:
    """The best common ancestor of ``a`` and ``b``, or ``None`` if none exists.

    The review command resolves ``merge-base(resolved_base, HEAD)`` and injects
    that concrete SHA as the cycle's two-dot ``review_base`` so the existing
    ``range_diff`` yields the three-dot ``base...HEAD`` scope FR-5.2 mandates.
    ``None`` means unrelated histories (no shared commit) — a fail-closed guard
    condition (FR-5.3), not an error to raise.
    """
    try:
        out = _run(repo, "merge-base", a, b).strip()
    except GitError:
        return None
    return out or None


def diff_range_empty(repo: Path, base: str, head: str) -> bool:
    """True iff ``git diff <base> <head>`` reports no changes (FR-5.3 guard).

    ``git diff --quiet`` exits 0 when the two tree states are identical and 1
    when they differ, so this is the cheap two-dot emptiness check. With
    ``base`` set to ``merge-base(resolved_base, head)`` it answers "does HEAD
    introduce anything since it diverged from base?" — the three-dot semantics.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "diff", "--quiet", base, head],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def remote_url(repo: Path, remote: str = "origin") -> str | None:
    """The fetch URL of ``remote`` (``git remote get-url``), or ``None``.

    Used to derive the stable, checkout-independent ``<repo-id>`` for a review
    run's out-of-repo state dir (§6 "Review state path"); ``None`` when the
    remote is unset, so the caller falls back to the repo toplevel path.
    """
    try:
        out = _run(repo, "remote", "get-url", remote).strip()
    except GitError:
        return None
    return out or None


def show_toplevel(repo: Path) -> str:
    """Absolute path of the repo's worktree root (``git rev-parse --show-toplevel``)."""
    return _run(repo, "rev-parse", "--show-toplevel").strip()


def remote_default_branch(repo: Path, remote: str = "origin") -> str | None:
    """The remote's default branch as ``<remote>/<name>`` (from ``<remote>/HEAD``).

    Resolves the ``refs/remotes/<remote>/HEAD`` symbolic ref set by ``clone`` or
    ``git remote set-head``; returns e.g. ``origin/main``, or ``None`` when the
    symref is absent. This is the last fallback in the FR-5.1 base-resolution
    order (after ``--base`` and a concrete ``config.base_branch``).
    """
    try:
        out = _run(repo, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD").strip()
    except GitError:
        return None
    prefix = f"refs/remotes/{remote}/"
    if out.startswith(prefix):
        return f"{remote}/{out[len(prefix):]}"
    return None


def path_is_ignored(repo: Path, relpath: str) -> bool:
    """True iff ``relpath`` is covered by a gitignore rule (``git check-ignore``).

    Used to enforce that an in-repo ``review.state_dir`` override is gitignored
    (FR-8.3), so the only legal in-repo review state is state invisible to
    ``git status``. ``check-ignore -q`` exits 0 when ignored, 1 when not; a
    non-existent path is fine (it matches patterns, not files).
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", "--", relpath],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def path_is_untracked(repo: Path, relpath: str) -> bool:
    """True iff ``relpath`` is an untracked file (porcelain ``??``).

    Used to decide whether an in-repo ``--intent`` file is user-owned untracked
    dirt — safe to exempt from the clean-tree entry contract (FR-2.4) — versus a
    tracked path whose uncommitted changes must NOT be masked (FR-9.2). Scoped to
    the single path with untracked-files pinned to ``all`` (never left to adopter
    git config): a tracked path, whether clean or modified, yields no ``??`` line
    and returns False, so it is never silently exempted from the clean checks.
    """
    out = _run(
        repo, "status", "--porcelain", "--untracked-files=all", "--", relpath
    )
    return any(line.startswith("?? ") for line in out.splitlines())


def tag_exists(repo: Path, name: str) -> bool:
    """True iff ``refs/tags/<name>`` exists (used for the ambiguous-ref guard)."""
    try:
        _run(repo, "rev-parse", "--verify", f"refs/tags/{name}")
        return True
    except GitError:
        return False


def ref_is_valid_commit(repo: Path, ref: str) -> bool:
    """True iff ``ref`` resolves to a commit object (any ref namespace).

    A read-only validity probe for a user-supplied ``--base`` (FR-5.1): it must
    name a real commit-ish before merge-base/diff run against it.
    """
    try:
        _run(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        return True
    except GitError:
        return False


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    try:
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            text=True,
            check=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def create_ref(repo: Path, ref: str, sha: str) -> None:
    """Create/update an arbitrary ref (used for rollback backup refs, F-010)."""
    _run(repo, "update-ref", ref, sha)


def create_ref_exclusive(repo: Path, ref: str, sha: str) -> None:
    """Atomically create ``ref`` at ``sha`` ONLY if it does not already exist.

    ``git update-ref --stdin`` with the ``create`` verb makes git's own ref
    store enforce creation semantics (the transaction fails when the ref
    exists), closing the check-then-write race a bare ``update-ref <ref>
    <sha>`` leaves open: two writers could both observe an absent ref and the
    later one would silently replace the earlier — for a recovery snapshot,
    displacing the only anchor of its unique objects (P2 review F-003).
    Raises :class:`GitError` when the ref already exists.
    """
    _run(repo, "update-ref", "--stdin", stdin=f"create {ref} {sha}\n")


def delete_ref(repo: Path, ref: str) -> None:
    """Delete an arbitrary ref, tolerating an already-absent one.

    The PR-mode checkout contract (FR-4.5) fetches the PR head into a scratch
    ref (``refs/gauntlet/pr/<N>``) purely to compute fast-forwardability without
    touching the user's local branch, then deletes it. The delete runs in a
    ``finally`` — including on the diverged/failure fail-closed paths — so it
    must not itself raise when the ref was never created; ``update-ref -d``
    against a missing ref is treated as a no-op.
    """
    try:
        _run(repo, "update-ref", "-d", ref)
    except GitError:
        # The scratch ref may never have been created (e.g. the fetch failed
        # before writing it); cleaning up nothing is not an error.
        pass


def reset_hard(repo: Path, sha: str) -> None:
    _run(repo, "reset", "--hard", sha)


def reset_soft(repo: Path, sha: str) -> None:
    """Move HEAD to ``sha`` keeping the index and working tree (``reset --soft``).

    Used by the checkpoint squash (FR-11.1): resetting HEAD back to the squash
    base leaves every ``P<N> wip:`` change staged, so a single follow-up commit
    collapses them into one non-empty ``P<N>:`` phase commit. The working tree is
    untouched — no implementation byte is lost, and residual uncommitted edits
    stay staged for the same commit.
    """
    _run(repo, "reset", "--soft", sha)


def unstage(repo: Path, paths: list[str]) -> None:
    """Reset the index entries under ``paths`` to HEAD (``git reset -- <paths>``).

    Used by the checkpoint squash: the ``reset --soft`` to the squash base leaves
    every commit in ``base..old-HEAD`` staged — INCLUDING any engine bookkeeping
    commit swept into that range by a checkpoint-preserving recovery (FR-11.2),
    whose force-committed ``manifest.json``/``RUN.md`` would otherwise land in the
    phase commit. Unstaging the run-bookkeeping paths here keeps the collapsed
    ``P<N>:`` commit free of engine state. The working tree is untouched — the
    live run keeps its on-disk bookkeeping files. A no-op when ``paths`` is empty
    or matches nothing in the index.
    """
    if not paths:
        return
    _run(repo, "reset", "-q", "HEAD", "--", *paths)


def rewind_impl_preserving_bookkeeping(
    repo: Path,
    target_sha: str,
    bookkeeping: list[str],
    message: str,
    *,
    identity: Identity,
) -> str:
    """Rewind tracked implementation files to ``target_sha`` in a single
    ``reset --hard`` whose target commit STILL carries the run ``bookkeeping``.

    ``target_sha`` is the phase base (F-003 re-run) or, when the phase landed
    intra-phase checkpoint commits, the latest ``P<N> wip:`` checkpoint (FR-11.2):
    rewinding to the checkpoint rather than the base preserves the completed
    milestones instead of discarding them.

    A plain ``reset --hard target_sha`` is unsafe when an engine checkpoint sits
    between ``target_sha`` and HEAD (a pending-response checkpoint, FR-2.2/FR-7.1):
    the force-committed ``manifest.json`` is tracked at HEAD but absent from
    ``target_sha``'s tree, so the reset *deletes it from disk* and moves the branch
    off the checkpoint — a kill in the gap before it is re-persisted permanently
    loses the human response (review F-001).

    Instead, build a commit on top of ``target_sha`` whose tree is ``target_sha``'s
    tree with ``bookkeeping`` overlaid from the current working tree, then point
    HEAD at it with one reset. The commit carries ONLY the bookkeeping diff vs
    ``target_sha`` (the implementation is unchanged), so passing the canonical
    checkpoint ``message`` makes it the single reachable replacement for the
    pending-response checkpoint — collapsing any redundant intermediate
    checkpoints rather than orphaning the state. Because the reset target already
    contains the bookkeeping, ``manifest.json`` is never momentarily removed and
    the response is, at every instant, present both on disk and in a reachable
    commit. Returns the new HEAD sha.

    Only the index is touched before the final reset (``read-tree``/``write-tree``
    leave HEAD, the branch ref, and the working tree alone), so a crash anywhere
    ahead of the reset leaves the pre-existing on-disk manifest and checkpoint
    intact for recovery to redo the rewind.
    """
    # Stage target_sha's tree, then overlay the live bookkeeping on top of it.
    _run(repo, "read-tree", target_sha)
    _run(repo, "add", "-f", "--", *bookkeeping)
    tree = _run(repo, "write-tree").strip()
    args = [
        "-c", f"user.name={identity.name}",
        "-c", f"user.email={identity.email}",
        # commit-tree reads the log message from stdin when no -m/-F is given,
        # so no model-derived text ever reaches argv (it never does here — the
        # message is a fixed engine string — but keep the invariant uniform).
        "commit-tree", tree, "-p", target_sha,
    ]
    new = _run(repo, *args, stdin=message).strip()
    reset_hard(repo, new)
    return new


def apply_patch_check(repo: Path, patch: str) -> bool:
    """True iff ``patch`` applies cleanly to the worktree (no side effects).

    ``git apply --check`` validates the unified diff against the current tree
    without touching a single byte — used to tell a human, before they approve a
    retro proposal (FR-6.4), whether the diff still applies."""
    return apply_patch_error(repo, patch) is None


def apply_patch_error(repo: Path, patch: str) -> str | None:
    """``None`` iff ``patch`` applies cleanly; else git's own error text.

    The concrete diagnostic ("patch does not apply", "corrupt patch at line N",
    "while searching for: …") is what a synthesiser regeneration re-ask (#55)
    needs to correct a stale-context diff — a bare boolean gave it nothing to
    act on. Side-effect-free (``--check``)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", "-"],
        input=patch, capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return None
    return (proc.stderr or proc.stdout).strip() or f"git apply --check exited {proc.returncode}"


def apply_patch(repo: Path, patch: str) -> None:
    """Apply a unified diff to the worktree (governed proposal apply, FR-6.4).

    The patch text is passed on stdin — never on argv — and is the only
    model-derived bytes that reach git here; path-containment is validated by
    the caller (review F-001) before this runs, and ``--check`` gates it first.
    """
    _run(repo, "apply", "-", stdin=patch)


def clean_untracked(repo: Path, *, exclude: list[str] | None = None) -> None:
    """Remove untracked files/dirs, preserving *ignored* paths (no ``-x``).

    Used after a ``reset_to_base`` rewind so a killed step's untracked partial
    files are discarded too (``reset --hard`` alone leaves them). ``exclude``
    paths are spared — the engine passes its own run root so a reset never wipes
    the run pointer / manifests / authored prd.md living under it.
    """
    args = ["clean", "-fd"]
    for pattern in exclude or []:
        args += ["-e", pattern]
    _run(repo, *args)

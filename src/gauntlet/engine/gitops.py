"""Thin git wrapper for the engine (FR-9, FR-8 transaction boundary).

The engine executes only human-committed configuration and git operations on
its own behalf — it never substitutes agent-authored text into a command line
(plan §0 trust model / review F-001). These helpers shell out to ``git`` with
explicit, fixed argv; the only model-derived value that reaches git is the
commit *message*, which is passed via a file/`-F`-style stdin path and is
treated as data (format-validated before it is used — see ``commit_format``).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git invocation failed. Carries argv + stderr for the manifest/log."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        super().__init__(
            f"git {' '.join(argv)} failed (exit {returncode}): {stderr.strip()}"
        )
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr


def _run(repo: Path, *args: str, stdin: str | None = None) -> str:
    argv = ["git", "-C", str(repo), *args]
    proc = subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc.stdout


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
    repo: Path, *, exclude: list[str] | None = None, untracked_all: bool = False
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
    """
    mode = "all" if untracked_all else "normal"
    return _run(
        repo, "status", "--porcelain", f"--untracked-files={mode}",
        *_exclude_pathspec(exclude),
    ).strip()


def is_clean(repo: Path, *, exclude: list[str] | None = None) -> bool:
    return status_porcelain(repo, exclude=exclude) == ""


def is_dirty_vs(repo: Path, base_sha: str, *, exclude: list[str] | None = None) -> bool:
    """True if the worktree (tracked + staged + untracked) differs from ``base_sha``.

    The engine's transaction boundary (review F-003) records a step's base SHA
    before any worktree-touching step. On resume it compares against that base:
    a difference means the killed step left partial edits.
    """
    if status_porcelain(repo, exclude=exclude) != "":
        return True
    # No working-tree changes; confirm HEAD still points at the recorded base.
    return head_sha(repo) != base_sha


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
    _run(repo, *args, stdin=message)
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
    _run(repo, *args, stdin=message)
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
    _run(repo, *args, stdin=message)
    return head_sha(repo)


def commit_subject(repo: Path, sha: str) -> str:
    return _run(repo, "log", "-1", "--format=%s", sha).strip()


def commit_message(repo: Path, sha: str) -> str:
    return _run(repo, "log", "-1", "--format=%B", sha).rstrip("\n")


def range_diff(repo: Path, base: str, head: str) -> str:
    """Diff for the confirm pass / review handoff (`base..head`)."""
    return _run(repo, "diff", f"{base}..{head}")


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


def rewind_impl_preserving_bookkeeping(
    repo: Path,
    base_sha: str,
    bookkeeping: list[str],
    message: str,
    *,
    identity: Identity,
) -> str:
    """Rewind tracked implementation files to ``base_sha`` in a single
    ``reset --hard`` whose target commit STILL carries the run ``bookkeeping``.

    A plain ``reset --hard base_sha`` is unsafe when an engine checkpoint sits
    between ``base_sha`` and HEAD (a pending-response checkpoint, FR-2.2/FR-7.1):
    the force-committed ``manifest.json`` is tracked at HEAD but absent from
    ``base_sha``'s tree, so the reset *deletes it from disk* and moves the branch
    off the checkpoint — a kill in the gap before it is re-persisted permanently
    loses the human response (review F-001).

    Instead, build a commit on top of ``base_sha`` whose tree is ``base_sha``'s
    tree with ``bookkeeping`` overlaid from the current working tree, then point
    HEAD at it with one reset. The commit carries ONLY the bookkeeping diff vs
    ``base_sha`` (the implementation is unchanged), so passing the canonical
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
    # Stage base_sha's tree, then overlay the live bookkeeping on top of it.
    _run(repo, "read-tree", base_sha)
    _run(repo, "add", "-f", "--", *bookkeeping)
    tree = _run(repo, "write-tree").strip()
    args = [
        "-c", f"user.name={identity.name}",
        "-c", f"user.email={identity.email}",
        # commit-tree reads the log message from stdin when no -m/-F is given,
        # so no model-derived text ever reaches argv (it never does here — the
        # message is a fixed engine string — but keep the invariant uniform).
        "commit-tree", tree, "-p", base_sha,
    ]
    new = _run(repo, *args, stdin=message).strip()
    reset_hard(repo, new)
    return new


def apply_patch_check(repo: Path, patch: str) -> bool:
    """True iff ``patch`` applies cleanly to the worktree (no side effects).

    ``git apply --check`` validates the unified diff against the current tree
    without touching a single byte — used to tell a human, before they approve a
    retro proposal (FR-6.4), whether the diff still applies."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", "-"],
        input=patch, capture_output=True, text=True,
    )
    return proc.returncode == 0


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


def backup_dirty_worktree(
    repo: Path, ref: str, message: str, *, exclude: list[str] | None = None
) -> str:
    """Snapshot the full dirty worktree (tracked + untracked) to a backup ref.

    Captures partial work that ``reset --hard`` would otherwise destroy
    (review F-003 / F-010 safety). ``exclude`` (the run root) is left out so the
    snapshot — and the subsequent reset — never touch the run bookkeeping.
    Returns the backup commit SHA.
    """
    _run(repo, "add", "-A", *_exclude_pathspec(exclude))
    tree = _run(repo, "write-tree").strip()
    parent = head_sha(repo)
    backup = _run(repo, "commit-tree", tree, "-p", parent, "-m", message).strip()
    _run(repo, "update-ref", ref, backup)
    return backup

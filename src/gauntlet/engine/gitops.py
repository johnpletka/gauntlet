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


def status_porcelain(repo: Path, *, exclude: list[str] | None = None) -> str:
    """Porcelain status; empty string means a clean worktree."""
    return _run(repo, "status", "--porcelain", *_exclude_pathspec(exclude)).strip()


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


def commit_subject(repo: Path, sha: str) -> str:
    return _run(repo, "log", "-1", "--format=%s", sha).strip()


def commit_message(repo: Path, sha: str) -> str:
    return _run(repo, "log", "-1", "--format=%B", sha).rstrip("\n")


def range_diff(repo: Path, base: str, head: str) -> str:
    """Diff for the confirm pass / review handoff (`base..head`)."""
    return _run(repo, "diff", f"{base}..{head}")


def diff_head(repo: Path, *, exclude: list[str] | None = None) -> str:
    """Working-tree diff vs HEAD (the change a commit step is about to record)."""
    return _run(repo, "diff", "HEAD", *_exclude_pathspec(exclude))


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


def reset_hard(repo: Path, sha: str) -> None:
    _run(repo, "reset", "--hard", sha)


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

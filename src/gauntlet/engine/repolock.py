"""The repo-global git lock (P7b, spike §8.3 layer 2).

`<git-common-dir>/gauntlet/.repo.lock` — held **only** for short shared-git
critical sections, never for a whole drive. The distinction matters: the
per-run driving lock and the worktree-global tree guard both live under the
*run root*, which is per-checkout. Two linked worktrees of one repository have
two different run roots and therefore two different tree guards, so two runs
can drive concurrently — while sharing one object database, one ref store and,
critically, **one worktree administration dir**.

What that actually costs today (this is layer 2's live critical section in
P7b, not a placeholder for P7c):

* the verifier's disposable copy is a real `git worktree add --detach`, torn
  down with `worktree remove --force` **and `git worktree prune`**
  (`verify.discard_disposable_copy`). Spike E8-C proves the prune is
  repository-wide: run A's teardown removes run B's admin entry the moment
  B's tree is momentarily missing. Spike E9-D proves concurrent `worktree add`
  loses a race on `index.lock` and leaves debris behind.

So this lock serializes the disposable-copy lifecycle across every worktree of
the repository. P7c adds the run worktree's own add/remove/prune to the same
section; nothing about the lock changes then.

**What is deliberately NOT under it** (review F-004b, a deviation from the
§8.3 critical-section list, stated rather than left silent). §8.3 also names
branch create/delete and snapshot ref writes. Both are already serialized by
git itself, and putting them here would buy nothing while costing the exact
thing §8.3 forbids — "never for a whole drive, or concurrent runs serialize":

* snapshot ref writes go through `gitops.create_ref_exclusive`, which uses
  `git update-ref --stdin` with the `create` verb, so git's own ref store
  enforces create-only semantics transactionally (P2 review F-003 closed that
  race properly, and `git_snapshot` already handles the lost-race case).
  Snapshot *creation* also hashes and writes trees, so a repo-global lock
  around it would serialize concurrent runs for a genuinely long section.
* branch create / delete / force-update across worktrees hard-refuse under
  git's own one-branch-one-worktree rule (spike E2-B / E2-D / E2-E), which is
  a stronger guarantee than an advisory lockfile, not a weaker one.

If a future phase finds a read-modify-write over branch state that git does
not cover, it belongs here — but it must stay short-held.

**Bounded wait, then fail closed.** Sections are sub-second, so a contended
acquirer waits rather than erroring — a fail-immediately repo lock would turn
routine concurrency into spurious failures, which is the "serialize whole
runs" outcome §8.3 forbids. Past the bound it raises: a lock held that long is
not a normal section.

**Reentrant per process.** A section may nest (a teardown inside a teardown on
an error path); re-entering from the same process is free and does not
re-acquire. Reentrancy is keyed on the resolved lock path, so two different
repositories in one process still exclude each other correctly.

Reclaim semantics are :func:`locking.record_is_live` verbatim — the same
fail-closed rule as the drive lock, with the same deliberate asymmetry (an
alive-but-unverifiable holder blocks).
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from gauntlet.engine import gitops, locking

# The lock lives under the git COMMON dir, so it is identical from every linked
# worktree (spike E1/E8) and invisible to `git status` / `git clean -xdff` in
# every one of them — no gitignore rule can be forgotten, because the file is
# inside `.git/` by construction (§6.2's reasoning, applied to the lock).
REPO_LOCK_DIRNAME = "gauntlet"
REPO_LOCK_NAME = ".repo.lock"

# Sections are sub-second. The bound is generous enough to absorb a slow
# `worktree add` on a large repo under load, short enough that a genuinely
# wedged holder surfaces as an actionable error rather than a hang.
DEFAULT_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.05

# Reentrancy depth per (resolved lock path, thread). Keyed by thread as well as
# path because reentrancy is a property of ONE call stack: a second thread in
# this process is a genuine second acquirer and must contend for the file, not
# inherit another thread's hold. Each thread only ever touches its own key, so
# the plain dict needs no lock of its own.
_held: dict[tuple[str, int], int] = {}


class RepoLockError(RuntimeError):
    """The repo-global git lock could not be acquired (fail closed)."""


def repo_lock_path(work_root: Path) -> Path:
    """`<git-common-dir>/gauntlet/.repo.lock` for the repository ``work_root`` is in."""
    return gitops.git_common_dir(work_root) / REPO_LOCK_DIRNAME / REPO_LOCK_NAME


def _acquire(path: Path, reason: str, timeout_s: float, sleep) -> str:
    """Block up to ``timeout_s`` for the lock; return the nonce we now hold.

    The deadline is checked at the TOP of every iteration, including after a
    successful stale reclaim, so no path through the loop can spin past it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    record = locking.new_record(slug=reason, run_id=None)
    payload = record.to_json()
    deadline = time.monotonic() + timeout_s
    kind = locking.LOCK_ABSENT
    observed: locking.LockRecord | None = None
    while True:
        if locking.link_into_place(path, record.nonce, payload):
            return record.nonce
        # The SAME tri-state read the drive lock and `driver_info` use (F-002),
        # so "unreadable" means the same thing at every lock scope.
        kind, observed = locking.read_lock_state(path)
        if time.monotonic() >= deadline:
            break
        if kind == locking.LOCK_ABSENT:
            continue  # vanished under us — race for the link again
        if kind == locking.LOCK_PRESENT and not locking.record_is_live(observed):
            # Proven-dead holder: reclaim under the same guard the drive lock
            # uses — unlink ONLY the record we observed as stale, never a fresh
            # owner's (F-004). A lost race just re-loops.
            if locking.unlink_if_nonce(path, observed.nonce):
                continue
        # A present-but-unreadable lock (torn, hand-edited, or permission-denied)
        # is NOT reclaimable: a read failure against a live holder must never
        # steal it. It waits out the bound and then reports, below.
        sleep(_POLL_INTERVAL_S)
    if kind == locking.LOCK_ABSENT:
        raise RepoLockError(
            f"could not acquire the repo-global git lock {path} within "
            f"{timeout_s:.0f}s: it is being taken and released faster than this "
            "process can claim it (a churning holder) — fail closed "
            "(spike §8.3 layer 2)"
        )
    if observed is None:
        raise RepoLockError(
            f"repo-global git lock {path} is present but unreadable and did not "
            f"clear within {timeout_s:.0f}s; remove it by hand once you have "
            "confirmed no gauntlet process is running (spike §8.3 layer 2)"
        )
    raise RepoLockError(
        f"could not acquire the repo-global git lock {path} within "
        f"{timeout_s:.0f}s (held by {observed.slug!r}, pid {observed.pid}); "
        "refusing to mutate shared git state concurrently (spike §8.3 layer 2). "
        "Wait for the other run's worktree operation to finish, or abort it."
    )


@contextmanager
def repo_lock(
    work_root: Path,
    *,
    reason: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    sleep=time.sleep,
) -> Iterator[None]:
    """Hold the repo-global git lock for one short shared-git critical section.

    ``reason`` is recorded as the lock record's ``slug`` so a contended
    acquirer's error names what is holding it. Reentrant within a process.
    """
    path = repo_lock_path(work_root)
    key = (str(path), threading.get_ident())
    if _held.get(key):
        _held[key] += 1
        try:
            yield
        finally:
            _held[key] -= 1
        return
    nonce = _acquire(path, reason, timeout_s, sleep)
    _held[key] = 1
    try:
        yield
    finally:
        _held[key] -= 1
        if not _held[key]:
            _held.pop(key, None)
        locking.unlink_if_nonce(path, nonce)


@contextmanager
def repo_lock_best_effort(
    work_root: Path, *, reason: str, timeout_s: float = DEFAULT_TIMEOUT_S
) -> Iterator[bool]:
    """:func:`repo_lock`, but yields ``False`` instead of raising when unavailable.

    For the **best-effort teardown** paths only. `discard_disposable_copy` is
    documented never to raise: a verifier whose copy could not be torn down
    must not fail the step it already completed. Serializing that teardown is a
    hygiene win, not a safety gate — the safety gate is the drive lock — so an
    unobtainable repo lock degrades to "prune anyway" rather than wedging a
    finished sub-step. Creation, by contrast, uses the raising form.

    Only the ACQUISITION is best-effort: an exception raised by the body still
    propagates (it is never swallowed as a failed acquire).
    """
    guard = repo_lock(work_root, reason=reason, timeout_s=timeout_s)
    try:
        guard.__enter__()
    except (RepoLockError, gitops.GitError, OSError):
        yield False
        return
    try:
        yield True
    finally:
        guard.__exit__(None, None, None)


def _reset_for_tests() -> None:
    """Drop the per-process reentrancy table (test isolation only)."""
    _held.clear()


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "REPO_LOCK_DIRNAME",
    "REPO_LOCK_NAME",
    "RepoLockError",
    "repo_lock",
    "repo_lock_best_effort",
    "repo_lock_path",
]

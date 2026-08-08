"""Child process for the kill -9 / resume crash test (not collected by pytest).

Usage: ``python _crash_child.py <repo> <slug> [mode]``. Starts a real engine run
that the parent SIGKILLs at a precise point in the kill-timing matrix:

- ``mid_step`` (default): the builder agent writes a *partial* file, drops the
  ``.crash_ready`` sentinel, then blocks forever — so the parent kills it
  mid-step (after the write-ahead manifest record and the partial edit, before
  the step completes).
- ``between_step``: the builder agent writes the *final* file and completes
  normally, so the kill (which the parent times via a later blocking ``tests``
  shell step in the pipeline) lands *after* the agent step is recorded done but
  before the commit step completes — the between-step recovery path.
- ``boundary:<n>:<when>:<sig>`` (P4, plan §5.3 / issue #62): a completing run
  that self-signals ``<sig>`` (``term``/``kill``) exactly ``<when>``
  (``before``/``after``) the ``<n>``-th orchestrator manifest persist — a real
  process death AT a durable persist boundary, deterministic by construction
  (no sentinel/polling race). If the run completes before persist ``n`` fires,
  the child writes ``.crash_boundary_done`` and exits 0, telling the parent
  the boundary matrix is exhausted.

  ``when`` gained a third value in P6: ``mid`` kills the process exactly
  BETWEEN the journal event append and the projection replace inside the
  ``n``-th ``Manifest.write_atomic`` — the one new durable sub-boundary the
  P6 journal introduces (plan §4.6: the event is write-ahead; the projection
  lands second). ``mid`` counts write_atomic calls (a superset of the
  orchestrator persists: every projection write in the process), so its
  matrix covers every event-append/projection-write boundary in a real run.

- ``wt:<point>:<sig>`` (P7c): a real process death at one of the five
  worktree-lifecycle boundaries, each mapping to exactly one row of the spike
  §11 recovery table so the fault matrix and that table cannot drift —

  * ``before_add``    — §11 rows 1/8: creation crash / partial create. No admin
    entry, but the BRANCH survives (E6-C), so a retry must not trip over it;
  * ``after_add``     — §11 row 6: the admin entry exists with no anti-prune
    lock yet, the window in which another run's prune could take it;
  * ``after_lock``    — §11 row 5: locked but the export dir is not written, so
    the first bookkeeping commit has no path to stage;
  * ``before_remove`` — §11 row 3: teardown crash with the tree still present
    and the branch still held, which is what blocks a later ``branch -D``;
  * ``after_remove``  — §11 row 2: the tree is gone and ``WorktreeReleased`` was
    never appended, so the journal and git disagree.

- ``lock:<point>:<sig>`` (P7b): a real process death at one of the three
  boundaries the split drive lock introduces —

  * ``after_tree``   — after the worktree-global tree guard is taken, before
    the run dir exists at all (the ``start`` window §8.3's per-run lock cannot
    cover);
  * ``after_run``    — after the per-run lock is published, so BOTH files are
    on disk and stale;
  * ``before_release`` — with both held, at the point the verb would have
    released them.

  Each leaves a stale lock a later verb must reclaim rather than be wedged by
  (R1). The child self-signals synchronously, so the boundary is exact.

The parent then resumes and asserts the engine recovers without lost/duplicated
effects.
"""

from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

from gauntlet.adapters.base import AdapterCapabilities, AgentResult
from gauntlet.engine.run import RunManager


class SleepyAdapter:
    name = "sleepy"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        cwd = Path(cwd)
        (cwd / "feature.py").write_text("PARTIAL — written before the kill\n")
        (cwd / ".crash_ready").write_text("1")
        time.sleep(120)  # block until the parent SIGKILLs us mid-step
        return AgentResult(text="never reached", exit_code=0)


class CompletingAdapter:
    """``between_step``: write the FINAL file and complete the step at once.

    The kill is timed by a later blocking ``tests`` shell step in the pipeline,
    so this agent step is already recorded ``done`` when the SIGKILL lands —
    exercising the between-step recovery path rather than the mid-step one.
    """

    name = "completing"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        (Path(cwd) / "feature.py").write_text("RECOVERED — final content\n")
        return AgentResult(text="implemented", exit_code=0)


def _arm_persist_boundary_kill(repo: Path, spec: str) -> None:
    """Self-signal at the n-th orchestrator persist (``boundary:<n>:<when>:<sig>``).

    Wraps ``Orchestrator._persist`` — the single durable manifest-write point —
    so the process dies exactly before/after one specific durable boundary.
    ``signal.raise_signal`` delivers synchronously; SIGTERM keeps its default
    disposition (terminate), SIGKILL is uncatchable by construction.
    """
    from gauntlet.engine.orchestrator import Orchestrator

    _, idx, when, sig_name = spec.split(":")
    target = int(idx)
    sig = signal.SIGKILL if sig_name == "kill" else signal.SIGTERM
    count = {"n": 0}

    if when == "mid":
        # P6: die exactly between the journal event append and the projection
        # replace — `Manifest.write_atomic` appends the event first, then
        # calls the module-level `_replace_atomic`; killing at the entry of
        # the n-th replace leaves the journal exactly one state ahead of the
        # on-disk projection (the one new durable sub-boundary P6 adds).
        from gauntlet.engine import manifest as Mmod

        real_replace = Mmod._replace_atomic

        def wrapped_replace(path, payload):
            if path.name == "manifest.json":
                count["n"] += 1
                if count["n"] == target:
                    signal.raise_signal(sig)
            real_replace(path, payload)

        Mmod._replace_atomic = wrapped_replace
    else:
        real = Orchestrator._persist

        def wrapped(self):
            count["n"] += 1
            if when == "before" and count["n"] == target:
                signal.raise_signal(sig)
            real(self)
            if when == "after" and count["n"] == target:
                signal.raise_signal(sig)

        Orchestrator._persist = wrapped

    def done_marker() -> None:
        if count["n"] < target:
            (repo / ".crash_boundary_done").write_text(str(count["n"]))

    import atexit

    atexit.register(done_marker)


def _arm_lock_boundary_kill(spec: str) -> None:
    """Self-signal at a drive-lock boundary (``lock:<point>:<sig>``, P7b).

    Wraps the three RunManager entry points that publish or drop a lock file,
    so the process dies with a precisely-known set of stale locks on disk.
    """
    from gauntlet.engine.run import RunManager

    _, point, sig_name = spec.split(":")
    sig = signal.SIGKILL if sig_name == "kill" else signal.SIGTERM

    real_acquire = RunManager._acquire_worktree_lock
    real_attach = RunManager._attach_run_lock
    real_release = RunManager._release_worktree_lock

    def acquire(self, slug, run_id, *, run_dir=None, tree_guard=True,
                slug_scope=False):
        handle = real_acquire(self, slug, run_id, run_dir=run_dir,
                              tree_guard=tree_guard, slug_scope=slug_scope)
        if point == "after_tree":
            signal.raise_signal(sig)
        return handle

    def attach(self, handle, run_dir, *, record=None):
        real_attach(self, handle, run_dir, record=record)
        if point == "after_run":
            signal.raise_signal(sig)

    def release(self, handle):
        if point == "before_release" and handle is not None:
            signal.raise_signal(sig)
        real_release(self, handle)

    RunManager._acquire_worktree_lock = acquire
    RunManager._attach_run_lock = attach
    RunManager._release_worktree_lock = release


def _arm_worktree_boundary_kill(spec: str) -> None:
    """Self-signal at a worktree-lifecycle boundary (``wt:<point>:<sig>``, P7c).

    Uses the module's own ``_boundary_hook`` seam rather than wrapping public
    functions, so the kill lands INSIDE the critical section (between the git
    calls) rather than merely around it — which is the only way to leave the
    half-states §11 rows 1/2/5/6 describe.
    """
    from gauntlet.engine import worktree as WT

    _, point, sig_name = spec.split(":")
    sig = signal.SIGKILL if sig_name == "kill" else signal.SIGTERM
    if point not in WT._BOUNDARIES:
        raise SystemExit(f"unknown worktree boundary {point!r}; "
                         f"known: {list(WT._BOUNDARIES)}")

    def hook(reached: str) -> None:
        if reached == point:
            signal.raise_signal(sig)

    WT._boundary_hook = hook


def main() -> int:
    repo, slug = Path(sys.argv[1]), sys.argv[2]
    mode = sys.argv[3] if len(sys.argv) > 3 else "mid_step"
    if mode.startswith("boundary:"):
        _arm_persist_boundary_kill(repo, mode)
        adapter = CompletingAdapter()
    elif mode.startswith("lock:"):
        _arm_lock_boundary_kill(mode)
        adapter = CompletingAdapter()
    elif mode.startswith("wt:"):
        _arm_worktree_boundary_kill(mode)
        adapter = CompletingAdapter()
    else:
        adapter = CompletingAdapter() if mode == "between_step" else SleepyAdapter()
    mgr = RunManager(repo)
    pipeline = repo / "pipelines" / "crash.yaml"
    mgr.start(slug, pipeline, use_judge=False, adapter_factory=lambda n: adapter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

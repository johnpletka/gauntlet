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

The parent then resumes and asserts the engine recovers without lost/duplicated
effects.
"""

from __future__ import annotations

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


def main() -> int:
    repo, slug = Path(sys.argv[1]), sys.argv[2]
    mode = sys.argv[3] if len(sys.argv) > 3 else "mid_step"
    adapter = CompletingAdapter() if mode == "between_step" else SleepyAdapter()
    mgr = RunManager(repo)
    pipeline = repo / "pipelines" / "crash.yaml"
    mgr.start(slug, pipeline, use_judge=False, adapter_factory=lambda n: adapter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

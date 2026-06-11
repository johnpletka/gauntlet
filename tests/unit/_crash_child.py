"""Child process for the kill -9 / resume crash test (not collected by pytest).

Usage: ``python _crash_child.py <repo> <slug>``. Starts a run whose builder
agent writes a *partial* file, drops a ``.crash_ready`` sentinel, then blocks
forever — so the parent can SIGKILL it precisely mid-step (after the write-ahead
manifest record and the partial edit, before the step completes). The parent
then resumes and asserts the engine recovers without lost/duplicated effects.
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


def main() -> int:
    repo, slug = Path(sys.argv[1]), sys.argv[2]
    mgr = RunManager(repo)
    pipeline = repo / "pipelines" / "crash.yaml"
    mgr.start(slug, pipeline, use_judge=False, adapter_factory=lambda n: SleepyAdapter())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

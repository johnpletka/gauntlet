"""Hard-timeout subprocess wrapper (FR-3.3).

Every CLI invocation goes through :func:`run_with_timeout`. Stuck headless
agents run until killed, so the wrapper enforces a wall-clock limit, kills the
whole process group on expiry, and still returns whatever output was captured
so the caller can build a checkpointable error result.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProcessOutput:
    """Outcome of a subprocess run, including the timeout-kill path."""

    argv: list[str]
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool


def run_with_timeout(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin_text: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ProcessOutput:
    """Run ``argv`` with a hard wall-clock timeout.

    On expiry the entire process group receives SIGKILL (CLIs spawn worker
    children; killing only the leader leaves orphans burning tokens). Partial
    stdout/stderr captured before the kill is returned with
    ``timed_out=True`` — the caller decides how to checkpoint it.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        text=True,
        start_new_session=True,  # own process group, so killpg reaps children
    )
    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        # Second communicate() collects whatever the pipes still hold.
        stdout, stderr = proc.communicate()
        timed_out = True
    return ProcessOutput(
        argv=list(argv),
        stdout=stdout or "",
        stderr=stderr or "",
        exit_code=proc.returncode,
        duration_s=time.monotonic() - start,
        timed_out=timed_out,
    )


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()

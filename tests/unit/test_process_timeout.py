"""Hard-timeout subprocess wrapper (FR-3.3): kill path + partial capture."""

import sys
import time

from gauntlet.adapters.process import run_with_timeout


def test_normal_completion():
    out = run_with_timeout(
        [sys.executable, "-c", "print('done')"], timeout_s=30
    )
    assert out.exit_code == 0
    assert not out.timed_out
    assert "done" in out.stdout


def test_timeout_kills_and_captures_partial_output():
    start = time.monotonic()
    out = run_with_timeout(
        [
            sys.executable,
            "-c",
            "import time; print('partial', flush=True); time.sleep(60)",
        ],
        timeout_s=1.5,
    )
    elapsed = time.monotonic() - start
    assert out.timed_out
    assert "partial" in out.stdout  # output before the kill is preserved
    assert out.exit_code != 0  # killed, not clean exit
    assert elapsed < 15  # killed promptly, did not run the full sleep


def test_timeout_kills_whole_process_group():
    # The child spawns a grandchild that would outlive a leader-only kill;
    # start_new_session + killpg must take the whole group down.
    out = run_with_timeout(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys, time;"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                "print(p.pid, flush=True); time.sleep(60)"
            ),
        ],
        timeout_s=1.5,
    )
    assert out.timed_out
    grandchild_pid = int(out.stdout.strip().splitlines()[0])
    # Give the kernel a beat, then verify the grandchild is gone.
    deadline = time.monotonic() + 5
    import os

    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    assert not alive, f"grandchild {grandchild_pid} survived the group kill"


def test_stdin_text_is_delivered():
    out = run_with_timeout(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        timeout_s=30,
        stdin_text="hello",
    )
    assert "HELLO" in out.stdout

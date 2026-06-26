"""Deadlock-safe incremental streaming reader in ``run_with_timeout`` (P1, FR-1).

Validates the load-bearing §1.3 assumption: we can give up ``communicate()`` for
a hand-rolled ``selectors`` reader that frames stdout on ``\\n`` and feeds a line
sink, while re-earning concurrent stdout+stderr drain, concurrent stdin feed, and
timeout+killpg with partial capture — at field-for-field ``ProcessOutput`` parity
and with zero deadlock.

The sink-on-error path, the multi-byte decode boundary, and the no-newline trailer
are the three places the hand-rolled reader could quietly diverge from
``communicate()``; each has a dedicated test.
"""

import locale
import os
import sys
import time

import pytest

from gauntlet.adapters.process import (
    StreamSinkError,
    run_with_timeout,
)


def _child(script: str) -> list[str]:
    return [sys.executable, "-c", script]


# --------------------------------------------------------------------------
# FR-1.1 — deadlock-safe concurrent stdout+stderr drain
# --------------------------------------------------------------------------


def test_no_deadlock_with_stderr_exceeding_pipe_buffer():
    # A child that emits N stdout lines over time while writing stderr volume
    # far exceeding the OS pipe buffer (~64 KiB). If stderr were not drained
    # concurrently the child would block on its stderr write and never finish.
    n = 20
    script = (
        "import sys, time\n"
        f"for i in range({n}):\n"
        "    sys.stdout.write(f'line {i}\\n'); sys.stdout.flush()\n"
        "    sys.stderr.write('x' * 100000); sys.stderr.flush()\n"
        "    time.sleep(0.005)\n"
    )
    lines: list[str] = []
    start = time.monotonic()
    out = run_with_timeout(
        _child(script), timeout_s=30, sink=lines.append
    )
    elapsed = time.monotonic() - start

    assert not out.timed_out
    assert out.exit_code == 0
    assert elapsed < 25  # completed, did not hang
    # The sink received all N lines, in order, before exit.
    assert [ln.rstrip("\n") for ln in lines] == [f"line {i}" for i in range(n)]
    # stderr was drained (kept for end-of-step handling) but never sinked.
    assert len(out.stderr) >= n * 100000


# --------------------------------------------------------------------------
# FR-1.2 — concurrent, non-blocking stdin feed
# --------------------------------------------------------------------------


def test_over_buffer_stdin_with_early_child_output_no_hang():
    # Prompt larger than the pipe buffer; the child emits output *before*
    # draining stdin. A blocking single-shot write would deadlock against the
    # child's early output; the write-readiness-driven feed must not.
    prompt = "a" * 200000
    script = (
        "import sys\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "data = sys.stdin.read()\n"
        "sys.stdout.write(f'len={len(data)}\\n'); sys.stdout.flush()\n"
    )
    lines: list[str] = []
    start = time.monotonic()
    out = run_with_timeout(
        _child(script), timeout_s=30, stdin_text=prompt, sink=lines.append
    )
    elapsed = time.monotonic() - start

    assert not out.timed_out
    assert elapsed < 25
    # Every prompt byte was delivered, in order (the child counts them).
    assert f"len={len(prompt)}" in out.stdout
    assert [ln.rstrip("\n") for ln in lines] == ["ready", f"len={len(prompt)}"]


def test_child_closes_stdin_early_no_raise_parity():
    # The child exits without reading the whole prompt — BrokenPipe on our
    # stdin write must be swallowed, with the same ProcessOutput as buffered.
    prompt = "b" * 200000
    script = (
        "import sys\n"
        "sys.stdout.write('bye\\n'); sys.stdout.flush()\n"
        # exits immediately without reading stdin
    )
    buffered = run_with_timeout(
        _child(script), timeout_s=30, stdin_text=prompt
    )
    lines: list[str] = []
    streamed = run_with_timeout(
        _child(script), timeout_s=30, stdin_text=prompt, sink=lines.append
    )

    assert not streamed.timed_out
    assert streamed.stdout == buffered.stdout
    assert streamed.stderr == buffered.stderr
    assert streamed.exit_code == buffered.exit_code
    assert streamed.timed_out == buffered.timed_out
    assert lines == ["bye\n"]


# --------------------------------------------------------------------------
# FR-1.3 — hard timeout + process-group kill, pre-kill lines retained
# --------------------------------------------------------------------------


def test_timeout_kills_and_retains_streamed_lines():
    script = (
        "import sys, time\n"
        "sys.stdout.write('before\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    lines: list[str] = []
    start = time.monotonic()
    out = run_with_timeout(
        _child(script), timeout_s=1.5, sink=lines.append
    )
    elapsed = time.monotonic() - start

    assert out.timed_out
    assert out.exit_code != 0  # killed, not a clean exit
    assert elapsed < 15  # killed promptly
    # Lines streamed before the kill are retained.
    assert "before\n" in lines


def test_timeout_kills_whole_process_group_streaming():
    # The streaming path must reap a spawned grandchild too (killpg, not a
    # leader-only kill).
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    lines: list[str] = []
    out = run_with_timeout(_child(script), timeout_s=1.5, sink=lines.append)
    assert out.timed_out
    grandchild_pid = int(lines[0].strip())
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    assert not alive, f"grandchild {grandchild_pid} survived the group kill"


def test_timeout_enforced_after_stdout_stderr_eof():
    # The child closes fd 1 and fd 2, then hangs. Pipe EOF must NOT end the run
    # early: the hard wall-clock timeout has to still fire on the still-running
    # child (F-001). Otherwise the final proc.wait() would block forever on a
    # child that closed its pipes and then slept — a fail-open regression.
    script = (
        "import os, time\n"
        "os.write(1, str(os.getpid()).encode() + b'\\n')\n"
        "os.close(1)\n"  # stdout EOF
        "os.close(2)\n"  # stderr EOF
        "time.sleep(60)\n"  # ...but the process stays alive
    )
    lines: list[str] = []
    start = time.monotonic()
    out = run_with_timeout(_child(script), timeout_s=1.5, sink=lines.append)
    elapsed = time.monotonic() - start

    assert out.timed_out  # timeout fired despite the early pipe EOF
    assert out.exit_code != 0  # killed, not a clean exit
    assert elapsed < 15  # did not hang on the final wait

    child_pid = int(lines[0].strip())
    # The hung child's process group was killed during teardown.
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    assert not alive, f"child {child_pid} survived the EOF-then-hang timeout"


# --------------------------------------------------------------------------
# FR-1.4 — field-for-field ProcessOutput parity
# --------------------------------------------------------------------------


def test_process_output_parity_deterministic_child():
    script = (
        "import sys\n"
        "for i in range(5):\n"
        "    sys.stdout.write(f'out {i}\\n')\n"
        "    sys.stderr.write(f'err {i}\\n')\n"
        "sys.stdout.flush(); sys.stderr.flush()\n"
    )
    buffered = run_with_timeout(_child(script), timeout_s=30)
    lines: list[str] = []
    streamed = run_with_timeout(_child(script), timeout_s=30, sink=lines.append)

    # Field-for-field equality (duration_s is inherently non-deterministic).
    assert streamed.argv == buffered.argv
    assert streamed.stdout == buffered.stdout
    assert streamed.stderr == buffered.stderr
    assert streamed.exit_code == buffered.exit_code
    assert streamed.timed_out == buffered.timed_out
    assert [ln for ln in lines] == [f"out {i}\n" for i in range(5)]


def test_trailing_partial_line_clean_exit():
    # A clean-exit child whose final write has no newline: the partial is in
    # ProcessOutput.stdout byte-for-byte (== buffered) but never sinked.
    script = (
        "import sys\n"
        "sys.stdout.write('complete\\n')\n"
        "sys.stdout.write('partial-no-newline')\n"
        "sys.stdout.flush()\n"
    )
    buffered = run_with_timeout(_child(script), timeout_s=30)
    lines: list[str] = []
    streamed = run_with_timeout(_child(script), timeout_s=30, sink=lines.append)

    assert not streamed.timed_out
    assert streamed.stdout == buffered.stdout == "complete\npartial-no-newline"
    assert lines == ["complete\n"]  # partial trailer never delivered to the sink


def test_trailing_partial_line_on_kill():
    # A killed child mid-line: the partial trailer is captured in
    # ProcessOutput.stdout but never sinked.
    script = (
        "import sys, time\n"
        "sys.stdout.write('complete\\n')\n"
        "sys.stdout.write('partial')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    lines: list[str] = []
    out = run_with_timeout(_child(script), timeout_s=1.5, sink=lines.append)

    assert out.timed_out
    assert "complete\npartial" in out.stdout
    assert lines == ["complete\n"]  # only the terminated line reached the sink


def _multibyte_char_for(enc: str) -> str | None:
    """A character that is multi-byte under ``enc``, or None if none handy."""
    if len("é".encode(enc)) > 1:
        return "é"
    return None


def test_multibyte_char_split_across_reads():
    enc = locale.getpreferredencoding(False)
    ch = _multibyte_char_for(enc)
    if ch is None:
        pytest.skip(f"locale encoding {enc!r} has no multi-byte test char")

    # The child writes a line whose multi-byte character is split across two
    # flushes (with a delay), so the reader's os.read calls receive its bytes
    # separately. Because we decode only at the newline boundary, the character
    # is always whole before decode — no UnicodeDecodeError, no mojibake.
    line = f"h{ch}llo\n"
    script = (
        "import sys, time\n"
        f"data = {line!r}.encode({enc!r})\n"
        "sys.stdout.buffer.write(data[:2]); sys.stdout.buffer.flush()\n"  # splits the char
        "time.sleep(0.2)\n"
        "sys.stdout.buffer.write(data[2:]); sys.stdout.buffer.flush()\n"
    )
    buffered = run_with_timeout(_child(script), timeout_s=30)
    lines: list[str] = []
    streamed = run_with_timeout(_child(script), timeout_s=30, sink=lines.append)

    assert lines == [line]  # decoded correctly despite the split
    assert "�" not in "".join(lines)  # no replacement char / mojibake
    assert streamed.stdout == buffered.stdout == line


# --------------------------------------------------------------------------
# FR-6.2 — fail-closed sink-fault teardown
# --------------------------------------------------------------------------


def test_sink_fault_kills_group_and_raises():
    # The sink raises on the Kth line against a still-running child. The reader
    # must kill the process group, close pipes, and re-raise (so the step is
    # recorded failed) — never leave a live child or hang.
    script = (
        "import os, sys, time\n"
        "print(os.getpid(), flush=True)\n"
        "for i in range(1000):\n"
        "    print(f'line {i}', flush=True)\n"
        "    time.sleep(0.02)\n"
        "time.sleep(60)\n"
    )
    received: list[str] = []

    def boom(line: str) -> None:
        received.append(line)
        if len(received) == 3:
            raise RuntimeError("disk full")

    start = time.monotonic()
    with pytest.raises(StreamSinkError) as excinfo:
        run_with_timeout(_child(script), timeout_s=30, sink=boom)
    elapsed = time.monotonic() - start

    assert elapsed < 15  # no hang
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert len(received) == 3  # raised on the 3rd line
    child_pid = int(received[0].strip())

    # The child's process group was killed during teardown.
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    assert not alive, f"child {child_pid} survived the sink-fault teardown"


def test_sink_fault_during_timeout_final_drain_still_cleans_up():
    # A sink fault that lands on the *final drain* (after the timeout kill),
    # not the main loop, must still kill/drain/reap before re-raising (F-002).
    #
    # The setup forces the faulting line to be emitted only during the drain:
    # the child writes its pid + an "ok" line, then 0.3s later a "BOOM" line,
    # then sleeps. A slow sink (0.4s/line) spends ~0.8s emitting the first two
    # lines in the main loop — past the 0.7s timeout — so the loop breaks with
    # "BOOM" still buffered, unread, in the pipe. After the killpg, the final
    # drain reads "BOOM" and the sink raises there. Cleanup must still run.
    script = (
        "import os, sys, time\n"
        "sys.stdout.write(str(os.getpid()) + '\\n')\n"
        "sys.stdout.write('ok\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(0.3)\n"
        "sys.stdout.write('BOOM\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    received: list[str] = []

    def boom(line: str) -> None:
        received.append(line)
        if line == "BOOM\n":
            raise RuntimeError("disk full")
        time.sleep(0.4)  # lag the main loop so the timeout fires before BOOM

    start = time.monotonic()
    with pytest.raises(StreamSinkError) as excinfo:
        run_with_timeout(_child(script), timeout_s=0.7, sink=boom)
    elapsed = time.monotonic() - start

    assert elapsed < 15  # no hang on the cleanup path
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    # The fault landed on the drain-emitted line, after the two main-loop lines.
    assert received[1:] == ["ok\n", "BOOM\n"]
    child_pid = int(received[0].strip())

    # Cleanup ran despite the drain-path fault: the process group was killed.
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.1)
    assert not alive, f"child {child_pid} survived the drain-fault teardown"


# --------------------------------------------------------------------------
# Flag-off / sink=None path is unchanged (FR-6.1 groundwork)
# --------------------------------------------------------------------------


def test_sink_none_uses_buffered_path():
    out = run_with_timeout(_child("print('done')"), timeout_s=30)
    assert out.exit_code == 0
    assert not out.timed_out
    assert "done" in out.stdout

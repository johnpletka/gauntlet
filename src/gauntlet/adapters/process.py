"""Hard-timeout subprocess wrapper (FR-3.3).

Every CLI invocation goes through :func:`run_with_timeout`. Stuck headless
agents run until killed, so the wrapper enforces a wall-clock limit, kills the
whole process group on expiry, and still returns whatever output was captured
so the caller can build a checkpointable error result.

Two modes share one signature:

* **Buffered (default, ``sink=None``)** — the historical path: one
  ``proc.communicate()`` that buffers all stdout/stderr until exit. Behavior is
  byte-for-byte what it has always been; the streaming flag being off means the
  adapters pass ``sink=None`` and land here.
* **Streaming (``sink`` provided)** — a hand-rolled ``selectors`` reader that
  re-earns ``communicate()``'s bundled guarantees (concurrent stdout+stderr
  drain, concurrent stdin feed, hard timeout + ``killpg`` with partial capture)
  while handing each *complete* newline-terminated stdout line to ``sink`` as it
  arrives. This is the live-observability producer (PRD live-run-observability,
  FR-1). ``ProcessOutput`` is field-for-field identical to the buffered path for
  a deterministic child — streaming changes *when* bytes land on disk, never
  *what* the result is.
"""

from __future__ import annotations

import io
import locale
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Read chunk for the streaming reader. Larger than a typical pipe buffer so a
# single ready stdout/stderr fd is drained in as few syscalls as possible.
_READ_CHUNK = 65536

# Upper bound on the post-kill drain. After ``killpg`` the whole process group
# is dead and its pipes EOF promptly; this is only a backstop so a wedged
# grandchild holding a pipe open can never hang teardown.
_FINAL_DRAIN_S = 5.0


@dataclass(frozen=True)
class ProcessOutput:
    """Outcome of a subprocess run, including the timeout-kill path."""

    argv: list[str]
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool


class StreamSinkError(RuntimeError):
    """A streaming ``sink`` raised while persisting a line (FR-6.2).

    Raised only after the child's process group has been killed and its pipes
    drained, so a sink fault never leaves a live child, an undrained pipe, or a
    skipped process-group cleanup. The original sink exception is the
    ``__cause__``; the adapter records the step as failed (fail-closed) rather
    than continuing with output silently dropped or un-redacted.
    """


def run_with_timeout(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin_text: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    sink: Callable[[str], None] | None = None,
) -> ProcessOutput:
    """Run ``argv`` with a hard wall-clock timeout.

    On expiry the entire process group receives SIGKILL (CLIs spawn worker
    children; killing only the leader leaves orphans burning tokens). Partial
    stdout/stderr captured before the kill is returned with
    ``timed_out=True`` — the caller decides how to checkpoint it.

    When ``sink`` is provided, the run streams: ``sink`` is invoked once per
    complete (newline-terminated) stdout line, in arrival order, as lines land
    — re-earning the buffered path's deadlock-safety, stdin feed, and
    timeout+kill semantics in a ``selectors`` loop. When ``sink`` is ``None``
    the historical buffered ``communicate()`` path runs unchanged.
    """
    if sink is None:
        return _run_buffered(
            argv, timeout_s=timeout_s, stdin_text=stdin_text, cwd=cwd, env=env
        )
    return _run_streaming(
        argv,
        timeout_s=timeout_s,
        stdin_text=stdin_text,
        cwd=cwd,
        env=env,
        sink=sink,
    )


def _run_buffered(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin_text: str | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
) -> ProcessOutput:
    """The historical buffered path — one ``communicate()``, unchanged."""
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


def _run_streaming(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin_text: str | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    sink: Callable[[str], None],
) -> ProcessOutput:
    """Incremental, deadlock-safe reader that frames stdout on ``\\n``.

    Re-earns the bundled guarantees ``communicate()`` gives for free:

    * concurrent stdout+stderr drain (finite pipe buffers deadlock otherwise);
    * concurrent, non-blocking stdin feed with partial-write accounting;
    * hard timeout → ``killpg`` → drain-remaining → ``timed_out=True``;
    * field-for-field ``ProcessOutput`` parity via separate raw byte buffers
      that capture every byte read — including a trailing non-terminated
      segment that is *never* handed to the sink (FR-1.4 / FR-2.4).

    stderr is drained for deadlock-safety only; it is never routed to the sink
    (FR-2.6).
    """
    # Match the buffered ``text=True`` path's codec exactly so the assembled
    # stdout/stderr are byte-identical. subprocess.Popen(text=True) wraps the
    # pipes in TextIOWrapper with this same locale default.
    enc = locale.getpreferredencoding(False)
    start = time.monotonic()
    use_stdin = stdin_text is not None

    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        bufsize=0,  # raw binary pipes; we frame + decode ourselves
        start_new_session=True,
    )

    # Raw byte buffers, maintained independently of the line sink: every byte
    # read is appended here regardless of newline framing, so a trailing
    # non-terminated segment is still captured byte-for-byte in ProcessOutput.
    raw_stdout = bytearray()
    raw_stderr = bytearray()
    # Bytes of the current unframed stdout line (not yet newline-terminated).
    line_buf = bytearray()

    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
    sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
    open_tags = {"stdout", "stderr"}

    stdin_bytes = stdin_text.encode(enc) if use_stdin else b""
    stdin_view = memoryview(stdin_bytes)
    state = {"stdin_offset": 0, "stdin_registered": False, "stdin_closed": False}

    def _close_stdin() -> None:
        if not use_stdin or state["stdin_closed"]:
            return
        if state["stdin_registered"]:
            try:
                sel.unregister(proc.stdin)
            except (KeyError, ValueError):
                pass
            state["stdin_registered"] = False
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        state["stdin_closed"] = True

    if use_stdin:
        if stdin_bytes:
            os.set_blocking(proc.stdin.fileno(), False)
            sel.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
            state["stdin_registered"] = True
        else:
            # Empty prompt: nothing to feed, just signal EOF to the child.
            _close_stdin()

    def _feed_stdin() -> None:
        # Registered for write only while bytes remain; advance an offset over
        # partial writes; close exactly once when the prompt is fully sent.
        # BrokenPipe (child closed its read end / exited early) is swallowed and
        # treated identically to communicate(input=...): no error, no hang.
        try:
            n = os.write(proc.stdin.fileno(), stdin_view[state["stdin_offset"] :])
        except BlockingIOError:
            return
        except (BrokenPipeError, OSError):
            _close_stdin()
            return
        state["stdin_offset"] += n
        if state["stdin_offset"] >= len(stdin_bytes):
            _close_stdin()

    def _emit_lines() -> None:
        # Frame on the newline delimiter; decode + sink each *complete* line.
        # Decoding only at the line boundary means a multi-byte character split
        # across OS reads is always whole before decode (\n=0x0A never appears
        # inside a multi-byte UTF-8 sequence). A trailing partial line has no
        # \n and is therefore never sinked (FR-2.4) — but it is already in
        # raw_stdout for the assembled-capture parity (FR-1.4).
        while True:
            nl = line_buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(line_buf[: nl + 1])
            del line_buf[: nl + 1]
            text = line.decode(enc)
            try:
                sink(text)
            except Exception as exc:  # fail-closed (FR-6.2)
                raise StreamSinkError(
                    "streaming sink failed while persisting a line"
                ) from exc

    def _on_read(key: selectors.SelectorKey, *, emit: bool) -> None:
        chunk = os.read(key.fileobj.fileno(), _READ_CHUNK)
        if not chunk:  # EOF
            try:
                sel.unregister(key.fileobj)
            except (KeyError, ValueError):
                pass
            open_tags.discard(key.data)
            return
        if key.data == "stdout":
            raw_stdout.extend(chunk)
            line_buf.extend(chunk)
            if emit:
                _emit_lines()
        else:  # stderr: drained for deadlock-safety only, never sinked (FR-2.6)
            raw_stderr.extend(chunk)

    timed_out = False
    pending_exc: BaseException | None = None
    try:
        while open_tags:
            remaining = timeout_s - (time.monotonic() - start)
            if remaining <= 0:
                timed_out = True
                break
            for key, _mask in sel.select(timeout=remaining):
                if key.data == "stdin":
                    _feed_stdin()
                else:
                    _on_read(key, emit=True)
    except BaseException as exc:  # sink fault or unexpected reader error
        pending_exc = exc

    # Teardown is identical for the timeout path and the sink-fault path: kill
    # the process group, drain whatever the pipes still hold into the raw
    # buffers, close stdin, reap. This guarantees the no-deadlock / killpg
    # guarantees hold on the fault path too.
    if timed_out or pending_exc is not None:
        _kill_process_group(proc)

    # On the sink-fault path the sink already failed — drain into the raw
    # buffers only (emit=False). On the timeout path, emit the complete lines
    # received before the kill so they are retained (FR-1.3).
    emit_during_drain = pending_exc is None
    drain_deadline = time.monotonic() + _FINAL_DRAIN_S
    while open_tags and time.monotonic() < drain_deadline:
        ready = sel.select(timeout=0.1)
        if not ready:
            if proc.poll() is not None:
                # Process is gone and nothing is pending; pipes have drained.
                break
            continue
        for key, _mask in ready:
            if key.data == "stdin":
                _close_stdin()
                continue
            _on_read(key, emit=emit_during_drain)

    _close_stdin()
    proc.wait()
    sel.close()

    if pending_exc is not None:
        raise pending_exc

    return ProcessOutput(
        argv=list(argv),
        stdout=_decode_like_text_mode(raw_stdout, enc),
        stderr=_decode_like_text_mode(raw_stderr, enc),
        exit_code=proc.returncode,
        duration_s=time.monotonic() - start,
        timed_out=timed_out,
    )


def _decode_like_text_mode(raw: bytearray, enc: str) -> str:
    """Decode raw bytes exactly as ``subprocess(text=True)`` would.

    ``TextIOWrapper`` with the default ``newline=None`` applies universal
    newline translation on read — the same translation ``communicate()`` does
    — so the assembled string is byte-for-byte what the buffered path returns.
    """
    return io.TextIOWrapper(io.BytesIO(bytes(raw)), encoding=enc).read()


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()

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
import threading
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

# Poll cadence for the suspend-aware deadline path (FR-5.2). When a heartbeat is
# active the deadline can be credited *upward* mid-run (a detected host suspend),
# so neither path may commit to a single fixed wall-clock wait: both re-evaluate
# ``deadline.remaining_s()`` at least this often. Off the deadline path (no active
# heartbeat) this constant is unused and behavior is byte-for-byte the historical
# single-``communicate()`` / uncapped-``select`` path.
_DEADLINE_POLL_S = 5.0

# Grace granted to reap a child whose pipes have already hit EOF when the
# wall-clock budget is exhausted. Pipe EOF is strong evidence the child is
# exiting, but EOF and reaping can be separated by scheduler latency; without a
# grace, ``proc.wait(timeout=0.0)`` polls once and falsely reports a timeout for
# a cleanly-completing child. Kept small so it never materially loosens the hard
# timeout (FR-3.3) — a child that stays alive past it is a genuine post-EOF hang.
_REAP_GRACE_S = 0.5


# --- in-flight agent-call probe (issue #103) ---------------------------------
# The engine's agent-liveness watchdog (engine/heartbeat.py) needs two live
# observables for the CLI call currently in flight: how long since the child
# last produced output, and whether the RECORDED child pid still exists. The
# child is spawned with ``start_new_session=True`` — it runs DETACHED in its
# own process group, so nothing group- or socket-scoped on the DRIVER can ever
# be evidence about the agent (the issue #103 second-occurrence false positive:
# a healthy detached builder looks exactly like a dead one from the driver's
# group). Registered here — the one chokepoint every CLI invocation passes
# through — as a single process-global slot, mirroring the heartbeat's
# active-writer registry: only one driver runs per process (the worktree lock
# guarantees it) and the adapters run agent calls sequentially, so one slot is
# sufficient and there is no cross-run leakage.
_probe_lock = threading.Lock()
_active_probe: "AgentCallProbe | None" = None


class AgentCallProbe:
    """Live observables of one in-flight CLI agent invocation (issue #103).

    ``pid``/``pgid`` are the spawned child's — with ``start_new_session=True``
    the child is its own session and group leader, so ``pgid == pid`` by
    construction and no post-spawn ``getpgid`` race exists. ``touch()`` is
    called by the streaming reader on every stdout/stderr chunk; the buffered
    path cannot observe output incrementally, so its silence age runs from
    spawn — which only makes the watchdog MORE conservative (silence is a
    necessary, never sufficient, trip condition).
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        *,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._proc = proc
        self.pid = proc.pid
        self.pgid = proc.pid  # start_new_session=True: the child leads its group
        self._mono = monotonic_clock
        now = monotonic_clock()
        self.started_monotonic = now
        self._output_lock = threading.Lock()
        self._last_output = now

    def touch(self) -> None:
        """Record that the child just produced output (any stdout/stderr bytes)."""
        with self._output_lock:
            self._last_output = self._mono()

    def silence_s(self) -> float:
        """Seconds since the child last produced observable output (or spawned)."""
        with self._output_lock:
            return max(0.0, self._mono() - self._last_output)

    def agent_gone(self) -> bool:
        """True ONLY when the agent is provably gone: pid dead AND group empty.

        Two conditions, BOTH required (each strictly narrows, never widens):

        * the recorded pid no longer exists — a non-blocking ``poll()`` that
          has reaped the child (we hold the exit status), or ``kill -0``
          failing with ``ProcessLookupError`` (a zombie still holds its pid,
          so ESRCH means dead AND reaped);
        * the agent's OWN process group is empty — ``killpg -0`` on the
          child's group (it leads its own group) raising
          ``ProcessLookupError``: a forked worker still in the group may be
          doing the real work while holding the output pipes, so a dead
          leader alone is not proof the attempt is unowned.

        Everything else fails open to "not provably gone": a live child, an
        unreaped zombie, a lock-contended ``poll`` (the waiting caller is
        about to consume the exit), a permission error, a recycled pid, a
        surviving group member. The DRIVER's group membership and socket
        state are deliberately NOT consulted — the child is detached by
        construction, so their absence is not evidence (issue #103, second
        occurrence reclassified: a healthy detached builder looks exactly
        like a dead one from the driver's group).
        """
        gone = False
        try:
            # Reap-if-reapable (non-blocking; a lock held by a concurrently
            # waiting caller makes this a no-op) so a dead child cannot linger
            # unreaped forever when the waiter itself is the wedged party.
            gone = self._proc.poll() is not None
        except OSError:
            return False
        if not gone:
            try:
                os.kill(self.pid, 0)
                return False  # pid exists (live or zombie) → not provably gone
            except ProcessLookupError:
                gone = True
            except OSError:
                return False  # unprovable (EPERM etc.) → fail open
        try:
            os.killpg(self.pgid, 0)
            return False  # a group member survives (worker/straggler) → fail open
        except ProcessLookupError:
            return True  # pid dead AND its whole group gone: provably unowned
        except OSError:
            return False

    def kill_group(self) -> None:
        """Best-effort SIGKILL of the agent's own (detached) process group.

        The termination-path counterpart of the detached spawn (issue #103):
        a driver that is being torn down must take its spawned agent's group
        with it, or the orphaned agent keeps editing the tree and collides
        with the builder the next resume spawns. Swallows every error — the
        group being already gone is the common case.
        """
        try:
            os.killpg(self.pgid, signal.SIGKILL)
        except OSError:
            pass


def _register_probe(proc: subprocess.Popen) -> AgentCallProbe:
    global _active_probe
    probe = AgentCallProbe(proc)
    with _probe_lock:
        _active_probe = probe
    return probe


def _clear_active_probe() -> None:
    global _active_probe
    with _probe_lock:
        _active_probe = None


def active_agent_probe() -> AgentCallProbe | None:
    """The probe for the CLI agent call currently in flight, or ``None``."""
    with _probe_lock:
        return _active_probe


def kill_active_agent_group() -> None:
    """SIGKILL the in-flight agent call's detached process group, if any.

    Called from the driver's termination paths (the SIGTERM forwarder the
    drive installs, and the watchdog's self-interrupt) so a detached agent
    never outlives the driver that spawned it (issue #103).
    """
    probe = active_agent_probe()
    if probe is not None:
        probe.kill_group()


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
    ``__cause__``. This is intentionally NOT an :class:`AdapterError`: it
    propagates past the adapters' (and engine's) ``except AdapterError`` handlers
    to the orchestrator's generic fail-closed handler, which records the step
    FAILED (FR-6.2) rather than continuing with output silently dropped or
    un-redacted. The streamed lines persisted before the fault stay on disk.
    """


def run_with_timeout(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin_text: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    sink: Callable[[str], None] | None = None,
    preexec_fn: Callable[[], None] | None = None,
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
    # A suspend-aware deadline is used only while a driver heartbeat is active
    # (FR-5.2): it credits detected host-suspension back to the wait, bounded by
    # the configured cap. Outside a driven run (tests, one-shot CLI, disabled
    # heartbeat) this is ``None`` and both paths keep their exact historical
    # timing. Imported lazily so the low-level adapter layer never hard-depends on
    # the engine package at import time.
    from gauntlet.engine.heartbeat import build_active_deadline

    deadline = build_active_deadline(timeout_s)
    # The in-flight probe (issue #103) is registered by each path right after
    # its Popen and cleared here unconditionally, so no exit — return, timeout,
    # sink fault, unexpected reader error — can leave a stale probe behind for
    # the agent-liveness watchdog to misread as a still-in-flight call.
    try:
        if sink is None:
            return _run_buffered(
                argv, timeout_s=timeout_s, stdin_text=stdin_text, cwd=cwd, env=env,
                deadline=deadline, preexec_fn=preexec_fn,
            )
        return _run_streaming(
            argv,
            timeout_s=timeout_s,
            stdin_text=stdin_text,
            cwd=cwd,
            env=env,
            sink=sink,
            deadline=deadline,
            preexec_fn=preexec_fn,
        )
    finally:
        _clear_active_probe()


def _run_buffered(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin_text: str | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    deadline: "object | None" = None,
    preexec_fn: Callable[[], None] | None = None,
) -> ProcessOutput:
    """The buffered path — one ``communicate()`` (or a polled one under a deadline).

    With ``deadline is None`` this is byte-for-byte the historical path: a single
    ``communicate(timeout=timeout_s)`` with the same kill/drain on expiry. With a
    suspend-aware ``deadline`` it polls ``communicate`` so the wait can absorb a
    mid-run host suspension (the deadline credits it, FR-5.2) — retrying
    ``communicate`` after a ``TimeoutExpired`` does not lose output.
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
        preexec_fn=preexec_fn,  # optional rlimit caps (verifier, PR #59 §7 item 5)
    )
    _register_probe(proc)  # issue #103: expose the recorded child pid live
    if deadline is None:
        try:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            # Second communicate() collects whatever the pipes still hold.
            stdout, stderr = proc.communicate()
            timed_out = True
    else:
        stdout, stderr, timed_out = _communicate_with_deadline(
            proc, stdin_text, deadline
        )
    return ProcessOutput(
        argv=list(argv),
        stdout=stdout or "",
        stderr=stderr or "",
        exit_code=proc.returncode,
        duration_s=time.monotonic() - start,
        timed_out=timed_out,
    )


def _communicate_with_deadline(
    proc: subprocess.Popen, stdin_text: str | None, deadline
) -> tuple[str, str, bool]:
    """Poll ``communicate`` until the child exits or the credited deadline lapses.

    ``deadline.remaining_s()`` is re-read each poll so a host suspension detected
    mid-wait (credited upward, FR-5.2) extends the wait instead of killing a
    healthy child. On genuine expiry the process group is killed and the pipes
    drained, mirroring the single-shot path's kill/drain contract. Input is fed
    only on the first ``communicate`` call (subprocess requires this); retries
    pass ``None`` and continue reading without re-writing stdin.
    """
    first = True
    while True:
        remaining = deadline.remaining_s()
        poll = max(0.0, min(remaining, _DEADLINE_POLL_S))
        try:
            stdout, stderr = proc.communicate(
                input=stdin_text if first else None, timeout=poll
            )
            return stdout or "", stderr or "", False
        except subprocess.TimeoutExpired:
            first = False
            if deadline.expired():
                _kill_process_group(proc)
                stdout, stderr = proc.communicate()
                return stdout or "", stderr or "", True


def _stream_remaining(deadline, timeout_s: float, start: float) -> float:
    """Remaining wall-clock budget for the streaming loop.

    Delegates to a suspend-aware ``deadline`` when one is active (FR-5.2), else
    the historical monotonic computation. Identical to the old expression when
    ``deadline is None``, so the non-driven path is unchanged.
    """
    if deadline is not None:
        return deadline.remaining_s()
    return timeout_s - (time.monotonic() - start)


def _run_streaming(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin_text: str | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    sink: Callable[[str], None],
    deadline: "object | None" = None,
    preexec_fn: Callable[[], None] | None = None,
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
        preexec_fn=preexec_fn,  # optional rlimit caps (verifier, PR #59 §7 item 5)
    )
    probe = _register_probe(proc)  # issue #103: recorded child pid + output age

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
        probe.touch()  # any stdout/stderr bytes are agent liveness (issue #103)
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
            remaining = _stream_remaining(deadline, timeout_s, start)
            if remaining <= 0:
                timed_out = True
                break
            # Under a suspend-aware deadline the budget can be credited upward
            # mid-wait, so never block longer than the poll cadence without
            # re-evaluating it; off the deadline path the select waits the full
            # remaining as before.
            sel_timeout = remaining if deadline is None else min(remaining, _DEADLINE_POLL_S)
            for key, _mask in sel.select(timeout=sel_timeout):
                if key.data == "stdin":
                    _feed_stdin()
                else:
                    _on_read(key, emit=True)
        # stdout/stderr are at EOF, but the child may still be alive — it can
        # close fd 1 and fd 2 and then hang. The hard timeout has to cover
        # process liveness, not just pipe liveness (FR-3.3, F-001), so wait for
        # exit under whatever wall-clock budget remains. On expiry, fall through
        # to the same killpg/drain/reap teardown as the in-loop timeout instead
        # of blocking forever on the final ``proc.wait()``.
        if not timed_out:
            remaining = _stream_remaining(deadline, timeout_s, start)
            # Both pipes are at EOF, so the child has closed fd 1 and fd 2 and is
            # almost certainly exiting. ``proc.wait(timeout=0.0)`` does NOT sleep —
            # it polls once and raises ``TimeoutExpired`` if the child has not yet
            # been reaped — so a bare ``max(remaining, 0.0)`` would falsely mark a
            # cleanly-completing child as timed out whenever the budget is spent at
            # the moment EOF lands (remaining ≤ 0) and EOF/reaping are separated by
            # scheduler latency. Floor the reap budget at the small ``_REAP_GRACE_S``
            # so a child that is genuinely finishing is reaped, while a child that
            # stays alive past it is still surfaced as a real post-EOF hang (FR-3.3).
            # A child exiting promptly returns immediately regardless of the ceiling,
            # so this only extends the wait when the budget was already exhausted.
            try:
                proc.wait(timeout=max(remaining, _REAP_GRACE_S))
            except subprocess.TimeoutExpired:
                timed_out = True
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
    # received before the kill so they are retained (FR-1.3). A sink fault
    # *during* this drain must not skip the cleanup below: record it, stop
    # emitting, and keep draining so the kill/drain/reap contract still
    # completes before we re-raise (FR-6.2, F-002).
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
            try:
                _on_read(key, emit=emit_during_drain)
            except StreamSinkError as exc:
                if pending_exc is None:
                    pending_exc = exc
                emit_during_drain = False

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

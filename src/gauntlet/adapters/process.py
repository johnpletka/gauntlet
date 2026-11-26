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
import re
import selectors
import shutil
import signal
import subprocess
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

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

# Process groups (``getpgid``/``killpg``/``start_new_session``) and
# ``selectors`` over pipes are POSIX-only. On native Windows the child is put in
# its own console process group and (best effort) its own Job Object, the
# "group kill" terminates that job and runs a ``taskkill /T`` tree kill, the
# post-kill drain is bounded, the vanish watchdog can never prove the group
# empty (so it fails closed and never fires — the hard wall-clock timeout plus
# the bounded drain keep the step finite), and a streaming ``sink`` is fed
# after a buffered run instead of line-by-line.
_POSIX = os.name == "posix"

# Bound on the Windows ``taskkill /T /F`` tree kill before falling back to
# killing only the leader.
_TASKKILL_TIMEOUT_S = 10.0


# Windows batch files (`.cmd`/`.bat`) are run by CreateProcess through
# `cmd.exe`, which re-parses the command line: `& | < > ^` outside quotes are
# operators, `%VAR%` expands even inside quotes, and `\"` is not an escape
# (the "BatBadBut" class — see Python's subprocess security notes). Agent argv
# carries filesystem paths, JSON schemas and system-prompt text, so it is never
# handed to `cmd.exe` unexamined (#178 review F-002).
_BATCH_SUFFIXES = (".cmd", ".bat")
# An npm cmd-shim forwards `%*` to `"%_prog%" "%dp0%\<script>"`; running
# `node <script>` directly bypasses `cmd.exe` entirely.
_NPM_SHIM_TARGET_RE = re.compile(r'"%dp0%\\([^"\r\n]+)"\s+%\*')
# Arguments made only of these characters need no quoting for `cmd.exe` or
# the CRT parser; anything else is double-quoted.
_BATCH_PLAIN_ARG_RE = re.compile(r"[A-Za-z0-9_\-./:=,+@\\]+")
# Characters that cannot be carried through `cmd.exe` safely even inside
# double quotes (`%` expands, `"` toggles quoting, newlines end the command).
_BATCH_UNSAFE_CHARS = frozenset('"%\r\n\x00')


class UnsafeBatchArgumentsError(OSError):
    """A Windows batch-file launch would hand ``cmd.exe`` an argument it would
    re-parse (fail closed: the spawn is refused, never run mangled)."""


def resolve_argv0(
    argv: Sequence[str], *, env: Mapping[str, str] | None = None
) -> list[str] | str:
    """The spawnable form of ``argv`` on this platform.

    POSIX, or an empty argv → ``argv`` unchanged.

    On native Windows:

    * A bare name is resolved with ``shutil.which`` against the child's
      ``PATH`` (an explicit path list, so the current directory — the run's
      worktree, which an agent can write — is NOT searched first).
      ``CreateProcess`` only appends ``.exe`` itself, so npm-installed CLIs
      (``codex.cmd``) would otherwise raise ``FileNotFoundError``.
    * A resolved npm ``.cmd`` shim is unwrapped to ``node <script> ...`` so the
      arguments never pass through ``cmd.exe``.
    * Any other batch file is launched with a command line whose non-trivial
      arguments are double-quoted (``cmd.exe`` treats operators inside quotes
      literally); an argument containing ``"``, ``%`` or a newline cannot be
      carried safely, so the launch is refused with
      :class:`UnsafeBatchArgumentsError`. This form is returned as a string.

    An unresolvable name is returned unchanged (the spawn then fails exactly as
    before).
    """
    out = list(argv)
    if _POSIX or not out:
        return out
    name = out[0]
    if not os.path.dirname(name):
        search = (env if env is not None else os.environ).get("PATH") or os.defpath
        resolved = shutil.which(name, path=search)
        if resolved:
            out[0] = resolved
    if not out[0].lower().endswith(_BATCH_SUFFIXES):
        return out
    unwrapped = _unwrap_npm_shim(out[0], env)
    if unwrapped is not None:
        return unwrapped + out[1:]
    return _batch_command_line(out)


def _unwrap_npm_shim(shim: str, env: Mapping[str, str] | None) -> list[str] | None:
    """``[node, script]`` for a standard npm cmd-shim, else ``None``."""
    try:
        text = Path(shim).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _NPM_SHIM_TARGET_RE.search(text)
    if match is None:
        return None
    base = Path(shim).parent
    # The shim names its script with Windows separators, relative to itself.
    script = base.joinpath(*PureWindowsPath(match.group(1)).parts)
    if not script.is_file():
        return None
    if script.suffix.lower() == ".exe":
        return [str(script)]
    if script.suffix.lower() not in (".js", ".cjs", ".mjs"):
        return None
    local_node = base / "node.exe"
    if local_node.is_file():
        node = str(local_node)
    else:
        search = (env if env is not None else os.environ).get("PATH") or os.defpath
        node = shutil.which("node", path=search)
    if not node:
        return None
    return [node, str(script)]


def _batch_command_line(argv: Sequence[str]) -> str:
    """A ``cmd.exe``-safe command line for a batch file, or refuse."""
    parts = []
    for i, arg in enumerate(argv):
        if _BATCH_UNSAFE_CHARS.intersection(arg):
            raise UnsafeBatchArgumentsError(
                f"refusing to pass argument {i} to batch file {argv[0]!r}: it "
                "contains a character cmd.exe re-parses even inside quotes "
                '(", %, or a newline). Install the CLI as a native executable '
                "(.exe) or via npm so Gauntlet can run it without cmd.exe."
            )
        if arg and _BATCH_PLAIN_ARG_RE.fullmatch(arg):
            parts.append(arg)
        else:
            # Inside quotes cmd.exe takes & | < > ^ ( ) literally. Backslashes
            # before the closing quote are doubled for the CRT parser.
            trailing = len(arg) - len(arg.rstrip("\\"))
            parts.append('"' + arg + "\\" * trailing + '"')
    return " ".join(parts)


def _spawn_isolation_kwargs() -> dict:
    """Popen kwargs that give the child its own process group, per platform."""
    if _POSIX:
        return {"start_new_session": True}
    return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}


def _text_kwargs() -> dict:
    """Popen text-mode kwargs. POSIX: the historical locale codec (unchanged).

    Windows: UTF-8 explicitly. The locale codec there is usually cp1252, which
    cannot encode prompt characters such as ``→`` (``UnicodeEncodeError`` on
    stdin) and mis-decodes the agents' UTF-8 output (#178 review F-003).
    """
    if _POSIX:
        return {"text": True}
    return {"text": True, "encoding": "utf-8", "errors": "replace"}


def _assign_windows_job(proc: subprocess.Popen) -> None:
    """Put a Windows child in its own Job Object (best effort).

    Every descendant the child starts joins the job, so the timeout kill
    (``TerminateJobObject``) reaches them even after their parent exited —
    ``taskkill /T`` walks live parent links and misses such orphans, which can
    then hold the output pipes open forever (#178 review F-001). Any failure
    leaves the child unjobbed; the ``taskkill`` tree kill and the bounded
    post-kill drain still apply.
    """
    if _POSIX:
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        if not k32.AssignProcessToJobObject(job, int(proc._handle)):  # type: ignore[attr-defined]
            k32.CloseHandle(job)
            return
    except Exception:  # noqa: BLE001 — best effort; the taskkill path remains
        return
    proc._gauntlet_job_kill = lambda: bool(k32.TerminateJobObject(job, 1))  # type: ignore[attr-defined]
    weakref.finalize(proc, k32.CloseHandle, job)


def _drain_after_kill(proc: subprocess.Popen) -> tuple[str | None, str | None]:
    """Collect output after a timeout kill.

    POSIX: the group kill closed every write end, so ``communicate()`` is the
    historical unbounded drain (unchanged). Windows: a descendant outside the
    kill's reach can keep a pipe open, so the drain is bounded — FR-3.3 promises
    a bounded step even when the tree kill is incomplete.
    """
    if _POSIX:
        return proc.communicate()
    return _drain_bounded(proc)

# Grace granted to reap a child whose pipes have already hit EOF when the
# wall-clock budget is exhausted. Pipe EOF is strong evidence the child is
# exiting, but EOF and reaping can be separated by scheduler latency; without a
# grace, ``proc.wait(timeout=0.0)`` polls once and falsely reports a timeout for
# a cleanly-completing child. Kept small so it never materially loosens the hard
# timeout (FR-3.3) — a child that stays alive past it is a genuine post-EOF hang.
_REAP_GRACE_S = 0.5


@dataclass(frozen=True)
class ProcessOutput:
    """Outcome of a subprocess run, including the timeout-kill path.

    ``agent_vanished`` (FR-5.3, #103): the agent-liveness watchdog stopped the
    wait because the child was PROVABLY gone (leader reaped, process group
    empty) while its output stream stayed silent past the configured bound and
    never delivered EOF. Distinct from ``timed_out`` (the FR-3.3 wall-clock
    budget): a vanished agent is an interruption to park-and-resume, not a
    budget expiry to halt on.
    """

    argv: list[str]
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool
    agent_vanished: bool = False


def _group_alive(pgid: int | None) -> bool | None:
    """Probe whether ANY process remains in ``pgid`` (fail-closed tri-state).

    ``killpg(pgid, 0)`` delivers no signal; it only reports existence. ``False``
    is the only value that can arm the watchdog, so anything ambiguous —
    a permission error (processes exist but are not ours) or an unexpected OS
    error — reads as alive/unknowable, never as proof of absence. A ``None``
    pgid (no POSIX process groups, e.g. native Windows) is always unknowable.
    """
    if pgid is None or not _POSIX:
        return None
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # something exists in the group; not proof of absence
    except OSError:
        return None  # unreadable → fail closed (keep waiting)


class _VanishWatch:
    """Per-invocation state for the agent-liveness watchdog (FR-5.3, #103).

    Samples the observables :func:`gauntlet.engine.heartbeat.watchdog_should_fire`
    decides over: child liveness (``proc.poll()``), process-group emptiness
    (cached pgid — the child is spawned with ``start_new_session=True`` so its
    pgid is its pid, captured up front because a reaped pid can no longer be
    queried), and silence measured from the later of the last observed output
    progress and the first observation of the child's death (so a child that
    dies mid-run gets the FULL bound before the watchdog acts — never a
    retroactive expiry off pre-death silence).
    """

    def __init__(
        self,
        proc: subprocess.Popen,
        bound_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.proc = proc
        self.bound_s = bound_s
        # How often the wait should re-check liveness: half the bound, capped
        # at the deadline cadence and floored so a tiny bound never hot-loops.
        # Detection latency is therefore O(bound), never worse than ~2 polls.
        self.poll_s = max(0.1, min(_DEADLINE_POLL_S, bound_s / 2.0))
        self._clock = clock
        self.pgid: int | None
        if not _POSIX:
            # No POSIX process groups: emptiness is unprovable, so
            # _group_alive reads None and the watchdog fails closed.
            self.pgid = None
            self._last_progress = clock()
            self._dead_since = None
            return
        try:
            self.pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            # start_new_session=True makes the child its own group leader.
            self.pgid = proc.pid
        self._last_progress = clock()
        self._dead_since: float | None = None

    def progress(self) -> None:
        """Record output progress (any stdout/stderr bytes observed)."""
        self._last_progress = self._clock()

    def check(self) -> bool:
        """True when the watchdog should stop the wait (proof-gated, #103)."""
        # Imported lazily, mirroring build_active_deadline: the adapter layer
        # never hard-depends on the engine package at import time.
        from gauntlet.engine.heartbeat import watchdog_should_fire

        now = self._clock()
        child_alive = self.proc.poll() is None
        if child_alive:
            self._dead_since = None
            return False
        if self._dead_since is None:
            self._dead_since = now
        silence_s = now - max(self._last_progress, self._dead_since)
        return watchdog_should_fire(
            child_alive=False,
            group_alive=_group_alive(self.pgid),
            silence_s=silence_s,
            silence_bound_s=self.bound_s,
        )


def effective_watchdog_silence_s(bound: float | None) -> float:
    """Resolve a configured watchdog bound: ``None`` → the engine default.

    An explicit ``0`` (or negative) means disabled and is returned as-is —
    :func:`_build_vanish_watch` maps it to no watchdog. Shared with the CLI
    adapters so their error messages name the bound that actually applied.
    """
    if bound is None:
        from gauntlet.engine.heartbeat import DEFAULT_AGENT_SILENT_TIMEOUT_S

        return DEFAULT_AGENT_SILENT_TIMEOUT_S
    return bound


def _build_vanish_watch(
    proc: subprocess.Popen, watchdog_silence_s: float | None
) -> _VanishWatch | None:
    """The watchdog for one spawn, or ``None`` when disabled (``<= 0``).

    ``None`` bound → the conservative engine default
    (:data:`gauntlet.engine.heartbeat.DEFAULT_AGENT_SILENT_TIMEOUT_S`);
    an explicit ``0`` (or negative) disables the watchdog entirely.
    """
    bound = effective_watchdog_silence_s(watchdog_silence_s)
    if bound <= 0:
        return None
    return _VanishWatch(proc, bound)


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
    watchdog_silence_s: float | None = None,
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

    ``watchdog_silence_s`` (FR-5.3, #103) bounds how long the wait may sit on
    a child that is PROVABLY gone (leader reaped, process group empty) while
    its pipes stay open with no EOF — the shape a dropped stream / escaped fd
    holder leaves behind, which neither ``communicate`` nor the select loop can
    otherwise distinguish from a healthy silent agent. On proof + bound the
    wait stops and the result carries ``agent_vanished=True``. ``None`` uses
    the engine default; ``0`` disables. A live child is NEVER touched by this
    bound, however silent — only the hard ``timeout_s`` applies to it.
    """
    # A suspend-aware deadline is used only while a driver heartbeat is active
    # (FR-5.2): it credits detected host-suspension back to the wait, bounded by
    # the configured cap. Outside a driven run (tests, one-shot CLI, disabled
    # heartbeat) this is ``None`` and both paths keep their exact historical
    # timing. Imported lazily so the low-level adapter layer never hard-depends on
    # the engine package at import time.
    from gauntlet.engine.heartbeat import build_active_deadline

    deadline = build_active_deadline(timeout_s)
    if sink is not None and not _POSIX:
        # selectors cannot wait on pipes on Windows: run buffered, then hand
        # the complete lines to the sink. Same result; only the timing of the
        # sink calls differs (they land after exit rather than live).
        out = _run_buffered(
            argv, timeout_s=timeout_s, stdin_text=stdin_text, cwd=cwd, env=env,
            deadline=deadline, preexec_fn=preexec_fn,
            watchdog_silence_s=watchdog_silence_s,
        )
        _replay_lines_to_sink(out.stdout, sink)
        return out
    if sink is None:
        return _run_buffered(
            argv, timeout_s=timeout_s, stdin_text=stdin_text, cwd=cwd, env=env,
            deadline=deadline, preexec_fn=preexec_fn,
            watchdog_silence_s=watchdog_silence_s,
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
        watchdog_silence_s=watchdog_silence_s,
    )


def _replay_lines_to_sink(stdout: str, sink: Callable[[str], None]) -> None:
    """Feed each complete (newline-terminated) line of ``stdout`` to ``sink``.

    Mirrors the streaming path's framing: a trailing non-terminated segment is
    never sinked (FR-2.4), and a sink fault is fail-closed (FR-6.2).
    """
    # Frame on "\n" ONLY, exactly like the streaming reader: str.splitlines
    # would also split on U+2028/U+2029/\x0c/\x85, which JSON printers emit
    # unescaped, yielding a non-terminated fragment mid-output.
    *complete, _tail = stdout.split("\n")
    for line in (c + "\n" for c in complete):
        try:
            sink(line)
        except Exception as exc:
            raise StreamSinkError(
                "streaming sink failed while persisting a line"
            ) from exc


def _run_buffered(
    argv: Sequence[str],
    *,
    timeout_s: float,
    stdin_text: str | None,
    cwd: Path | None,
    env: Mapping[str, str] | None,
    deadline: "object | None" = None,
    preexec_fn: Callable[[], None] | None = None,
    watchdog_silence_s: float | None = None,
) -> ProcessOutput:
    """The buffered path — one ``communicate()`` (polled under a deadline/watchdog).

    With neither a suspend-aware ``deadline`` nor an armed watchdog this is
    byte-for-byte the historical path: a single ``communicate(timeout=timeout_s)``
    with the same kill/drain on expiry. Otherwise it polls ``communicate`` so the
    wait can absorb a mid-run host suspension (the deadline credits it, FR-5.2)
    and can notice a provably-vanished child (#103) — retrying ``communicate``
    after a ``TimeoutExpired`` does not lose output.
    """
    start = time.monotonic()
    proc = subprocess.Popen(
        resolve_argv0(argv, env=env),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        preexec_fn=preexec_fn,  # optional rlimit caps (verifier, PR #59 §7 item 5)
        # own process group, so the group kill reaps children
        **_spawn_isolation_kwargs(),
        **_text_kwargs(),
    )
    _assign_windows_job(proc)
    watch = _build_vanish_watch(proc, watchdog_silence_s)
    if deadline is None and watch is None:
        try:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            # Second communicate() collects whatever the pipes still hold.
            stdout, stderr = _drain_after_kill(proc)
            timed_out = True
        vanished = False
    else:
        stdout, stderr, timed_out, vanished = _communicate_with_deadline(
            proc, stdin_text, deadline, timeout_s=timeout_s, watch=watch
        )
    return ProcessOutput(
        argv=list(argv),
        stdout=stdout or "",
        stderr=stderr or "",
        exit_code=proc.returncode,
        duration_s=time.monotonic() - start,
        timed_out=timed_out,
        agent_vanished=vanished,
    )


def _communicate_with_deadline(
    proc: subprocess.Popen,
    stdin_text: str | None,
    deadline,
    *,
    timeout_s: float | None = None,
    watch: "_VanishWatch | None" = None,
) -> tuple[str, str, bool, bool]:
    """Poll ``communicate`` until exit, credited-deadline lapse, or a vanish proof.

    ``deadline.remaining_s()`` is re-read each poll so a host suspension detected
    mid-wait (credited upward, FR-5.2) extends the wait instead of killing a
    healthy child; with ``deadline is None`` the plain ``timeout_s`` budget
    applies. On genuine expiry the process group is killed and the pipes
    drained, mirroring the single-shot path's kill/drain contract. Input is fed
    only on the first ``communicate`` call (subprocess requires this); retries
    pass ``None`` and continue reading without re-writing stdin.

    Each poll also consults the agent-liveness ``watch`` (#103): a child that is
    provably gone (reaped leader, empty group) with pipes still open past the
    silence bound stops the wait with ``vanished=True``. The drain there is
    BOUNDED (never a bare ``communicate()``): the pipe's write end is held by
    something outside the dead group by construction, so EOF may never come.
    """
    first = True
    start = time.monotonic()
    while True:
        if deadline is not None:
            remaining = deadline.remaining_s()
        else:
            remaining = timeout_s - (time.monotonic() - start)
        cadence = _DEADLINE_POLL_S if watch is None else min(
            _DEADLINE_POLL_S, watch.poll_s
        )
        poll = max(0.0, min(remaining, cadence))
        try:
            stdout, stderr = proc.communicate(
                input=stdin_text if first else None, timeout=poll
            )
            return stdout or "", stderr or "", False, False
        except subprocess.TimeoutExpired:
            first = False
            if watch is not None and watch.check():
                # Proof-gated: leader reaped + group empty + silence past the
                # bound. Best-effort group kill (usually a no-op on an empty
                # group), then a BOUNDED drain — the pipe never EOFing is the
                # premise, so an unbounded communicate() would hang forever.
                _kill_process_group(proc)
                stdout, stderr = _drain_bounded(proc)
                return stdout or "", stderr or "", False, True
            if deadline is not None:
                expired = deadline.expired()
            else:
                expired = (time.monotonic() - start) >= timeout_s
            if expired:
                _kill_process_group(proc)
                stdout, stderr = _drain_after_kill(proc)
                return stdout or "", stderr or "", True, False


def _drain_bounded(proc: subprocess.Popen) -> tuple[str | None, str | None]:
    """Collect buffered output with a hard bound, then force-close the pipes.

    Used only on the vanished-agent path (#103), where the write end of the
    child's pipes is provably held by nothing in the (empty) process group —
    i.e. by an escaped fd holder the kill cannot reach — so waiting for EOF
    could block forever. A short grace collects the common case (EOF arrives
    because nothing holds the pipe after all); past it the pipes are closed and
    whatever ``communicate`` had internally buffered is unrecoverable — an
    acceptable loss on a step that is about to park INTERRUPTED for a re-run.
    """
    try:
        return proc.communicate(timeout=_REAP_GRACE_S)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        return None, None


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
    watchdog_silence_s: float | None = None,
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
        resolve_argv0(argv, env=env),
        stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        bufsize=0,  # raw binary pipes; we frame + decode ourselves
        preexec_fn=preexec_fn,  # optional rlimit caps (verifier, PR #59 §7 item 5)
        **_spawn_isolation_kwargs(),
    )

    # Raw byte buffers, maintained independently of the line sink: every byte
    # read is appended here regardless of newline framing, so a trailing
    # non-terminated segment is still captured byte-for-byte in ProcessOutput.
    raw_stdout = bytearray()
    raw_stderr = bytearray()
    # Bytes of the current unframed stdout line (not yet newline-terminated).
    line_buf = bytearray()

    # Agent-liveness watchdog (#103): armed per spawn; every observed chunk is
    # progress, so it can only fire on a provably-gone child whose pipes went
    # silent past the bound without EOF.
    watch = _build_vanish_watch(proc, watchdog_silence_s)

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
        if watch is not None:
            watch.progress()  # any observed bytes are liveness progress (#103)

    timed_out = False
    vanished = False
    pending_exc: BaseException | None = None
    try:
        while open_tags:
            remaining = _stream_remaining(deadline, timeout_s, start)
            if remaining <= 0:
                timed_out = True
                break
            # Under a suspend-aware deadline the budget can be credited upward
            # mid-wait, so never block longer than the poll cadence without
            # re-evaluating it; the armed watchdog needs the same cadence to
            # poll child liveness. With neither, the select waits the full
            # remaining as before.
            if deadline is None and watch is None:
                sel_timeout = remaining
            else:
                cadence = _DEADLINE_POLL_S if watch is None else min(
                    _DEADLINE_POLL_S, watch.poll_s
                )
                sel_timeout = min(remaining, cadence)
            for key, _mask in sel.select(timeout=sel_timeout):
                if key.data == "stdin":
                    _feed_stdin()
                else:
                    _on_read(key, emit=True)
            # Agent-liveness watchdog (#103): proof-gated — a live child never
            # fires this, and EOF on both pipes exits the loop above without it.
            if watch is not None and watch.check():
                vanished = True
                break
        # stdout/stderr are at EOF, but the child may still be alive — it can
        # close fd 1 and fd 2 and then hang. The hard timeout has to cover
        # process liveness, not just pipe liveness (FR-3.3, F-001), so wait for
        # exit under whatever wall-clock budget remains. On expiry, fall through
        # to the same killpg/drain/reap teardown as the in-loop timeout instead
        # of blocking forever on the final ``proc.wait()``.
        if not timed_out and not vanished:
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
    if timed_out or vanished or pending_exc is not None:
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
        agent_vanished=vanished,
    )


def _decode_like_text_mode(raw: bytearray, enc: str) -> str:
    """Decode raw bytes exactly as ``subprocess(text=True)`` would.

    ``TextIOWrapper`` with the default ``newline=None`` applies universal
    newline translation on read — the same translation ``communicate()`` does
    — so the assembled string is byte-for-byte what the buffered path returns.
    """
    return io.TextIOWrapper(io.BytesIO(bytes(raw)), encoding=enc).read()


def _kill_process_group(proc: subprocess.Popen) -> None:
    if not _POSIX:
        _kill_process_tree_windows(proc)
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def _kill_process_tree_windows(proc: subprocess.Popen) -> None:
    """Windows group kill: the child's Job Object, then ``taskkill /T /F``.

    Agent CLIs spawn workers (often via a ``.cmd`` shim), so killing only the
    leader would orphan them. Terminating the job reaches every descendant,
    including ones whose parent already exited; ``taskkill /T`` covers a child
    that could not be put in a job (or a descendant started before the
    assignment landed). The leader is always killed as a backstop.
    """
    job_kill = getattr(proc, "_gauntlet_job_kill", None)
    if job_kill is not None:
        try:
            job_kill()
        except Exception:  # noqa: BLE001 — fall through to taskkill
            pass
    if proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_TASKKILL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass

"""Driver heartbeat, suspend detection, and stall classification (P2, FR-5).

The driver writes a small ``heartbeat.json`` into its run-instance dir every
``interval`` seconds carrying a monotonic + wallclock pair and its pid. A host
sleep (laptop lid) suspends the whole driver process — including the writer
thread — so on wake the *next* heartbeat lands late. Comparing two consecutive
heartbeats lets the engine tell a genuine suspension from a wedged step, credit
the suspended time back to the running step's deadline (bounded by a cap), and
render a truthful stalled-run classification in ``status`` (FR-5.1–5.3).

Everything decision-shaped here is a **pure function** over observables so it is
directly unit-testable with injected clock pairs (the FR-5 acceptance shape):

* :func:`detect_suspension` — the dual detector (primary clock-skew rule +
  platform-independent wallclock-cadence fallback) over a consecutive pair.
* :func:`classify_stall` — the disjoint, fail-closed state predicates
  (``host_suspended`` / ``driver_orphaned`` / ``agent_silent``).
* :class:`SuspendAwareDeadline` — the deadline accountant that credits detected
  suspension back to a step's timeout, capped, driven by injectable clocks.

The only stateful pieces are :class:`HeartbeatWriter` (a daemon thread that just
writes the file + accumulates detected intervals) and a process-global registry
of the active writer, which :mod:`gauntlet.adapters.process` polls for credit
without any plumbing through the adapter layer (the PRD's "single JSON file
polled by the deadline accountant" design).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# --- constants (Q5-ratified) -------------------------------------------------
HEARTBEAT_FILENAME = "heartbeat.json"
# The writer appends each detected interval here the instant it fires, so live
# `status` sees a just-ended sleep before the drive drains into the manifest and
# a `kill -9` mid-drive keeps the evidence (FR-5.1/5.3, data over inference).
SUSPENSIONS_LOG_FILENAME = "suspensions.jsonl"
# Cadence: a heartbeat every 15s (FR-5.1). The detector threshold (30s) is 2×
# the cadence so ordinary scheduler jitter between two writes never trips it.
DEFAULT_HEARTBEAT_INTERVAL_S = 15.0
SUSPEND_THRESHOLD_S = 30.0
# Past this much *credited* suspension a step still dies with halt_reason=timeout
# (FR-5.2) — the cap keeps the fail-closed property (a truly wedged step is not
# masked as sleep forever). Default 12h.
DEFAULT_SUSPEND_CREDIT_CAP_S = 12 * 3600.0
# A running step whose adapter child has produced no output for this long, on a
# healthy driver with a continuous clock, is `agent_silent` (FR-5.3). Default 5m.
DEFAULT_AGENT_SILENCE_S = 300.0
# Agent-liveness watchdog bound (issue #103): an in-flight CLI agent call whose
# output has been silent this long AND whose recorded pid is provably gone is
# self-converted into the interrupted → resume path. Deliberately far above the
# FR-5.3 advisory threshold: 40+ minute silent-but-working turns are normal and
# legitimate, and silence alone NEVER trips the watchdog — this bound only
# delays action once the agent process is already proven dead. ≤ 0 disables.
DEFAULT_AGENT_WATCHDOG_SILENCE_S = 20 * 60.0
# Watchdog poll cadence, and the confirmation window a provably-gone probe must
# survive (re-observed gone, same call still in flight) before the trip fires —
# so the benign instant between a child's exit and its waiter returning can
# never race the trip (fail open).
AGENT_WATCHDOG_POLL_S = 30.0
AGENT_WATCHDOG_CONFIRM_S = 60.0
# The run-instance sidecar recording the spawned agent's pid/pgid while a call
# is in flight (issue #103, "data over inference"): operators get the real pid
# to point forensics at — the agent is DETACHED from the driver's group, so
# group-scoped `ps` proves nothing — and a future `recover` can kill the
# recorded agent group instead of orphaning it.
AGENT_PROCESS_FILENAME = "agent-process.json"

# --- stall classifications (FR-5.3) ------------------------------------------
STALL_HOST_SUSPENDED = "host_suspended"
STALL_DRIVER_ORPHANED = "driver_orphaned"
STALL_AGENT_SILENT = "agent_silent"

# The wallclock stamp format for a heartbeat / a recorded suspension interval:
# UTC, hyphen-delimited, trailing Z — the same shape the run's other bookkeeping
# uses (run.RunManager._utc_stamp) so it is filesystem/JSON-safe and parseable.
_WALLCLOCK_FMT = "%Y-%m-%dT%H-%M-%SZ"


def format_wallclock(dt: datetime) -> str:
    """Render a UTC datetime as the heartbeat/suspension wallclock string."""
    return dt.astimezone(timezone.utc).strftime(_WALLCLOCK_FMT)


def parse_wallclock(text: str) -> datetime | None:
    """Parse a heartbeat/suspension wallclock string back to an aware datetime.

    Fail-closed: an unparseable value returns ``None`` so a torn/foreign stamp
    can never drive a suspension credit off a bogus delta.
    """
    try:
        return datetime.strptime(text, _WALLCLOCK_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class HeartbeatSample:
    """One heartbeat: a monotonic + wallclock pair plus the writer's pid."""

    monotonic_s: float
    wallclock_utc: str
    pid: int

    def to_dict(self) -> dict:
        return {
            "monotonic_s": self.monotonic_s,
            "wallclock_utc": self.wallclock_utc,
            "pid": self.pid,
        }

    @classmethod
    def from_dict(cls, data: object) -> "HeartbeatSample | None":
        """Parse a persisted heartbeat, or ``None`` if malformed (fail closed)."""
        if not isinstance(data, dict):
            return None
        mono = data.get("monotonic_s")
        wall = data.get("wallclock_utc")
        pid = data.get("pid")
        if not isinstance(mono, (int, float)) or isinstance(mono, bool):
            return None
        if not isinstance(wall, str) or not wall:
            return None
        if not isinstance(pid, int) or isinstance(pid, bool):
            return None
        return cls(monotonic_s=float(mono), wallclock_utc=wall, pid=pid)

    @classmethod
    def read(cls, path: Path) -> "HeartbeatSample | None":
        try:
            import json

            return cls.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError):
            return None


@dataclass(frozen=True)
class Suspension:
    """A detected host-suspension interval (manifest ``suspensions[]`` entry)."""

    start: str
    end: str
    gap_s: int

    def to_dict(self) -> dict:
        return {"start": self.start, "end": self.end, "gap_s": self.gap_s}

    @classmethod
    def from_dict(cls, data: object) -> "Suspension | None":
        """Parse a persisted interval, or ``None`` if malformed (fail closed)."""
        if not isinstance(data, dict):
            return None
        start = data.get("start")
        end = data.get("end")
        gap = data.get("gap_s")
        if not isinstance(start, str) or not isinstance(end, str):
            return None
        if not isinstance(gap, int) or isinstance(gap, bool):
            return None
        return cls(start=start, end=end, gap_s=gap)


def read_persisted_suspensions(run_dir: Path) -> list[Suspension]:
    """Read the intervals the heartbeat writer persisted live (FR-5.1/5.3).

    The append-only ``suspensions.jsonl`` is the run-safe store `status` reads so
    a just-ended sleep surfaces before the drive drains it into the manifest, and
    a crash mid-drive does not lose it. Fail-closed: a torn/foreign line is
    skipped rather than yielding a bogus interval. Absent file → empty list.
    """
    import json

    out: list[Suspension] = []
    try:
        text = (run_dir / SUSPENSIONS_LOG_FILENAME).read_text()
    except (OSError, ValueError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = Suspension.from_dict(json.loads(line))
        except ValueError:
            continue
        if parsed is not None:
            out.append(parsed)
    return out


def detect_suspension(
    prev: HeartbeatSample,
    cur: HeartbeatSample,
    *,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    threshold_s: float = SUSPEND_THRESHOLD_S,
) -> Suspension | None:
    """Detect a host suspension straddling two consecutive heartbeats (FR-5.1).

    Two detectors run over the same pair; either firing records the interval,
    whose duration is always ``Δwallclock``:

    * **Primary (clock skew).** ``Δwallclock − Δmonotonic > threshold`` — holds
      on platforms whose monotonic clock *excludes* suspend (the real time
      advanced far more than the monotonic clock saw).
    * **Fallback (wallclock cadence).** ``Δwallclock > interval + threshold`` —
      runs unconditionally so a platform whose monotonic *advances through*
      suspend (skew ≈ 0) is still caught: the writer wrote ``cur`` (so the driver
      is demonstrably alive, distinguishing this from ``driver_orphaned``), just
      far later than the cadence allows.

    Returns ``None`` for sub-threshold jitter, or when either stamp is
    unparseable / wallclock ran backwards (fail-closed: never credit off a bogus
    delta).
    """
    prev_wall = parse_wallclock(prev.wallclock_utc)
    cur_wall = parse_wallclock(cur.wallclock_utc)
    if prev_wall is None or cur_wall is None:
        return None
    d_wall = (cur_wall - prev_wall).total_seconds()
    if d_wall <= 0:
        return None  # clock stepped backwards (NTP) — not a suspension
    d_mono = cur.monotonic_s - prev.monotonic_s
    skew = d_wall - d_mono
    primary = skew > threshold_s
    fallback = d_wall > interval_s + threshold_s
    if not (primary or fallback):
        return None
    return Suspension(
        start=prev.wallclock_utc, end=cur.wallclock_utc, gap_s=int(round(d_wall))
    )


def classify_stall(
    *,
    pid_alive: bool,
    pair_gap_s: float | None,
    clock_skew: bool,
    hb_age_s: float | None,
    agent_output_age_s: float | None,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    threshold_s: float = SUSPEND_THRESHOLD_S,
    agent_silence_s: float = DEFAULT_AGENT_SILENCE_S,
) -> str | None:
    """Classify a running step's stall state from disjoint observables (FR-5.3).

    Inputs are sampled together at detection/status time (never a re-read):

    * ``pid_alive`` — liveness of the recorded heartbeat pid.
    * ``pair_gap_s`` / ``clock_skew`` — the width and skew-verdict of the latest
      persisted consecutive heartbeat pair (from :func:`detect_suspension`);
      ``clock_skew`` is ``True`` iff that pair fired a detector. ``None`` gap when
      no pair straddles the gap (the writer died mid-gap).
    * ``hb_age_s`` — age of the *latest* heartbeat (meaningful when no post-gap
      heartbeat was written); ``None`` when there is no heartbeat at all.
    * ``agent_output_age_s`` — time since the running step's adapter child last
      appended output; ``None`` when not applicable / no output yet.

    The predicates are disjoint and evaluated in order; anything not matched is
    ``None`` (a healthy in-progress step). The ambiguous shape — a live driver
    that stopped writing with **no** clock-skew evidence — is deliberately
    surfaced as ``agent_silent`` (hung), never ``host_suspended``: fail closed so
    a genuinely wedged step is never masked as sleep and never credited.
    """
    limit = interval_s + threshold_s
    hb_stale = hb_age_s is not None and hb_age_s > limit
    has_skew_pair = (
        clock_skew and pair_gap_s is not None and pair_gap_s > limit
    )
    # 1. host_suspended — a skew pair exists AND the driver survived (pid alive).
    if has_skew_pair and pid_alive:
        return STALL_HOST_SUSPENDED
    # 2. driver_orphaned — the writer died mid-gap (stale heartbeat, pid dead).
    if hb_stale and not pid_alive:
        return STALL_DRIVER_ORPHANED
    # 3. agent_silent — driver healthy + writing on schedule, agent producing
    #    nothing past the silence threshold; AND the fail-closed ambiguous shape
    #    (a live driver that stopped writing with no clock-skew evidence — blocked
    #    or wedged) is surfaced here as hung, not as sleep.
    if pid_alive:
        driver_writing = hb_age_s is not None and hb_age_s <= limit
        agent_starved = (
            agent_output_age_s is not None and agent_output_age_s > agent_silence_s
        )
        if driver_writing and agent_starved:
            return STALL_AGENT_SILENT
        if hb_stale:  # live driver, stale heartbeat, no skew → ambiguous → hung
            return STALL_AGENT_SILENT
    return None


class SuspendAwareDeadline:
    """A step deadline that credits detected host-suspension back, capped (FR-5.2).

    The step is allowed to run for ``timeout_s`` of *active* (non-suspended) time
    plus up to ``credit_cap_s`` of detected suspension. Expressed uniformly in
    wallclock terms so it is correct on both clock semantics:

        remaining = timeout_s + min(credited_suspension, cap) − wallclock_elapsed

    where ``credited_suspension`` is the larger of (a) the wallclock/monotonic
    skew this call's own clocks observed — which captures suspend on a monotonic-
    *excludes*-suspend platform without any heartbeat — and (b) the total the
    heartbeat writer detected via its cadence rule, which captures suspend on a
    monotonic-*advances-through* platform where (a) is ≈0. Past the cap the
    excess is not credited, so a truly wedged step still expires.
    """

    def __init__(
        self,
        *,
        timeout_s: float,
        credit_cap_s: float = DEFAULT_SUSPEND_CREDIT_CAP_S,
        detected_suspension_s: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.timeout_s = timeout_s
        self.credit_cap_s = credit_cap_s
        self._detected = detected_suspension_s or (lambda: 0.0)
        self._wall = wall_clock
        self._mono = monotonic_clock
        self._wall_start = wall_clock()
        self._mono_start = monotonic_clock()

    def _credited(self, wall_elapsed: float, mono_elapsed: float) -> float:
        own_skew = max(0.0, wall_elapsed - mono_elapsed)
        detected = max(0.0, self._detected())
        return min(max(own_skew, detected), self.credit_cap_s)

    def remaining_s(self) -> float:
        wall_elapsed = self._wall() - self._wall_start
        mono_elapsed = self._mono() - self._mono_start
        return self.timeout_s + self._credited(wall_elapsed, mono_elapsed) - wall_elapsed

    def expired(self) -> bool:
        return self.remaining_s() <= 0.0


# --- keep-awake (caffeinate) -------------------------------------------------
# Opt-in: altering host sleep behavior is an explicit human choice (CLAUDE.md
# machine-state rule / §7), so the default is off. On darwin `caffeinate -i`
# asserts an idle-sleep prevention; `-w <pid>` scopes the assertion to the
# driver's lifetime so it is released automatically when the driver exits — no
# lingering system-wide assertion.


def keep_awake_command(pid: int, *, enabled: bool, platform: str = sys.platform) -> list[str] | None:
    """The `caffeinate` command to keep the host awake for ``pid``, or ``None``.

    Returns ``None`` (no wrapper) when disabled, or on a non-darwin platform
    (where the flag is ignored with a warning by the caller). Kept pure so the
    FR-5.4 acceptance can assert command construction without spawning anything.
    """
    if not enabled or platform != "darwin":
        return None
    return ["caffeinate", "-i", "-w", str(pid)]


class KeepAwake:
    """Context manager that runs `caffeinate -i -w <pid>` for the driver's life.

    A no-op when disabled or off-darwin (the caller warns once on a non-darwin
    ``keep_awake: true``). The child is best-effort: a missing `caffeinate`
    binary is swallowed (the run proceeds without the assertion) rather than
    failing the run over a power hint.
    """

    def __init__(self, *, enabled: bool, pid: int | None = None,
                 platform: str = sys.platform) -> None:
        self.enabled = enabled
        self.platform = platform
        self.pid = pid if pid is not None else os.getpid()
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> "KeepAwake":
        argv = keep_awake_command(self.pid, enabled=self.enabled, platform=self.platform)
        if argv is None:
            return self
        try:
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            self._proc = None
        return self

    def __exit__(self, *exc) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
        except (ProcessLookupError, OSError):
            pass
        self._proc = None


# --- process-global active-heartbeat registry --------------------------------
# The adapters' subprocess wrapper (adapters/process.py) needs the running
# driver's suspension credit + cap, but it has no handle on the run-scoped
# writer. Rather than plumb a deadline object through every adapter, the writer
# registers itself here on start and clears on stop; process.py polls it. Only
# one driver runs per process (the worktree lock guarantees it), so a single
# slot is sufficient and there is no cross-run leakage.
_active_lock = threading.Lock()
_active: "HeartbeatWriter | None" = None


def build_active_deadline(timeout_s: float) -> SuspendAwareDeadline | None:
    """A deadline crediting the active driver's detected suspension, or ``None``.

    ``None`` when no heartbeat writer is active (tests, non-driver contexts, or a
    disabled heartbeat), so :func:`run_with_timeout` keeps its plain-timeout
    behavior byte-for-byte.
    """
    with _active_lock:
        writer = _active
    if writer is None:
        return None
    return SuspendAwareDeadline(
        timeout_s=timeout_s,
        credit_cap_s=writer.credit_cap_s,
        detected_suspension_s=writer.detected_suspension_s,
    )


class HeartbeatWriter:
    """A daemon thread that writes ``heartbeat.json`` and detects suspensions.

    The thread does no manifest I/O (the orchestrator is the sole manifest
    writer); it only writes the heartbeat file and accumulates detected
    :class:`Suspension` intervals in memory. The driver drains
    :meth:`drain_suspensions` into the manifest at safe points, and the deadline
    accountant reads :meth:`detected_suspension_s` for live credit.
    """

    def __init__(
        self,
        run_dir: Path,
        *,
        interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
        threshold_s: float = SUSPEND_THRESHOLD_S,
        credit_cap_s: float = DEFAULT_SUSPEND_CREDIT_CAP_S,
        monotonic_clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.path = run_dir / HEARTBEAT_FILENAME
        self.suspend_log = run_dir / SUSPENSIONS_LOG_FILENAME
        self.interval_s = interval_s
        self.threshold_s = threshold_s
        self.credit_cap_s = credit_cap_s
        self._mono = monotonic_clock
        self._wall = wall_clock or (lambda: datetime.now(timezone.utc))
        self._pid = os.getpid()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._prev: HeartbeatSample | None = None
        self._suspensions: list[Suspension] = []
        self._total_suspend_s = 0.0

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> "HeartbeatWriter":
        global _active
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._write_sample()  # an immediate first heartbeat, then cadence
        with _active_lock:
            _active = self
        self._thread = threading.Thread(
            target=self._loop, name="gauntlet-heartbeat", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        global _active
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval_s + 5.0)
        with _active_lock:
            if _active is self:
                _active = None

    def __enter__(self) -> "HeartbeatWriter":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- accessors ------------------------------------------------------------
    def detected_suspension_s(self) -> float:
        with self._lock:
            return self._total_suspend_s

    def drain_suspensions(self) -> list[Suspension]:
        """Return and clear the suspensions detected since the last drain."""
        with self._lock:
            drained = self._suspensions
            self._suspensions = []
            return drained

    # -- internals ------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            self._write_sample()

    def _write_sample(self) -> None:
        sample = HeartbeatSample(
            monotonic_s=self._mono(),
            wallclock_utc=format_wallclock(self._wall()),
            pid=self._pid,
        )
        self._persist(sample)
        with self._lock:
            prev = self._prev
            self._prev = sample
        if prev is not None:
            interval = detect_suspension(
                prev, sample, interval_s=self.interval_s, threshold_s=self.threshold_s
            )
            if interval is not None:
                with self._lock:
                    self._suspensions.append(interval)
                    self._total_suspend_s += interval.gap_s
                # Persist promptly so live `status` and a crash both see it
                # (FR-5.1/5.3), independent of the drive's manifest drain.
                self._append_suspension_log(interval)

    def _append_suspension_log(self, interval: "Suspension") -> None:
        """Append a detected interval to the run-safe log (best-effort, FR-5.1).

        Append-only + fsync so a live reader gets whole lines and a crash keeps
        the evidence. A failed append must never crash the heartbeat thread — the
        interval is still in memory and will drain into the manifest at drive
        exit, so the file is a best-effort live/crash mirror, not the source.
        """
        import json

        try:
            with open(self.suspend_log, "a") as fh:
                fh.write(json.dumps(interval.to_dict()) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            pass

    def _persist(self, sample: HeartbeatSample) -> None:
        import json

        payload = json.dumps(sample.to_dict(), indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".hb-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise


# --- agent-liveness watchdog (issue #103) ------------------------------------
class AgentLivenessWatchdog:
    """A daemon thread that self-recovers a driver wedged on a vanished agent.

    Issue #103, twice-observed: an agent-executing step's process/stream died
    but the wait never returned — the driver blocked forever at 0% CPU while
    ``status`` reported ``in_progress`` and the FR-5.3 classifier said
    ``agent_silent``, which nothing acted on. A human had to run forensics and
    apply ``gauntlet recover`` by hand. This watchdog converts that wedge into
    the normal ``interrupted → resume`` path from INSIDE the driver.

    It acts ONLY when every one of these holds, sampled together on each poll:

    * a CLI agent call is in flight (:func:`adapters.process.active_agent_probe`);
    * its output has been silent past ``silence_s`` (config
      ``agent_watchdog_silence_s``, default 20 min);
    * the RECORDED agent pid is provably gone
      (:meth:`~gauntlet.adapters.process.AgentCallProbe.agent_gone` — child
      reaped, or ``kill -0`` proves no such process). The agent runs DETACHED
      in its own group, so driver-group emptiness / socket silence are NOT
      evidence and are never consulted (the #103 second-occurrence false
      positive);
    * all of it re-observed, same call still in flight, across ``confirm_s``.

    Everything else fails open — a silent-but-live agent (40+ minute working
    turns are legitimate), an API-mode call (no subprocess, so liveness cannot
    be disproven — no probe is ever registered), any probe error: the watchdog
    keeps waiting, and a healthy long-running agent is never killed.

    ``on_trip`` is invoked at most once, with the tripped probe (so the
    action can re-verify the SAME call is still in flight); the RunManager
    wires it to the same FR-5.6 machinery ``gauntlet recover`` applies from
    outside, so every existing resume/recovery semantic holds unchanged.
    ``record_path`` (optional) is the :data:`AGENT_PROCESS_FILENAME` sidecar
    this thread keeps current — best-effort forensic data, never a safety
    input.
    """

    def __init__(
        self,
        *,
        silence_s: float,
        on_trip: Callable[[object], object],
        poll_s: float = AGENT_WATCHDOG_POLL_S,
        confirm_s: float = AGENT_WATCHDOG_CONFIRM_S,
        record_path: Path | None = None,
        probe_source: "Callable[[], object | None] | None" = None,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.silence_s = silence_s
        self.on_trip = on_trip
        self.poll_s = poll_s
        self.confirm_s = confirm_s
        self.record_path = record_path
        self._probe_source = probe_source or self._default_probe_source
        self._mono = monotonic_clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._observed: object | None = None
        self._gone_since: float | None = None
        self._recorded_pid: int | None = None

    @staticmethod
    def _default_probe_source() -> "object | None":
        # Imported lazily so the engine module graph never hard-couples to the
        # adapter layer at import time (mirrors process.py's inverse import).
        from gauntlet.adapters import process as P

        return P.active_agent_probe()

    # -- lifecycle -------------------------------------------------------------
    def start(self) -> "AgentLivenessWatchdog":
        self._thread = threading.Thread(
            target=self._loop, name="gauntlet-agent-watchdog", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_s + 5.0)
        self._clear_record()

    def __enter__(self) -> "AgentLivenessWatchdog":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- internals -------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.wait(self.poll_s):
            self._poll_once()

    def _poll_once(self) -> None:
        """One watchdog observation; every uncertainty resets and fails open."""
        if self._stop.is_set():
            return  # stopped or already tripped — one-shot, never re-armed
        try:
            probe = self._probe_source()
            self._record_probe(probe)
            if probe is None or probe is not self._observed:
                # No call in flight, or a NEW call since the last poll: start
                # fresh — trip evidence never carries across attempts.
                self._observed = probe
                self._gone_since = None
                return
            if probe.silence_s() < self.silence_s or not probe.agent_gone():
                self._gone_since = None
                return
            now = self._mono()
            if self._gone_since is None:
                self._gone_since = now  # first gone observation: arm, don't act
                return
            if now - self._gone_since < self.confirm_s:
                return
            # Confirmed: same call, silent past the bound, pid provably gone
            # across the window. Re-read the registry once more so a call that
            # concluded in the last instant stands the watchdog down.
            if self._probe_source() is not probe:
                self._observed = None
                self._gone_since = None
                return
            self._stop.set()  # one-shot: never a second trip attempt
            self.on_trip(probe)
        except Exception:
            # Fail open: an internal error must never trip, crash the thread,
            # or count toward gone-ness.
            self._gone_since = None

    def _record_probe(self, probe: "object | None") -> None:
        """Keep the ``agent-process.json`` sidecar current (best-effort)."""
        if self.record_path is None:
            return
        try:
            if probe is None:
                self._clear_record()
                return
            pid = getattr(probe, "pid", None)
            pgid = getattr(probe, "pgid", None)
            if not isinstance(pid, int) or pid == self._recorded_pid:
                return
            import json

            self.record_path.write_text(
                json.dumps(
                    {
                        "pid": pid,
                        "pgid": pgid if isinstance(pgid, int) else pid,
                        "recorded_at": format_wallclock(
                            datetime.now(timezone.utc)
                        ),
                    },
                    indent=2,
                )
            )
            self._recorded_pid = pid
        except OSError:
            pass  # forensic data only — never a reason to fail the watchdog

    def _clear_record(self) -> None:
        if self.record_path is None or self._recorded_pid is None:
            return
        try:
            os.unlink(self.record_path)
        except OSError:
            pass
        self._recorded_pid = None

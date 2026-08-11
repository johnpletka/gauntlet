"""Agent-liveness watchdog (issue #103): self-recovery from a vanished agent.

A driver can block forever on an agent call whose process died without the wait
ever returning — observed twice on run `job-platform-base`: 0% CPU, no agent
process, FR-5.3 saying `agent_silent`, and nothing acting on it. The watchdog
converts that wedge into the normal ``interrupted → resume`` path. It is tested
adversarially around its two load-bearing properties:

* **Fail open on uncertainty:** silence alone NEVER trips it (40+ minute
  silent-but-working turns are legitimate); only the RECORDED agent pid being
  provably gone — child reaped, or ``kill -0`` ESRCH — plus silence past the
  bound, re-confirmed on the same in-flight call, may act. The agent runs
  DETACHED in its own process group (``start_new_session``), so driver-group
  emptiness and driver-socket silence are NOT evidence and are never consulted
  (the #103 second-occurrence false positive: a healthy detached builder is
  invisible to group-scoped forensics).
* **Recover-parity:** the trip applies the exact FR-5.6 machinery `gauntlet
  recover` uses — durable intent → finalize (step INTERRUPTED + §6.4 record +
  intent cleared + lock released) — so every existing resume/recovery semantic
  holds unchanged.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from gauntlet.adapters import process as P
from gauntlet.engine import heartbeat as HB
from gauntlet.engine import manifest as M
from gauntlet.engine.heartbeat import AgentLivenessWatchdog
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.run import (
    DRIVING_LOCK_NAME,
    RECOVERY_INTENT_NAME,
    RunManager,
    _LockRecord,
)
from gauntlet.procident import read_process_identity

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

THIS_HOST = socket.gethostname()


@pytest.fixture(autouse=True)
def _clean_probe_slot():
    """The probe registry is process-global; never leak one across tests."""
    P._clear_active_probe()
    yield
    P._clear_active_probe()


@pytest.fixture
def procs():
    """Spawn session-leader subprocesses and reap them at teardown."""
    spawned: list[subprocess.Popen] = []

    def make(code: str = "import time; time.sleep(120)") -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            start_new_session=True,  # own process group, pgid == pid
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spawned.append(proc)
        return proc

    yield make
    for proc in spawned:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except OSError:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def _mgr(root: Path, *, extra_config: str = "") -> RunManager:
    (root / ".gauntlet").mkdir(parents=True, exist_ok=True)
    (root / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML + extra_config)
    return RunManager(root)


def _setup_run(
    root: Path,
    *,
    slug: str = "demo",
    run_id: str = "run-1",
    step_status: str = M.RUNNING,
) -> Path:
    slug_dir = root / "runs" / slug
    run_dir = slug_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    man = Manifest(
        run_id=run_id,
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_RUNNING,
        current_step="implement",
        steps=[StepRecord(id="implement", type="agent_task", status=step_status)],
    )
    man.write_atomic(run_dir / "manifest.json")
    (slug_dir / "active-run.txt").write_text(run_id)
    return run_dir


def _write_own_lock(root: Path, *, nonce: str = "wd-nonce") -> _LockRecord:
    """The degenerate self-form of the FR-5.1 gate: OUR OWN live lock record."""
    ident = read_process_identity(os.getpid())
    rec = _LockRecord(
        nonce=nonce,
        slug="demo",
        run_id="run-1",
        pid=os.getpid(),
        pgid=os.getpgid(os.getpid()),
        started_at="2026-08-10T00-00-00",
        host=THIS_HOST,
        proc_identity=ident.to_dict() if ident else None,
    )
    lp = root / "runs" / DRIVING_LOCK_NAME
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(rec.to_json())
    return rec


class _FakeProbe:
    """A stand-in for AgentCallProbe with settable observables."""

    def __init__(self, *, silence: float = 0.0, gone: bool = False,
                 pid: int = 4242) -> None:
        self.silence = silence
        self.gone = gone
        self.pid = pid
        self.pgid = pid

    def silence_s(self) -> float:
        return self.silence

    def agent_gone(self) -> bool:
        return self.gone


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _watchdog(source, *, on_trip, clock=None, silence_s=600.0, confirm_s=60.0):
    return AgentLivenessWatchdog(
        silence_s=silence_s,
        on_trip=on_trip,
        confirm_s=confirm_s,
        probe_source=source,
        monotonic_clock=clock or _Clock(),
    )


# ---- the in-flight probe (adapters/process.py) -------------------------------


def test_probe_registered_during_call_and_cleared_after():
    """`run_with_timeout` exposes the child pid while — and only while — in flight."""
    seen: list = []

    def sink(line: str) -> None:
        seen.append(P.active_agent_probe())

    out = P.run_with_timeout(
        [sys.executable, "-u", "-c", "print('hello')"],
        timeout_s=30.0,
        sink=sink,
    )
    assert out.exit_code == 0
    assert seen and seen[0] is not None  # registered while the call ran
    assert seen[0].pid == seen[0].pgid  # start_new_session: child leads its group
    assert P.active_agent_probe() is None  # cleared unconditionally on exit


def test_probe_cleared_after_buffered_call():
    out = P.run_with_timeout(
        [sys.executable, "-c", "pass"], timeout_s=30.0,
    )
    assert out.exit_code == 0
    assert P.active_agent_probe() is None


def test_agent_gone_only_when_pid_provably_dead(procs):
    """A LIVE detached child is never 'gone' — group/socket views are not used."""
    proc = procs()
    probe = P.AgentCallProbe(proc)
    assert probe.agent_gone() is False  # healthy detached agent: fail open
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=10)  # reaped: pid provably no longer exists, group empty
    assert probe.agent_gone() is True


def test_agent_gone_fails_open_while_a_group_worker_survives():
    """A dead leader whose forked worker still owns the attempt is NOT gone.

    Buffered calls cannot observe the worker's output, so without the
    group-empty conjunct a leader-exits-early pattern would read as a wedge
    and a healthy attempt would be killed — the exact class of false positive
    issue #103's second occurrence warned about.
    """
    import time

    # The leader forks a same-group worker, prints "ready", then sleeps; the
    # readline below makes the worker's existence deterministic, not timed.
    proc = subprocess.Popen(
        [
            sys.executable, "-u", "-c",
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(120)']); "
            "print('ready'); time.sleep(120)",
        ],
        start_new_session=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        probe = P.AgentCallProbe(proc)
        os.kill(proc.pid, signal.SIGKILL)  # kill ONLY the leader, not the group
        proc.wait(timeout=10)
        assert probe.agent_gone() is False  # the worker keeps the group alive
        os.killpg(proc.pid, signal.SIGKILL)  # now take the whole group
        deadline = time.time() + 10
        while time.time() < deadline:
            if probe.agent_gone():
                break
            time.sleep(0.1)
        assert probe.agent_gone() is True
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except Exception:
            pass


def test_probe_silence_runs_from_last_touch():
    clock = _Clock(100.0)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        probe = P.AgentCallProbe(proc, monotonic_clock=clock)
        clock.now = 130.0
        assert probe.silence_s() == pytest.approx(30.0)
        probe.touch()
        assert probe.silence_s() == pytest.approx(0.0)
    finally:
        proc.wait(timeout=10)


def test_kill_active_agent_group_takes_the_detached_group(procs):
    """The termination-path guarantee: no orphaned agent survives the driver."""
    proc = procs()
    P._register_probe(proc)
    try:
        P.kill_active_agent_group()
        assert proc.wait(timeout=10) != 0  # SIGKILLed, not still sleeping
    finally:
        P._clear_active_probe()


# ---- the watchdog decision loop (engine/heartbeat.py) ------------------------


def test_no_probe_never_trips():
    trips: list[float] = []
    wd = _watchdog(lambda: None, on_trip=trips.append)
    for _ in range(10):
        wd._poll_once()
    assert trips == []


def test_silent_but_live_agent_never_trips():
    """The load-bearing fail-open: silence alone is NEVER a trip condition."""
    trips: list[float] = []
    probe = _FakeProbe(silence=99_999.0, gone=False)  # a 27h silent worker
    clock = _Clock()
    wd = _watchdog(lambda: probe, on_trip=trips.append, clock=clock)
    for _ in range(20):
        wd._poll_once()
        clock.now += 3600.0
    assert trips == []


def test_gone_but_recently_talking_agent_never_trips():
    trips: list[float] = []
    probe = _FakeProbe(silence=1.0, gone=True)
    wd = _watchdog(lambda: probe, on_trip=trips.append)
    for _ in range(10):
        wd._poll_once()
    assert trips == []


def test_trip_requires_confirmed_goneness_on_the_same_call():
    trips: list = []
    probe = _FakeProbe(silence=700.0, gone=True)
    clock = _Clock()
    wd = _watchdog(lambda: probe, on_trip=trips.append, clock=clock,
                   silence_s=600.0, confirm_s=60.0)
    wd._poll_once()  # observe the call
    wd._poll_once()  # first gone observation: armed, not acted
    assert trips == []
    clock.now = 30.0
    wd._poll_once()  # inside the confirmation window: still armed
    assert trips == []
    clock.now = 61.0
    wd._poll_once()  # confirmed → one-shot trip, handed the tripped probe
    assert trips == [probe]
    clock.now = 200.0
    wd._poll_once()  # stopped: never a second trip attempt
    assert trips == [probe]


def test_new_call_resets_the_confirmation_window():
    trips: list = []
    first = _FakeProbe(silence=700.0, gone=True)
    holder = {"probe": first}
    clock = _Clock()
    wd = _watchdog(lambda: holder["probe"], on_trip=trips.append, clock=clock,
                   silence_s=600.0, confirm_s=60.0)
    wd._poll_once()
    wd._poll_once()  # armed on the first call
    holder["probe"] = _FakeProbe(silence=700.0, gone=True)  # a NEW attempt
    clock.now = 120.0
    wd._poll_once()  # trip evidence never carries across attempts
    assert trips == []


def test_call_concluding_at_the_last_instant_stands_down():
    trips: list[float] = []
    probe = _FakeProbe(silence=700.0, gone=True)
    calls = {"n": 0}
    clock = _Clock()

    def source():
        # The registry empties between the confirmation and the final re-read
        # (the adapter returned after all): the trip must stand down.
        calls["n"] += 1
        if clock.now >= 61.0 and calls["n"] % 2 == 0:
            return None
        return probe

    wd = _watchdog(source, on_trip=trips.append, clock=clock,
                   silence_s=600.0, confirm_s=60.0)
    wd._poll_once()
    wd._poll_once()
    clock.now = 61.0
    wd._poll_once()
    assert trips == []


def test_probe_errors_fail_open():
    trips: list[float] = []

    def source():
        raise RuntimeError("boom")

    wd = _watchdog(source, on_trip=trips.append)
    for _ in range(5):
        wd._poll_once()  # swallowed; never trips, never crashes
    assert trips == []


def test_sidecar_records_and_clears_the_agent_process(tmp_path):
    """`agent-process.json` names the DETACHED agent pid for forensics (#103)."""
    record = tmp_path / HB.AGENT_PROCESS_FILENAME
    probe = _FakeProbe(silence=0.0, gone=False, pid=7777)
    holder = {"probe": probe}
    wd = _watchdog(lambda: holder["probe"], on_trip=lambda s: None)
    wd.record_path = record
    wd._poll_once()
    assert record.exists()
    import json

    data = json.loads(record.read_text())
    assert data["pid"] == 7777
    assert data["pgid"] == 7777
    holder["probe"] = None
    wd._poll_once()
    assert not record.exists()


def test_thread_lifecycle_starts_and_stops():
    wd = AgentLivenessWatchdog(
        silence_s=600.0, on_trip=lambda s: None, poll_s=0.05,
        probe_source=lambda: None,
    )
    with wd:
        assert any(
            t.name == "gauntlet-agent-watchdog" for t in threading.enumerate()
        )
    assert not any(
        t.name == "gauntlet-agent-watchdog" and t.is_alive()
        for t in threading.enumerate()
    )


# ---- the trip action: recover-parity self-interrupt (engine/run.py) ----------


def test_self_interrupt_finalizes_exactly_like_recover(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_own_lock(tmp_path, nonce="wd-nonce")
    probe = _FakeProbe(silence=1234.0, gone=True)
    monkeypatch.setattr(P, "active_agent_probe", lambda: probe)

    intent = mgr._watchdog_self_interrupt(run_dir, probe)

    assert intent is not None
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_FAILED
    step = man.record("implement")
    assert step.status == M.INTERRUPTED
    assert step.halt_reason == M.HALT_REASON_OPERATOR_RECOVER
    assert len(man.recoveries) == 1
    rec = man.recoveries[0]
    assert rec.actor == "agent-liveness-watchdog"
    assert rec.actor_source == "driver_watchdog"
    assert rec.signal_outcome == M.SIGNAL_ALREADY_DEAD
    assert rec.lock_nonce == "wd-nonce"
    assert rec.pid == os.getpid()
    assert rec.prior_step_status == M.RUNNING
    assert rec.prior_run_status == M.RUN_RUNNING
    assert rec.resulting_step_status == M.INTERRUPTED
    assert "issue #103" in (rec.reason or "")
    # FR-5.6 steps 7–8 exactly as `recover` leaves them: intent cleared, the
    # wedged driver's lock released under the nonce guard.
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()
    assert not (tmp_path / "runs" / DRIVING_LOCK_NAME).exists()


def test_self_interrupt_stands_down_when_lock_is_not_ours(tmp_path, monkeypatch):
    """Only OUR OWN live lock authorizes a self-interrupt (fail open)."""
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    ident = read_process_identity(os.getpid())
    rec = _LockRecord(
        nonce="foreign", slug="demo", run_id="run-1",
        pid=2_000_000_000, pgid=2_000_000_000,
        started_at="2026-08-10T00-00-00", host=THIS_HOST,
        proc_identity=ident.to_dict() if ident else None,
    )
    (tmp_path / "runs" / DRIVING_LOCK_NAME).write_text(rec.to_json())
    probe = _FakeProbe(silence=1234.0, gone=True)
    monkeypatch.setattr(P, "active_agent_probe", lambda: probe)

    assert mgr._watchdog_self_interrupt(run_dir, probe) is None
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_RUNNING
    assert man.record("implement").status == M.RUNNING
    assert man.recoveries == []
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


def test_self_interrupt_stands_down_without_a_running_step(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path, step_status=M.DONE)
    _write_own_lock(tmp_path)
    probe = _FakeProbe(silence=1234.0, gone=True)
    monkeypatch.setattr(P, "active_agent_probe", lambda: probe)

    assert mgr._watchdog_self_interrupt(run_dir, probe) is None
    man = Manifest.load(run_dir / "manifest.json")
    assert man.record("implement").status == M.DONE
    assert man.recoveries == []
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()


def test_self_interrupt_stands_down_when_the_call_concluded(tmp_path, monkeypatch):
    """The post-intent probe re-check: a concluded/replaced call unwinds cleanly."""
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    _write_own_lock(tmp_path)
    # The registry now holds a DIFFERENT (new) call than the tripped one — the
    # strictest form of "the wedged call is no longer in flight".
    monkeypatch.setattr(
        P, "active_agent_probe", lambda: _FakeProbe(silence=0.0, gone=False)
    )
    tripped = _FakeProbe(silence=1234.0, gone=True)

    assert mgr._watchdog_self_interrupt(run_dir, tripped) is None
    man = Manifest.load(run_dir / "manifest.json")
    assert man.status == M.RUN_RUNNING
    assert man.record("implement").status == M.RUNNING
    assert man.recoveries == []
    assert not (run_dir / RECOVERY_INTENT_NAME).exists()  # unlinked, not stranded


# ---- drive wiring (engine/run.py) --------------------------------------------


def test_watchdog_context_runs_thread_and_restores_sigterm(tmp_path):
    mgr = _mgr(tmp_path)
    run_dir = _setup_run(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    with mgr._agent_watchdog(run_dir):
        assert any(
            t.name == "gauntlet-agent-watchdog" for t in threading.enumerate()
        )
        # The SIGTERM forwarder is installed so an outside kill (recover's
        # SIGTERM) takes the detached agent group with the driver.
        assert signal.getsignal(signal.SIGTERM) is not before
    assert signal.getsignal(signal.SIGTERM) is before


def test_watchdog_disabled_by_config_bound(tmp_path):
    mgr = _mgr(tmp_path, extra_config="agent_watchdog_silence_s: 0\n")
    run_dir = _setup_run(tmp_path)
    with mgr._agent_watchdog(run_dir):
        assert not any(
            t.name == "gauntlet-agent-watchdog" and t.is_alive()
            for t in threading.enumerate()
        )

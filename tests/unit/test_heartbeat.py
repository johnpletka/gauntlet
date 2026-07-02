"""Driver heartbeat, suspend detection, stall classification, deadline (FR-5, P2).

Pure-function coverage of the FR-5 acceptance shapes (injected clock pairs /
observable tuples), plus the keep-awake command construction (FR-5.4) and the
auto-resume decision (FR-3.4).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gauntlet.engine import heartbeat as HB
from gauntlet.engine.manifest import ScheduledResume
from gauntlet.engine.run import (
    AUTO_RESUME_EXHAUST,
    AUTO_RESUME_NONE,
    AUTO_RESUME_RESUME,
    AUTO_RESUME_WAIT,
    next_auto_resume_action,
)

T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _sample(mono: float, at: datetime, pid: int = 4242) -> HB.HeartbeatSample:
    return HB.HeartbeatSample(
        monotonic_s=mono, wallclock_utc=HB.format_wallclock(at), pid=pid
    )


# --- FR-5.1: detect_suspension (primary + fallback + jitter) -----------------
def test_primary_skew_detector_fires_when_monotonic_excludes_suspend():
    # 40-minute wallclock gap; monotonic advanced only the cadence (suspend NOT
    # counted) → large skew → primary rule fires, interval width is Δwallclock.
    prev = _sample(100.0, T0)
    cur = _sample(115.0, T0 + timedelta(minutes=40))
    s = HB.detect_suspension(prev, cur)
    assert s is not None
    assert s.gap_s == 2400
    assert s.start == prev.wallclock_utc and s.end == cur.wallclock_utc


def test_fallback_cadence_detector_fires_when_monotonic_advances_through_suspend():
    # 40-minute gap; monotonic advanced through the suspend (skew ≈ 0). The
    # primary rule would miss it; the wallclock-cadence fallback catches it.
    prev = _sample(100.0, T0)
    cur = _sample(100.0 + 2400.0, T0 + timedelta(minutes=40))
    s = HB.detect_suspension(prev, cur)
    assert s is not None
    assert s.gap_s == 2400


def test_subthreshold_jitter_records_nothing():
    prev = _sample(100.0, T0)
    cur = _sample(116.0, T0 + timedelta(seconds=16))  # ~cadence + jitter
    assert HB.detect_suspension(prev, cur) is None


def test_backwards_wallclock_is_not_a_suspension():
    prev = _sample(100.0, T0)
    cur = _sample(115.0, T0 - timedelta(seconds=5))  # NTP step back
    assert HB.detect_suspension(prev, cur) is None


# --- FR-5.3: classify_stall (three states + fail-closed ambiguous) -----------
def test_classify_host_suspended_reads_pair_even_with_fresh_heartbeat():
    # The driver survived the sleep and wrote the post-wake heartbeat (fresh age),
    # while the straddling pair carries the skew. Classification reads the pair,
    # not the live file age, and credits.
    got = HB.classify_stall(
        pid_alive=True, pair_gap_s=2400, clock_skew=True,
        hb_age_s=2.0, agent_output_age_s=2400.0,
    )
    assert got == HB.STALL_HOST_SUSPENDED


def test_classify_driver_orphaned_when_writer_died_mid_gap():
    got = HB.classify_stall(
        pid_alive=False, pair_gap_s=None, clock_skew=False,
        hb_age_s=3600.0, agent_output_age_s=None,
    )
    assert got == HB.STALL_DRIVER_ORPHANED


def test_classify_agent_silent_when_driver_healthy_but_output_starved():
    got = HB.classify_stall(
        pid_alive=True, pair_gap_s=None, clock_skew=False,
        hb_age_s=5.0, agent_output_age_s=600.0, agent_silence_s=300.0,
    )
    assert got == HB.STALL_AGENT_SILENT


def test_classify_ambiguous_stale_heartbeat_fails_closed_to_agent_silent():
    # A live driver that stopped writing with NO clock-skew evidence — the shape a
    # bare timeout would misread as suspend — must be agent_silent (hung), never
    # host_suspended, and credit nothing.
    hung = HB.classify_stall(
        pid_alive=True, pair_gap_s=None, clock_skew=False,
        hb_age_s=3600.0, agent_output_age_s=None,
    )
    assert hung == HB.STALL_AGENT_SILENT
    # The SAME stale gap that DOES carry clock_skew flips to host_suspended —
    # proving the skew predicate is what separates sleep from a hang.
    slept = HB.classify_stall(
        pid_alive=True, pair_gap_s=3600, clock_skew=True,
        hb_age_s=3600.0, agent_output_age_s=None,
    )
    assert slept == HB.STALL_HOST_SUSPENDED


def test_classify_healthy_run_is_none():
    assert HB.classify_stall(
        pid_alive=True, pair_gap_s=None, clock_skew=False,
        hb_age_s=5.0, agent_output_age_s=10.0,
    ) is None


# --- FR-5.2: SuspendAwareDeadline (credit + cap) -----------------------------
class _Clock:
    """A programmable wall+monotonic clock pair for deadline tests."""

    def __init__(self):
        self.wall = 0.0
        self.mono = 0.0

    def w(self) -> float:
        return self.wall

    def m(self) -> float:
        return self.mono


def test_deadline_credits_a_suspension_within_the_cap():
    # 600s timeout; a 1h host suspend that monotonic EXCLUDED (wall jumped, mono
    # did not) is credited back, so the step is not expired mid-work.
    clk = _Clock()
    dl = HB.SuspendAwareDeadline(
        timeout_s=600.0, credit_cap_s=12 * 3600.0,
        wall_clock=clk.w, monotonic_clock=clk.m,
    )
    clk.wall = 3600.0 + 100.0  # 1h suspend + 100s of active work
    clk.mono = 100.0           # monotonic saw only the 100s of active work
    assert not dl.expired()
    assert dl.remaining_s() == 500.0  # 600 - 100 active seconds


def test_deadline_expires_past_the_credit_cap():
    # Same 1h gap but the cap is below it: the excess is not credited and the
    # step expires (fail-closed — a truly wedged step still dies).
    clk = _Clock()
    dl = HB.SuspendAwareDeadline(
        timeout_s=600.0, credit_cap_s=1800.0,  # 30-min cap < 1h gap
        wall_clock=clk.w, monotonic_clock=clk.m,
    )
    clk.wall = 3600.0 + 100.0
    clk.mono = 100.0
    assert dl.expired()


def test_deadline_credits_detected_suspension_on_advancing_monotonic():
    # Fallback platform: monotonic advanced through the suspend (wall == mono, so
    # own-skew ≈ 0), but the heartbeat writer detected the gap and the deadline
    # credits it from the detected source.
    clk = _Clock()
    dl = HB.SuspendAwareDeadline(
        timeout_s=600.0, credit_cap_s=12 * 3600.0,
        detected_suspension_s=lambda: 3600.0,
        wall_clock=clk.w, monotonic_clock=clk.m,
    )
    clk.wall = 3600.0 + 100.0
    clk.mono = 3600.0 + 100.0  # monotonic advanced through the suspend
    assert not dl.expired()
    assert dl.remaining_s() == 500.0


# --- FR-5.4: keep_awake command construction ---------------------------------
def test_keep_awake_command_on_darwin_when_enabled():
    assert HB.keep_awake_command(999, enabled=True, platform="darwin") == [
        "caffeinate", "-i", "-w", "999"
    ]


def test_keep_awake_command_none_when_disabled_or_off_darwin():
    assert HB.keep_awake_command(999, enabled=False, platform="darwin") is None
    assert HB.keep_awake_command(999, enabled=True, platform="linux") is None


# --- FR-3.4: next_auto_resume_action -----------------------------------------
def test_auto_resume_action_none_without_schedule():
    assert next_auto_resume_action(None, T0) == (AUTO_RESUME_NONE, 0.0)


def test_auto_resume_action_resume_when_due():
    sr = ScheduledResume(attempt_at=(T0 - timedelta(seconds=1)).isoformat(),
                         attempts=0, max_attempts=3)
    action, _ = next_auto_resume_action(sr, T0)
    assert action == AUTO_RESUME_RESUME


def test_auto_resume_action_wait_when_ahead():
    sr = ScheduledResume(attempt_at=(T0 + timedelta(seconds=120)).isoformat(),
                         attempts=0, max_attempts=3)
    action, wait_s = next_auto_resume_action(sr, T0)
    assert action == AUTO_RESUME_WAIT
    assert wait_s == 120.0


def test_auto_resume_action_exhaust_at_ceiling():
    sr = ScheduledResume(attempt_at=(T0 - timedelta(seconds=1)).isoformat(),
                         attempts=3, max_attempts=3)
    action, _ = next_auto_resume_action(sr, T0)
    assert action == AUTO_RESUME_EXHAUST


# --- HeartbeatWriter: write + detect + drain (no thread; deterministic) -------
def test_writer_persists_sample_and_drains_detected_suspension(tmp_path):
    clk = _Clock()
    walls = [T0, T0 + timedelta(minutes=40)]
    monos = [100.0, 115.0]  # monotonic excludes the suspend → primary detector

    def next_wall() -> datetime:
        return walls[min(idx[0], len(walls) - 1)]

    def next_mono() -> float:
        return monos[min(idx[0], len(monos) - 1)]

    idx = [0]
    w = HB.HeartbeatWriter(tmp_path, monotonic_clock=next_mono, wall_clock=next_wall)
    w._write_sample()               # first sample
    assert (tmp_path / HB.HEARTBEAT_FILENAME).exists()
    idx[0] = 1
    w._write_sample()               # second sample straddles the gap
    drained = w.drain_suspensions()
    assert len(drained) == 1 and drained[0].gap_s == 2400
    assert w.drain_suspensions() == []  # drained once, cleared
    # the deadline credit source reflects the detected total
    assert w.detected_suspension_s() == 2400.0

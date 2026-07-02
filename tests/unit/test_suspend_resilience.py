"""Suspend/sleep resilience wiring (harness-efficiency P2, FR-5 + FR-3.4).

Covers the engine-side integration the pure tests in ``test_heartbeat.py`` do
not: the ``halt_reason=timeout`` stamp on the deadline halt (FR-5.2/FR-7.2), the
config knobs + auto-resume load warning (FR-3.4/FR-5.4), the ``status --json``
suspension block (FR-5.3), and the in-process auto-resume loop (FR-3.4).
"""

from __future__ import annotations

import contextlib
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gauntlet.adapters.base import (
    FAILURE_TRANSIENT_USAGE_LIMIT,
    AdapterCapabilities,
    AgentFailedError,
    AgentResult,
    AgentTimeoutError,
    FailureInfo,
    Usage,
)
from gauntlet.engine import heartbeat as HB
from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, ScheduledResume, StepRecord
from gauntlet.engine.run import RunManager

from test_orchestrator import _build

PIPE = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, output: out.txt, prompt_text: do the real work}
"""


def _manifest() -> Manifest:
    return Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )


class _RaiseOnce:
    name = "fake"

    def __init__(self, exc):
        self.capabilities = AdapterCapabilities(
            repo_write=True, structured_output="native", resume=True
        )
        self.exc = exc
        self.timeout_s = 600.0

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        raise self.exc


# --- FR-5.2 / FR-7.2: the deadline halt stamps halt_reason=timeout -----------
def test_timeout_halt_stamps_halt_reason_timeout(fixture_repo):
    man = _manifest()
    exc = AgentTimeoutError("killed after 600s", partial=AgentResult(text="", exit_code=-9))
    orch = _build(fixture_repo, PIPE, adapters={"builder": _RaiseOnce(exc)}, manifest=man)
    assert orch.drive() == M.RUN_PARKED  # a halt parks the run for a human
    rec = man.record("implement")
    assert rec.status == M.HALTED
    assert rec.halt_reason == M.HALT_REASON_TIMEOUT
    # Disjoint: a terminal halt carries halt_reason with parked_reason null.
    assert rec.parked_reason is None


# --- FR-3.4: scheduled_resume arming (auto) vs none (notify) -----------------
def _transient(retry_after_s=None):
    return AgentFailedError(
        "usage limit hit",
        partial=AgentResult(text="", session_id="sess-1",
                            usage=Usage(input_tokens=1, output_tokens=0), exit_code=1),
        failure_info=FailureInfo(
            kind=FAILURE_TRANSIENT_USAGE_LIMIT, marker="m", retry_after_s=retry_after_s,
        ),
    )


def test_auto_mode_arms_scheduled_resume_on_usage_limit_park(fixture_repo):
    man = _manifest()
    cfg = {"agents": {"builder": {"adapter": "claude-code"}},
           "resume_on_quota": "auto", "keep_awake": True}
    orch = _build(fixture_repo, PIPE, config=cfg,
                  adapters={"builder": _RaiseOnce(_transient(retry_after_s=300))},
                  manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.parked_reason == M.PARKED_REASON_USAGE_LIMIT
    assert rec.scheduled_resume is not None
    assert rec.scheduled_resume.attempts == 0
    assert rec.scheduled_resume.attempt_at == rec.quota_reset_at  # reset-time target


def test_notify_mode_never_arms_a_schedule(fixture_repo):
    man = _manifest()
    cfg = {"agents": {"builder": {"adapter": "claude-code"}}, "resume_on_quota": "notify"}
    orch = _build(fixture_repo, PIPE, config=cfg,
                  adapters={"builder": _RaiseOnce(_transient(retry_after_s=300))},
                  manifest=man)
    assert orch.drive() == M.RUN_PARKED
    assert man.record("implement").scheduled_resume is None


def test_auto_mode_no_schedule_without_reset_time(fixture_repo):
    # F-003: a transient usage-limit park with NO reported reset time must not arm
    # a schedule — arming with attempt_at=now() makes the auto loop treat it as due
    # immediately and burn every attempt in an unspaced hot loop (FR-3.4 spacing).
    man = _manifest()
    cfg = {"agents": {"builder": {"adapter": "claude-code"}},
           "resume_on_quota": "auto", "keep_awake": True}
    orch = _build(fixture_repo, PIPE, config=cfg,
                  adapters={"builder": _RaiseOnce(_transient(retry_after_s=None))},
                  manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.parked_reason == M.PARKED_REASON_USAGE_LIMIT
    assert rec.quota_reset_at is None
    assert rec.scheduled_resume is None  # plain park, no hot-loop schedule


# --- FR-3.4 / FR-5.4: config validation + load warnings ----------------------
def test_resume_on_quota_rejects_unknown_value():
    with pytest.raises(ValueError):
        RunConfig.model_validate({"resume_on_quota": "sometimes"})


def test_auto_without_keep_awake_or_scheduler_warns():
    with pytest.warns(UserWarning, match="auto"):
        RunConfig.model_validate({"resume_on_quota": "auto"})


def test_auto_with_keep_awake_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning would raise
        RunConfig.model_validate({"resume_on_quota": "auto", "keep_awake": True})


def test_auto_with_external_scheduler_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        RunConfig.model_validate({"resume_on_quota": "auto", "external_scheduler": True})


# --- FR-5.3: status suspension block renders the three classifications --------
T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)


def _write_heartbeat(run_dir: Path, mono: float, at: datetime) -> None:
    import json

    (run_dir / HB.HEARTBEAT_FILENAME).write_text(
        json.dumps(HB.HeartbeatSample(mono, HB.format_wallclock(at), 4242).to_dict())
    )


def _man_running() -> Manifest:
    m = _manifest()
    m.status = M.RUN_RUNNING
    m.steps.append(StepRecord(id="implement", type="agent_task", status=M.RUNNING))
    return m


def test_status_view_host_suspended(tmp_path):
    # Live driver just woke: fresh heartbeat whose wallclock == the recorded
    # interval's end (the skew pair), pid alive → host_suspended.
    now = T0 + timedelta(minutes=41)
    woke_at = T0 + timedelta(minutes=40)
    _write_heartbeat(tmp_path, 115.0, woke_at)
    m = _man_running()
    m.suspensions.append(
        M.Suspension(start=HB.format_wallclock(T0), end=HB.format_wallclock(woke_at), gap_s=2400)
    )
    view = op.compute_suspension_view(m, tmp_path, op.LIVENESS_ALIVE, now=now)
    assert view["classification"] == HB.STALL_HOST_SUSPENDED
    assert view["intervals"][0]["gap_s"] == 2400


def test_status_view_driver_orphaned(tmp_path):
    # Stale heartbeat, driver proven gone → driver_orphaned.
    _write_heartbeat(tmp_path, 100.0, T0)
    m = _man_running()
    view = op.compute_suspension_view(
        m, tmp_path, op.LIVENESS_ORPHANED, now=T0 + timedelta(hours=1)
    )
    assert view["classification"] == HB.STALL_DRIVER_ORPHANED


def test_status_view_agent_silent(tmp_path):
    # Fresh heartbeat (driver writing), no skew pair, but the step's events.jsonl
    # is old → agent_silent.
    _write_heartbeat(tmp_path, 100.0, T0)
    steps_dir = tmp_path / "steps" / "implement"
    steps_dir.mkdir(parents=True)
    events = steps_dir / "events.jsonl"
    events.write_text('{"e":1}\n')
    import os

    old = (T0 - timedelta(hours=1)).timestamp()
    os.utime(events, (old, old))
    m = _man_running()
    view = op.compute_suspension_view(
        m, tmp_path, op.LIVENESS_ALIVE, now=T0, agent_silence_s=300.0
    )
    assert view["classification"] == HB.STALL_AGENT_SILENT


def test_status_view_null_when_no_heartbeat_and_no_intervals(tmp_path):
    m = _man_running()
    assert op.compute_suspension_view(m, tmp_path, op.LIVENESS_ALIVE, now=T0) is None


def test_heartbeat_writer_persists_detected_interval_live(tmp_path):
    # F-001: the writer appends a detected interval to suspensions.jsonl the
    # instant it fires — before any manifest drain — so live status and a crash
    # both see it, while it also stays in memory for the drive-exit drain.
    monos = iter([100.0, 130.0])  # monotonic barely advanced (suspend excluded)
    walls = iter([T0, T0 + timedelta(minutes=40)])  # wallclock jumped 40m
    w = HB.HeartbeatWriter(
        tmp_path, monotonic_clock=lambda: next(monos), wall_clock=lambda: next(walls)
    )
    w._write_sample()  # prev
    w._write_sample()  # cur → detects the ~40m gap
    persisted = HB.read_persisted_suspensions(tmp_path)
    assert len(persisted) == 1 and persisted[0].gap_s == 2400
    assert len(w.drain_suspensions()) == 1  # also queued for the manifest drain


def test_read_persisted_suspensions_skips_malformed_lines(tmp_path):
    # Fail-closed: a torn/foreign line is skipped, never a bogus interval.
    (tmp_path / HB.SUSPENSIONS_LOG_FILENAME).write_text(
        'not json\n{"start":"a","end":"b","gap_s":5}\n{"start":"x"}\n\n'
    )
    out = HB.read_persisted_suspensions(tmp_path)
    assert len(out) == 1 and out[0].gap_s == 5


def test_read_persisted_suspensions_absent_file_is_empty(tmp_path):
    assert HB.read_persisted_suspensions(tmp_path) == []


def test_status_view_reads_live_persisted_suspensions(tmp_path):
    # F-001: a just-detected interval in suspensions.jsonl (NOT yet drained into
    # the manifest) is surfaced live and classifies host_suspended.
    import json

    now = T0 + timedelta(minutes=41)
    woke_at = T0 + timedelta(minutes=40)
    _write_heartbeat(tmp_path, 115.0, woke_at)
    (tmp_path / HB.SUSPENSIONS_LOG_FILENAME).write_text(
        json.dumps(
            HB.Suspension(
                start=HB.format_wallclock(T0),
                end=HB.format_wallclock(woke_at),
                gap_s=2400,
            ).to_dict()
        )
        + "\n"
    )
    m = _man_running()  # manifest has NO suspensions yet (drive still running)
    view = op.compute_suspension_view(m, tmp_path, op.LIVENESS_ALIVE, now=now)
    assert view["classification"] == HB.STALL_HOST_SUSPENDED
    assert view["intervals"][0]["gap_s"] == 2400


def test_status_view_dedups_manifest_and_live_suspensions(tmp_path):
    # A drained interval lives in BOTH the manifest and the append-only log;
    # status must union-dedup so one sleep is not reported twice.
    import json

    now = T0 + timedelta(minutes=41)
    woke_at = T0 + timedelta(minutes=40)
    _write_heartbeat(tmp_path, 115.0, woke_at)
    iv = M.Suspension(
        start=HB.format_wallclock(T0), end=HB.format_wallclock(woke_at), gap_s=2400
    )
    m = _man_running()
    m.suspensions.append(iv)
    (tmp_path / HB.SUSPENSIONS_LOG_FILENAME).write_text(
        json.dumps({"start": iv.start, "end": iv.end, "gap_s": iv.gap_s}) + "\n"
    )
    view = op.compute_suspension_view(m, tmp_path, op.LIVENESS_ALIVE, now=now)
    assert len(view["intervals"]) == 1  # deduped, not double-reported


def test_render_footer_surfaces_suspension_view():
    # F-004: the human status footer surfaces classification, heartbeat age, and
    # each detected interval — FR-5.3 parity with `--json`, not JSON-only.
    driver = op.DriverInfo(op.LIVENESS_ALIVE, 4242, "host", "2026-07-02T12-00-00")
    m = _man_running()
    rstate = op.compute_run_state(m, driver.state)
    view = {
        "classification": HB.STALL_HOST_SUSPENDED,
        "last_heartbeat_age_s": 3.0,
        "intervals": [
            {"start": "2026-07-02T12-00-00Z", "end": "2026-07-02T12-40-00Z", "gap_s": 2400}
        ],
    }
    text = "\n".join(op.render_footer(driver, rstate, suspension=view))
    assert "host_suspended" in text
    assert "heartbeat: last written 3.0s ago" in text
    assert "detected suspensions: 1" in text
    assert "2400s" in text


def test_render_footer_no_suspension_lines_when_none():
    driver = op.DriverInfo(op.LIVENESS_ALIVE, 4242, "host", "s")
    m = _man_running()
    rstate = op.compute_run_state(m, driver.state)
    lines = op.render_footer(driver, rstate, suspension=None)
    assert not any(ln.startswith("suspension:") for ln in lines)


def test_status_payload_with_suspension_validates(tmp_path):
    _write_heartbeat(tmp_path, 100.0, T0)
    m = _man_running()
    driver = op.DriverInfo(op.LIVENESS_ALIVE, 4242, "host", "2026-07-02T12-00-00")
    rstate = op.compute_run_state(m, driver.state)
    view = op.compute_suspension_view(m, tmp_path, driver.state, now=T0 + timedelta(seconds=5))
    payload = op.status_payload(
        m, driver, rstate, None,
        run_root=tmp_path, run_instance_dir=tmp_path, suspension=view,
    )  # raises StatusContractError on any schema drift
    assert payload["suspension"]["last_heartbeat_age_s"] == 5.0


# --- FR-3.4: the in-process auto-resume loop ---------------------------------
class _AutoResumeHarness:
    """A RunManager with a scripted `_resume_once` and a fake clock/sleep.

    The manifest lives on disk under a run instance; each stubbed resume runs a
    scripted outcome (keep the usage-limit park, or complete the run) so the loop
    can be exercised deterministically without a real adapter or drive.
    """

    def __init__(self, tmp_path: Path, *, outcomes):
        self.repo = tmp_path
        cfg = RunConfig.model_validate({
            "resume_on_quota": "auto", "keep_awake": True, "run_root": "runs",
            "max_auto_resume_attempts": 3,
        })
        self.mgr = RunManager(tmp_path, config=cfg)
        self.run_dir = tmp_path / "runs" / "demo" / "run-1"
        self.run_dir.mkdir(parents=True)
        (tmp_path / "runs" / "demo" / "active-run.txt").write_text("run-1")
        self.outcomes = list(outcomes)
        self.resume_calls = 0
        self.now = T0
        self.wait_entries = 0  # how many times the wait context was entered
        self.mgr._resume_once = self._fake_resume  # type: ignore[assignment]

    @contextlib.contextmanager
    def _wait_context(self, run_dir):
        # A hermetic stand-in for the real heartbeat/keep-awake wait context so
        # the loop is exercised without spawning a heartbeat thread or caffeinate.
        self.wait_entries += 1
        yield

    def _clock(self) -> str:
        return self.now.isoformat()

    def _sleep(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)

    def _load(self) -> Manifest:
        return Manifest.load(self.run_dir / "manifest.json")

    def _save(self, man: Manifest) -> None:
        man.write_atomic(self.run_dir / "manifest.json")

    def park(self, *, attempt_at: datetime, attempts: int = 0) -> None:
        m = _manifest()
        m.status = M.RUN_PARKED
        m.steps.append(StepRecord(
            id="implement", type="agent_task", status=M.PARKED,
            parked_reason=M.PARKED_REASON_USAGE_LIMIT,
            scheduled_resume=ScheduledResume(
                attempt_at=attempt_at.isoformat(), attempts=attempts, max_attempts=3),
        ))
        self._save(m)

    def _fake_resume(self, slug, *, response=None, use_judge=True, adapter_factory=None,
                     extra_context=None, clock=None):
        self.resume_calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else "reparks"
        man = self._load()
        step = man.record("implement")
        if outcome == "done":
            man.status = M.RUN_DONE
            step.status = M.DONE
            step.parked_reason = None
            step.scheduled_resume = None
        # "reparks": leave the usage-limit park + schedule as the loop left it
        # (attempts already incremented + persisted before this call).
        self._save(man)
        return man.status

    def run(self) -> str:
        return self.mgr._auto_resume_if_scheduled(
            "demo", M.RUN_PARKED, use_judge=False, adapter_factory=None,
            extra_context=None, clock=self._clock, sleep=self._sleep,
            wait_context=self._wait_context,
        )


def test_auto_resume_resumes_once_when_due_then_completes(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.park(attempt_at=T0 - timedelta(seconds=1))  # already due
    h.run()
    assert h.resume_calls == 1
    assert h._load().status == M.RUN_DONE


def test_auto_resume_waits_for_a_future_reset_then_resumes(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.park(attempt_at=T0 + timedelta(seconds=120))  # not yet due
    h.run()
    assert h.resume_calls == 1
    assert h.now >= T0 + timedelta(seconds=120)  # the loop waited out the reset


def test_auto_resume_stops_at_max_attempts_with_exhaustion_note(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["reparks", "reparks", "reparks", "reparks"])
    h.park(attempt_at=T0 - timedelta(seconds=1))
    h.run()
    assert h.resume_calls == 3  # exactly max_auto_resume_attempts spaced attempts
    step = h._load().record("implement")
    assert step.scheduled_resume is None  # schedule cleared at exhaustion
    assert "auto-resume exhausted" in (step.notes or "")


def test_notify_mode_auto_loop_is_a_noop(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.mgr.config.resume_on_quota = "notify"
    h.park(attempt_at=T0 - timedelta(seconds=1))
    assert h.run() == M.RUN_PARKED
    assert h.resume_calls == 0  # never re-invoked in notify mode


def test_auto_resume_wait_runs_under_heartbeat_keepawake_context(tmp_path):
    # F-002: the quota wait keeps the heartbeat/keep-awake context live so the
    # waiting driver still heartbeats and (opt-in) holds the host awake.
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.park(attempt_at=T0 + timedelta(seconds=90))  # a real wait precedes resume
    h.run()
    assert h.wait_entries >= 1  # the wait context wrapped the wait
    assert h.resume_calls == 1


def test_auto_resume_no_wait_context_when_immediately_due(tmp_path):
    # No wait → no heartbeat/keep-awake churn (context entered only for waits).
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.park(attempt_at=T0 - timedelta(seconds=1))  # already due
    h.run()
    assert h.wait_entries == 0


def test_auto_resume_defers_to_a_concurrent_lock_holder(tmp_path):
    # F-005: with another driver holding the worktree lock, the auto-resume loop
    # must NOT write attempts/exhaustion outside the lock — it defers, consuming
    # no attempt and driving no resume.
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.park(attempt_at=T0 - timedelta(seconds=1))  # due → would otherwise resume
    handle = h.mgr._acquire_worktree_lock("demo", "other-run")
    try:
        assert h.run() == M.RUN_PARKED  # deferred, not resumed
    finally:
        h.mgr._release_worktree_lock(handle)
    assert h.resume_calls == 0
    assert h._load().record("implement").scheduled_resume.attempts == 0

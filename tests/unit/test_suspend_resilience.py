"""Suspend/sleep resilience wiring (harness-efficiency P2, FR-5 + FR-3.4).

Covers the engine-side integration the pure tests in ``test_heartbeat.py`` do
not: the ``halt_reason=timeout`` stamp on the deadline halt (FR-5.2/FR-7.2), the
config knobs + auto-resume load warning (FR-3.4/FR-5.4), the ``status --json``
suspension block (FR-5.3), and the in-process auto-resume loop (FR-3.4).
"""

from __future__ import annotations

import contextlib
import json
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gauntlet.adapters.base import (
    FAILURE_TRANSIENT_DEPENDENCY,
    FAILURE_TRANSIENT_USAGE_LIMIT,
    AdapterCapabilities,
    AgentFailedError,
    AgentResult,
    AgentTimeoutError,
    FailureInfo,
    Usage,
)
from gauntlet.adapters.failure_markers import classify_claude_failure
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


def test_auto_mode_arms_fallback_schedule_without_reset_time(fixture_repo):
    # #166: a recognized quota denial with no structured deadline uses a spaced
    # engine fallback. The provider fact remains unknown; prose is not parsed.
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
    assert rec.scheduled_resume is not None
    assert rec.scheduled_resume.policy == "until_cancelled"
    assert rec.scheduled_resume.interval_s == 1800
    assert rec.scheduled_resume.deadline_source == "fallback"
    assert datetime.fromisoformat(rec.scheduled_resume.attempt_at) > datetime.fromisoformat(rec.ended)
    assert rec.auto_resume_history[-1].outcome == "quota_denied"


# --- FR-3.4 / FR-5.4: config validation + load warnings ----------------------
def test_resume_on_quota_rejects_unknown_value():
    with pytest.raises(ValueError):
        RunConfig.model_validate({"resume_on_quota": "sometimes"})


def test_quota_retry_interval_must_be_positive():
    with pytest.raises(ValueError, match="quota_retry_interval_s"):
        RunConfig.model_validate({"quota_retry_interval_s": 0})


def test_auto_without_keep_awake_or_scheduler_warns():
    # keep_awake defaults on (#134), so the no-survival case must opt out.
    with pytest.warns(UserWarning, match="auto"):
        RunConfig.model_validate({"resume_on_quota": "auto", "keep_awake": False})


def test_keep_awake_defaults_on_and_survives_auto():
    cfg = RunConfig.model_validate({})
    assert cfg.keep_awake is True
    with warnings.catch_warnings():
        warnings.simplefilter("error")
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

    def __init__(self, tmp_path: Path, *, outcomes, config: dict | None = None):
        self.repo = tmp_path
        cfg = RunConfig.model_validate({
            "resume_on_quota": "auto", "keep_awake": True, "run_root": "runs",
            "max_auto_resume_attempts": 3, **(config or {}),
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

    def park(self, *, attempt_at: datetime, attempts: int = 0,
             reason: str = M.PARKED_REASON_USAGE_LIMIT,
             schedule_reason: str | None = "same") -> None:
        # ``schedule_reason``: "same" stamps the schedule with ``reason`` (what
        # the orchestrator does since #134); None leaves it unstamped (a
        # pre-#134 manifest); any other value simulates a stale stamp.
        stamp = reason if schedule_reason == "same" else schedule_reason
        m = _manifest()
        m.status = M.RUN_PARKED
        m.steps.append(StepRecord(
            id="implement", type="agent_task", status=M.PARKED,
            parked_reason=reason,
            quota_reset_at=attempt_at.isoformat(),
            scheduled_resume=ScheduledResume(
                attempt_at=attempt_at.isoformat(), attempts=attempts, max_attempts=3,
                reason=stamp,
                policy=(
                    "until_cancelled"
                    if reason == M.PARKED_REASON_USAGE_LIMIT else "bounded"
                ),
                interval_s=(
                    1800 if reason == M.PARKED_REASON_USAGE_LIMIT else None
                )),
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
        elif step.scheduled_resume is not None:
            # A real finalizer schedules from the current denial time, never the
            # stale prior deadline. Mirror that spacing in this loop harness.
            step.scheduled_resume.attempt_at = (
                self.now + timedelta(seconds=step.scheduled_resume.interval_s or 2)
            ).isoformat()
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


def test_provider_auto_resume_stops_at_max_attempts_with_exhaustion_note(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["reparks", "reparks", "reparks", "reparks"])
    h.mgr.config.resume_on_provider_unavailable = "auto"
    h.park(
        attempt_at=T0 - timedelta(seconds=1),
        reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE,
    )
    h.run()
    assert h.resume_calls == 3  # exactly max_auto_resume_attempts spaced attempts
    step = h._load().record("implement")
    assert step.scheduled_resume is None  # schedule cleared at exhaustion
    assert "auto-resume exhausted" in (step.notes or "")


def test_quota_auto_resume_continues_past_shared_ceiling_then_completes(tmp_path):
    h = _AutoResumeHarness(
        tmp_path, outcomes=["reparks", "reparks", "reparks", "reparks", "done"]
    )
    h.park(attempt_at=T0 - timedelta(seconds=1))
    assert h.run() == M.RUN_DONE
    assert h.resume_calls == 5
    history = h._load().record("implement").auto_resume_history
    assert [e.attempt for e in history if e.outcome == "attempt_started"] == [1, 2, 3, 4, 5]


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


# --- #134: scheduled auto-resume generalized to provider_unavailable parks ----
def _dependency(retry_after_s=None):
    return AgentFailedError(
        "api call failed: simulated timeout",
        partial=AgentResult(text="", session_id="sess-1",
                            usage=Usage(input_tokens=1, output_tokens=0), exit_code=1),
        failure_info=FailureInfo(
            kind=FAILURE_TRANSIENT_DEPENDENCY, marker="api_timeout",
            retry_after_s=retry_after_s,
        ),
    )


def _cfg(**over):
    # dependency_retry_attempts: 0 → the first dependency failure parks at once
    # (no in-process retry sleeps), with the backoff deadline still recorded.
    base = {"agents": {"builder": {"adapter": "claude-code"}},
            "keep_awake": True, "dependency_retry_attempts": 0}
    base.update(over)
    return base


def test_provider_auto_arms_schedule_on_provider_unavailable_park(fixture_repo):
    man = _manifest()
    orch = _build(fixture_repo, PIPE, config=_cfg(resume_on_provider_unavailable="auto"),
                  adapters={"builder": _RaiseOnce(_dependency())}, manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert rec.quota_reset_at is not None  # the backoff deadline (plan §5.2)
    assert rec.scheduled_resume is not None
    assert rec.scheduled_resume.attempt_at == rec.quota_reset_at
    assert rec.scheduled_resume.attempts == 0
    assert rec.scheduled_resume.max_attempts == 3
    assert rec.scheduled_resume.reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE


def test_provider_auto_arms_with_retry_after_deadline(fixture_repo):
    man = _manifest()
    orch = _build(fixture_repo, PIPE, config=_cfg(resume_on_provider_unavailable="auto"),
                  adapters={"builder": _RaiseOnce(_dependency(retry_after_s=120))},
                  manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.retry_after_s == 120
    assert rec.scheduled_resume is not None
    assert rec.scheduled_resume.attempt_at == rec.quota_reset_at


def test_provider_notify_default_never_arms_a_schedule(fixture_repo):
    man = _manifest()
    orch = _build(fixture_repo, PIPE, config=_cfg(),
                  adapters={"builder": _RaiseOnce(_dependency())}, manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert rec.quota_reset_at is not None  # deadline recorded, but no schedule
    assert rec.scheduled_resume is None


def test_quota_auto_alone_does_not_arm_a_provider_park(fixture_repo):
    # Each knob governs only its own park reason.
    man = _manifest()
    orch = _build(fixture_repo, PIPE, config=_cfg(resume_on_quota="auto"),
                  adapters={"builder": _RaiseOnce(_dependency())}, manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert rec.scheduled_resume is None


def test_provider_auto_alone_does_not_arm_a_usage_limit_park(fixture_repo):
    man = _manifest()
    orch = _build(fixture_repo, PIPE, config=_cfg(resume_on_provider_unavailable="auto"),
                  adapters={"builder": _RaiseOnce(_transient(retry_after_s=300))},
                  manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.parked_reason == M.PARKED_REASON_USAGE_LIMIT
    assert rec.scheduled_resume is None


def test_usage_limit_schedule_is_stamped_with_its_reason(fixture_repo):
    man = _manifest()
    orch = _build(fixture_repo, PIPE, config=_cfg(resume_on_quota="auto"),
                  adapters={"builder": _RaiseOnce(_transient(retry_after_s=300))},
                  manifest=man)
    assert orch.drive() == M.RUN_PARKED
    assert man.record("implement").scheduled_resume.reason == M.PARKED_REASON_USAGE_LIMIT


def test_scheduled_resume_loads_without_reason_field():
    # Additive: a schedule persisted before #134 has no `reason` and still loads.
    sched = ScheduledResume.model_validate({"attempt_at": T0.isoformat(), "attempts": 1})
    assert sched.reason is None
    assert sched.max_attempts == 3


# --- #134: config validation + survival warning for the new knob -------------
def test_resume_on_provider_unavailable_rejects_unknown_value():
    with pytest.raises(ValueError, match="resume_on_provider_unavailable"):
        RunConfig.model_validate({"resume_on_provider_unavailable": "sometimes"})


def test_resume_on_provider_unavailable_normalizes_case():
    cfg = RunConfig.model_validate(
        {"resume_on_provider_unavailable": " Auto ", "keep_awake": True}
    )
    assert cfg.resume_on_provider_unavailable == "auto"
    assert cfg.any_auto_resume


def test_provider_auto_without_keep_awake_or_scheduler_warns():
    with pytest.warns(UserWarning, match="resume_on_provider_unavailable: auto"):
        RunConfig.model_validate({"resume_on_provider_unavailable": "auto", "keep_awake": False})


def test_provider_auto_with_keep_awake_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        RunConfig.model_validate(
            {"resume_on_provider_unavailable": "auto", "keep_awake": True}
        )


def test_provider_auto_with_external_scheduler_does_not_warn():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        RunConfig.model_validate(
            {"resume_on_provider_unavailable": "auto", "external_scheduler": True}
        )


def test_both_knobs_auto_warn_once_naming_both():
    with pytest.warns(UserWarning) as rec:
        RunConfig.model_validate(
            {"resume_on_quota": "auto", "resume_on_provider_unavailable": "auto", "keep_awake": False}
        )
    msgs = [str(w.message) for w in rec if "auto-resume" in str(w.message)]
    assert len(msgs) == 1
    assert "resume_on_quota" in msgs[0]
    assert "resume_on_provider_unavailable" in msgs[0]


def test_default_config_has_no_auto_resume():
    assert not RunConfig.model_validate({}).any_auto_resume


# --- #134: the wait loop on a provider_unavailable park ----------------------
_PROVIDER_AUTO = {"resume_on_quota": "notify", "resume_on_provider_unavailable": "auto"}


def test_auto_resume_provider_park_resumes_when_due(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["done"], config=_PROVIDER_AUTO)
    h.park(attempt_at=T0 - timedelta(seconds=1),
           reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE)
    assert h.run() == M.RUN_DONE
    assert h.resume_calls == 1


def test_auto_resume_provider_park_waits_out_the_backoff_deadline(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["done"], config=_PROVIDER_AUTO)
    h.park(attempt_at=T0 + timedelta(seconds=90),
           reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE)
    h.run()
    assert h.resume_calls == 1
    assert h.now >= T0 + timedelta(seconds=90)
    assert h.wait_entries >= 1  # heartbeat/keep-awake context spans the wait


def test_auto_resume_provider_park_exhausts_to_a_plain_provider_park(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["reparks"] * 4, config=_PROVIDER_AUTO)
    h.park(attempt_at=T0 - timedelta(seconds=1),
           reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE)
    assert h.run() == M.RUN_PARKED
    assert h.resume_calls == 3  # exactly max_auto_resume_attempts
    step = h._load().record("implement")
    assert step.status == M.PARKED
    assert step.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert step.scheduled_resume is None  # left as a plain park
    assert step.quota_reset_at is not None  # deadline kept: still a legitimate wait
    assert "auto-resume exhausted" in (step.notes or "")
    assert "plain provider_unavailable park" in (step.notes or "")


def test_provider_park_ignored_when_only_quota_knob_is_auto(tmp_path):
    # `resume_on_quota: auto` (the harness default) must not drive a
    # provider_unavailable park; its schedule stays untouched for a manual resume.
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.park(attempt_at=T0 - timedelta(seconds=1),
           reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE)
    assert h.run() == M.RUN_PARKED
    assert h.resume_calls == 0
    sched = h._load().record("implement").scheduled_resume
    assert sched is not None and sched.attempts == 0


def test_usage_limit_park_ignored_when_only_provider_knob_is_auto(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["done"], config=_PROVIDER_AUTO)
    h.park(attempt_at=T0 - timedelta(seconds=1))  # usage_limit
    assert h.run() == M.RUN_PARKED
    assert h.resume_calls == 0


def test_unstamped_schedule_is_read_as_the_step_park_reason(tmp_path):
    # A pre-#134 manifest has no `reason` stamp: route by the step's own reason.
    h = _AutoResumeHarness(tmp_path, outcomes=["done"], config=_PROVIDER_AUTO)
    h.park(attempt_at=T0 - timedelta(seconds=1),
           reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE, schedule_reason=None)
    assert h.run() == M.RUN_DONE
    assert h.resume_calls == 1


def test_stale_schedule_stamp_never_routes_the_wrong_knob(tmp_path):
    # A usage_limit step carrying a schedule stamped provider_unavailable must
    # not be driven by the provider knob (nor by the quota knob: harness default
    # `resume_on_quota: auto` still requires the stamp to match).
    h = _AutoResumeHarness(tmp_path, outcomes=["done"],
                           config={"resume_on_provider_unavailable": "auto"})
    h.park(attempt_at=T0 - timedelta(seconds=1),
           reason=M.PARKED_REASON_USAGE_LIMIT,
           schedule_reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE)
    assert h.run() == M.RUN_PARKED
    assert h.resume_calls == 0


def test_knob_flipped_to_notify_mid_wait_stops_the_loop(tmp_path):
    # The governing knob is re-read every pass: flipping it to notify during
    # the wait stops the loop before it drives another resume.
    h = _AutoResumeHarness(tmp_path, outcomes=["done"], config=_PROVIDER_AUTO)
    h.park(attempt_at=T0 + timedelta(seconds=30),
           reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE)
    real_sleep = h._sleep

    def flip_then_sleep(seconds):
        h.mgr.config.resume_on_provider_unavailable = "notify"
        real_sleep(seconds)

    h._sleep = flip_then_sleep
    assert h.run() == M.RUN_PARKED
    assert h.resume_calls == 0
    assert h._load().record("implement").scheduled_resume is not None


def test_repo_config_flipped_to_notify_mid_wait_stops_the_loop(tmp_path):
    # A CLI-created manager has a live config path. Prove the loop reloads that
    # file rather than merely observing mutations to its in-memory model.
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    config_dir = tmp_path / ".gauntlet"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "resume_on_quota: auto\n"
        "keep_awake: true\n"
        "run_root: runs\n"
        "quota_retry_interval_s: 1800\n"
    )
    h.mgr._live_config_path = config_path
    h.park(attempt_at=T0 + timedelta(seconds=30))
    real_sleep = h._sleep

    def disable_then_sleep(seconds):
        config_path.write_text(
            "resume_on_quota: notify\n"
            "keep_awake: true\n"
            "run_root: runs\n"
            "quota_retry_interval_s: 1800\n"
        )
        real_sleep(seconds)

    h._sleep = disable_then_sleep
    assert h.run() == M.RUN_PARKED
    assert h.resume_calls == 0


def test_abort_cancels_quota_wait_before_provider_call(tmp_path):
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.park(attempt_at=T0 + timedelta(seconds=30))
    real_sleep = h._sleep

    def abort_then_sleep(seconds):
        h.mgr.abort("demo")
        real_sleep(seconds)

    h._sleep = abort_then_sleep
    assert h.run() == M.RUN_PARKED  # wrapper returns the last drive status
    assert h._load().status == M.RUN_ABORTED
    assert h.resume_calls == 0


def test_both_knobs_auto_drive_either_park_reason(tmp_path):
    both = {"resume_on_quota": "auto", "resume_on_provider_unavailable": "auto"}
    for reason in (M.PARKED_REASON_USAGE_LIMIT, M.PARKED_REASON_PROVIDER_UNAVAILABLE):
        sub = tmp_path / reason
        sub.mkdir()
        h = _AutoResumeHarness(sub, outcomes=["done"], config=both)
        h.park(attempt_at=T0 - timedelta(seconds=1), reason=reason)
        assert h.run() == M.RUN_DONE, reason
        assert h.resume_calls == 1, reason


# --- #134: end-to-end through the real resume path ---------------------------
_E2E_CONFIG = """
base_branch: main
run_root: runs
interrupted_step: park
dependency_retry_attempts: 0
dependency_retry_base_s: 2.0
dependency_retry_max_delay_s: 5.0
resume_on_provider_unavailable: auto
external_scheduler: true
agents:
  builder: {adapter: claude-code}
"""

_E2E_PIPE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""


class _FlakyDependencyAdapter:
    """Raises a classified dependency failure ``fail_times`` times, then succeeds."""

    name = "fake"
    timeout_s = 600.0

    def __init__(self, fail_times: int):
        self.capabilities = AdapterCapabilities(
            repo_write=True, structured_output="native", resume=True
        )
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls.append({"prompt": prompt, "session": session})
        if len(self.calls) <= self.fail_times:
            raise _dependency()
        return AgentResult(text="done", session_id="s1", exit_code=0)


def _seed_e2e(repo: Path, config_text: str = _E2E_CONFIG):
    from conftest import git
    from gauntlet.engine.manifest import PipelineRef
    from gauntlet.engine.pipeline import load_pipeline

    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(config_text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed config")
    git(repo, "checkout", "-qb", "gauntlet/demo")
    slug_dir = repo / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")
    (run_dir / "pipeline.yaml").write_text(_E2E_PIPE)
    (slug_dir / ".gitignore").write_text(".gitignore\nactive-run.txt\n")
    (slug_dir / "active-run.txt").write_text("run-1")
    _, phash = load_pipeline(run_dir / "pipeline.yaml")
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash=phash),
        status=M.RUN_RUNNING,
    )
    man.write_atomic(run_dir / "manifest.json")
    return RunManager(repo), run_dir


class _FakeTime:
    """A fake orchestrator clock whose `sleep` advances it (no real waiting)."""

    def __init__(self):
        self.now = T0

    def clock(self) -> str:
        return self.now.isoformat()

    def sleep(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


_QUOTA_E2E_CONFIG = """
base_branch: main
run_root: runs
interrupted_step: park
resume_on_quota: auto
quota_retry_interval_s: 30
external_scheduler: true
agents:
  builder: {adapter: claude-code}
"""


class _FixtureQuotaAdapter:
    """Replays the committed text-only Claude quota envelope, then recovers."""

    name = "fake"
    timeout_s = 600.0

    def __init__(self, fail_times: int):
        self.capabilities = AdapterCapabilities(
            repo_write=True, structured_output="native", resume=True
        )
        event = json.loads(
            (Path(__file__).parents[2] / ".gauntlet/failure-fixtures/claude/usage-limit.json")
            .read_text()
        )
        self.event = event
        self.info = classify_claude_failure(event, 1)
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls.append({"prompt": prompt, "session": session})
        partial = Path(cwd) / "partial-work.txt"
        if not partial.exists():
            partial.write_text("preserved across quota retries\n")
        if len(self.calls) <= self.fail_times:
            raise AgentFailedError(
                "claude usage limit",
                partial=AgentResult(
                    text=self.event["result"], session_id="fixture-session",
                    raw_events=[self.event], exit_code=1,
                ),
                failure_info=self.info,
            )
        return AgentResult(text="done", session_id="fixture-session", exit_code=0)


def test_text_only_quota_retries_beyond_three_then_continues(fixture_repo):
    mgr, run_dir = _seed_e2e(fixture_repo, _QUOTA_E2E_CONFIG)
    mgr._auto_resume_wait_context = lambda run_dir: contextlib.nullcontext()
    adapter = _FixtureQuotaAdapter(fail_times=5)
    ft = _FakeTime()
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter,
        clock=ft.clock, auto_sleep=ft.sleep,
    )
    assert status == M.RUN_DONE
    assert len(adapter.calls) == 6
    assert all(call["session"] == "fixture-session" for call in adapter.calls[1:])
    assert (fixture_repo / "partial-work.txt").read_text().startswith("preserved")
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.DONE and rec.scheduled_resume is None
    assert [e.attempt for e in rec.auto_resume_history if e.outcome == "attempt_started"] == [1, 2, 3, 4, 5]
    assert len([e for e in rec.auto_resume_history if e.outcome == "quota_denied"]) == 5


def test_e2e_provider_park_auto_resumes_through_the_plain_retry_path(fixture_repo):
    """A dependency failure parks provider_unavailable with a backoff deadline;
    under `resume_on_provider_unavailable: auto` the SAME `resume` verb waits
    out the deadline and performs the plain retry resume (a fresh dependency
    episode, plan §5.2) — the recovered provider completes the run with no
    operator action and no `--response`."""
    mgr, run_dir = _seed_e2e(fixture_repo)
    assert mgr.config.resume_on_provider_unavailable == "auto"
    mgr._auto_resume_wait_context = lambda run_dir: contextlib.nullcontext()
    adapter = _FlakyDependencyAdapter(fail_times=1)
    ft = _FakeTime()
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter,
        clock=ft.clock, auto_sleep=ft.sleep,
    )
    assert status == M.RUN_DONE
    assert len(adapter.calls) == 2  # the park, then the one auto-resume retry
    assert ft.now > T0  # the loop actually waited out the backoff deadline
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.DONE
    assert rec.parked_reason is None
    assert rec.scheduled_resume is None  # cleared on the DONE finalization
    assert rec.dependency_attempts == 0  # episode ended; budget reset


def test_e2e_provider_park_exhausts_then_leaves_plain_park(fixture_repo):
    mgr, run_dir = _seed_e2e(fixture_repo)
    mgr._auto_resume_wait_context = lambda run_dir: contextlib.nullcontext()
    adapter = _FlakyDependencyAdapter(fail_times=99)
    ft = _FakeTime()
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter,
        clock=ft.clock, auto_sleep=ft.sleep,
    )
    assert status == M.RUN_PARKED
    assert len(adapter.calls) == 1 + 3  # the park + max_auto_resume_attempts retries
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert rec.scheduled_resume is None
    assert rec.quota_reset_at is not None
    assert "auto-resume exhausted" in (rec.notes or "")


# --- #166 review: spacing floor, evidence gating, reload resilience, cancel ---
def test_quota_schedule_deadline_rules():
    now = T0
    # a still-future structured hint wins, floored at the minimum spacing
    at, src = M.quota_schedule_deadline(now, (now + timedelta(hours=5)).isoformat(), 1800)
    assert (datetime.fromisoformat(at), src) == (now + timedelta(hours=5), "provider_hint")
    at, src = M.quota_schedule_deadline(now, (now + timedelta(seconds=1)).isoformat(), 1800)
    assert (datetime.fromisoformat(at), src) == (
        now + timedelta(seconds=M.QUOTA_RETRY_MIN_SPACING_S), "provider_hint")
    # stale, missing or unparseable hints fall back to the interval
    for hint in [(now - timedelta(seconds=1)).isoformat(), None, "not a time"]:
        at, src = M.quota_schedule_deadline(now, hint, 1800)
        assert (datetime.fromisoformat(at), src) == (now + timedelta(seconds=1800), "fallback")


def test_short_structured_hint_is_floored_not_hot_looped(fixture_repo):
    # Review F1: with no attempt ceiling, `Retry-After: 1` must not become a
    # resume every second. The hint still governs (provider_hint) but is spaced.
    man = _manifest()
    cfg = {"agents": {"builder": {"adapter": "claude-code"}},
           "resume_on_quota": "auto", "keep_awake": True}
    orch = _build(fixture_repo, PIPE, config=cfg,
                  adapters={"builder": _RaiseOnce(_transient(retry_after_s=1))},
                  manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    sched = rec.scheduled_resume
    assert sched is not None and sched.deadline_source == "provider_hint"
    spacing = datetime.fromisoformat(sched.attempt_at) - datetime.fromisoformat(rec.ended)
    assert spacing >= timedelta(seconds=M.QUOTA_RETRY_MIN_SPACING_S)
    assert sched.armed_at == rec.ended
    assert sched.consecutive_denials == 1


def test_notify_mode_records_no_auto_resume_evidence(fixture_repo):
    # Review F5: no loop exists in notify mode, so a denial is just a park.
    man = _manifest()
    cfg = {"agents": {"builder": {"adapter": "claude-code"}}, "resume_on_quota": "notify"}
    orch = _build(fixture_repo, PIPE, config=cfg,
                  adapters={"builder": _RaiseOnce(_transient(retry_after_s=None))},
                  manifest=man)
    assert orch.drive() == M.RUN_PARKED
    rec = man.record("implement")
    assert rec.scheduled_resume is None
    assert rec.auto_resume_history == []


def test_auto_resume_history_is_a_bounded_ring():
    rec = StepRecord(id="implement", type="agent_task", status=M.PARKED)
    for i in range(M.AUTO_RESUME_HISTORY_MAX + 10):
        M.append_auto_resume_event(rec, M.AutoResumeEvent(
            at=T0.isoformat(), attempt=i, reason=M.PARKED_REASON_USAGE_LIMIT,
            outcome="attempt_started"))
    assert len(rec.auto_resume_history) == M.AUTO_RESUME_HISTORY_MAX
    assert rec.auto_resume_history[0].attempt == 10  # oldest dropped first


def test_arm_next_attempt_spaces_an_unbounded_schedule_write_ahead(tmp_path):
    # Review F7/C2: the write-ahead increment also moves the deadline out one
    # interval, so a resume that dies before re-parking is not retried at once.
    h = _AutoResumeHarness(tmp_path, outcomes=[])
    h.park(attempt_at=T0 - timedelta(seconds=1))
    assert h.mgr._arm_next_attempt("demo", h.run_dir, "run-1", now=T0) is True
    sched = h._load().record("implement").scheduled_resume
    assert sched.attempts == 1
    assert datetime.fromisoformat(sched.attempt_at) == T0 + timedelta(seconds=1800)


def test_config_reload_failure_waits_without_provider_call_until_valid(tmp_path):
    # Review F2 follow-up: a transiently unreadable config must neither end the
    # wait nor authorize a provider call using stale knobs.  The second sleep is
    # caused solely by the failed reload; repair after observing zero resumes.
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    config_dir = tmp_path / ".gauntlet"
    config_dir.mkdir()
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        "resume_on_quota: auto\nkeep_awake: true\nrun_root: runs\n"
    )
    h.mgr._live_config_path = config_path
    h.park(attempt_at=T0 + timedelta(seconds=90))
    real_sleep = h._sleep
    writes = {"n": 0}

    def corrupt_then_sleep(seconds):
        writes["n"] += 1
        if writes["n"] == 1:
            config_path.write_text("resume_on_quota: [unterminated\n")  # mid-save
        elif writes["n"] == 2:
            assert h.resume_calls == 0
            config_path.write_text(
                "resume_on_quota: auto\nkeep_awake: true\nrun_root: runs\n"
            )
        real_sleep(seconds)

    h._sleep = corrupt_then_sleep
    assert h.run() == M.RUN_DONE
    assert h.resume_calls == 1
    assert any("could not be reloaded" in w for w in h._load().warnings)


def test_auto_resume_executor_uses_fresh_live_heartbeat_without_lock(
    tmp_path, monkeypatch
):
    # The quota waiter intentionally has no drive lock while sleeping.  A fresh
    # heartbeat from a live PID is affirmative executor evidence for status.
    _write_heartbeat(tmp_path, 100.0, T0)
    monkeypatch.setattr(op, "_probe_pid", lambda pid: "alive")
    driver = op.DriverInfo(op.LIVENESS_NONE, None, None, None)
    assert op.auto_resume_executor_live(
        driver, tmp_path, now=T0 + timedelta(seconds=10)
    ) is True
    assert op.auto_resume_executor_live(
        driver, tmp_path, now=T0 + timedelta(seconds=60)
    ) is False


def test_flip_to_notify_clears_the_quota_schedule_and_records_cancellation(tmp_path):
    # Review F6: cancellation is persisted (schedule cleared, event + note) so
    # `status`, `--json`, the sweep and the notifier all agree nothing fires.
    h = _AutoResumeHarness(tmp_path, outcomes=["done"])
    h.park(attempt_at=T0 + timedelta(seconds=30), attempts=2)
    real_sleep = h._sleep

    def flip_then_sleep(seconds):
        h.mgr.config.resume_on_quota = "notify"
        real_sleep(seconds)

    h._sleep = flip_then_sleep
    assert h.run() == M.RUN_PARKED
    assert h.resume_calls == 0
    rec = h._load().record("implement")
    assert rec.scheduled_resume is None
    assert rec.auto_resume_history[-1].outcome == "cancelled"
    assert rec.auto_resume_history[-1].attempt == 2
    assert "auto-resume cancelled after 2 attempts" in (rec.notes or "")


_ESCALATING_QUOTA_E2E_CONFIG = _QUOTA_E2E_CONFIG + "quota_denials_before_escalation: 3\n"


def test_consecutive_denials_escalate_once_but_retries_continue(fixture_repo):
    # Review F3: a restriction that never clears is flagged as possibly
    # persistent exactly once (schedule stamp, history event, run warning) —
    # and the loop still keeps its contract of retrying until it succeeds.
    mgr, run_dir = _seed_e2e(fixture_repo, _ESCALATING_QUOTA_E2E_CONFIG)
    mgr._auto_resume_wait_context = lambda run_dir: contextlib.nullcontext()
    adapter = _FixtureQuotaAdapter(fail_times=5)
    ft = _FakeTime()
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter,
        clock=ft.clock, auto_sleep=ft.sleep,
    )
    assert status == M.RUN_DONE
    assert len(adapter.calls) == 6
    man = Manifest.load(run_dir / "manifest.json")
    rec = man.record("implement")
    escalations = [e for e in rec.auto_resume_history if e.outcome == "escalated"]
    assert len(escalations) == 1
    assert escalations[0].at == [
        e for e in rec.auto_resume_history if e.outcome == "quota_denied"
    ][2].at  # stamped on the third consecutive denial
    assert sum("consecutive quota denials" in w for w in man.warnings) == 1
    assert rec.scheduled_resume is None  # cleared on DONE

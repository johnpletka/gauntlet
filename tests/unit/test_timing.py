"""Clock-time evidence: per-call capture + the `gauntlet report` time section.

Covers the capture context manager (outcome mapping, append-only records on
the step record, no-op on a record-less context), the engine wiring (a driven
adversarial cycle and a driven agent_task + phase-commit leave labelled
Invocations on their manifest records), the journal replay of parked
intervals, the aggregation math (per step / per agent / per activity, the
overall split into agent / parked / suspended / other), the rendering, and
the manifest's additive round-trip of the new field.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from gauntlet.adapters.base import (
    AdapterError,
    AgentFailedError,
    AgentTimeoutError,
    MalformedOutputError,
    SessionNotFoundError,
)
from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import Invocation, Manifest, PipelineRef, StepRecord
from gauntlet.engine.timing import (
    activity_kind,
    build_timing,
    fmt_duration,
    outcome_of,
    parked_seconds_from_events,
    record_invocation,
    render_timing,
)

from conftest import FakeAdapter
from test_cycle import CONFIRM, REVIEW, SeqAdapter, V, cycle_repo, run_cycle  # noqa: F401
from test_steptypes import _orch

T0 = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)


def _ts(minutes: float) -> str:
    return (T0 + timedelta(minutes=minutes)).isoformat()


def _inv(label, wall_s, *, agent="builder", outcome="ok", attempt=1):
    return Invocation(
        agent=agent, label=label, started=_ts(0), ended=_ts(wall_s / 60),
        wall_s=wall_s, outcome=outcome, attempt=attempt,
    )


def _manifest(status=M.RUN_DONE) -> Manifest:
    return Manifest(
        run_id="run-x", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:h"),
        status=status,
    )


# --- capture -------------------------------------------------------------------
class _Ctx:
    def __init__(self, record):
        self.record = record


def test_record_invocation_appends_ok_record_with_label_agent_attempt():
    rec = StepRecord(id="implement", type="agent_task", agent="builder", attempts=2)
    with record_invocation(_Ctx(rec), agent="builder", label="call"):
        pass
    assert len(rec.invocations) == 1
    inv = rec.invocations[0]
    assert (inv.agent, inv.label, inv.outcome, inv.attempt) == ("builder", "call", "ok", 2)
    assert inv.wall_s >= 0.0
    assert datetime.fromisoformat(inv.ended) >= datetime.fromisoformat(inv.started)


@pytest.mark.parametrize(
    "exc, outcome",
    [
        (AgentTimeoutError("t"), "timeout"),
        (SessionNotFoundError("s"), "session_not_found"),
        (MalformedOutputError("m"), "malformed"),
        (AgentFailedError("f"), "failed"),
        (AdapterError("a"), "failed"),
        (RuntimeError("boom"), "error"),
    ],
)
def test_record_invocation_records_failure_outcome_and_reraises(exc, outcome):
    rec = StepRecord(id="cycle", type="adversarial_cycle")
    with pytest.raises(type(exc)):
        with record_invocation(_Ctx(rec), agent="reviewer", label="r1-review"):
            raise exc
    assert [i.outcome for i in rec.invocations] == [outcome]
    assert outcome_of(None) == "ok"


def test_record_invocation_is_append_only_across_calls():
    rec = StepRecord(id="cycle", type="adversarial_cycle")
    ctx = _Ctx(rec)
    with record_invocation(ctx, agent="reviewer", label="r1-review"):
        pass
    with record_invocation(ctx, agent="triage", label="r1-triage"):
        pass
    assert [i.label for i in rec.invocations] == ["r1-review", "r1-triage"]


def test_record_invocation_without_a_record_is_a_noop():
    class Bare:
        pass

    with record_invocation(Bare(), agent="x", label="call"):
        pass  # no AttributeError, nothing to record on


# --- engine wiring ---------------------------------------------------------------
def test_driven_cycle_records_labelled_invocations(cycle_repo):
    reviewer = SeqAdapter(REVIEW({"id": "F-001", "severity": "major", "claim": "c",
                                  "evidence": "e", "category": "correctness",
                                  "location": {"file": "prd.md"}}),
                          CONFIRM({"finding_id": "F-001", "verdict": "resolved"}))
    triage = SeqAdapter(V("F-001"))
    builder = FakeAdapter(text="fixed", writes={"prd.md": "ARTIFACT-BODY-SENTINEL\nfixed\n"})
    status, man, _ = run_cycle(
        cycle_repo, {"reviewer": reviewer, "triage": triage, "builder": builder}
    )
    assert status == M.RUN_DONE
    rec = man.record("cycle")
    labels = [i.label for i in rec.invocations]
    assert labels == ["r1-review", "r1-triage", "r1-fix", "r1-confirm"]
    assert [i.agent for i in rec.invocations] == ["reviewer", "triage", "builder", "reviewer"]
    assert all(i.outcome == "ok" and i.wall_s >= 0 for i in rec.invocations)
    # Persisted through the manifest's own round-trip, not just in memory.
    loaded = Manifest.model_validate_json(man.model_dump_json())
    assert [i.label for i in loaded.record("cycle").invocations] == labels


def test_driven_agent_task_and_commit_record_invocations(fixture_repo):
    builder = FakeAdapter(text="done", writes={"a.txt": "a\n"})
    drafter = FakeAdapter(text="P1: Add a\n\nbody text.\n")
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: commit, type: commit, message_agent: msg, max_redrafts: 0}
"""
    cfg = {"agents": {"builder": {"adapter": "claude-code"}, "msg": {"adapter": "api", "model": "h"}}}
    orch = _orch(fixture_repo, text, config=cfg, adapters={"builder": builder, "msg": drafter})
    assert orch.drive() == M.RUN_DONE
    impl = orch.manifest.record("implement")
    assert [(i.agent, i.label, i.outcome) for i in impl.invocations] == [
        ("builder", "call", "ok")
    ]
    commit = orch.manifest.record("commit")
    assert [(i.agent, i.label) for i in commit.invocations] == [("msg", "commit-message")]


# --- journal replay of parked intervals ---------------------------------------
def _event(seq, minutes, steps):
    """A journal event with a state snapshot carrying the given step rows."""
    return {
        "seq": seq, "ts": _ts(minutes),
        "state_json": json.dumps({"run_id": "r", "steps": steps}),
    }


def _row(id_, status, *, reason=None, type_="agent_task", iteration=None):
    return {"id": id_, "iteration": iteration, "type": type_, "status": status,
            "parked_reason": reason}


def test_parked_seconds_replay_credits_each_park_to_its_reason():
    events = [
        _event(1, 0, [_row("cycle", "running")]),
        _event(2, 10, [_row("cycle", "parked", reason="usage_limit")]),
        _event(3, 25, [_row("cycle", "running")]),          # resumed after 15m
        _event(4, 30, [_row("cycle", "parked", reason="response")]),
        _event(5, 40, [_row("cycle", "done")]),             # 10m on response
        _event(6, 41, [_row("cycle", "done"), _row("gate", "parked", type_="human_gate")]),
    ]
    end = T0 + timedelta(minutes=61)  # the gate is still parked: 20m so far
    out = parked_seconds_from_events(events, end=end)
    assert out[("cycle", None)] == {"usage_limit": 15 * 60.0, "response": 10 * 60.0}
    assert out[("gate", None)] == {"gate": 20 * 60.0}


def test_parked_seconds_replay_skips_unparseable_and_orders_by_seq():
    events = [
        _event(3, 20, [_row("s", "done")]),
        {"seq": 2, "ts": "not-a-time", "state_json": json.dumps({"steps": []})},
        {"seq": 4, "ts": _ts(5), "state_json": "{broken"},
        _event(1, 0, [_row("s", "parked", reason="gate")]),
        {"seq": 5, "ts": _ts(30)},  # no state snapshot at all
    ]
    out = parked_seconds_from_events(events, end=T0 + timedelta(hours=1))
    assert out == {("s", None): {"gate": 20 * 60.0}}


# --- aggregation ----------------------------------------------------------------
def _timed_manifest() -> Manifest:
    man = _manifest()
    man.steps.append(StepRecord(
        id="implement", type="agent_task", agent="builder", status=M.DONE, attempts=1,
        started=_ts(0), ended=_ts(60),
        invocations=[_inv("call", 50 * 60)],
    ))
    man.steps.append(StepRecord(
        id="impl-cycle", type="adversarial_cycle", status=M.DONE, attempts=2,
        started=_ts(60), ended=_ts(120),
        invocations=[
            _inv("r1-review", 10 * 60, agent="reviewer"),
            _inv("r1-review-1-gemini", 4 * 60, agent="gemini"),
            _inv("r1-triage", 60, agent="triage"),
            _inv("r1-triage", 60, agent="triage"),
            _inv("r1-fix", 8 * 60, agent="builder", outcome="failed"),
            _inv("r1-fix", 6 * 60, agent="builder"),
            _inv("r1-confirm", 3 * 60, agent="reviewer"),
            _inv("r1-verify", 2 * 60, agent="verifier"),
        ],
    ))
    man.steps.append(StepRecord(
        id="gate", type="human_gate", status=M.DONE, started=_ts(120), ended=_ts(150),
    ))
    man.agent_usage["builder"] = M.UsageTotals(input_tokens=1)
    man.agent_usage["legacy"] = M.UsageTotals(input_tokens=1)  # billed, no calls
    man.suspensions.append(M.Suspension(start=_ts(70), end=_ts(72), gap_s=120))
    return man


def _journal_for_timed_manifest():
    return [
        _event(1, 60, [_row("implement", "done"), _row("impl-cycle", "running", type_="adversarial_cycle")]),
        _event(2, 90, [_row("implement", "done"), _row("impl-cycle", "parked", reason="usage_limit", type_="adversarial_cycle")]),
        _event(3, 100, [_row("implement", "done"), _row("impl-cycle", "running", type_="adversarial_cycle")]),
        _event(4, 120, [_row("implement", "done"), _row("impl-cycle", "done", type_="adversarial_cycle"),
                        _row("gate", "parked", type_="human_gate")]),
        _event(5, 150, [_row("implement", "done"), _row("impl-cycle", "done", type_="adversarial_cycle"),
                        _row("gate", "done", type_="human_gate")]),
    ]


def test_build_timing_splits_overall_into_agent_parked_suspended_other():
    data = build_timing(
        _timed_manifest(), events=_journal_for_timed_manifest(),
        model_of={"builder": "claude-code/opus", "reviewer": "codex/gpt-5.5"},
        now=T0 + timedelta(hours=5),
    )
    assert data.overall_s == pytest.approx(150 * 60)
    assert not data.in_progress
    # 50m implement + 34m of cycle calls
    assert data.active_s == pytest.approx((50 + 10 + 4 + 1 + 1 + 8 + 6 + 3 + 2) * 60)
    assert data.calls == 9
    assert data.parked == {"usage_limit": 10 * 60.0, "gate": 30 * 60.0}
    assert data.suspended_s == 120.0
    assert data.other_s == pytest.approx(150 * 60 - data.active_s - 40 * 60 - 120)
    assert data.has_journal and data.steps_without_calls == 0


def test_build_timing_per_step_rows():
    data = build_timing(
        _timed_manifest(), events=_journal_for_timed_manifest(), now=T0 + timedelta(hours=5)
    )
    by = {s.leaf: s for s in data.steps}
    impl, cycle, gate = by["implement"], by["impl-cycle"], by["gate"]
    assert impl.wall_s == pytest.approx(3600) and impl.active_s == pytest.approx(3000)
    assert impl.parked == {} and impl.calls == 1 and impl.by_kind == {"implement": 3000.0}
    assert cycle.wall_s == pytest.approx(3600) and cycle.calls == 8 and cycle.attempts == 2
    assert cycle.parked == {"usage_limit": 600.0}
    assert cycle.by_kind == {
        "review": 14 * 60.0, "triage": 120.0, "fix": 14 * 60.0,
        "confirm": 180.0, "verify": 120.0,
    }
    assert gate.active_s is None and gate.calls == 0 and gate.parked == {"gate": 1800.0}


def test_build_timing_per_agent_maps_model_and_lists_billed_profiles_without_calls():
    data = build_timing(
        _timed_manifest(), model_of={"builder": "claude-code/opus"}, now=T0 + timedelta(hours=5)
    )
    by = {a.agent: a for a in data.agents}
    assert by["builder"].model == "claude-code/opus"
    assert by["builder"].calls == 3  # implement call + failed fix + fix
    assert by["builder"].active_s == pytest.approx((50 + 8 + 6) * 60)
    assert by["builder"].pct == pytest.approx(100.0 * 64 / 85)
    assert by["reviewer"].model is None and by["reviewer"].calls == 2
    legacy = by["legacy"]
    assert legacy.calls == 0 and legacy.active_s is None and legacy.pct is None


def test_build_timing_per_activity_pools_cycle_kinds_first_then_steps_by_time():
    data = build_timing(_timed_manifest(), now=T0 + timedelta(hours=5))
    assert [k.kind for k in data.kinds] == [
        "review", "triage", "fix", "confirm", "verify", "implement",
    ]
    by = {k.kind: k for k in data.kinds}
    assert by["review"].calls == 2 and by["review"].active_s == pytest.approx(14 * 60)
    assert by["fix"].calls == 2  # the failed attempt's time is real time
    assert by["implement"].pct == pytest.approx(100.0 * 50 / 85)


def test_build_timing_without_journal_counts_only_gate_parks():
    data = build_timing(_timed_manifest(), events=None, now=T0 + timedelta(hours=5))
    assert not data.has_journal
    by = {s.leaf: s for s in data.steps}
    # A gate step's own span is its park (data, not a guess); every other
    # step's park kind is unavailable.
    assert by["gate"].parked == {"gate": 30 * 60.0}
    assert by["implement"].parked is None and by["impl-cycle"].parked is None
    assert data.parked == {"gate": 30 * 60.0}
    # `other` then absorbs the non-gate parks (overall − agent − gate − suspended).
    assert data.other_s == pytest.approx(150 * 60 - data.active_s - 30 * 60 - 120)
    text = render_timing(data)
    assert "no state journal" in text
    assert "includes non-gate parks — no journal" in text


def test_build_timing_without_journal_credits_a_live_gate_park_up_to_now():
    man = _manifest(status=M.RUN_PARKED)
    man.steps.append(StepRecord(id="gate", type="human_gate", status=M.PARKED,
                                started=_ts(0), ended=_ts(0)))  # ended stamped at park
    data = build_timing(man, now=T0 + timedelta(minutes=25))
    assert data.steps[0].parked == {"gate": 25 * 60.0}
    assert data.parked == {"gate": 25 * 60.0}


def test_build_timing_live_run_closes_open_intervals_at_now():
    man = _manifest(status=M.RUN_RUNNING)
    man.steps.append(StepRecord(
        id="implement", type="agent_task", agent="builder", status=M.RUNNING,
        started=_ts(0), invocations=[_inv("call", 600)],
    ))
    now = T0 + timedelta(minutes=45)
    data = build_timing(man, now=now)
    assert data.in_progress
    assert data.overall_s == pytest.approx(45 * 60)
    assert data.steps[0].wall_s == pytest.approx(45 * 60)
    assert "in progress" in render_timing(data)
    # A parked (live) run's open park is credited up to now.
    man.status = M.RUN_PARKED
    man.steps[0].status = M.PARKED
    man.steps[0].ended = _ts(30)
    events = [_event(1, 0, [_row("implement", "running")]),
              _event(2, 30, [_row("implement", "parked", reason="response")])]
    data = build_timing(man, events=events, now=now)
    assert data.parked == {"response": 15 * 60.0}
    assert data.steps[0].wall_s == pytest.approx(30 * 60)  # ended is stamped on park


def test_build_timing_legacy_run_without_calls_reports_unavailable_agent_time():
    man = _manifest()
    man.steps.append(StepRecord(
        id="implement", type="agent_task", agent="builder", status=M.DONE,
        started=_ts(0), ended=_ts(30),
    ))
    man.steps.append(StepRecord(id="gate", type="human_gate", status=M.DONE,
                                started=_ts(30), ended=_ts(40)))
    data = build_timing(man, now=T0 + timedelta(hours=1))
    assert data.active_s is None and data.calls == 0 and data.other_s is None
    assert data.steps_without_calls == 1  # the gate never calls an agent
    assert data.overall_s == pytest.approx(40 * 60)
    assert data.parked == {"gate": 10 * 60.0}  # the gate's own span, no journal needed
    text = render_timing(data)
    assert "recorded no per-call timing" in text
    assert "(no agent calls recorded)" in text


def test_build_timing_empty_manifest_renders():
    data = build_timing(_manifest(), now=T0)
    assert data.overall_s is None and data.first_started is None
    text = render_timing(data)
    assert "Time report" in text and "(no steps recorded)" in text


def test_build_timing_tolerates_naive_and_malformed_stamps():
    man = _manifest()
    man.steps.append(StepRecord(id="a", type="agent_task", agent="b", status=M.DONE,
                                started="2026-09-01T10:00:00", ended="2026-09-01T10:10:00"))
    man.steps.append(StepRecord(id="c", type="agent_task", agent="b", status=M.DONE,
                                started="garbage", ended=None))
    data = build_timing(man, now=T0 + timedelta(hours=1))
    by = {s.leaf: s for s in data.steps}
    assert by["a"].wall_s == pytest.approx(600) and by["c"].wall_s is None
    assert data.overall_s == pytest.approx(600)


# --- rendering ------------------------------------------------------------------
def test_render_timing_has_every_section_and_breakdown():
    data = build_timing(
        _timed_manifest(), events=_journal_for_timed_manifest(),
        model_of={"builder": "claude-code/opus"}, now=T0 + timedelta(hours=5),
    )
    text = render_timing(data)
    assert "Time report — run run-x (demo) [done]" in text
    for heading in ("overall:", "agent time:", "parked:", "suspended:", "other:",
                    "Per agent profile", "Per activity", "Per step:"):
        assert heading in text
    assert "gate 30m 00s" in text and "usage_limit 10m 00s" in text
    assert "claude-code/opus" in text
    assert "review 14m 00s · triage 2m 00s · fix 14m 00s · confirm 3m 00s · verify 2m 00s" in text
    assert "2h 30m" in text  # overall


@pytest.mark.parametrize(
    "secs, cell",
    [(None, "—"), (0, "0s"), (42.4, "42s"), (65, "1m 05s"), (3599.6, "60m 00s"),
     (3600, "1h 00m"), (7380, "2h 03m")],
)
def test_fmt_duration(secs, cell):
    assert fmt_duration(secs) == cell


@pytest.mark.parametrize(
    "label, step_id, kind",
    [("r1-review", "impl-cycle", "review"), ("r12-review-1-gemini", "c", "review"),
     ("r2-triage", "c", "triage"), ("r1-fix", "c", "fix"), ("r1-confirm", "c", "confirm"),
     ("r1-verify", "c", "verify"), ("response-disposition", "c", "disposition"),
     ("call", "implement", "implement"), ("call-repair1", "implement", "implement"),
     ("commit-message", "commit", "commit")],
)
def test_activity_kind(label, step_id, kind):
    assert activity_kind(label, step_id) == kind


# --- manifest additivity ----------------------------------------------------------
def test_manifest_without_invocations_field_loads_with_empty_list(tmp_path):
    man = _manifest()
    man.steps.append(StepRecord(id="a", type="agent_task", agent="b"))
    raw = json.loads(man.model_dump_json())
    del raw["steps"][0]["invocations"]  # a pre-timing manifest
    loaded = Manifest.model_validate_json(json.dumps(raw))
    assert loaded.record("a").invocations == []


def test_manifest_round_trips_invocations(tmp_path):
    man = _manifest()
    man.steps.append(StepRecord(id="a", type="agent_task", agent="b",
                                invocations=[_inv("call", 12.5, outcome="malformed")]))
    path = tmp_path / "manifest.json"
    man.write_atomic(path)
    loaded = Manifest.load(path)
    inv = loaded.record("a").invocations[0]
    assert (inv.label, inv.wall_s, inv.outcome, inv.attempt) == ("call", 12.5, "malformed", 1)


# --- end to end: a real drive's journal replays into parked time --------------
def test_real_journal_from_a_driven_gate_park_replays_parked_time(fixture_repo):
    from gauntlet.engine import journal

    builder = FakeAdapter(text="done", writes={"a.txt": "a\n"})
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: gate, type: human_gate}
"""
    orch = _orch(fixture_repo, text, adapters={"builder": builder})
    assert orch.drive() == M.RUN_PARKED
    events = journal.read_events(orch.run_dir)
    assert events and any(e.get("state_json") for e in events)
    now = datetime.now(timezone.utc) + timedelta(minutes=10)
    data = build_timing(orch.manifest, events=events, now=now)
    assert data.has_journal and data.in_progress
    by = {s.leaf: s for s in data.steps}
    assert by["implement"].calls == 1 and by["implement"].parked == {}
    assert set(by["gate"].parked) == {"gate"}
    assert by["gate"].parked["gate"] >= 9 * 60  # parked since the drive, up to `now`
    assert data.parked == by["gate"].parked
    text_out = render_timing(data)
    assert "gate" in text_out and "in progress" in text_out

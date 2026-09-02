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


def _inv(
    label, wall_s, *, agent="builder", outcome="ok", attempt=1, start_min=0
):
    return Invocation(
        agent=agent, label=label, started=_ts(start_min),
        ended=_ts(start_min + wall_s / 60),
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
            # The ensemble reviews and per-finding triage calls overlap. Their
            # per-profile rows retain agent-seconds; the overall split uses the
            # elapsed-time union.
            _inv("r1-review", 10 * 60, agent="reviewer", start_min=60),
            _inv("r1-review-1-gemini", 4 * 60, agent="gemini", start_min=60),
            _inv("r1-triage", 60, agent="triage", start_min=70),
            _inv("r1-triage", 60, agent="triage", start_min=70),
            _inv(
                "r1-fix", 8 * 60, agent="builder", outcome="failed", start_min=71
            ),
            _inv("r1-fix", 6 * 60, agent="builder", start_min=79),
            _inv("r1-confirm", 3 * 60, agent="reviewer", start_min=85),
            _inv("r1-verify", 2 * 60, agent="verifier", start_min=88),
        ],
    ))
    man.steps.append(StepRecord(
        id="gate", type="human_gate", status=M.DONE, started=_ts(120), ended=_ts(150),
    ))
    man.agent_usage["builder"] = M.UsageTotals(input_tokens=1)
    man.agent_usage["legacy"] = M.UsageTotals(input_tokens=1)  # billed, no calls
    man.suspensions.append(M.Suspension(start=_ts(55), end=_ts(57), gap_s=120))
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
    # Agent-seconds preserve all nine calls (85m); elapsed agent time unions the
    # overlapping ensemble/triage calls (80m) for a true overall partition.
    assert data.agent_seconds_s == pytest.approx(
        (50 + 10 + 4 + 1 + 1 + 8 + 6 + 3 + 2) * 60
    )
    assert data.active_s == pytest.approx(80 * 60)
    assert data.calls == 9
    assert data.parked == {"usage_limit": 10 * 60.0, "gate": 30 * 60.0}
    assert data.suspended_s == 120.0
    assert data.other_s == pytest.approx(150 * 60 - data.active_s - 40 * 60 - 120)
    assert data.has_journal and data.steps_without_calls == 0


def test_build_timing_partition_deduplicates_calls_parks_and_suspensions():
    man = _manifest()
    man.steps.append(StepRecord(
        id="cycle", type="adversarial_cycle", status=M.DONE,
        started=_ts(0), ended=_ts(60),
        invocations=[
            _inv("r1-triage", 30 * 60, agent="triage"),
            _inv("r1-triage", 30 * 60, agent="triage"),
        ],
    ))
    # One suspension overlaps the calls and one overlaps the park. Suspension
    # takes precedence, then park, then the union of calls.
    man.suspensions = [
        M.Suspension(start=_ts(10), end=_ts(20), gap_s=600),
        M.Suspension(start=_ts(40), end=_ts(50), gap_s=600),
    ]
    events = [
        _event(1, 0, [_row("cycle", "running", type_="adversarial_cycle")]),
        _event(2, 30, [_row(
            "cycle", "parked", reason="response", type_="adversarial_cycle"
        )]),
        _event(3, 60, [_row("cycle", "done", type_="adversarial_cycle")]),
    ]
    data = build_timing(man, events=events, now=T0 + timedelta(hours=2))
    assert data.overall_s == 60 * 60
    assert data.agent_seconds_s == 60 * 60  # two concurrent 30m calls
    assert data.active_s == 20 * 60         # union 0–30 minus suspend 10–20
    assert data.parked == {"response": 20 * 60.0}  # 30–60 minus suspend 40–50
    assert data.suspended_s == 20 * 60
    assert data.other_s == 0
    assert (
        data.active_s + sum(data.parked.values())
        + data.suspended_s + data.other_s
    ) == data.overall_s


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
    assert data.active_s is None and data.agent_seconds_s is None
    assert data.calls == 0 and data.other_s is None
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
    for heading in ("overall:", "agent time:", "agent-secs:", "parked:",
                    "suspended:", "other:",
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
    # The overall partition is clipped to the manifest's first-start → now
    # bounds; the per-step evidence is the raw journal interval. Those clocks
    # can differ by the sub-second write-ahead gap in this real-drive fixture.
    assert data.parked["gate"] == pytest.approx(
        by["gate"].parked["gate"], abs=2.0
    )
    text_out = render_timing(data)
    assert "gate" in text_out and "in progress" in text_out


# --- provenance: adapter / model / effort frozen on each call ------------------
class _CfgCtx:
    """A context whose config resolves one profile, like a real StepContext."""

    def __init__(self, record, config):
        self.record = record
        self.config = config


def test_record_invocation_freezes_profile_adapter_model_effort():
    from gauntlet.engine.config import RunConfig

    cfg = RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "claude-code", "model": "opus", "effort": "high"}}}
    )
    rec = StepRecord(id="implement", type="agent_task", agent="builder")
    with record_invocation(_CfgCtx(rec, cfg), agent="builder", label="call"):
        pass
    inv = rec.invocations[0]
    assert (inv.adapter, inv.model, inv.effort) == ("claude-code", "opus", "high")


def test_record_invocation_built_adapter_model_and_effort_override_win():
    from types import SimpleNamespace

    from gauntlet.engine.config import RunConfig

    cfg = RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "codex", "model": "gpt-5.5", "effort": "medium"}}}
    )
    rec = StepRecord(id="implement", type="agent_task", agent="builder")
    with record_invocation(
        _CfgCtx(rec, cfg), agent="builder", label="call",
        adapter=SimpleNamespace(model="gpt-5.5-pro"), effort="xhigh",
    ):
        pass
    inv = rec.invocations[0]
    assert (inv.adapter, inv.model, inv.effort) == ("codex", "gpt-5.5-pro", "xhigh")


def test_record_invocation_unknown_profile_or_no_config_records_none():
    from gauntlet.engine.config import RunConfig

    cfg = RunConfig.model_validate({"agents": {"builder": {"adapter": "claude-code"}}})
    rec = StepRecord(id="x", type="agent_task", agent="ghost")
    with record_invocation(_CfgCtx(rec, cfg), agent="ghost", label="call"):
        pass
    with record_invocation(_Ctx(rec), agent="builder", label="call"):
        pass
    assert [(i.adapter, i.model, i.effort) for i in rec.invocations] == [
        (None, None, None), (None, None, None),
    ]


def test_driven_cycle_freezes_each_role_provenance(cycle_repo):
    reviewer = SeqAdapter(REVIEW(), CONFIRM())
    triage = SeqAdapter()
    builder = FakeAdapter(text="fixed")
    status, man, _ = run_cycle(
        cycle_repo, {"reviewer": reviewer, "triage": triage, "builder": builder}
    )
    assert status == M.RUN_DONE
    # BASE_CONFIG: reviewer=codex (no model), triage=api/h; zero findings → the
    # cycle converges after review alone.
    provenance = [(i.agent, i.adapter, i.model) for i in man.record("cycle").invocations]
    assert provenance == [("reviewer", "codex", None)]


def test_build_timing_prefers_frozen_model_over_live_config():
    man = _manifest()
    man.steps.append(StepRecord(
        id="implement", type="agent_task", agent="builder", status=M.DONE,
        started=_ts(0), ended=_ts(10),
        invocations=[
            Invocation(agent="builder", label="call", started=_ts(0), ended=_ts(5),
                       wall_s=300, adapter="claude-code", model="opus", effort="high"),
            Invocation(agent="builder", label="call-repair1", started=_ts(5), ended=_ts(9),
                       wall_s=240, adapter="claude-code", model="opus", effort="high"),
            Invocation(agent="reviewer", label="r1-review", started=_ts(9), ended=_ts(10),
                       wall_s=60),  # nothing frozen → falls back to config
        ],
    ))
    data = build_timing(
        man, model_of={"builder": "claude-code/sonnet", "reviewer": "codex/gpt-5.5"},
        now=T0 + timedelta(hours=1),
    )
    by = {a.agent: a for a in data.agents}
    assert by["builder"].model == "claude-code/opus@high"  # what ran, not today's config
    assert by["reviewer"].model == "codex/gpt-5.5"
    assert "claude-code/opus@high" in render_timing(data)


# --- raw token counters flow through every accumulator ------------------------
def test_usage_totals_accumulate_cache_writes_and_reasoning():
    from gauntlet.adapters.base import Usage

    t = M.UsageTotals()
    t.add(Usage(input_tokens=1, output_tokens=2, cache_creation_input_tokens=700,
                reasoning_output_tokens=50))
    t.add(Usage(input_tokens=1, output_tokens=2))  # counters absent → unchanged
    t.add(M.UsageTotals(cache_creation_input_tokens=300, reasoning_output_tokens=5))
    assert (t.cache_creation_input_tokens, t.reasoning_output_tokens) == (1000, 55)
    raw = json.loads(t.model_dump_json())
    del raw["cache_creation_input_tokens"]; del raw["reasoning_output_tokens"]
    assert M.UsageTotals.model_validate(raw).cache_creation_input_tokens == 0


def test_usage_accumulator_carries_raw_counters_per_agent():
    from gauntlet.adapters.base import Usage
    from gauntlet.engine.steptypes import _UsageAccumulator

    acc = _UsageAccumulator()
    acc.add(Usage(input_tokens=5, cache_creation_input_tokens=100), agent="builder")
    acc.add(Usage(input_tokens=5, reasoning_output_tokens=40), agent="reviewer")
    other = _UsageAccumulator()
    other.add(Usage(input_tokens=1, reasoning_output_tokens=2), agent="reviewer")
    acc.merge(other)
    total = acc.result()
    assert (total.cache_creation_input_tokens, total.reasoning_output_tokens) == (100, 42)
    by = acc.by_agent()
    assert by["builder"].cache_creation_input_tokens == 100
    assert by["builder"].reasoning_output_tokens is None  # never reported → None
    assert by["reviewer"].reasoning_output_tokens == 42


def test_cost_report_shows_raw_counters():
    from gauntlet.engine.report import build_report, render_report

    man = _manifest()
    man.agent_usage["builder"] = M.UsageTotals(
        input_tokens=10, output_tokens=5, cache_creation_input_tokens=700,
        reasoning_output_tokens=0,
    )
    man.agent_usage["reviewer"] = M.UsageTotals(
        input_tokens=10, output_tokens=5, reasoning_output_tokens=44,
    )
    man.totals = M.UsageTotals(input_tokens=20, output_tokens=10,
                               cache_creation_input_tokens=700, reasoning_output_tokens=44)
    data = build_report(man)
    by = {a.agent: a for a in data.agents}
    assert by["builder"].cache_creation_input_tokens == 700
    assert by["reviewer"].reasoning_output_tokens == 44
    assert (data.total_cache_creation_input, data.total_reasoning_output) == (700, 44)
    text = render_report(man)
    assert "cache-w" in text and "reason" in text
    assert "700 cache-w / 44 reasoning out" in text


# --- effective config snapshot at run start ------------------------------------
def test_run_start_snapshots_effective_config(tmp_path):
    from conftest import git
    from gauntlet.engine.run import RunManager, render_config_snapshot

    import yaml as _yaml

    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@gauntlet.local")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("snapshot fixture\n")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(
        "base_branch: main\nrun_root: runs\n"
        "agents:\n  builder: {adapter: claude-code, model: opus, effort: high}\n"
    )
    (repo / "pipelines").mkdir()
    (repo / "pipelines" / "one.yaml").write_text(
        "name: one\nversion: 1\nstages:\n  - id: s\n    steps:\n"
        "      - {id: implement, type: agent_task, agent: builder, prompt_text: go}\n"
    )
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "-M", "main")
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# PRD\n\nReal human-authored PRD.\n")
    status = mgr.start(
        "demo", repo / "pipelines" / "one.yaml", use_judge=False,
        adapter_factory=lambda n: FakeAdapter(text="done", writes={"a.txt": "a\n"}),
    )
    assert status == M.RUN_DONE
    run_dir = mgr.layout("demo").active_run_dir()
    snap = (run_dir / "config.yaml").read_text()
    assert snap.startswith("# Effective run configuration")
    data = _yaml.safe_load(snap)
    assert data["agents"]["builder"]["model"] == "opus"
    assert data["agents"]["builder"]["effort"] == "high"
    assert data["run_root"] == "runs"
    assert "step_timeout_s" in data["agents"]["builder"]  # defaults made explicit
    assert snap == render_config_snapshot(mgr.config)
    # The pipeline snapshot beside it is untouched.
    assert (run_dir / "pipeline.yaml").exists()
    # Evidence, not state: the manifest and drive are unaffected by its presence.
    assert mgr.status("demo").record("implement").invocations[0].model == "opus"

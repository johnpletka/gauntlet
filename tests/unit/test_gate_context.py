"""Gate decision context (harness-efficiency P8, FR-8.1/FR-8.2).

The gate block must make a gate decision possible from ``status --json`` (and the
web GateView) alone (PRD G6): the upstream cycle's convergence summary, the prior
human ``--response``/rejection decisions, and the per-escalated-finding triage
reasoning — all sourced from the manifest + the cycle's persisted artifacts, never
a transcript. FR-8.2: the gate's ``next_actions`` carry a one-line consequence,
and a reject names the cycle it re-runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from gauntlet.adapters._structured import validate_schema
from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine.manifest import (
    Checkpoint,
    HumanResponse,
    Manifest,
    PipelineRef,
    StepRecord,
)
from gauntlet.engine.pipeline import Pipeline
from gauntlet.logging.redact import RedactionSettings

STATUS_SCHEMA = json.loads(
    (Path(__file__).resolve().parents[2] / "schemas" / "status.json").read_text()
)


# --------------------------------------------------------------------------- #
# fixture: a converged 2-round cycle + one rejection, parked at the gate
# --------------------------------------------------------------------------- #
def _round_findings(ids: list[str]) -> dict:
    return {
        "findings": [
            {"id": i, "severity": "major", "category": "correctness",
             "location": "x.py:1", "claim": f"claim {i}", "evidence": "e",
             "suggested_fix": None}
            for i in ids
        ],
        "open_questions": [], "summary": "s",
    }


def _round_triage(verdicts: list[tuple[str, str]]) -> dict:
    # verdicts: (finding_id, action)
    return {
        "verdicts": [
            {"finding_id": fid, "verdict": "legitimate", "reasoning": f"r {fid}",
             "action": action, "confidence": "high", "target_artifact": None}
            for fid, action in verdicts
        ]
    }


def _write_cycle_run(tmp: Path) -> tuple[Manifest, StepRecord, StepRecord, Path]:
    """A run parked at ``plan-approve`` behind a converged ``plan-cycle`` that ran
    two rounds and took one gate rejection. Returns (man, cycle_rec, gate_rec,
    run_instance_dir) with all round artifacts on disk."""
    run_dir = tmp / "runs" / "demo" / "run-1"
    (run_dir / "artifacts" / "r1").mkdir(parents=True)
    (run_dir / "artifacts" / "r2").mkdir(parents=True)
    arts = run_dir / "artifacts"

    # Round 1: 3 raised, 2 fix_now, 1 reject.
    (arts / "r1" / "findings.json").write_text(
        json.dumps(_round_findings(["F-001", "F-002", "F-003"]))
    )
    (arts / "r1" / "triage.json").write_text(
        json.dumps(_round_triage(
            [("F-001", "fix_now"), ("F-002", "fix_now"), ("F-003", "reject")]
        ))
    )
    # Round 2: 2 raised, 1 fix_now, 1 defer.
    (arts / "r2" / "findings.json").write_text(
        json.dumps(_round_findings(["F-004", "F-005"]))
    )
    (arts / "r2" / "triage.json").write_text(
        json.dumps(_round_triage([("F-004", "fix_now"), ("F-005", "defer")]))
    )
    # Latest-round-wins artifacts the gate `show:`s — one verdict flagged
    # escalated (blocking) + one flagged low_confidence (minor) → both surface.
    (arts / "findings.json").write_text(json.dumps({
        "findings": [
            {"id": "F-004", "severity": "blocking", "category": "correctness",
             "location": "x.py:9", "claim": "still broken", "evidence": "e",
             "suggested_fix": None},
            {"id": "F-005", "severity": "nit", "category": "style",
             "location": "x.py:2", "claim": "naming", "evidence": "e",
             "suggested_fix": None},
        ],
        "open_questions": [], "summary": "s",
    }))
    (arts / "triage.json").write_text(json.dumps({
        "verdicts": [
            {"finding_id": "F-004", "verdict": "legitimate",
             "reasoning": "blocking; human must decide", "action": "fix_now",
             "confidence": "low", "target_artifact": None, "escalated": True},
            {"finding_id": "F-005", "verdict": "bikeshedding",
             "reasoning": "low-confidence nit, carried for human eyes",
             "action": "defer", "confidence": "low", "target_artifact": None,
             "low_confidence": True},
        ]
    }))

    cp = lambda sub, rnd: Checkpoint(
        sub_step=sub, round=rnd, handoff_sha=f"sha{rnd}",
        artifact=f"artifacts/r{rnd}/{sub}.json" if sub != "fix" else None,
    )
    # Checkpoint sub_step "review" persists findings.json, "triage" persists
    # triage.json — the artifact filename differs from the sub_step name, so map.
    cycle_rec = StepRecord(
        id="plan-cycle", type="adversarial_cycle", status=M.DONE,
        metrics={"rounds": 2, "findings_total": 5, "accepted_total": 3,
                 "accepted_resolved_total": 3, "verdict_counts": {},
                 "confirm_counts": {}},
        checkpoints=[
            Checkpoint(sub_step="review", round=1, handoff_sha="sha1",
                       artifact="artifacts/r1/findings.json"),
            Checkpoint(sub_step="triage", round=1, handoff_sha="sha1",
                       artifact="artifacts/r1/triage.json"),
            Checkpoint(sub_step="review", round=2, handoff_sha="sha2",
                       artifact="artifacts/r2/findings.json"),
            Checkpoint(sub_step="triage", round=2, handoff_sha="sha2",
                       artifact="artifacts/r2/triage.json"),
        ],
        human_responses=[
            HumanResponse(
                response_id="plan-cycle-resp-1",
                response_text="tighten the FR-3 wording",
                timestamp="2026-07-03T10-00-00Z", user="john",
                response_attempt=1, state="consumed",
            )
        ],
    )
    gate_rec = StepRecord(id="plan-approve", type="human_gate", status=M.PARKED,
                          notes="awaiting human decision")
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:x"),
        status=M.RUN_PARKED, current_step="plan-approve",
        steps=[cycle_rec, gate_rec],
    )
    man.write_atomic(run_dir / "manifest.json")
    return man, cycle_rec, gate_rec, run_dir


# --------------------------------------------------------------------------- #
# FR-8.1 — all three sections from manifest + artifacts alone
# --------------------------------------------------------------------------- #
def test_gate_context_renders_all_three_sections(tmp_path):
    man, cycle_rec, gate_rec, run_dir = _write_cycle_run(tmp_path)
    ctx = op.compute_gate_context(man, run_dir, gate_rec)

    # (1) convergence: aggregate + per-round raised/fixed/declined.
    assert ctx["cycle_step_id"] == "plan-cycle"
    conv = ctx["convergence"]
    assert conv["rounds"] == 2
    assert conv["findings_total"] == 5
    assert conv["accepted_total"] == 3
    assert conv["per_round"] == [
        {"round": 1, "raised": 3, "fixed": 2, "declined": 1},
        {"round": 2, "raised": 2, "fixed": 1, "declined": 1},
    ]

    # (2) prior human responses/rejections with timestamps.
    assert len(ctx["prior_responses"]) == 1
    pr = ctx["prior_responses"][0]
    assert pr["response_id"] == "plan-cycle-resp-1"
    assert pr["response_text"] == "tighten the FR-3 wording"
    assert pr["timestamp"] == "2026-07-03T10-00-00Z"
    assert pr["user"] == "john"

    # (3) per-escalated-finding triage reasoning: the escalated + low_confidence
    # verdicts, merged with their finding.
    esc = {e["finding_id"]: e for e in ctx["escalated"]}
    assert set(esc) == {"F-004", "F-005"}
    assert esc["F-004"]["severity"] == "blocking"
    assert esc["F-004"]["reasoning"] == "blocking; human must decide"
    assert esc["F-005"]["reasoning"] == "low-confidence nit, carried for human eyes"


def test_gate_context_reads_no_transcript(tmp_path):
    # The block is assembled from manifest + artifacts only: deleting every
    # transcript/events file leaves it fully populated.
    man, _, gate_rec, run_dir = _write_cycle_run(tmp_path)
    (run_dir / "steps").mkdir(exist_ok=True)  # no transcript.md / events.jsonl
    ctx = op.compute_gate_context(man, run_dir, gate_rec)
    assert ctx["convergence"]["per_round"] and ctx["escalated"]


def test_gate_block_validates_in_status_payload(tmp_path):
    man, _, gate_rec, run_dir = _write_cycle_run(tmp_path)
    ctx = op.compute_gate_context(man, run_dir, gate_rec)
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    assert rstate.state == op.STATE_PARKED_GATE
    payload = op.status_payload(
        man, op.DriverInfo(op.LIVENESS_NONE, None, None, None), rstate, None,
        run_root=tmp_path / "runs", run_instance_dir=run_dir, gate=ctx,
    )
    validate_schema(payload, STATUS_SCHEMA)
    assert payload["gate"]["cycle_step_id"] == "plan-cycle"
    assert payload["gate"]["convergence"]["per_round"][0]["fixed"] == 2


def test_gate_context_no_upstream_cycle_is_fail_soft(tmp_path):
    # A lone gate (no preceding adversarial_cycle) still yields a shaped block:
    # null cycle/convergence, empty lists — schema-valid, never a crash.
    run_dir = tmp_path / "runs" / "demo" / "run-2"
    run_dir.mkdir(parents=True)
    gate_rec = StepRecord(id="g", type="human_gate", status=M.PARKED)
    man = Manifest(
        run_id="run-2", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:x"),
        status=M.RUN_PARKED, current_step="g", steps=[gate_rec],
    )
    man.write_atomic(run_dir / "manifest.json")
    ctx = op.compute_gate_context(man, run_dir, gate_rec)
    assert ctx == {
        "cycle_step_id": None, "convergence": None,
        "prior_responses": [], "escalated": [],
    }


def test_gate_context_pipeline_resolves_same_stage_cycle(tmp_path):
    # With the pipeline snapshot, the upstream cycle is resolved by the reject-path
    # rule (same non-foreach stage). For the standard plan stage this still names
    # plan-cycle — the pipeline path agrees with the manifest heuristic here.
    man, _, gate_rec, run_dir = _write_cycle_run(tmp_path)
    pipeline = Pipeline.model_validate({
        "name": "standard", "version": 1, "stages": [
            {"id": "plan", "steps": [
                {"id": "plan-cycle", "type": "adversarial_cycle"},
                {"id": "plan-approve", "type": "human_gate"},
            ]},
        ],
    })
    ctx = op.compute_gate_context(man, run_dir, gate_rec, pipeline=pipeline)
    assert ctx["cycle_step_id"] == "plan-cycle"
    assert ctx["convergence"]["rounds"] == 2


def test_gate_context_pipeline_ignores_prior_stage_cycle(tmp_path):
    # F-001: the manifest heuristic would name plan-cycle (the last cycle before
    # the gate in step order), but the pipeline puts the gate in a LATER stage with
    # no cycle of its own — a reject there is terminal. The gate context must not
    # advertise a re-run reject will not perform: cycle_step_id/convergence null.
    man, _, gate_rec, run_dir = _write_cycle_run(tmp_path)
    # Manifest heuristic (no pipeline) still names the cross-stage cycle...
    assert op.compute_gate_context(
        man, run_dir, gate_rec
    )["cycle_step_id"] == "plan-cycle"
    # ...but resolving over the pipeline (cycle in a prior stage) yields None.
    pipeline = Pipeline.model_validate({
        "name": "standard", "version": 1, "stages": [
            {"id": "plan", "steps": [
                {"id": "plan-cycle", "type": "adversarial_cycle"},
            ]},
            {"id": "build", "steps": [
                {"id": "plan-approve", "type": "human_gate"},
            ]},
        ],
    })
    ctx = op.compute_gate_context(man, run_dir, gate_rec, pipeline=pipeline)
    assert ctx["cycle_step_id"] is None
    assert ctx["convergence"] is None


def test_gate_context_pipeline_foreach_gate_names_same_iteration_cycle(tmp_path):
    # #98: a gate inside a foreach stage now resolves its same-stage cycle (a
    # reject re-drives it), so the gate context names the cycle record at the
    # GATE's own iteration — F-001 still holds: what is advertised is exactly
    # what a reject performs.
    man, cycle_rec, gate_rec, run_dir = _write_cycle_run(tmp_path)
    pipeline = Pipeline.model_validate({
        "name": "standard", "version": 1, "stages": [
            {"id": "phases", "foreach": "plan.phases", "steps": [
                {"id": "plan-cycle", "type": "adversarial_cycle"},
                {"id": "plan-approve", "type": "human_gate"},
            ]},
        ],
    })
    ctx = op.compute_gate_context(man, run_dir, gate_rec, pipeline=pipeline)
    assert ctx["cycle_step_id"] == "plan-cycle"
    assert ctx["convergence"]["rounds"] == 2

    # Iteration matching: with per-iteration records, the context resolves the
    # cycle at the gate's iteration, not the first record with the right id.
    cycle_rec.iteration = "0"
    gate_rec.iteration = "1"
    other = StepRecord(id="plan-cycle", type="adversarial_cycle", status=M.DONE,
                       iteration="1",
                       metrics={"rounds": 1, "findings_total": 1,
                                "accepted_total": 1,
                                "accepted_resolved_total": 1,
                                "verdict_counts": {}, "confirm_counts": {}})
    man.steps.insert(1, other)
    ctx = op.compute_gate_context(man, run_dir, gate_rec, pipeline=pipeline)
    assert ctx["cycle_step_id"] == "plan-cycle"
    assert ctx["convergence"]["rounds"] == 1  # iteration "1" record, not "0"


def test_gate_context_redacts_extra_env_var_only_secret(tmp_path, monkeypatch):
    # F-002: a secret whose env-var name misses the default KEY/TOKEN heuristic but
    # is configured under redaction.extra_env_vars must be masked. Without the
    # configured redaction it leaks; threading it through masks it.
    monkeypatch.setenv("TRACKER_PROJECT", "plaintext-value-abcdef123456")
    man, cycle_rec, gate_rec, run_dir = _write_cycle_run(tmp_path)
    cycle_rec.human_responses[0].response_text = (
        "see project plaintext-value-abcdef123456 for context"
    )
    man.write_atomic(run_dir / "manifest.json")

    # Default settings miss it (proves the name heuristic alone is insufficient).
    leaked = op.compute_gate_context(man, run_dir, gate_rec)
    assert "plaintext-value-abcdef123456" in leaked["prior_responses"][0]["response_text"]

    # Configured redaction masks it.
    redaction = RedactionSettings(extra_env_vars=["TRACKER_PROJECT"])
    ctx = op.compute_gate_context(man, run_dir, gate_rec, redaction=redaction)
    assert "plaintext-value-abcdef123456" not in ctx["prior_responses"][0]["response_text"]


def test_gate_context_redacts_content_bearing_fields(tmp_path, monkeypatch):
    # PRD §7: content-bearing gate fields go through the redaction path. A secret
    # in a prior response / reasoning is masked, not surfaced verbatim.
    monkeypatch.setenv("MY_API_TOKEN", "sk-secret-value-1234567890")
    man, cycle_rec, gate_rec, run_dir = _write_cycle_run(tmp_path)
    cycle_rec.human_responses[0].response_text = (
        "use token sk-secret-value-1234567890 to proceed"
    )
    man.write_atomic(run_dir / "manifest.json")
    ctx = op.compute_gate_context(man, run_dir, gate_rec)
    assert "sk-secret-value-1234567890" not in ctx["prior_responses"][0]["response_text"]


# --------------------------------------------------------------------------- #
# FR-8.2 — next_actions carry a consequence; reject names the cycle
# --------------------------------------------------------------------------- #
def test_gate_next_actions_consequence_names_cycle(tmp_path):
    man, _, _, run_dir = _write_cycle_run(tmp_path)
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    by_label = {a.label: a for a in rstate.next_actions}
    assert by_label["approve"].consequence == (
        "continues the run past this gate to the next stage"
    )
    # The reject consequence names the exact upstream cycle it re-runs.
    assert by_label["reject"].consequence == (
        "re-runs the 'plan-cycle' adversarial cycle with your notes "
        "injected as a new round"
    )
    # ... and it is carried in the serialized action dict (the golden payload).
    reject = next(a for a in rstate.next_actions if a.label == "reject").to_dict()
    assert reject["consequence"].startswith("re-runs the 'plan-cycle'")


def test_gate_reject_consequence_terminal_without_cycle():
    # A gate with no upstream cycle: reject is terminal, and the consequence
    # names both the run-ending outcome and the --terminal requirement (#98) —
    # the status annotation IS the footgun warning.
    man = Manifest(
        run_id="r", slug="s", branch="gauntlet/s", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_PARKED, current_step="g",
        steps=[StepRecord(id="g", type="human_gate", status=M.PARKED)],
    )
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    reject = next(a for a in rstate.next_actions if a.label == "reject")
    assert "TERMINALLY fails the run" in reject.consequence
    assert "--terminal" in reject.consequence
    assert reject.command.endswith("--terminal")


def test_non_gate_actions_carry_null_consequence():
    # Consequence is always present in the serialized action (schema requires it),
    # and null for actions with nothing distinct to spell out.
    man = Manifest(
        run_id="r", slug="s", branch="gauntlet/s", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_RUNNING,
        steps=[StepRecord(id="s", type="agent_task", status=M.RUNNING)],
    )
    rstate = op.compute_run_state(man, op.LIVENESS_ALIVE)
    for a in rstate.next_actions:
        assert "consequence" in a.to_dict()
        assert a.consequence is None

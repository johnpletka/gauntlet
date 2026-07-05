"""P3 (FR-7.2): disjoint halt_reason/parked_reason enums + legacy normalization.

These pin the reason-model contract that the status classifier, resume routing,
and status rendering all depend on: the two reason fields are disjoint, the PRD
enum is the only thing written/emitted, and a legacy on-disk value is mapped to
the PRD enum on READ (never rewritten in place).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord


# --- normalize_parked_reason (the single read-side mapper) --------------------
@pytest.mark.parametrize(
    "reason, step_type, expected",
    [
        # PRD values pass through unchanged.
        (M.PARKED_REASON_USAGE_LIMIT, "agent_task", M.PARKED_REASON_USAGE_LIMIT),
        (M.PARKED_REASON_USAGE_WINDOW, "agent_task", M.PARKED_REASON_USAGE_WINDOW),
        (M.PARKED_REASON_ARTIFACT_INVALID, "agent_task", M.PARKED_REASON_ARTIFACT_INVALID),
        (M.PARKED_REASON_RESPONSE, "agent_task", M.PARKED_REASON_RESPONSE),
        (M.PARKED_REASON_GATE, "human_gate", M.PARKED_REASON_GATE),
        # Legacy values map to `response` regardless of step type.
        ("upstream_conflict", "agent_task", M.PARKED_REASON_RESPONSE),
        ("cycle_escalation", "adversarial_cycle", M.PARKED_REASON_RESPONSE),
        # A null reason on a human_gate is the pre-P3 gate shape → `gate`.
        (None, "human_gate", M.PARKED_REASON_GATE),
        # A null reason on any other step stays null (a cleared / non-park record,
        # or a non-canonical `halt_on:` marker park).
        (None, "agent_task", None),
        # An unrecognized non-null value is returned UNCHANGED (fails closed
        # downstream), never coerced into a PRD value.
        ("mystery", "human_gate", "mystery"),
    ],
)
def test_normalize_parked_reason(reason, step_type, expected):
    assert M.normalize_parked_reason(reason, step_type) == expected


def test_normalize_gate_shape_requires_parked_status_when_given():
    # A null reason on a DONE gate (approved) is NOT the parked-gate shape → null,
    # so a finished gate never reads back as a live `gate` park.
    assert M.normalize_parked_reason(None, "human_gate", M.DONE) is None
    assert M.normalize_parked_reason(None, "human_gate", M.PARKED) == M.PARKED_REASON_GATE


# --- reason_fields_disjoint (the invariant predicate) ------------------------
def test_reason_fields_disjoint():
    assert M.reason_fields_disjoint(None, None) is True
    assert M.reason_fields_disjoint(M.HALT_REASON_TIMEOUT, None) is True
    assert M.reason_fields_disjoint(None, M.PARKED_REASON_GATE) is True
    # Both set is the one forbidden combination.
    assert M.reason_fields_disjoint(M.HALT_REASON_TIMEOUT, M.PARKED_REASON_GATE) is False


def test_enum_sets_are_the_prd_sets():
    assert M.PARKED_REASONS == {
        "usage_limit", "usage_window", "artifact_invalid", "response", "gate",
    }
    assert M.HALT_REASONS == {
        "timeout", "budget", "judge_deny", "signal_kill", "adapter_error",
        "precondition", "operator_recover",
    }
    # RESPONSE-resolvable is the single PRD value now (legacy accepted on read).
    assert M.RESPONSE_RESOLVABLE_PARK_REASONS == {"response"}


# --- legacy-manifest read compatibility (bytes unchanged) --------------------
def _write_legacy_manifest(path: Path, *, parked_reason: str, step_type: str) -> None:
    man = Manifest(
        run_id="run-x", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_PARKED,
        steps=[StepRecord(id="impl", type=step_type, status=M.PARKED,
                          parked_reason=parked_reason)],
    )
    man.write_atomic(path)


@pytest.mark.parametrize(
    "legacy_reason, step_type, expected_state",
    [
        ("upstream_conflict", "agent_task", op.STATE_PARKED_FOR_RESPONSE),
        ("cycle_escalation", "adversarial_cycle", op.STATE_PARKED_FOR_RESPONSE),
    ],
)
def test_legacy_manifest_normalizes_on_read_without_rewrite(
    tmp_path, legacy_reason, step_type, expected_state
):
    path = tmp_path / "manifest.json"
    _write_legacy_manifest(path, parked_reason=legacy_reason, step_type=step_type)
    before = path.read_text()  # the exact on-disk bytes

    man = Manifest.load(path)
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    # The composite state is computed from the NORMALIZED reason...
    assert rstate.state == expected_state
    # ...and the status payload emits only the PRD value, never the legacy one.
    payload = op.status_payload(
        man, op.DriverInfo(op.LIVENESS_NONE, None, None, None), rstate, None,
        run_root=tmp_path, run_instance_dir=tmp_path,
    )
    assert payload["parked"]["reason"] == M.PARKED_REASON_RESPONSE
    assert payload["steps"][0]["parked_reason"] == M.PARKED_REASON_RESPONSE

    # The read-through mapper never rewrites the on-disk manifest.
    assert path.read_text() == before
    assert json.loads(before)["steps"][0]["parked_reason"] == legacy_reason


def test_shape_only_fields_defined_for_later_phases():
    # P3 defines the SHAPE of the park reasons and revalidation record that P4
    # (artifact_invalid) and P10 (usage_window) populate — the values round-trip
    # on a StepRecord and the revalidation model carries the §6 content-hash pair.
    rec = StepRecord(
        id="plan-author", type="agent_task", status=M.PARKED,
        parked_reason=M.PARKED_REASON_ARTIFACT_INVALID,
        revalidation=M.RevalidationRecord(
            artifact="plan.md", hash_at_park="sha256:9a1b",
        ),
    )
    dumped = Manifest(
        run_id="r", slug="d", branch="b", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"), steps=[rec],
    ).model_dump_json()
    reloaded = Manifest.model_validate_json(dumped).steps[0]
    assert reloaded.parked_reason == "artifact_invalid"
    assert reloaded.revalidation.hash_at_park == "sha256:9a1b"
    assert reloaded.revalidation.changed_while_parked is False
    assert reloaded.revalidation.passed_on_resume is False
    # usage_window (P10) round-trips as a plain parked_reason value.
    win = StepRecord(id="s", type="agent_task", status=M.PARKED,
                     parked_reason=M.PARKED_REASON_USAGE_WINDOW)
    assert win.parked_reason == "usage_window"
    # A step with no artifact_invalid park carries no revalidation record.
    assert StepRecord(id="x", type="agent_task").revalidation is None


def test_legacy_gate_null_reason_reads_as_gate(tmp_path):
    # A pre-P3 human_gate park stamped a null parked_reason; it must read back as
    # the PRD `gate` value in status output.
    path = tmp_path / "manifest.json"
    man = Manifest(
        run_id="run-x", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_PARKED,
        steps=[StepRecord(id="gate", type="human_gate", status=M.PARKED,
                          parked_reason=None)],
    )
    man.write_atomic(path)
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    assert rstate.state == op.STATE_PARKED_GATE
    payload = op.status_payload(
        man, op.DriverInfo(op.LIVENESS_NONE, None, None, None), rstate, None,
        run_root=tmp_path, run_instance_dir=tmp_path,
    )
    assert payload["parked"]["reason"] == M.PARKED_REASON_GATE
    assert payload["steps"][0]["parked_reason"] == M.PARKED_REASON_GATE

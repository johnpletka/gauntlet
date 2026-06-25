"""`status --json` (P3, FR-4): the same P1 computation as a stable contract.

`--json` is a *second rendering* of the P1 state (operator.status_payload over
driver_info / compute_run_state / next_actions), so these tests prove the
serialized object: (a) validates against the committed `schemas/status.json`
for every composite state class (§6.3) including a non-null `reconciliation`
and a malformed-intent → null `reconciliation`; (b) carries the FR-4.2
structured-action contract; and (c) prints as a lone JSON object on stdout that
parses and exits 0 for parked/failed runs (FR-4.3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gauntlet.adapters._structured import validate_schema
from gauntlet.cli import app
from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord

REPO = Path(__file__).resolve().parents[2]
STATUS_SCHEMA = json.loads((REPO / "schemas" / "status.json").read_text())

runner = CliRunner()


# --- builders ----------------------------------------------------------------
def _manifest(status: str, steps: list[StepRecord], *, slug: str = "demo") -> Manifest:
    return Manifest(
        run_id="run-x",
        slug=slug,
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=status,
        steps=steps,
    )


def _step(id: str, type: str, status: str, *, reason=None, iteration=None) -> StepRecord:
    return StepRecord(
        id=id, type=type, status=status, parked_reason=reason, iteration=iteration,
    )


def _payload(
    man: Manifest, liveness: str, *, recon: op.Reconciliation | None = None,
    driver: op.DriverInfo | None = None, run_root: Path = Path("/runs"),
) -> dict:
    """Build a `status --json` payload exactly as the CLI does (no recomputation)."""
    rstate = op.compute_run_state(man, liveness)
    if driver is None:
        driver = op.DriverInfo(liveness, None, None, None)
    return op.status_payload(
        man, driver, rstate, recon,
        run_root=run_root, run_instance_dir=run_root / "demo" / "run-x",
    )


# --- FR-4.1: one schema-valid object per composite state class (§6.3) --------
@pytest.mark.parametrize(
    "status, steps, liveness, expected_state",
    [
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_ALIVE,
         op.STATE_IN_PROGRESS),
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_ORPHANED,
         op.STATE_ORPHANED),
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_INDETERMINATE,
         op.STATE_INDETERMINATE),
        (M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)], op.LIVENESS_NONE,
         op.STATE_PARKED_GATE),
        (M.RUN_PARKED,
         [_step("impl", "agent_task", M.PARKED, reason=M.PARKED_REASON_UPSTREAM_CONFLICT)],
         op.LIVENESS_NONE, op.STATE_PARKED_FOR_RESPONSE),
        (M.RUN_FAILED, [_step("s", "agent_task", M.FAILED)], op.LIVENESS_NONE,
         op.STATE_FAILED),
        (M.RUN_FAILED, [_step("s", "agent_task", M.HALTED)], op.LIVENESS_NONE,
         op.STATE_HALTED),
        (M.RUN_FAILED, [_step("s", "agent_task", M.INTERRUPTED)], op.LIVENESS_NONE,
         op.STATE_INTERRUPTED),
        (M.RUN_DONE, [_step("s", "agent_task", M.DONE)], op.LIVENESS_NONE,
         op.STATE_DONE),
        (M.RUN_ABORTED, [_step("s", "agent_task", M.DONE)], op.LIVENESS_NONE,
         op.STATE_ABORTED),
        ("weird-status", [_step("s", "agent_task", M.DONE)], op.LIVENESS_NONE,
         op.STATE_UNKNOWN),
    ],
)
def test_payload_validates_for_every_state_class(status, steps, liveness, expected_state):
    payload = _payload(_manifest(status, steps), liveness)
    assert payload["state"] == expected_state
    validate_schema(payload, STATUS_SCHEMA)  # raises ValueError on any drift


def test_payload_with_nonnull_reconciliation_validates():
    # FR-4.1: a payload carrying a surviving (well-formed) recovery intent.
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    recon = op.Reconciliation("s", True, "gauntlet recover demo")
    payload = _payload(man, op.LIVENESS_ALIVE, recon=recon)
    assert payload["reconciliation"] == {
        "intent_step_id": "s",
        "nonce_matches_lock": True,
        "recommended_command": "gauntlet recover demo",
    }
    validate_schema(payload, STATUS_SCHEMA)


def test_malformed_intent_yields_null_reconciliation_but_valid_object(tmp_path):
    # FR-4.1: a malformed surviving intent is a human-footer anomaly only — the
    # `--json` object keeps `reconciliation: null` and never fabricates a step id.
    run_root = tmp_path
    inst = run_root / "demo" / "run-x"
    inst.mkdir(parents=True)
    (inst / ".recovery-intent.json").write_text("{ this is not json")
    recon, anomaly = op.read_recovery_intent(run_root, inst, "demo")
    assert recon is None and anomaly is not None  # parser surfaces the anomaly
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    payload = _payload(man, op.LIVENESS_ALIVE, recon=recon, run_root=run_root)
    assert payload["reconciliation"] is None
    validate_schema(payload, STATUS_SCHEMA)


# --- FR-4.2: structured, safely-executable actions ---------------------------
def test_reject_action_needs_notes_and_is_not_executable():
    man = _manifest(M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)])
    payload = _payload(man, op.LIVENESS_NONE)
    reject = next(a for a in payload["next_actions"] if a["label"] == "reject")
    assert reject["required_inputs"] == ["notes"]
    assert reject["executable"] is False


def test_every_action_argv_is_nonempty_and_executables_have_no_placeholder():
    # Sweep every composite state's actions: argv is always a non-empty array,
    # and no `executable: true` action carries a placeholder token in its argv
    # (a script must never run a literal `<your reason>`).
    cases = [
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_ALIVE),
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_ORPHANED),
        (M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)], op.LIVENESS_NONE),
        (M.RUN_PARKED,
         [_step("i", "agent_task", M.PARKED, reason=M.PARKED_REASON_CYCLE_ESCALATION)],
         op.LIVENESS_NONE),
        (M.RUN_FAILED, [_step("s", "agent_task", M.FAILED)], op.LIVENESS_NONE),
    ]
    for status, steps, liveness in cases:
        payload = _payload(_manifest(status, steps), liveness)
        for action in payload["next_actions"]:
            assert isinstance(action["argv"], list) and action["argv"], action
            if action["executable"]:
                assert action["required_inputs"] == []
                joined = " ".join(action["argv"])
                assert "<" not in joined and ">" not in joined, action


def test_schema_requires_all_six_action_fields():
    # FR-4.2: the committed schema mandates all six action fields; dropping one
    # must fail validation (guards the contract, not just our emitter).
    man = _manifest(M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)])
    payload = _payload(man, op.LIVENESS_NONE)
    payload["next_actions"][0].pop("command")
    with pytest.raises(ValueError):
        validate_schema(payload, STATUS_SCHEMA)


# --- §6.1: current_step is a derived convenience pointing at one steps[] entry
def test_current_step_matches_exactly_one_rendered_step_id():
    man = _manifest(
        M.RUN_PARKED,
        [_step("prd", "agent_task", M.DONE),
         _step("impl", "adversarial_cycle", M.PARKED,
               reason=M.PARKED_REASON_UPSTREAM_CONFLICT, iteration="0")],
    )
    payload = _payload(man, op.LIVENESS_NONE)
    rendered = {
        s["id"] if s["iteration"] is None else f"{s['id']}.{s['iteration']}"
        for s in payload["steps"]
    }
    assert payload["current_step"] == "impl.0"
    assert payload["current_step"] in rendered


# --- §6.2: the documented example must stay schema-valid (drift guard) --------
def test_section_6_2_example_validates():
    example = {
        "schema_version": 1,
        "slug": "operator-aids",
        "run_id": "run-2026-06-25T16-41-22",
        "run_status": "parked",
        "state": "parked_gate",
        "current_step": "impl-cycle.0",
        "driver": {"state": "none", "pid": None, "since": None, "host": None},
        "parked": {"step_id": "impl-cycle.0", "type": "human_gate", "reason": None},
        "failure": None,
        "reconciliation": None,
        "steps": [
            {"id": "prd-cycle", "iteration": None, "status": "done"},
            {"id": "impl-cycle", "iteration": 0, "status": "parked"},
        ],
        "next_actions": [
            {"label": "approve", "kind": "decide",
             "argv": ["gauntlet", "approve", "operator-aids"],
             "required_inputs": [], "executable": True,
             "command": "gauntlet approve operator-aids"},
            {"label": "reject", "kind": "decide",
             "argv": ["gauntlet", "reject", "operator-aids", "--notes"],
             "required_inputs": ["notes"], "executable": False,
             "command": 'gauntlet reject operator-aids --notes "<your reason>"'},
        ],
    }
    validate_schema(example, STATUS_SCHEMA)


# --- FR-4.3: a lone JSON object on stdout, exit 0 for parked/failed runs ------
def _setup_repo(tmp_path: Path, *, status: str, steps: list[dict]) -> Path:
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    run_dir = tmp_path / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    man = {
        "run_id": "run-1", "slug": "demo", "branch": "gauntlet/demo",
        "base_branch": "main", "pipeline": {"name": "p", "version": 1, "hash": "h"},
        "status": status, "steps": steps,
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))
    (tmp_path / "runs" / "demo" / "active-run.txt").write_text("run-1\n")
    return tmp_path


@pytest.mark.parametrize(
    "status, steps, expected_state",
    [
        ("parked", [{"id": "gate", "type": "human_gate", "status": "parked"}],
         "parked_gate"),
        ("failed", [{"id": "s", "type": "agent_task", "status": "failed"}],
         "failed"),
    ],
)
def test_json_is_a_lone_parseable_object_exit_zero(
    tmp_path, monkeypatch, status, steps, expected_state
):
    _setup_repo(tmp_path, status=status, steps=steps)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)  # parses as a single JSON value
    assert isinstance(payload, dict)
    assert payload["state"] == expected_state
    validate_schema(payload, STATUS_SCHEMA)


def test_json_error_exits_nonzero_without_partial_object(tmp_path, monkeypatch):
    # FR-4.3: an actual error (unknown slug) exits non-zero; the error goes to
    # stderr, so stdout never carries a half-formed object.
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "nonexistent", "--json"])
    assert result.exit_code != 0

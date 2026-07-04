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
import yaml
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
        (M.RUN_PARKED,
         [_step("impl", "agent_task", M.PARKED, reason=M.PARKED_REASON_USAGE_LIMIT)],
         op.LIVENESS_NONE, op.STATE_PARKED_USAGE_LIMIT),
        (M.RUN_PARKED,
         [_step("plan-author", "agent_task", M.PARKED,
                reason=M.PARKED_REASON_ARTIFACT_INVALID)],
         op.LIVENESS_NONE, op.STATE_PARKED_ARTIFACT_INVALID),
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
        # Additive FR-7.1 timing/usage fields: always present, null when N/A.
        "current_step_elapsed_s": None,
        "current_step_timeout_remaining_s": None,
        "run_elapsed_s": 1820.0,
        "totals": {"input_tokens": 41200, "output_tokens": 9800,
                   "cached_input_tokens": 12000, "cost_usd": 1.87},
        "agent_usage": {
            "builder": {"input_tokens": 41200, "output_tokens": 9800,
                        "cached_input_tokens": 12000, "cost_usd": 1.87}
        },
        # Additive FR-10.3 advisory channel: always present, empty when none.
        "warnings": [],
        "quota": None,
        "driver": {"state": "none", "pid": None, "since": None, "host": None},
        # FR-7.2: a human_gate park now stamps/emits the normalized `gate` reason.
        "parked": {"step_id": "impl-cycle.0", "type": "human_gate", "reason": "gate"},
        "failure": None,
        "reconciliation": None,
        "current_step_freshness": None,
        # Additive FR-5.3 field: always present, null when nothing to report.
        "suspension": None,
        # PRD §6 gate block: always present, body populated in P8; null for now.
        "gate": None,
        "steps": [
            {"id": "prd-cycle", "iteration": None, "status": "done",
             "duration_s": 620.0, "notes": None,
             "halt_reason": None, "parked_reason": None},
            {"id": "impl-cycle", "iteration": 0, "status": "parked",
             "duration_s": None, "notes": "awaiting human decision",
             "halt_reason": None, "parked_reason": "gate"},
        ],
        "next_actions": [
            {"label": "approve", "kind": "decide",
             "argv": ["gauntlet", "approve", "operator-aids"],
             "required_inputs": [], "executable": True,
             "command": "gauntlet approve operator-aids",
             "consequence": "continues the run past this gate to the next stage"},
            {"label": "reject", "kind": "decide",
             "argv": ["gauntlet", "reject", "operator-aids", "--notes"],
             "required_inputs": ["notes"], "executable": False,
             "command": 'gauntlet reject operator-aids --notes "<your reason>"',
             "consequence": "terminally rejects the gate (no upstream cycle to re-run)"},
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


def test_status_json_resolves_shell_step_timeout_from_snapshot(tmp_path, monkeypatch):
    # F-003: the status path resolves the effective timeout from the persisted
    # pipeline.yaml snapshot with the same precedence as execution. A running
    # SHELL step carries its own `timeout_s` and no agent, so the old profile-only
    # path reported `current_step_timeout_remaining_s: null`; it now reports the
    # real deadline.
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    run_dir = tmp_path / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    pipeline = {
        "name": "p", "version": 1,
        "stages": [{"id": "s", "steps": [
            {"id": "lint", "type": "shell", "run": "echo hi", "timeout_s": 100000},
        ]}],
    }
    (run_dir / "pipeline.yaml").write_text(yaml.dump(pipeline))
    man = {
        "run_id": "run-1", "slug": "demo", "branch": "gauntlet/demo",
        "base_branch": "main", "pipeline": {"name": "p", "version": 1, "hash": "h"},
        "status": "running",
        "steps": [{"id": "lint", "type": "shell", "status": "running",
                   "started": "2026-07-02T00:00:00+00:00"}],
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))
    (tmp_path / "runs" / "demo" / "active-run.txt").write_text("run-1\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["current_step"] == "lint"
    # A real deadline is reported (a number), not null as the profile-only path did.
    assert payload["current_step_timeout_remaining_s"] is not None
    validate_schema(payload, STATUS_SCHEMA)


def test_json_error_exits_nonzero_without_partial_object(tmp_path, monkeypatch):
    # FR-4.3: an actual error (unknown slug) exits non-zero; the error goes to
    # stderr, so stdout never carries a half-formed object.
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "nonexistent", "--json"])
    assert result.exit_code != 0


# --- F-001: a non-canonical iteration can never desync current_step / steps[] -
@pytest.mark.parametrize("bad_iteration", ["01", "00", "+1", "-1", " 1", "1 ", "bad", "1.0", ""])
def test_noncanonical_iteration_fails_closed(bad_iteration):
    # A leading-zero form ("01") rendered "step.01" as current_step but "step.1"
    # in steps[] (int 1); a non-numeric value rendered "step.bad" vs a null
    # iteration ("step"). Both surfaces now route through one canonical
    # representation and fail closed on a non-canonical value rather than emit a
    # contradictory object.
    man = _manifest(
        M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING, iteration=bad_iteration)]
    )
    with pytest.raises(op.StatusContractError):
        _payload(man, op.LIVENESS_ALIVE)


@pytest.mark.parametrize("good_iteration, expected", [("0", 0), ("1", 1), ("12", 12)])
def test_canonical_iteration_current_step_matches_steps(good_iteration, expected):
    man = _manifest(
        M.RUN_PARKED,
        [_step("impl", "agent_task", M.PARKED,
               reason=M.PARKED_REASON_UPSTREAM_CONFLICT, iteration=good_iteration)],
    )
    payload = _payload(man, op.LIVENESS_NONE)
    assert payload["steps"][0]["iteration"] == expected
    assert payload["current_step"] == f"impl.{expected}"
    validate_schema(payload, STATUS_SCHEMA)


# --- F-002: a step id with traversal can never escape run_root via evidence_path
@pytest.mark.parametrize("bad_id", ["../../outside", "..", "a/b", "/abs", "x\x00y"])
def test_traversal_step_id_in_failure_fails_closed(bad_id):
    # failure.evidence_path is `steps/<rendered-id>`; relative_to() is lexical and
    # would not strip a traversal/absolute/separator id. The id is validated as a
    # single safe path segment first, so a corrupt manifest fails closed instead
    # of emitting a `..`/absolute evidence_path that violates schemas/status.json.
    man = _manifest(M.RUN_FAILED, [_step(bad_id, "agent_task", M.FAILED)])
    with pytest.raises(op.StatusContractError):
        _payload(man, op.LIVENESS_NONE)


def test_safe_failure_step_id_yields_contained_evidence_path():
    man = _manifest(M.RUN_FAILED, [_step("impl", "agent_task", M.FAILED)])
    payload = _payload(man, op.LIVENESS_NONE, run_root=Path("/runs"))
    ev = payload["failure"]["evidence_path"]
    assert ev == "demo/run-x/steps/impl"
    assert not ev.startswith("/") and ".." not in ev
    validate_schema(payload, STATUS_SCHEMA)


# --- F-003: an out-of-enum persisted value can never reach a consumer ---------
def test_out_of_enum_step_status_fails_closed():
    # StepRecord.status accepts arbitrary strings, but steps[].status is a closed
    # enum. The completed payload is validated before emission, so a malformed
    # status fails closed rather than printing schema-invalid JSON.
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", "weird-status")])
    with pytest.raises(op.StatusContractError):
        _payload(man, op.LIVENESS_ALIVE)


def test_malformed_driver_since_fails_closed():
    # A driver.since that is not the §6.1 timestamp format (e.g. an ISO offset)
    # fails schema validation at emission rather than leaking a non-conforming
    # value into the contract (F-003/F-004).
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    bad_driver = op.DriverInfo(op.LIVENESS_ALIVE, 42, "host", "2026-06-25T16:41:22+00:00")
    with pytest.raises(op.StatusContractError):
        _payload(man, op.LIVENESS_ALIVE, driver=bad_driver)


def test_usage_limit_park_reports_resume_next_action():
    # FR-3.2: a usage_limit park surfaces as the distinct `parked_usage_limit`
    # state whose ONLY next action is a plain `resume` (no --response decision).
    man = _manifest(
        M.RUN_PARKED,
        [_step("implement", "agent_task", M.PARKED, reason=M.PARKED_REASON_USAGE_LIMIT)],
    )
    payload = _payload(man, op.LIVENESS_NONE)
    assert payload["state"] == "parked_usage_limit"
    assert payload["parked"]["reason"] == "usage_limit"
    assert payload["parked"]["step_id"] == "implement"
    validate_schema(payload, STATUS_SCHEMA)
    actions = payload["next_actions"]
    assert [a["label"] for a in actions] == ["resume"]
    assert actions[0]["kind"] == "control"
    assert actions[0]["executable"] is True
    assert actions[0]["command"] == "gauntlet resume demo"


def test_gate_block_always_present_and_null_when_not_threaded():
    # PRD §6 promises a top-level `gate {...} | null` in the status contract. The
    # field is required by the schema (a consumer can rely on it) and emitted for
    # every state class. The POPULATED body (P8/FR-8.1) is assembled by the
    # I/O-bearing operator.compute_gate_context in the caller and threaded in; when
    # it is NOT threaded (the pure serializer's default) the block is `null` even
    # at a gate park — a valid degraded case (fail-soft, never a crash).
    assert "gate" in STATUS_SCHEMA["required"]
    for status, steps, liveness in [
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_ALIVE),
        (M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)], op.LIVENESS_NONE),
        (M.RUN_DONE, [_step("s", "agent_task", M.DONE)], op.LIVENESS_NONE),
    ]:
        payload = _payload(_manifest(status, steps), liveness)
        assert "gate" in payload and payload["gate"] is None
        validate_schema(payload, STATUS_SCHEMA)
    # The schema requires the field: dropping it fails validation.
    payload = _payload(
        _manifest(M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)]),
        op.LIVENESS_NONE,
    )
    payload.pop("gate")
    with pytest.raises(ValueError):
        validate_schema(payload, STATUS_SCHEMA)


# --- FR-10.3: advisory warnings surface in the status contract ---------------
def test_manifest_warnings_surface_in_status_payload():
    # FR-10.3: an advisory usage-window shortfall stamped into manifest.warnings
    # is surfaced by `status` (not only the manifest), so an operator does not
    # have to open the manifest to see it (F-001).
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    warn = "[implement] usage-window admission (FR-10.3): provider 'anthropic' …"
    man.warnings.append(warn)
    payload = _payload(man, op.LIVENESS_ALIVE)
    assert payload["warnings"] == [warn]
    validate_schema(payload, STATUS_SCHEMA)


def test_warnings_always_present_empty_when_none():
    # Always present, empty array when no warning was recorded (never omitted).
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    payload = _payload(man, op.LIVENESS_ALIVE)
    assert payload["warnings"] == []
    validate_schema(payload, STATUS_SCHEMA)
    # The schema requires the field: dropping it fails validation.
    payload.pop("warnings")
    with pytest.raises(ValueError):
        validate_schema(payload, STATUS_SCHEMA)


def test_embedded_schema_matches_committed_file():
    # operator validates against an EMBEDDED copy of the schema (the committed
    # file is not packaged in the wheel); guard the two against drift (F-003).
    assert op.STATUS_SCHEMA == STATUS_SCHEMA


# --- FR-7.1: additive status fields (timing, usage, quota, per-step) ----------
from datetime import datetime, timezone  # noqa: E402

_V0_SCHEMA = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "status_schema_v0.json").read_text()
)


def test_new_fields_present_and_valid():
    # Every FR-7.1 field is always present (nullable, never omitted) and the
    # payload validates against the shipped schema.
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    payload = _payload(man, op.LIVENESS_ALIVE)
    for key in ("current_step_elapsed_s", "current_step_timeout_remaining_s",
                "run_elapsed_s", "totals", "agent_usage", "quota"):
        assert key in payload, key
    for key in ("duration_s", "notes", "halt_reason", "parked_reason"):
        assert key in payload["steps"][0], key
    assert set(payload["totals"]) == {
        "input_tokens", "output_tokens", "cached_input_tokens", "cost_usd"
    }
    validate_schema(payload, STATUS_SCHEMA)


def test_timing_and_usage_values_from_manifest():
    # Deterministic timing with a fixed `now`; totals/agent_usage from the manifest.
    man = _manifest(M.RUN_RUNNING, [
        StepRecord(id="s", type="agent_task", status=M.RUNNING,
                   started="2026-07-02T04:00:00+00:00"),
    ])
    man.totals = M.UsageTotals(input_tokens=100, output_tokens=20,
                               cached_input_tokens=40, cost_usd=1.25)
    man.agent_usage["builder"] = M.UsageTotals(input_tokens=100, output_tokens=20,
                                               cached_input_tokens=40, cost_usd=1.25)
    now = datetime(2026, 7, 2, 4, 5, 0, tzinfo=timezone.utc)  # +300s
    rstate = op.compute_run_state(man, op.LIVENESS_ALIVE)
    payload = op.status_payload(
        man, op.DriverInfo(op.LIVENESS_ALIVE, None, None, None), rstate, None,
        run_root=Path("/runs"), run_instance_dir=Path("/runs/demo/run-x"),
        now=now, current_step_timeout_s=600.0,
    )
    assert payload["current_step_elapsed_s"] == 300.0
    assert payload["current_step_timeout_remaining_s"] == 300.0  # 600 - 300
    assert payload["run_elapsed_s"] == 300.0
    assert payload["steps"][0]["duration_s"] == 300.0
    assert payload["totals"]["cost_usd"] == 1.25
    assert payload["agent_usage"]["builder"]["cached_input_tokens"] == 40
    validate_schema(payload, STATUS_SCHEMA)


def test_quota_block_present_only_on_usage_limit_park():
    man = _manifest(M.RUN_PARKED, [
        StepRecord(id="impl", type="agent_task", status=M.PARKED,
                   parked_reason=M.PARKED_REASON_USAGE_LIMIT,
                   quota_reset_at="2026-07-02T09:00:00+00:00"),
    ])
    payload = _payload(man, op.LIVENESS_NONE)
    assert payload["state"] == "parked_usage_limit"
    assert payload["quota"] == {"reset_at": "2026-07-02T09:00:00+00:00"}
    validate_schema(payload, STATUS_SCHEMA)
    # A gate park has no quota block.
    gate = _manifest(M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)])
    assert _payload(gate, op.LIVENESS_NONE)["quota"] is None


def test_per_step_reason_fields_are_disjoint_and_normalized():
    man = _manifest(M.RUN_FAILED, [
        StepRecord(id="a", type="agent_task", status=M.HALTED,
                   halt_reason=M.HALT_REASON_TIMEOUT),
        StepRecord(id="b", type="adversarial_cycle", status=M.PARKED,
                   parked_reason="cycle_escalation"),  # legacy → response
    ])
    payload = _payload(man, op.LIVENESS_NONE)
    a, b = payload["steps"]
    assert a["halt_reason"] == "timeout" and a["parked_reason"] is None
    assert b["parked_reason"] == "response" and b["halt_reason"] is None


def test_new_output_fails_against_captured_v0_schema():
    # FR-7.1 re-pin cost, documented not hidden: a consumer validating the new
    # output against a PINNED pre-PRD schema copy (additionalProperties:false)
    # rejects the additive fields. This asserts the break surfaces at upgrade.
    assert _V0_SCHEMA.get("additionalProperties") is False
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    payload = _payload(man, op.LIVENESS_ALIVE)
    with pytest.raises(ValueError):
        validate_schema(payload, _V0_SCHEMA)


# --- P3 exit criterion: every reachable state is explainable from --json alone
@pytest.mark.parametrize(
    "status, steps, liveness, expect_state, expect_cause_field",
    [
        (M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)], op.LIVENESS_NONE,
         "parked_gate", ("parked", "reason", "gate")),
        (M.RUN_PARKED,
         [StepRecord(id="impl", type="agent_task", status=M.PARKED,
                     parked_reason=M.PARKED_REASON_RESPONSE)],
         op.LIVENESS_NONE, "parked_for_response", ("parked", "reason", "response")),
        (M.RUN_PARKED,
         [StepRecord(id="impl", type="agent_task", status=M.PARKED,
                     parked_reason=M.PARKED_REASON_USAGE_LIMIT)],
         op.LIVENESS_NONE, "parked_usage_limit", ("parked", "reason", "usage_limit")),
        (M.RUN_PARKED,
         [StepRecord(id="s", type="agent_task", status=M.HALTED,
                     halt_reason=M.HALT_REASON_TIMEOUT)],
         op.LIVENESS_NONE, "halted", ("steps", 0, "halt_reason", "timeout")),
        (M.RUN_FAILED,
         [StepRecord(id="s", type="agent_task", status=M.FAILED,
                     halt_reason=M.HALT_REASON_ADAPTER_ERROR)],
         op.LIVENESS_NONE, "failed", ("steps", 0, "halt_reason", "adapter_error")),
        (M.RUN_PARKED,
         [StepRecord(id="s", type="agent_task", status=M.INTERRUPTED,
                     halt_reason=M.HALT_REASON_SIGNAL_KILL)],
         op.LIVENESS_NONE, "interrupted", ("steps", 0, "halt_reason", "signal_kill")),
    ],
)
def test_every_state_explainable_from_json(
    status, steps, liveness, expect_state, expect_cause_field
):
    payload = _payload(_manifest(status, steps), liveness)
    assert payload["state"] == expect_state
    # The identifying cause is present in the JSON, and a next action is offered.
    if expect_cause_field[0] == "parked":
        _, key, val = expect_cause_field
        assert payload["parked"][key] == val
    else:
        _, idx, key, val = expect_cause_field
        assert payload["steps"][idx][key] == val
    assert payload["next_actions"]  # a concrete next command is always offered
    validate_schema(payload, STATUS_SCHEMA)


# --- F-004: the schema enforces the normative §6.1 timestamp for driver.since -
@pytest.mark.parametrize("since", [
    "2026-06-25T16:41:22",        # colon-delimited time
    "2026-06-25T16-41-22+00:00",  # trailing offset
    "2026-06-25T16-41-22Z",       # zulu suffix
    "2026-06-25 16-41-22",        # space instead of T
    "garbage",
])
def test_schema_rejects_nonconforming_driver_since(since):
    payload = _payload(
        _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)]),
        op.LIVENESS_ALIVE,
    )
    payload["driver"]["since"] = since
    with pytest.raises(ValueError):
        validate_schema(payload, STATUS_SCHEMA)


def test_schema_accepts_conforming_driver_since():
    payload = _payload(
        _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)]),
        op.LIVENESS_ALIVE,
    )
    payload["driver"]["since"] = "2026-06-25T16-41-22"
    validate_schema(payload, STATUS_SCHEMA)


# --- the CLI turns a contract violation into a non-zero exit, empty stdout ----
@pytest.mark.parametrize("run_status, steps", [
    ("running", [{"id": "s", "type": "agent_task", "status": "running",
                  "iteration": "01"}]),                                    # F-001
    ("failed", [{"id": "../../x", "type": "agent_task", "status": "failed"}]),  # F-002
    ("running", [{"id": "s", "type": "agent_task", "status": "weird"}]),   # F-003
])
def test_cli_json_contract_violation_exits_nonzero_empty_stdout(
    tmp_path, monkeypatch, run_status, steps
):
    _setup_repo(tmp_path, status=run_status, steps=steps)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["status", "demo", "--json"])
    assert result.exit_code != 0
    assert result.stdout.strip() == ""  # no half-formed object on stdout
    assert "error:" in result.stderr

"""Operator observability core (P1, FR-1/FR-2 + FR-3.1a/FR-5.6 report half).

Adversarial coverage of the load-bearing assumption (§1.3): truthful liveness
and a correct next action computed cheaply from the drive lock + process
identity — with **zero** false ``alive`` on a dead driver and **zero** false
``orphaned`` on a live-but-unverifiable one. Every test is read-only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.procident import read_process_identity

THIS_HOST = "test-host"


# --- fixtures / builders -----------------------------------------------------
def _identity(value: int = 1_750_000_000) -> dict:
    return {"platform": "darwin", "value": value, "unit": "epoch_seconds"}


def _write_lock(
    run_root: Path,
    *,
    slug: str = "demo",
    pid: int = 4242,
    host: str = THIS_HOST,
    proc_identity: dict | None = None,
    nonce: str = "nonce-1",
    pgid: int | None = None,
    started_at: str = "2026-06-25T16-41-22",
    run_id: str = "run-x",
    raw: str | None = None,
) -> None:
    path = run_root / ".driving.lock"
    if raw is not None:
        path.write_text(raw)
        return
    if proc_identity is None:
        proc_identity = _identity()
    rec = {
        "nonce": nonce,
        "slug": slug,
        "run_id": run_id,
        "pid": pid,
        "pgid": pgid if pgid is not None else pid,
        "started_at": started_at,
        "host": host,
        "proc_identity": proc_identity,
    }
    path.write_text(json.dumps(rec, indent=2))


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


def _step(id: str, type: str, status: str, *, reason=None, iteration=None,
          metrics=None) -> StepRecord:
    return StepRecord(
        id=id, type=type, status=status, parked_reason=reason,
        iteration=iteration, metrics=metrics or {},
    )


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(op, "_this_host", lambda: THIS_HOST)
    return THIS_HOST


@pytest.fixture
def probe(monkeypatch):
    """Drive ``_probe_pid`` deterministically; returns a setter."""
    state = {"value": "alive"}
    monkeypatch.setattr(op, "_probe_pid", lambda pid: state["value"])

    def set_probe(value: str) -> None:
        state["value"] = value

    return set_probe


@pytest.fixture
def identity_read(monkeypatch):
    """Drive ``read_process_identity`` deterministically; returns a setter."""
    state = {"value": op.ProcessIdentity.from_dict(_identity())}
    monkeypatch.setattr(op, "read_process_identity", lambda pid: state["value"])

    def set_identity(value) -> None:
        state["value"] = value

    return set_identity


# --- FR-2.4: the total failure-mode table, one test per row a–h --------------
def test_row_a_no_lock_is_none(tmp_path):
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_NONE


def test_row_b_foreign_slug_is_none(tmp_path, host, probe, identity_read):
    _write_lock(tmp_path, slug="other")
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_NONE


def test_row_c_dead_pid_is_orphaned(tmp_path, host, probe, identity_read):
    probe("dead")
    _write_lock(tmp_path)
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_ORPHANED


def test_row_d_pid_reuse_is_orphaned_not_alive(tmp_path, host, probe, identity_read):
    # Recorded and freshly-read identities BOTH present and unequal → PID reuse.
    probe("alive")
    _write_lock(tmp_path, proc_identity=_identity(value=111))
    identity_read(op.ProcessIdentity.from_dict(_identity(value=222)))
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_ORPHANED


def test_row_e_live_verified_same_host_is_alive(tmp_path, host, probe, identity_read):
    probe("alive")
    _write_lock(tmp_path, proc_identity=_identity(value=999))
    identity_read(op.ProcessIdentity.from_dict(_identity(value=999)))
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_ALIVE


def test_row_f_identity_unobtainable_is_indeterminate_not_orphaned(
    tmp_path, host, probe, identity_read
):
    # Live PID, recorded proc_identity null → indeterminate, NOT orphaned
    # (the false-orphaned-on-a-live-driver case §1.3 forbids).
    probe("alive")
    _write_lock(tmp_path, proc_identity=None)
    identity_read(None)
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_INDETERMINATE


def test_row_f_fresh_read_null_is_indeterminate(tmp_path, host, probe, identity_read):
    probe("alive")
    _write_lock(tmp_path, proc_identity=_identity(value=5))
    identity_read(None)  # recorded present, fresh unobtainable
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_INDETERMINATE


def test_row_g_malformed_lock_is_indeterminate(tmp_path, host, probe, identity_read):
    _write_lock(tmp_path, raw="{ not valid json ")
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_INDETERMINATE


def test_row_g_missing_field_is_indeterminate(tmp_path):
    # Missing the required `pid` → _LockRecord.from_json returns None → malformed.
    (tmp_path / ".driving.lock").write_text(json.dumps({"nonce": "n", "slug": "demo"}))
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_INDETERMINATE


def test_row_h_foreign_host_is_indeterminate(tmp_path, host, probe, identity_read):
    # Identities equal but host differs → indeterminate, never alive/orphaned.
    probe("alive")
    _write_lock(tmp_path, host="some-other-host", proc_identity=_identity(value=7))
    identity_read(op.ProcessIdentity.from_dict(_identity(value=7)))
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_INDETERMINATE


def test_rows_fgh_yield_no_mutating_next_action(tmp_path, host, probe, identity_read):
    # Indeterminate (the f/g/h outcome) under a running manifest must offer only
    # read-only inspection — never resume/approve/recover.
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    actions = op.next_actions(man, op.LIVENESS_INDETERMINATE)
    assert {a.kind for a in actions} == {"observe"}
    assert all(a.executable for a in actions)


def test_real_alive_path_unmocked(tmp_path):
    # Prove the real procident path: the current process is alive, identity
    # matches, host matches → alive (no monkeypatching).
    pid = os.getpid()
    ident = read_process_identity(pid)
    if ident is None:
        pytest.skip("process identity unobtainable on this platform")
    import socket

    _write_lock(tmp_path, pid=pid, host=socket.gethostname(),
                proc_identity=ident.to_dict())
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_ALIVE


# --- FR-2.2: liveness ignores manifest.status --------------------------------
def test_running_manifest_with_dead_driver_is_orphaned(tmp_path, host, probe, identity_read):
    probe("dead")
    _write_lock(tmp_path)
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    liveness = op.driver_liveness(tmp_path, "demo")
    assert liveness == op.LIVENESS_ORPHANED
    assert op.composite_state(man, liveness) == op.STATE_ORPHANED


def test_running_manifest_with_null_identity_live_pid_is_indeterminate(
    tmp_path, host, probe, identity_read
):
    probe("alive")
    _write_lock(tmp_path, proc_identity=None)
    identity_read(None)
    assert op.driver_liveness(tmp_path, "demo") == op.LIVENESS_INDETERMINATE


# --- §6.3: composite-state decision table, one case per class ----------------
@pytest.mark.parametrize(
    "status, steps, liveness, expected, expected_cmds",
    [
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_ALIVE,
         op.STATE_IN_PROGRESS, ["gauntlet logs demo", "gauntlet status demo --json"]),
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_ORPHANED,
         op.STATE_ORPHANED, ["gauntlet resume demo"]),
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_NONE,
         op.STATE_ORPHANED, ["gauntlet resume demo"]),
        (M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)], op.LIVENESS_INDETERMINATE,
         op.STATE_INDETERMINATE, ["gauntlet logs demo", "gauntlet status demo --json"]),
        (M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)], op.LIVENESS_NONE,
         op.STATE_PARKED_GATE,
         ["gauntlet approve demo", 'gauntlet reject demo --notes "<your reason>"']),
        (M.RUN_PARKED,
         [_step("impl", "agent_task", M.PARKED, reason=M.PARKED_REASON_UPSTREAM_CONFLICT)],
         op.LIVENESS_NONE, op.STATE_PARKED_FOR_RESPONSE,
         ['gauntlet resume demo --response "<your decision>"']),
        (M.RUN_FAILED, [_step("s", "agent_task", M.FAILED)], op.LIVENESS_NONE,
         op.STATE_FAILED, ["gauntlet logs demo", "gauntlet resume demo"]),
        (M.RUN_FAILED, [_step("s", "agent_task", M.HALTED)], op.LIVENESS_NONE,
         op.STATE_HALTED, ["gauntlet logs demo", "gauntlet resume demo"]),
        (M.RUN_FAILED, [_step("s", "agent_task", M.INTERRUPTED)], op.LIVENESS_NONE,
         op.STATE_INTERRUPTED, ["gauntlet logs demo", "gauntlet resume demo"]),
        (M.RUN_DONE, [_step("s", "agent_task", M.DONE)], op.LIVENESS_NONE,
         op.STATE_DONE, []),
        (M.RUN_ABORTED, [_step("s", "agent_task", M.DONE)], op.LIVENESS_NONE,
         op.STATE_ABORTED, []),
        ("weird-status", [_step("s", "agent_task", M.DONE)], op.LIVENESS_NONE,
         op.STATE_UNKNOWN, ["gauntlet logs demo", "gauntlet status demo --json"]),
    ],
)
def test_composite_state_and_actions(status, steps, liveness, expected, expected_cmds):
    man = _manifest(status, steps)
    rstate = op.compute_run_state(man, liveness)
    assert rstate.state == expected
    assert [a.command for a in rstate.next_actions] == expected_cmds


def test_parked_for_response_classifies_cycle_escalation():
    man = _manifest(
        M.RUN_PARKED,
        [_step("cyc", "adversarial_cycle", M.PARKED,
               reason=M.PARKED_REASON_CYCLE_ESCALATION, iteration="0")],
    )
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    assert rstate.state == op.STATE_PARKED_FOR_RESPONSE
    assert rstate.parked.step_id == "cyc.0"  # rendered with iteration


# --- §6.3a: contradictions → unknown → read-only only ------------------------
@pytest.mark.parametrize(
    "status, steps",
    [
        # parked run, zero parked steps
        (M.RUN_PARKED, [_step("s", "agent_task", M.RUNNING)]),
        # parked run, two parked steps
        (M.RUN_PARKED,
         [_step("a", "human_gate", M.PARKED), _step("b", "human_gate", M.PARKED)]),
        # parked non-gate step with no reason → no defined response
        (M.RUN_PARKED, [_step("s", "agent_task", M.PARKED)]),
        # parked with an unknown reason value
        (M.RUN_PARKED, [_step("g", "human_gate", M.PARKED, reason="mystery")]),
        # failed run with no terminal failure step
        (M.RUN_FAILED, [_step("s", "agent_task", M.DONE)]),
        # descriptor present under a `—` status (running with a failed step)
        (M.RUN_RUNNING, [_step("s", "agent_task", M.FAILED)]),
        # done with a parked step is contradictory
        (M.RUN_DONE, [_step("s", "human_gate", M.PARKED)]),
    ],
)
def test_contradictions_map_to_unknown_readonly(status, steps):
    man = _manifest(status, steps)
    rstate = op.compute_run_state(man, op.LIVENESS_ALIVE)
    assert rstate.state == op.STATE_UNKNOWN
    assert {a.kind for a in rstate.next_actions} == {"observe"}


# --- FR-1.2: footer commands == next_actions command fields ------------------
def test_footer_commands_equal_next_actions(host):
    man = _manifest(M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)])
    driver = op.DriverInfo(op.LIVENESS_NONE, None, None, None)
    rstate = op.compute_run_state(man, driver.state)
    lines = op.render_footer(driver, rstate)
    footer_cmds = [ln.strip()[2:] for ln in lines if ln.strip().startswith("$ ")]
    assert footer_cmds == [a.command for a in rstate.next_actions]


def test_footer_done_has_no_action_lines():
    man = _manifest(M.RUN_DONE, [_step("s", "agent_task", M.DONE)])
    driver = op.DriverInfo(op.LIVENESS_NONE, None, None, None)
    rstate = op.compute_run_state(man, driver.state)
    lines = op.render_footer(driver, rstate)
    assert not [ln for ln in lines if ln.strip().startswith("$ ")]
    assert any("none — the run is finished" in ln for ln in lines)


def test_footer_driver_line_shows_pid_host_since():
    driver = op.DriverInfo(op.LIVENESS_ALIVE, 4242, "h1", "2026-06-25T16-41-22")
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    rstate = op.compute_run_state(man, op.LIVENESS_ALIVE)
    lines = op.render_footer(driver, rstate)
    assert any("driver: alive" in ln and "pid 4242" in ln and "host h1" in ln
               and "since 2026-06-25T16-41-22" in ln for ln in lines)


# --- FR-1.3: unknown / indeterminate suggest no mutating verb ----------------
def test_unknown_status_suggests_no_mutating_verb():
    man = _manifest("bogus", [_step("s", "agent_task", M.RUNNING)])
    actions = op.next_actions(man, op.LIVENESS_NONE)
    assert all(a.kind == "observe" for a in actions)
    assert all(a.executable for a in actions)


def test_unparseable_lock_under_running_suggests_no_mutating_verb(tmp_path):
    _write_lock(tmp_path, raw="garbage")
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])
    liveness = op.driver_liveness(tmp_path, "demo")
    assert liveness == op.LIVENESS_INDETERMINATE
    actions = op.next_actions(man, liveness)
    assert {a.kind for a in actions} == {"observe"}


# --- FR-4.2 object shape (built in P1, serialized in P3) ---------------------
def test_action_shape_reject_requires_notes_non_executable():
    man = _manifest(M.RUN_PARKED, [_step("gate", "human_gate", M.PARKED)])
    actions = op.next_actions(man, op.LIVENESS_NONE)
    by_label = {a.label: a for a in actions}
    reject = by_label["reject"]
    assert reject.required_inputs == ["notes"]
    assert reject.executable is False
    # No executable action's argv contains a placeholder token.
    for a in actions:
        assert a.argv, "argv must be a non-empty array"
        if a.executable:
            assert not any("<" in tok for tok in a.argv)


# --- FR-3.1a: default-step selection by manifest order, not mtime ------------
def test_select_default_step_last_non_terminal_highest_iteration():
    steps = [
        _step("a", "agent_task", M.DONE),
        _step("cyc", "adversarial_cycle", M.DONE, iteration="0"),
        _step("cyc", "adversarial_cycle", M.RUNNING, iteration="1"),
    ]
    man = _manifest(M.RUN_RUNNING, steps)
    rec = op.select_default_step(man)
    assert op.render_step_id(rec) == "cyc.1"


def test_select_default_step_all_done_returns_last_done():
    steps = [_step("a", "agent_task", M.DONE), _step("b", "shell", M.SKIPPED),
             _step("c", "commit", M.DONE)]
    man = _manifest(M.RUN_DONE, steps)
    assert op.render_step_id(op.select_default_step(man)) == "c"


def test_resolve_run_instance_active_pointer(tmp_path):
    slug_dir = tmp_path / "demo"
    (slug_dir / "run-2026-01-01T00-00-00").mkdir(parents=True)
    (slug_dir / "run-2026-02-02T00-00-00").mkdir()
    (slug_dir / "active-run.txt").write_text("run-2026-01-01T00-00-00\n")
    assert op.resolve_run_instance(slug_dir).name == "run-2026-01-01T00-00-00"


def test_resolve_run_instance_greatest_when_no_pointer(tmp_path):
    slug_dir = tmp_path / "demo"
    (slug_dir / "run-2026-01-01T00-00-00").mkdir(parents=True)
    (slug_dir / "run-2026-02-02T00-00-00").mkdir()
    assert op.resolve_run_instance(slug_dir).name == "run-2026-02-02T00-00-00"


def test_resolve_run_instance_pointer_to_missing_errors_with_list(tmp_path):
    slug_dir = tmp_path / "demo"
    (slug_dir / "run-2026-02-02T00-00-00").mkdir(parents=True)
    (slug_dir / "active-run.txt").write_text("run-gone\n")
    with pytest.raises(op.RunResolutionError) as exc:
        op.resolve_run_instance(slug_dir)
    assert "run-2026-02-02T00-00-00" in str(exc.value)


# --- FR-3.1a: transcript-leaf resolution (metadata-driven, not mtime) --------
def _make_steps_layout(run_dir: Path, leaf: str, subdirs: list[str]) -> Path:
    step_dir = run_dir / "steps" / leaf
    for sd in subdirs:
        (step_dir / sd).mkdir(parents=True)
    step_dir.mkdir(parents=True, exist_ok=True)
    return step_dir


def test_transcript_leaf_atomic_step(tmp_path):
    rec = _step("impl", "agent_task", M.DONE)
    _make_steps_layout(tmp_path, "impl", [])
    assert op.resolve_transcript_dir(tmp_path, rec) == tmp_path / "steps" / "impl"


def test_transcript_leaf_cycle_reverse_role_order(tmp_path):
    # rounds=2; round-2 sub-dirs present. As roles are removed, resolution walks
    # confirm -> fix -> triage/<greatest-id> -> review, all independent of mtime.
    rec = _step("cyc", "adversarial_cycle", M.PARKED, metrics={"rounds": 2})
    _make_steps_layout(
        tmp_path, "cyc",
        ["r1-review", "r2-review", "r2-triage/F-001", "r2-triage/F-009",
         "r2-fix", "r2-confirm"],
    )
    base = tmp_path / "steps" / "cyc"
    assert op.resolve_transcript_dir(tmp_path, rec) == base / "r2-confirm"

    (base / "r2-confirm").rmdir()
    assert op.resolve_transcript_dir(tmp_path, rec) == base / "r2-fix"

    (base / "r2-fix").rmdir()
    assert op.resolve_transcript_dir(tmp_path, rec) == base / "r2-triage" / "F-009"

    (base / "r2-triage" / "F-009").rmdir()
    (base / "r2-triage" / "F-001").rmdir()
    (base / "r2-triage").rmdir()
    assert op.resolve_transcript_dir(tmp_path, rec) == base / "r2-review"


def test_transcript_leaf_cycle_round_from_dirs_when_no_metrics(tmp_path):
    rec = _step("cyc", "adversarial_cycle", M.DONE)  # no metrics
    _make_steps_layout(tmp_path, "cyc", ["r1-confirm", "r2-confirm"])
    base = tmp_path / "steps" / "cyc"
    assert op.resolve_transcript_dir(tmp_path, rec) == base / "r2-confirm"


def test_transcript_leaf_retrospective(tmp_path):
    rec = _step("retro", "retrospective", M.DONE)
    _make_steps_layout(tmp_path, "retro", ["retro-alpha", "retro-beta"])
    base = tmp_path / "steps" / "retro"
    assert op.resolve_transcript_dir(tmp_path, rec) == base / "retro-beta"
    (base / "synthesis").mkdir()
    assert op.resolve_transcript_dir(tmp_path, rec) == base / "synthesis"


def test_transcript_leaf_unknown_type_falls_back_to_atomic(tmp_path):
    rec = _step("x", "some_future_type", M.DONE)
    _make_steps_layout(tmp_path, "x", [])
    assert op.resolve_transcript_dir(tmp_path, rec) == tmp_path / "steps" / "x"


# --- FR-5.6 report half: read-only recovery-intent parser --------------------
def _write_intent(run_dir: Path, *, step_id="impl.1", lock_nonce="nonce-1") -> None:
    payload = {
        "step_id": step_id,
        "lock_nonce": lock_nonce,
        "pid": 1, "pgid": 1, "host": THIS_HOST,
        "prior_step_status": "running", "prior_run_status": "running",
    }
    (run_dir / ".recovery-intent.json").write_text(json.dumps(payload))


def test_intent_absent_returns_null(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    recon, anomaly = op.read_recovery_intent(tmp_path, run_dir, "demo")
    assert recon is None and anomaly is None


def test_intent_matching_nonce_finalize_branch(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    _write_intent(run_dir, lock_nonce="nonce-1")
    _write_lock(tmp_path, nonce="nonce-1")
    recon, anomaly = op.read_recovery_intent(tmp_path, run_dir, "demo")
    assert anomaly is None
    assert recon.intent_step_id == "impl.1"
    assert recon.nonce_matches_lock is True
    assert recon.recommended_command == "gauntlet recover demo"


def test_intent_differing_nonce_stale_branch(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    _write_intent(run_dir, lock_nonce="nonce-OLD")
    _write_lock(tmp_path, nonce="nonce-NEW")
    recon, _ = op.read_recovery_intent(tmp_path, run_dir, "demo")
    assert recon.nonce_matches_lock is False


def test_intent_absent_lock_is_finalize_branch(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    _write_intent(run_dir)
    # no lock at all
    recon, _ = op.read_recovery_intent(tmp_path, run_dir, "demo")
    assert recon.nonce_matches_lock is True


def test_intent_unreadable_lock_fails_closed(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    _write_intent(run_dir)
    _write_lock(tmp_path, raw="not json")
    recon, _ = op.read_recovery_intent(tmp_path, run_dir, "demo")
    assert recon.nonce_matches_lock is False


def test_intent_malformed_yields_anomaly_and_null_recon(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    (run_dir / ".recovery-intent.json").write_text("{ broken")
    recon, anomaly = op.read_recovery_intent(tmp_path, run_dir, "demo")
    assert recon is None
    assert anomaly is not None and "recovery-intent" in anomaly


def test_intent_incomplete_missing_step_id_yields_anomaly(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    (run_dir / ".recovery-intent.json").write_text(json.dumps({"lock_nonce": "n"}))
    recon, anomaly = op.read_recovery_intent(tmp_path, run_dir, "demo")
    assert recon is None and anomaly is not None


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_status_reads_write_nothing_even_with_surviving_intent(tmp_path, host, probe, identity_read):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    _write_lock(tmp_path, nonce="nonce-1")
    _write_intent(run_dir, lock_nonce="nonce-1")
    man = _manifest(M.RUN_RUNNING, [_step("s", "agent_task", M.RUNNING)])

    before = _snapshot(tmp_path)
    driver = op.driver_info(tmp_path, "demo")
    rstate = op.compute_run_state(man, driver.state)
    recon, anomaly = op.read_recovery_intent(tmp_path, run_dir, "demo")
    op.render_footer(driver, rstate, reconciliation=recon, anomaly=anomaly)
    assert _snapshot(tmp_path) == before  # zero mutation


def test_status_reads_write_nothing_with_malformed_intent(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    (run_dir / ".recovery-intent.json").write_text("{ broken")
    before = _snapshot(tmp_path)
    op.read_recovery_intent(tmp_path, run_dir, "demo")
    assert _snapshot(tmp_path) == before


def test_intent_symlink_escape_refused_no_outside_read(tmp_path):
    run_dir = tmp_path / "run-x"
    run_dir.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"step_id": "x", "lock_nonce": "n"}))
    link = run_dir / ".recovery-intent.json"
    link.symlink_to(outside)
    recon, anomaly = op.read_recovery_intent(tmp_path, run_dir, "demo")
    # Refused with no out-of-tree read: no fabricated reconciliation object.
    assert recon is None
    assert anomaly is not None

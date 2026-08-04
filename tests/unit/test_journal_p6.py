"""P6 — append-only authoritative state journal (plan §4.6/§5.5/§8, R4/R5/R8).

Deterministic layers over real throwaway Git repositories:

* **Projection acceptance** — delete ``manifest.json`` and rebuild it from the
  journal byte-identically (every P1–P5 field round-trips); corrupt it and the
  malformed original is preserved as evidence while the rebuild converges;
  the read-only status surface and the mutating resume path agree on the
  rebuild action BEFORE it runs (R4), from one shared assessment.
* **Branch-reset acceptance (R8)** — resetting the run branch materializes an
  old committed manifest, which pre-P6 rewound the state machine; now the
  journal head survives, status classifies from it, and a plain resume
  continues with no lost attempts and no re-run of completed work.
* **Idempotency** — torn / duplicate / partially-flushed event files are
  reconciled deterministically by idempotency key (quarantined as evidence,
  never deleted, never double-applied); rebuild output is unaffected.
* **Migration (plan §8)** — a pre-P6 run dir (manifest only, no journal)
  resumes, classifies, and completes exactly as before, gaining one
  deterministic ``JournalGenesis`` event on first mutating contact (same
  input ⇒ same event bytes, modulo the injected clock).
* **Vocabulary + envelope** — the §4.6 event kinds are derived from the same
  persisted transitions the manifest already rides (table-driven), and every
  event carries the full §4.6 envelope.
* **Plan §9** — the destructive-verb boundary holds at every new P6 call
  site; the journal module itself cannot reach git at all.

The kill-at-every-boundary matrix (including the new event-append /
projection-write ``mid`` boundary) lives in
``test_recovery_unification.test_kill_at_every_persist_boundary_classifies_and_converges``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeAdapter, git

from gauntlet.engine import gitops, journal as J, manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine import recovery_exec as RX
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord

from test_recovery_unification import _commit, _seed


def _resume(mgr, writes=None, **kwargs):
    adapter = FakeAdapter(writes=writes or {"clean.py": "out\n"})
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter, **kwargs
    )
    return status, adapter


def _events(run_dir: Path) -> list[dict]:
    return J.read_events(run_dir)


def _state_events(run_dir: Path) -> list[dict]:
    return [e for e in _events(run_dir) if e.get("state_json") is not None]


# =============================================================================
# Lifecycle-literal + vocabulary drift guards
# =============================================================================


def test_journal_lifecycle_literals_match_manifest_constants():
    """journal.py is deliberately import-free (plan §9); its lifecycle
    literals are pinned 1:1 to manifest.py's constants here."""
    assert J._RUNNING == M.RUNNING
    assert J._DONE == M.DONE
    assert J._FAILED == M.FAILED
    assert J._INTERRUPTED == M.INTERRUPTED
    assert J._PARKED == M.PARKED
    assert J._HALTED == M.HALTED
    assert J._SKIPPED == M.SKIPPED
    assert J._REASON_ARTIFACT_INVALID == M.PARKED_REASON_ARTIFACT_INVALID
    assert J._DEPENDENCY_REASONS == frozenset(
        {
            M.PARKED_REASON_USAGE_LIMIT,
            M.PARKED_REASON_USAGE_WINDOW,
            M.PARKED_REASON_PROVIDER_UNAVAILABLE,
        }
    )


def test_event_vocabulary_is_the_plan_4_6_set_plus_genesis():
    assert set(J.EVENT_KINDS) == {
        "JournalGenesis",
        "AttemptStarted",
        "AgentCallStarted",
        "AgentCallFinished",
        "CheckpointObserved",
        "ArtifactValidationFailed",
        "DependencyUnavailable",
        "AttemptInterrupted",
        "RecoverySnapshotCreated",
        "RecoveryActionPlanned",
        "RecoveryActionApplied",
        "StepCompleted",
        "RunStatusChanged",
    }


# =============================================================================
# Kind derivation: table-driven over the persisted transition shapes
# =============================================================================


def _state(status="running", steps=(), commits=0, warnings=()):
    return {
        "run_id": "run-1",
        "status": status,
        "current_step": steps[0]["id"] if steps else None,
        "steps": list(steps),
        "commits": [
            {"step_id": "s", "phase": f"P{i + 1}", "sha": "a" * 40}
            for i in range(commits)
        ],
        "warnings": list(warnings),
    }


def _step(id="s", status="running", **kw):
    return {"id": id, "iteration": None, "attempts": 0, "status": status, **kw}


_KIND_ROWS = {
    "fresh_running_is_attempt_started": (
        _state(steps=[_step(status="pending")]),
        _state(steps=[_step(status="running")]),
        "AttemptStarted",
    ),
    "new_record_running_is_attempt_started": (
        _state(steps=[]),
        _state(steps=[_step(status="running")]),
        "AttemptStarted",
    ),
    "rerunning_started_step_is_agent_call_started": (
        _state(steps=[_step(status="parked", started="t0")]),
        _state(steps=[_step(status="running", started="t0")]),
        "AgentCallStarted",
    ),
    "done_is_step_completed": (
        _state(steps=[_step(status="running")]),
        _state(status="running", steps=[_step(status="done")]),
        "StepCompleted",
    ),
    "skipped_is_step_completed": (
        _state(steps=[_step(status="pending")]),
        _state(steps=[_step(status="skipped")]),
        "StepCompleted",
    ),
    "interrupted_is_attempt_interrupted": (
        _state(steps=[_step(status="running")]),
        _state(status="parked", steps=[_step(status="interrupted")]),
        "AttemptInterrupted",
    ),
    "artifact_park_is_validation_failed": (
        _state(steps=[_step(status="running")]),
        _state(status="parked", steps=[
            _step(status="parked", parked_reason="artifact_invalid")]),
        "ArtifactValidationFailed",
    ),
    "usage_limit_park_is_dependency_unavailable": (
        _state(steps=[_step(status="running")]),
        _state(status="parked", steps=[
            _step(status="parked", parked_reason="usage_limit")]),
        "DependencyUnavailable",
    ),
    "usage_window_park_is_dependency_unavailable": (
        _state(steps=[_step(status="running")]),
        _state(status="parked", steps=[
            _step(status="parked", parked_reason="usage_window")]),
        "DependencyUnavailable",
    ),
    "provider_park_is_dependency_unavailable": (
        _state(steps=[_step(status="running")]),
        _state(status="parked", steps=[
            _step(status="parked", parked_reason="provider_unavailable")]),
        "DependencyUnavailable",
    ),
    "response_park_is_agent_call_finished": (
        _state(steps=[_step(status="running")]),
        _state(status="parked", steps=[
            _step(status="parked", parked_reason="response")]),
        "AgentCallFinished",
    ),
    "gate_park_is_agent_call_finished": (
        _state(steps=[_step(status="running")]),
        _state(status="parked", steps=[
            _step(status="parked", parked_reason="gate")]),
        "AgentCallFinished",
    ),
    "failed_is_agent_call_finished": (
        _state(steps=[_step(status="running")]),
        _state(status="failed", steps=[_step(status="failed")]),
        "AgentCallFinished",
    ),
    "halted_is_agent_call_finished": (
        _state(steps=[_step(status="running")]),
        _state(status="parked", steps=[_step(status="halted")]),
        "AgentCallFinished",
    ),
    "checkpoint_growth_is_checkpoint_observed": (
        _state(steps=[_step(status="running", checkpoints=[])]),
        _state(steps=[_step(status="running", checkpoints=[
            {"sub_step": "review", "round": 1, "handoff_sha": "b" * 40}])]),
        "CheckpointObserved",
    ),
    "commit_growth_is_checkpoint_observed": (
        _state(steps=[_step(status="running")], commits=0),
        _state(steps=[_step(status="running")], commits=1),
        "CheckpointObserved",
    ),
    "run_status_only_is_run_status_changed": (
        _state(status="running", steps=[_step(status="parked")]),
        _state(status="parked", steps=[_step(status="parked")]),
        "RunStatusChanged",
    ),
    "warnings_only_is_run_status_changed": (
        _state(steps=[_step(status="running")]),
        _state(steps=[_step(status="running")], warnings=["advisory"]),
        "RunStatusChanged",
    ),
    "fresh_manifest_is_run_status_changed": (
        None,
        _state(steps=[]),
        "RunStatusChanged",
    ),
}


@pytest.mark.parametrize("row", sorted(_KIND_ROWS), ids=sorted(_KIND_ROWS))
def test_derive_kind_table(row):
    prev, cur, expected = _KIND_ROWS[row]
    kind, changes = J.derive_kind(prev, cur)
    assert kind == expected
    assert changes  # every derivation names its evidence


def test_derive_kind_precedence_terminalization_over_run_status():
    """The P4 one-write terminalization (step outcome + run status in one
    persist) classifies by the step outcome; the run transition stays in the
    changes payload."""
    prev = _state(status="running", steps=[_step(status="running")])
    cur = _state(status="running", steps=[_step(status="done")])
    cur["status"] = "running"
    kind, changes = J.derive_kind(prev, cur)
    assert kind == "StepCompleted"
    prev2 = _state(status="running", steps=[_step(status="running")])
    cur2 = _state(status="parked", steps=[
        _step(status="parked", parked_reason="usage_limit")])
    kind2, changes2 = J.derive_kind(prev2, cur2)
    assert kind2 == "DependencyUnavailable"
    assert any("run: running -> parked" in c for c in changes2)


# =============================================================================
# Envelope + write-ahead behavior on a real run
# =============================================================================


def test_every_event_carries_the_full_envelope_and_seq_is_monotonic(
    fixture_repo,
):
    mgr, man, base, run_dir = _seed(fixture_repo)
    status, adapter = _resume(mgr)
    assert status == M.RUN_DONE
    events = _events(run_dir)
    assert events, "a real run must journal its transitions"
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    for event in events:
        for key in (
            "schema_version", "seq", "event_id", "run_id", "step",
            "iteration", "attempt_id", "ts", "observed_branch_sha",
            "idempotency_key", "kind", "payload",
        ):
            assert key in event, f"envelope field {key} missing: {event}"
        assert event["schema_version"] == J.JOURNAL_SCHEMA_VERSION
        assert event["kind"] in J.EVENT_KINDS
        assert event["run_id"] == "run-1"
    # The seeded repo is a real git repo: observed HEAD is recorded evidence.
    assert any(e["observed_branch_sha"] for e in events)
    # The run's core transitions are all present, in order.
    kinds = [e["kind"] for e in events]
    assert "AttemptStarted" in kinds or "AgentCallStarted" in kinds
    assert "StepCompleted" in kinds
    # The head state event is byte-identical to the on-disk projection.
    head = _state_events(run_dir)[-1]
    assert head["state_json"] == (run_dir / "manifest.json").read_text()


def test_identical_repersist_appends_no_event(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    man = Manifest(
        run_id="run-1", slug="demo", branch="b", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
    )
    man.write_atomic(run_dir / "manifest.json")
    count = len(_events(run_dir))
    man.write_atomic(run_dir / "manifest.json")  # same state — no transition
    assert len(_events(run_dir)) == count


# =============================================================================
# Projection acceptance: delete / corrupt / rebuild (plan §5.5, R4/R5)
# =============================================================================


def _rich_manifest() -> Manifest:
    """A manifest exercising every P1–P5 field the projection must round-trip."""
    return Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="h" * 8),
        status=M.RUN_PARKED,
        current_step="cycle",
        warnings=["advisory warning"],
        steps=[
            StepRecord(
                id="implement", type="agent_task", agent="builder",
                status=M.DONE, started="t0", ended="t1", attempts=1,
                base_sha="a" * 40, session_id="s-1",
                usage=M.UsageTotals(input_tokens=10, output_tokens=5,
                                    cached_input_tokens=2, cost_usd=0.25),
                metrics={"rounds": 2},
                resumed_from_checkpoint="P1 wip: milestone",
                human_responses=[M.HumanResponse(
                    response_id="implement-resp-1", response_text="proceed",
                    timestamp="t0", user="op", response_attempt=1,
                    state=M.RESPONSE_CONSUMED)],
            ),
            StepRecord(
                id="lint", type="agent_task", status=M.PARKED,
                parked_reason=M.PARKED_REASON_ARTIFACT_INVALID,
                recovery_cause="artifact_invalid",
                recovery_disposition="edit_then_retry",
                revalidation=M.RevalidationRecord(
                    artifact="runs/demo/plan.md", hash_at_park="sha256:p",
                    hash_at_resume="sha256:q", changed_while_parked=True,
                    passed_on_resume=True, validator="plan_phases",
                    diagnostic="bad phase id"),
            ),
            StepRecord(
                id="deps", type="agent_task", status=M.FAILED,
                halt_reason=M.HALT_REASON_ADAPTER_ERROR,
                failure_kind=M.FAILURE_KIND_SIDE_EFFECT_FREE,
                recovery_cause="internal_error", recovery_disposition="retry",
                dependency_attempts=3,
            ),
            StepRecord(
                id="cycle", type="adversarial_cycle", status=M.PARKED,
                parked_reason=M.PARKED_REASON_USAGE_LIMIT,
                parked_substep="r1-fix", retry_after_s=120,
                quota_reset_at="2026-08-04T00:02:00+00:00",
                scheduled_resume=M.ScheduledResume(
                    attempt_at="2026-08-04T00:02:00+00:00", attempts=1),
                checkpoints=[M.Checkpoint(
                    sub_step="review", round=1, handoff_sha="c" * 40,
                    artifact="artifacts/r1/findings.json")],
                agent_usage={"reviewer": M.UsageTotals(input_tokens=7)},
            ),
        ],
        suspensions=[M.Suspension(start="t0", end="t1", gap_s=60)],
        commits=[M.CommitRecord(step_id="implement", phase="P1", sha="d" * 40)],
        totals=M.UsageTotals(input_tokens=17, output_tokens=5,
                             cached_input_tokens=2, cost_usd=0.25),
        agent_usage={"builder": M.UsageTotals(input_tokens=10)},
    )


def test_delete_and_rebuild_is_byte_identical_including_p1_p5_fields(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    path = run_dir / "manifest.json"
    man = _rich_manifest()
    man.write_atomic(path)  # transition 1
    man.warnings.append("second transition")
    man.write_atomic(path)  # transition 2 (head)
    original = path.read_bytes()
    path.unlink()
    seq, _event_id = J.write_projection_from_head(run_dir)
    assert path.read_bytes() == original  # byte-for-byte, by construction
    assert seq == _state_events(run_dir)[-1]["seq"]
    # And it still loads with every field intact.
    loaded = Manifest.load(path)
    assert loaded.record("lint").revalidation.validator == "plan_phases"
    assert loaded.record("deps").dependency_attempts == 3
    assert loaded.record("cycle").scheduled_resume is not None
    assert loaded.record("cycle").checkpoints[0].sub_step == "review"


def test_missing_manifest_rebuilds_through_resume_and_converges(fixture_repo):
    mgr, man, base, run_dir = _seed(fixture_repo)
    pre = (run_dir / "manifest.json").read_bytes()
    (run_dir / "manifest.json").unlink()
    # The mutating entry rebuilds byte-identically (the rebuilt state event's
    # bytes ARE the pre-deletion bytes), then appends the loud audit warning
    # as its own journaled transition — so the R5 fingerprint provably moves.
    mgr._reconcile_projection(run_dir, "demo")
    events = _state_events(run_dir)
    assert events[-2]["state_json"].encode() == pre  # the rebuilt projection
    rebuilt = json.loads((run_dir / "manifest.json").read_text())
    expected = json.loads(pre)
    expected["warnings"] = rebuilt["warnings"]  # only the audit note differs
    assert rebuilt == expected
    assert any("rebuilt manifest.json" in w for w in rebuilt["warnings"])
    status, adapter = _resume(mgr)
    assert status == M.RUN_DONE
    final = Manifest.load(run_dir / "manifest.json")
    assert any("rebuilt manifest.json" in w for w in final.warnings)


def test_corrupt_manifest_preserves_evidence_and_rebuild_converges(
    fixture_repo,
):
    mgr, man, base, run_dir = _seed(fixture_repo)
    pre = (run_dir / "manifest.json").read_bytes()
    garbage = b"{{{ not json \x00 torn"
    (run_dir / "manifest.json").write_bytes(garbage)

    # R4: BEFORE the rebuild runs, the read-only surface and the mutating
    # path derive the SAME action from the same shared assessment.
    view = op.load_projection_view(fixture_repo, run_dir, slug="demo")
    assert view.health == J.HEALTH_CORRUPT and view.rebuild_pending
    assert view.manifest is not None  # classifies from the journal head
    assert view.manifest.record("implement").status == M.RUNNING
    planned = RX.projection_rebuild_assessment(
        fixture_repo, run_dir, slug="demo"
    )
    assert planned is not None
    assessment, action = planned
    assert view.action == action  # one construction point, zero drift
    rendered = op.projection_rebuild_action("demo", view.action)
    assert rendered.argv == ["gauntlet", "resume", "demo"]
    assert "journal" in rendered.consequence

    # The advertised action, taken: plain resume rebuilds then converges.
    status, adapter = _resume(mgr)
    assert status == M.RUN_DONE
    corrupt_copies = sorted(run_dir.glob("manifest.corrupt-*.json"))
    assert corrupt_copies, "malformed original must be preserved as evidence"
    assert corrupt_copies[0].read_bytes() == garbage
    final = Manifest.load(run_dir / "manifest.json")
    assert any("rebuilt manifest.json" in w for w in final.warnings)
    assert any("preserved" in w for w in final.warnings)
    # The rebuild recorded its applied action in the journal.
    kinds = [e["kind"] for e in _events(run_dir)]
    assert "RecoveryActionApplied" in kinds


def test_rebuild_precondition_recheck_fails_closed_on_moved_projection(
    fixture_repo,
):
    """The executor re-verifies the evidence fingerprint under the lock: a
    projection that changed between assessment and apply refuses with zero
    mutation (transaction step 2/3)."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    (run_dir / "manifest.json").write_bytes(b"corrupt v1")
    planned = RX.projection_rebuild_assessment(fixture_repo, run_dir, slug="demo")
    assert planned is not None
    assessment, action = planned
    (run_dir / "manifest.json").write_bytes(b"corrupt v2 moved")
    executor = RX.RecoveryExecutor(
        fixture_repo, run_dir, run_id="run-1", run_root="runs"
    )
    with pytest.raises(RX.RecoveryPreconditionError, match="fingerprint"):
        executor.apply_rebuild(assessment, action)
    assert (run_dir / "manifest.json").read_bytes() == b"corrupt v2 moved"
    assert not list(run_dir.glob("manifest.corrupt-*.json"))


def test_status_json_renders_projection_block_and_rebuild_action(
    fixture_repo, monkeypatch
):
    """CLI-level R4: `status --json` on a corrupt projection exits 0,
    classifies from the journal head, emits the additive `projection` block,
    and renders the rebuild resume as the first next action."""
    from typer.testing import CliRunner

    from gauntlet.cli import app

    mgr, man, base, run_dir = _seed(fixture_repo)
    (run_dir / "manifest.json").write_bytes(b"{{{ corrupt")
    monkeypatch.chdir(fixture_repo)
    result = CliRunner().invoke(app, ["status", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["projection"] == {
        "health": "corrupt",
        "journal_seq": _state_events(run_dir)[-1]["seq"],
        "rebuild_pending": True,
    }
    first = payload["next_actions"][0]
    assert first["argv"] == ["gauntlet", "resume", "demo"]
    assert first["kind"] == "recover"
    assert "journal" in first["consequence"]
    op._validate_status_payload(payload)  # additive: still schema-valid


def test_status_json_projection_is_null_when_healthy(fixture_repo, monkeypatch):
    from typer.testing import CliRunner

    from gauntlet.cli import app

    mgr, man, base, run_dir = _seed(fixture_repo)
    monkeypatch.chdir(fixture_repo)
    result = CliRunner().invoke(app, ["status", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["projection"] is None


# =============================================================================
# Branch-reset acceptance (R8): the state machine survives a branch reset
# =============================================================================


def test_branch_reset_materializing_old_manifest_does_not_reset_state(
    fixture_repo,
):
    """R8 headline: a run-branch reset restores the TRACKED manifest.json to
    an old committed state — which pre-P6 rewound the state machine (lost
    attempts, re-run of completed work). With the journal authoritative,
    status classifies from the journal head and a plain resume continues:
    the completed step is not re-run and the reconciliation is loud."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    # Bookkeeping commit carries the RUNNING-state manifest (S1) on the branch.
    gitops.commit_run_bookkeeping(
        fixture_repo, "gauntlet: manifest checkpoint",
        ["runs/demo/run-1/manifest.json"], identity=gitops.ENGINE_IDENTITY,
    )
    # The run completes: state advances to S2 (journaled), uncommitted.
    status, first_adapter = _resume(mgr)
    assert status == M.RUN_DONE
    assert len(first_adapter.calls) == 1
    s2 = (run_dir / "manifest.json").read_bytes()

    # The reset (the file effect every sanctioned rewind's reset_hard has):
    # the tracked manifest.json snaps back to the committed S1 — a RUNNING
    # step under a running run. Pre-P6 this WAS the state machine rewinding.
    git(fixture_repo, "reset", "-q", "--hard", "HEAD")
    stale = Manifest.load(run_dir / "manifest.json")
    assert stale.record("implement").status == M.RUNNING  # the pre-P6 trap

    # Read-only surface: classifies from the journal head, names the repair.
    view = op.load_projection_view(fixture_repo, run_dir, slug="demo")
    assert view.health == J.HEALTH_STALE and view.rebuild_pending
    assert view.manifest.record("implement").status == M.DONE
    assert view.manifest.status == M.RUN_DONE

    # Mutating surface: plain resume catches the projection up and finds the
    # run already complete — no lost attempts, no re-run of completed work.
    second = FakeAdapter(writes={"clean.py": "out\n"})
    result = mgr.resume("demo", use_judge=False, adapter_factory=lambda n: second)
    assert result == M.RUN_DONE
    assert second.calls == []  # the completed step did NOT re-run
    final = Manifest.load(run_dir / "manifest.json")
    assert final.record("implement").status == M.DONE
    assert any("projection catch-up" in w for w in final.warnings)
    # The catch-up itself restored S2's content (plus the loud audit note).
    assert json.loads(s2)["steps"] == [
        s for s in json.loads(
            (run_dir / "manifest.json").read_text())["steps"]
    ]


# =============================================================================
# Idempotency: torn / duplicate / partially-flushed events (deliverable 3)
# =============================================================================


def _torn(run_dir):
    head = _state_events(run_dir)[-1]
    seq = head["seq"] + 1
    name = f"evt-{seq:08d}-StepCompleted-{'0' * 12}.json"
    (J.journal_dir(run_dir) / name).write_bytes(b'{"schema_version": 1, "tor')
    return name


def _empty(run_dir):
    head = _state_events(run_dir)[-1]
    seq = head["seq"] + 1
    name = f"evt-{seq:08d}-StepCompleted-{'1' * 12}.json"
    (J.journal_dir(run_dir) / name).write_bytes(b"")
    return name


def _duplicate(run_dir):
    jdir = J.journal_dir(run_dir)
    paths = sorted(
        p for p in jdir.iterdir() if J._EVENT_NAME_RE.match(p.name)
    )
    source = paths[-1]
    match = J._EVENT_NAME_RE.match(source.name)
    seq = int(match.group("seq")) + 1
    event = json.loads(source.read_text())
    event["seq"] = seq  # a replayed append: same key, next sequence
    name = f"evt-{seq:08d}-{match.group('kind')}-{match.group('key')}.json"
    (jdir / name).write_text(json.dumps(event, sort_keys=True, indent=1))
    return name


_FAULTS = {"torn": _torn, "empty": _empty, "duplicate": _duplicate}
_QUARANTINE_SUFFIX = {"torn": ".torn", "empty": ".torn", "duplicate": ".dup"}


@pytest.mark.parametrize("fault", sorted(_FAULTS), ids=sorted(_FAULTS))
def test_faulty_event_is_reconciled_deterministically(tmp_path, fault):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    path = run_dir / "manifest.json"
    man = _rich_manifest()
    man.write_atomic(path)
    man.warnings.append("t2")
    man.write_atomic(path)
    pre = path.read_bytes()
    head_before = _state_events(run_dir)[-1]

    name = _FAULTS[fault](run_dir)
    # Read-only status skips the fault without touching it.
    ro = J.projection_status(run_dir, mutate=False)
    assert ro.health == J.HEALTH_OK
    assert (J.journal_dir(run_dir) / name).exists()

    # The mutating reconcile quarantines it as evidence — never deletes it,
    # never double-applies, and the state chain is unaffected.
    outcome = J.reconcile_projection(run_dir)
    assert outcome.health == J.HEALTH_OK
    quarantined = J.journal_dir(run_dir) / (name + _QUARANTINE_SUFFIX[fault])
    assert quarantined.exists()
    assert not (J.journal_dir(run_dir) / name).exists()
    assert _state_events(run_dir)[-1]["event_id"] == head_before["event_id"]
    assert path.read_bytes() == pre

    # Deterministic: a second reconcile is a no-op.
    outcome2 = J.reconcile_projection(run_dir)
    assert outcome2.health == J.HEALTH_OK
    # Rebuild output is unaffected by the fault (applied exactly once).
    path.unlink()
    J.write_projection_from_head(run_dir)
    assert path.read_bytes() == pre


def test_quarantined_seq_is_never_reused(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    man = _rich_manifest()
    man.write_atomic(run_dir / "manifest.json")
    name = _torn(run_dir)
    torn_seq = int(J._SEQ_CLAIM_RE.match(name).group("seq"))
    J.reconcile_projection(run_dir)  # quarantines the torn claim
    man.warnings.append("next")
    man.write_atomic(run_dir / "manifest.json")
    new_seq = _state_events(run_dir)[-1]["seq"]
    assert new_seq > torn_seq  # append-only ordering stays unambiguous


def test_unjournaled_valid_manifest_is_adopted_loudly_not_overwritten(
    tmp_path,
):
    """A valid same-run manifest the journal has never seen (stale-driver
    write or hand-edit) is adopted as a new transition — data is never lost
    in either direction, and the journal retains the prior state."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    path = run_dir / "manifest.json"
    man = _rich_manifest()
    man.write_atomic(path)
    prior_head = _state_events(run_dir)[-1]
    # Simulate a direct (unjournaled) write: bytes change, journal does not.
    edited = Manifest.load(path)
    edited.warnings.append("hand-edited out of band")
    text = edited.model_dump_json(indent=2)
    path.write_text(text)

    outcome = J.reconcile_projection(run_dir)
    assert outcome.health == J.HEALTH_ADOPTED
    head = _state_events(run_dir)[-1]
    assert head["state_json"] == text
    assert head["payload"].get("adopted_outside_engine") is True
    assert head["seq"] > prior_head["seq"]
    assert path.read_text() == text  # the newest claim survives


# =============================================================================
# Migration (deliverable 4, plan §8)
# =============================================================================


def _make_pre_p6(fixture_repo):
    """A pre-P6 run dir: manifest bytes on disk, NO journal."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    import shutil

    shutil.rmtree(J.journal_dir(run_dir))
    return mgr, man, base, run_dir


def test_pre_p6_run_classifies_exactly_as_before_without_writes(fixture_repo):
    mgr, man, base, run_dir = _make_pre_p6(fixture_repo)
    view = op.load_projection_view(fixture_repo, run_dir, slug="demo")
    assert view.health == J.HEALTH_NO_JOURNAL
    assert view.payload_block() is None  # status --json: projection null
    rstate = op.compute_run_state(
        view.manifest, op.driver_liveness(fixture_repo / "runs", "demo")
    )
    assert rstate.state == op.STATE_ORPHANED
    assert not J.journal_dir(run_dir).exists()  # read-only status wrote nothing


def test_pre_p6_run_gains_genesis_on_first_contact_and_completes(fixture_repo):
    mgr, man, base, run_dir = _make_pre_p6(fixture_repo)
    pre_bytes = (run_dir / "manifest.json").read_text()
    status, adapter = _resume(mgr)
    assert status == M.RUN_DONE
    events = _events(run_dir)
    assert events[0]["kind"] == "JournalGenesis"
    assert events[0]["seq"] == 1
    assert events[0]["state_json"] == pre_bytes  # bytes embedded verbatim
    assert events[0]["payload"]["migrated_from"] == "manifest.json"
    # The rest of the run journaled normally after the genesis.
    assert [e["kind"] for e in events].count("JournalGenesis") == 1
    assert _state_events(run_dir)[-1]["state_json"] == (
        run_dir / "manifest.json"
    ).read_text()


def test_pre_p6_corrupt_manifest_gets_no_genesis_and_repairs_as_before(
    tmp_path,
):
    """A pre-P6 run whose manifest is ALREADY corrupt has no valid state to
    seed the journal with: genesis is refused (embedding the corrupt bytes
    would later 'rebuild' a hand-repaired manifest back to corruption), the
    run keeps its exact pre-P6 failure, and the first contact AFTER a repair
    seeds the genesis from the repaired bytes."""
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_bytes(b"{{{ corrupt pre-P6")
    assert J.ensure_genesis(run_dir) is None
    outcome = J.reconcile_projection(run_dir)
    assert outcome.health == J.HEALTH_NO_JOURNAL  # exactly the pre-P6 shape
    text = _rich_manifest().model_dump_json(indent=2)
    (run_dir / "manifest.json").write_text(text)
    outcome2 = J.reconcile_projection(run_dir)
    assert outcome2.health == J.HEALTH_GENESIS
    assert _state_events(run_dir)[-1]["state_json"] == text


def test_genesis_is_deterministic_modulo_clock(tmp_path):
    clock = lambda: "2026-08-04T00:00:00+00:00"  # noqa: E731
    text = _rich_manifest().model_dump_json(indent=2)
    files = []
    for name in ("a", "b"):
        run_dir = tmp_path / name
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(text)
        event = J.ensure_genesis(run_dir, clock=clock)
        assert event is not None
        paths = list(J.journal_dir(run_dir).iterdir())
        assert len(paths) == 1
        files.append((paths[0].name, paths[0].read_bytes()))
    assert files[0] == files[1]  # same input -> same event name and bytes


# =============================================================================
# Executor audit events (plan §4.6 Recovery* vocabulary)
# =============================================================================


def test_executor_transaction_journals_snapshot_planned_applied(fixture_repo):
    mgr, man, base, run_dir = _seed(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")  # dirty mid-edit
    status, adapter = _resume(mgr)  # parks INTERRUPTED under the park policy
    assert status == M.RUN_PARKED
    status2, adapter2 = _resume(mgr, reset_interrupted=True)
    assert status2 == M.RUN_DONE
    events = _events(run_dir)
    by_kind = {}
    for event in events:
        by_kind.setdefault(event["kind"], []).append(event)
    assert "RecoverySnapshotCreated" in by_kind
    assert "RecoveryActionPlanned" in by_kind
    assert "RecoveryActionApplied" in by_kind
    planned = by_kind["RecoveryActionPlanned"][0]["payload"]
    applied = by_kind["RecoveryActionApplied"][0]["payload"]
    assert planned["intent_id"] == applied["intent_id"]
    assert planned["snapshot_ref"].startswith("refs/gauntlet/recovery/")


# =============================================================================
# Plan §9: the destructive-verb boundary holds at every new P6 call site
# =============================================================================


def test_p6_call_sites_stay_free_of_direct_destructive_git_verbs():
    import inspect

    from gauntlet.engine.run import RunManager

    for func in (
        J.record_transition,
        J.reconcile_projection,
        J.projection_status,
        J.ensure_genesis,
        J.write_projection_from_head,
        RX.projection_rebuild_assessment,
        RX.RecoveryExecutor.apply_rebuild,
        op.load_projection_view,
        RunManager._reconcile_projection,
    ):
        source = inspect.getsource(func)
        for verb in (
            "gitops.reset_hard(",
            "gitops.clean_untracked(",
            "gitops.rewind_impl_preserving_bookkeeping(",
            "gitops.checkout_branch(",
        ):
            assert verb not in source, (
                f"{func.__qualname__} calls {verb} directly; every Git "
                "mutation must route through RecoveryExecutor (plan §9)"
            )


def test_journal_module_cannot_reach_git_or_subprocess():
    """journal/projection writes are file mutations only: the module imports
    no gauntlet engine module, no gitops, and no subprocess machinery, so a
    destructive git verb is unreachable from it by construction."""
    source = Path(J.__file__).read_text()
    for banned in (
        "import subprocess", "subprocess.", "gitops.", "import gitops",
        "from gauntlet",
    ):
        assert banned not in source, f"journal.py must not use {banned!r}"

"""P1: manifest schema for the conflict-park discriminator + human_responses.

Round-trip fidelity and back-compat for the two new ``StepRecord`` fields
(``parked_reason``, ``human_responses``) added for the resume-with-response
feature (FR-2, FR-2.1). The orchestration lifecycle of ``parked_reason`` is
covered in ``test_orchestrator.py``; here we pin only the persisted shape.
"""

import json

from gauntlet.engine.manifest import (
    PARKED_REASON_UPSTREAM_CONFLICT,
    HumanResponse,
    Manifest,
    PipelineRef,
    StepRecord,
)


def _manifest() -> Manifest:
    return Manifest(
        run_id="run-x",
        slug="demo",
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:abc"),
    )


def _responses() -> list[HumanResponse]:
    return [
        HumanResponse(
            response_id="impl-resp-1",
            response_text="Ratify option 1: remove asset_root change.",
            timestamp="2026-06-24T00:00:00+00:00",
            user="john@pletka.com",
            response_attempt=1,
            state="consumed",
        ),
        HumanResponse(
            response_id="impl-resp-2",
            response_text="Proceed; defer post-v1 work to FUTURE.md.",
            timestamp="2026-06-24T01:00:00+00:00",
            user="john@pletka.com",
            response_attempt=2,
            state="pending",
        ),
    ]


def test_step_record_defaults_are_back_compat():
    # The two new fields default so existing call sites (and old manifests) need
    # no change: no conflict reason, no recorded responses.
    rec = StepRecord(id="a", type="agent_task")
    assert rec.parked_reason is None
    assert rec.human_responses == []


def test_round_trip_preserves_parked_reason_and_responses(tmp_path):
    man = _manifest()
    man.upsert(
        StepRecord(
            id="implement",
            type="agent_task",
            status="parked",
            parked_reason=PARKED_REASON_UPSTREAM_CONFLICT,
            human_responses=_responses(),
        )
    )
    path = tmp_path / "manifest.json"
    man.write_atomic(path)
    loaded = Manifest.load(path)

    rec = loaded.record("implement")
    assert rec.parked_reason == PARKED_REASON_UPSTREAM_CONFLICT
    assert [r.response_id for r in rec.human_responses] == [
        "impl-resp-1",
        "impl-resp-2",
    ]
    # full field fidelity on the second (pending) entry
    second = rec.human_responses[1]
    assert second.response_text == "Proceed; defer post-v1 work to FUTURE.md."
    assert second.timestamp == "2026-06-24T01:00:00+00:00"
    assert second.user == "john@pletka.com"
    assert second.response_attempt == 2
    assert second.state == "pending"


def test_round_trip_is_byte_stable(tmp_path):
    # Persisting a loaded manifest reproduces identical bytes — no field churn.
    man = _manifest()
    man.upsert(
        StepRecord(
            id="implement",
            type="agent_task",
            status="parked",
            parked_reason=PARKED_REASON_UPSTREAM_CONFLICT,
            human_responses=_responses(),
        )
    )
    path = tmp_path / "manifest.json"
    man.write_atomic(path)
    first = path.read_text()
    Manifest.load(path).write_atomic(path)
    assert path.read_text() == first


def test_loads_legacy_manifest_without_new_fields(tmp_path):
    # Back-compat: a manifest written before this feature has neither field; it
    # must load and default to None / [], not raise on the missing keys.
    legacy = {
        "run_id": "run-x",
        "slug": "demo",
        "branch": "gauntlet/demo",
        "base_branch": "main",
        "pipeline": {"name": "demo", "version": 1, "hash": "sha256:abc"},
        "steps": [
            {"id": "implement", "type": "agent_task", "status": "parked"},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(legacy, indent=2))

    loaded = Manifest.load(path)
    rec = loaded.record("implement")
    assert rec.parked_reason is None
    assert rec.human_responses == []

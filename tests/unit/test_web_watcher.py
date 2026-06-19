"""Watcher edge-triggering (P2, FR-8.1): one event per *semantic* transition.

Drives a fixture manifest through scripted atomic writes and asserts the watcher
emits exactly one event per transition under the semantic identity tuple
``(run_id, current_step, current_step_status, run_status)`` — never collapsing
distinct gate parks, and never firing on a semantic no-op rewrite (mtime is only
a re-read gate, review F-002).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.web.store import RunStore
from gauntlet.web.watcher import Watcher


def _manifest(slug: str, run_id: str, *, status: str, steps, current_step) -> Manifest:
    return Manifest(
        run_id=run_id,
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:dead"),
        status=status,
        current_step=current_step,
        steps=steps,
    )


def _write(run_dir: Path, man: Manifest) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    man.write_atomic(run_dir / "manifest.json")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    return repo


@pytest.fixture
def store(repo: Path) -> RunStore:
    return RunStore(repo, RunConfig())


def _run_dir(repo: Path, slug: str, run_id: str) -> Path:
    return repo / "runs" / slug / run_id


def test_first_observation_emits_once(repo: Path, store: RunStore):
    rd = _run_dir(repo, "alpha", "run-1")
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="running",
            steps=[StepRecord(id="impl", type="agent_task", status="running")],
            current_step="impl",
        ),
    )
    w = Watcher(store)
    events = w.poll_once()
    assert len(events) == 1
    ev = events[0]
    assert ev.slug == "alpha"
    assert ev.run_id == "run-1"
    assert ev.run_status == "running"
    assert ev.current_step == "impl"
    assert ev.current_step_status == "running"
    # A re-poll with no write emits nothing.
    assert w.poll_once() == []


def test_one_event_per_transition(repo: Path, store: RunStore):
    rd = _run_dir(repo, "alpha", "run-1")
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="running",
            steps=[StepRecord(id="impl", type="agent_task", status="running")],
            current_step="impl",
        ),
    )
    w = Watcher(store)
    assert len(w.poll_once()) == 1  # discovery

    # transition: step done, run moves to a gate
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="parked",
            steps=[
                StepRecord(id="impl", type="agent_task", status="done"),
                StepRecord(id="gate", type="human_gate", status="parked"),
            ],
            current_step="gate",
        ),
    )
    evs = w.poll_once()
    assert len(evs) == 1
    assert evs[0].run_status == "parked"
    assert evs[0].current_step == "gate"
    assert evs[0].current_step_status == "parked"
    assert w.poll_once() == []


def test_two_distinct_gates_not_collapsed(repo: Path, store: RunStore):
    """Two parks at *different* steps must both emit (FR-8.1 vs coarse keying)."""
    rd = _run_dir(repo, "alpha", "run-1")
    # park at gate A
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="parked",
            steps=[StepRecord(id="gateA", type="human_gate", status="parked")],
            current_step="gateA",
        ),
    )
    w = Watcher(store)
    first = w.poll_once()
    assert len(first) == 1 and first[0].current_step == "gateA"

    # park at gate B — run_status stays "parked", only current_step changes.
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="parked",
            steps=[
                StepRecord(id="gateA", type="human_gate", status="done"),
                StepRecord(id="gateB", type="human_gate", status="parked"),
            ],
            current_step="gateB",
        ),
    )
    second = w.poll_once()
    assert len(second) == 1
    assert second[0].current_step == "gateB"
    # The coarse (run_id, run_status) key the PRD rejects would have swallowed
    # this second "parked" — assert it did not.
    assert second[0].run_status == "parked"


def test_revision_change_emits_even_when_semantic_fields_identical(
    repo: Path, store: RunStore
):
    """FR-8.1: ``manifest_revision`` (mtime) is part of the event identity, so a
    rewrite that changes only the revision still emits (review F-001). The PRD
    includes the revision so a re-entry into the same semantic state is observed;
    suppressing duplicate *notifications* is FR-9.1's separate concern (P6).
    """
    rd = _run_dir(repo, "alpha", "run-1")
    man = _manifest(
        "alpha", "run-1", status="running",
        steps=[StepRecord(id="impl", type="agent_task", status="running")],
        current_step="impl",
    )
    _write(rd, man)
    w = Watcher(store)
    assert len(w.poll_once()) == 1
    mpath = rd / "manifest.json"
    st = mpath.stat()

    # Rewrite the *same* four semantic fields — only a non-identity field and the
    # revision change. Force a strictly-later mtime so the assertion does not
    # depend on filesystem mtime granularity.
    man.totals.input_tokens += 7
    _write(rd, man)
    os.utime(mpath, ns=(st.st_atime_ns, st.st_mtime_ns + 1000))

    evs = w.poll_once()
    assert len(evs) == 1
    # The four semantic fields are unchanged; only the revision advanced.
    assert evs[0].run_status == "running"
    assert evs[0].current_step == "impl"
    assert evs[0].current_step_status == "running"
    assert evs[0].revision == st.st_mtime_ns + 1000
    assert w.poll_once() == []  # no further write → unchanged mtime → nothing


def test_current_step_status_change_emits(repo: Path, store: RunStore):
    """Same run_status + same current_step but the *step* status flips → emit."""
    rd = _run_dir(repo, "alpha", "run-1")
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="running",
            steps=[StepRecord(id="impl", type="agent_task", status="pending")],
            current_step="impl",
        ),
    )
    w = Watcher(store)
    assert len(w.poll_once()) == 1
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="running",
            steps=[StepRecord(id="impl", type="agent_task", status="running")],
            current_step="impl",
        ),
    )
    evs = w.poll_once()
    assert len(evs) == 1
    assert evs[0].current_step_status == "running"


def test_multiple_runs_tracked_independently(repo: Path, store: RunStore):
    _write(
        _run_dir(repo, "alpha", "run-1"),
        _manifest(
            "alpha", "run-1", status="running",
            steps=[StepRecord(id="a", type="agent_task", status="running")],
            current_step="a",
        ),
    )
    _write(
        _run_dir(repo, "beta", "run-1"),
        _manifest(
            "beta", "run-1", status="parked",
            steps=[StepRecord(id="g", type="human_gate", status="parked")],
            current_step="g",
        ),
    )
    w = Watcher(store)
    evs = w.poll_once()
    assert {e.slug for e in evs} == {"alpha", "beta"}
    # Advancing only alpha emits exactly one event (beta is untouched).
    _write(
        _run_dir(repo, "alpha", "run-1"),
        _manifest(
            "alpha", "run-1", status="done",
            steps=[StepRecord(id="a", type="agent_task", status="done")],
            current_step=None,
        ),
    )
    evs2 = w.poll_once()
    assert len(evs2) == 1 and evs2[0].slug == "alpha" and evs2[0].run_status == "done"


def test_broken_then_valid_manifest_recovers(repo: Path, store: RunStore):
    """A torn/unparseable manifest emits nothing; a later valid write emits."""
    rd = _run_dir(repo, "alpha", "run-1")
    rd.mkdir(parents=True)
    mpath = rd / "manifest.json"
    mpath.write_text("{ not valid json")
    w = Watcher(store)
    assert w.poll_once() == []  # fail closed, no phantom transition
    # Now a valid manifest lands.
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="running",
            steps=[StepRecord(id="a", type="agent_task", status="running")],
            current_step="a",
        ),
    )
    evs = w.poll_once()
    assert len(evs) == 1 and evs[0].run_status == "running"


def test_identity_tuple_includes_revision():
    from gauntlet.web.watcher import WatchEvent

    ev = WatchEvent(
        slug="s", run_id="run-1", run_status="parked",
        current_step="gate", current_step_status="parked",
        current_step_notes="note", updated="2026-01-01T00:00:00",
        revision=123456789,
    )
    # FR-8.1 identity is the four semantic fields + manifest_revision (mtime ns);
    # the ISO `updated` display string and `notes` are NOT part of it (F-001).
    assert ev.identity == ("run-1", "gate", "parked", "parked", 123456789)


def test_foreach_active_iteration_is_current(repo: Path, store: RunStore):
    """`current_step` carries no iteration; the watcher must report the *active*
    foreach iteration's status, not a completed earlier one (review F-003)."""
    rd = _run_dir(repo, "alpha", "run-1")
    # Two iterations of one fan-out step share id "review": iter 0 done, iter 1
    # running. `current_step` is just "review" (no iteration).
    _write(
        rd,
        _manifest(
            "alpha", "run-1", status="running",
            steps=[
                StepRecord(id="review", type="agent_task", status="done", iteration="0"),
                StepRecord(id="review", type="agent_task", status="running", iteration="1"),
            ],
            current_step="review",
        ),
    )
    w = Watcher(store)
    evs = w.poll_once()
    assert len(evs) == 1
    # Must reflect the running iteration (1), not the done one (0).
    assert evs[0].current_step == "review"
    assert evs[0].current_step_status == "running"

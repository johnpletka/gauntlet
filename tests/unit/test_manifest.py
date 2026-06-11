"""Manifest round-trip + atomic write (§7, FR-8.2)."""

import json

from gauntlet.adapters.base import Usage
from gauntlet.engine.manifest import (
    Manifest,
    PipelineRef,
    StepRecord,
    UsageTotals,
)


def _manifest() -> Manifest:
    return Manifest(
        run_id="run-x",
        slug="demo",
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:abc"),
    )


def test_round_trip(tmp_path):
    man = _manifest()
    man.upsert(StepRecord(id="a", type="shell", status="done"))
    path = tmp_path / "manifest.json"
    man.write_atomic(path)
    loaded = Manifest.load(path)
    assert loaded.run_id == "run-x"
    assert loaded.record("a").status == "done"


def test_atomic_write_leaves_no_temp(tmp_path):
    man = _manifest()
    path = tmp_path / "manifest.json"
    man.write_atomic(path)
    man.write_atomic(path)
    leftovers = [p for p in path.parent.iterdir() if p.name.startswith(".manifest-")]
    assert leftovers == []
    # the file is valid JSON every time
    json.loads(path.read_text())


def test_upsert_replaces_same_id_and_iteration():
    man = _manifest()
    man.upsert(StepRecord(id="a", type="shell", status="running"))
    man.upsert(StepRecord(id="a", type="shell", status="done"))
    assert len([r for r in man.steps if r.id == "a"]) == 1
    assert man.record("a").status == "done"


def test_iteration_records_are_distinct():
    man = _manifest()
    man.upsert(StepRecord(id="impl", type="agent_task", iteration="0"))
    man.upsert(StepRecord(id="impl", type="agent_task", iteration="1"))
    assert man.record("impl", "0") is not man.record("impl", "1")


def test_usage_totals_accumulate_and_handle_none_cost():
    totals = UsageTotals()
    totals.add(Usage(input_tokens=10, output_tokens=5, cost_usd=0.01))
    totals.add(Usage(input_tokens=20, output_tokens=1, cost_usd=None))
    assert totals.input_tokens == 30
    assert totals.output_tokens == 6
    assert totals.cost_usd == 0.01

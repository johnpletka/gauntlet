"""P10 — per-step ledger append + window admission wiring (FR-10.1/10.2/10.3).

Drives the orchestrator to assert the machine-global ledger integration end to
end: every agent step with usage appends a content-free ledger row; a
window-constrained provider with insufficient headroom warns (advisory, default)
or parks ``usage_window`` before the step launches (enforce). The ledger path is
the per-test temp file the autouse conftest fixture points ``GAUNTLET_LEDGER_PATH``
at, so nothing here touches the real machine-global ledger.
"""

from __future__ import annotations

from gauntlet.adapters.base import Usage
from gauntlet.engine import ledger as L, manifest as M
from gauntlet.engine import operator

from conftest import FakeAdapter
from test_orchestrator import _build

STEP = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, output: out.txt, prompt_text: go}
"""


def _window_cfg(*, budget: float, enforce: bool, fallback: float | None):
    window = {"window_hours": 5, "window_budget": budget, "enforce": enforce}
    if fallback is not None:
        window["fallback_estimate"] = fallback
    return {
        "agents": {"builder": {"adapter": "claude-code"}},
        "providers": {"anthropic": window},
    }


# --- per-step ledger append (FR-10.1) ----------------------------------------


def test_agent_step_appends_content_free_ledger_row(fixture_repo):
    adapter = FakeAdapter(
        writes={"src.py": "print(1)\n"},
        text="done",
        usage=Usage(input_tokens=120, output_tokens=30, cost_usd=0.4),
    )
    orch = _build(fixture_repo, STEP, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_DONE

    rows = L.load_rows(L.default_ledger_path())
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == "run-1"
    assert row.step_id == "implement"
    assert row.provider == "anthropic"  # claude-code → anthropic
    assert row.profile == "builder"
    assert row.step_type == "agent_task"
    assert row.input_tokens == 120 and row.output_tokens == 30
    assert row.cost_usd == 0.4
    # Content-free: no prompt/output text leaks into the row's JSON.
    assert "print(1)" not in row.model_dump_json()
    assert "go" not in row.model_dump_json()


def test_ledger_append_is_idempotent_across_resume(fixture_repo):
    """A step re-finalized on resume never double-counts (de-dup by key)."""
    adapter = FakeAdapter(text="done", usage=Usage(input_tokens=50, output_tokens=0))
    orch = _build(fixture_repo, STEP, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_DONE
    # Re-drive the SAME manifest (a resume): the step is already DONE and not
    # re-executed, but even a re-append would be de-duped by run_id::step_id.
    orch2 = _build(fixture_repo, STEP, adapters={"builder": adapter},
                   manifest=orch.manifest)
    orch2.drive()
    rows = L.load_rows(L.default_ledger_path())
    assert len([r for r in rows if r.step_id == "implement"]) == 1


# --- advisory admission (FR-10.3 default) ------------------------------------


def test_advisory_short_headroom_warns_but_launches(fixture_repo):
    # Empty ledger + a large fallback estimate over a tiny budget → insufficient,
    # but enforce=False, so the step still runs to DONE and a warning is recorded.
    cfg = _window_cfg(budget=100, enforce=False, fallback=1000)
    adapter = FakeAdapter(text="done", usage=Usage(input_tokens=10, output_tokens=0))
    orch = _build(fixture_repo, STEP, config=cfg, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_DONE

    assert orch.manifest.record("implement").status == M.DONE
    assert adapter.calls, "advisory mode must still launch the step"
    warnings = orch.manifest.warnings
    assert any("usage-window admission" in w and "implement" in w for w in warnings)


# --- enforce admission (FR-10.3) ---------------------------------------------


def test_enforce_short_headroom_parks_before_step_starts(fixture_repo):
    cfg = _window_cfg(budget=100, enforce=True, fallback=1000)
    adapter = FakeAdapter(text="done", usage=Usage(input_tokens=10, output_tokens=0))
    orch = _build(fixture_repo, STEP, config=cfg, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED

    rec = orch.manifest.record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_USAGE_WINDOW
    assert rec.halt_reason is None  # disjoint reason invariant (FR-7.2)
    # Parked BEFORE the step started: no adapter call, no usage recorded.
    assert adapter.calls == []
    assert rec.usage.input_tokens == 0
    # A projected replenishment time is stamped (surfaced as the quota reset).
    assert rec.quota_reset_at is not None
    assert "usage-window admission" in (rec.notes or "")


def test_enforce_sufficient_headroom_launches(fixture_repo):
    # Budget comfortably exceeds the fallback estimate → sufficient → launch.
    cfg = _window_cfg(budget=100000, enforce=True, fallback=100)
    adapter = FakeAdapter(text="done", usage=Usage(input_tokens=10, output_tokens=0))
    orch = _build(fixture_repo, STEP, config=cfg, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_DONE
    assert orch.manifest.record("implement").status == M.DONE


def test_enforce_no_history_no_fallback_admits(fixture_repo):
    """Unknown estimate (empty ledger, no fallback) never blocks (§4.2)."""
    cfg = _window_cfg(budget=1, enforce=True, fallback=None)
    adapter = FakeAdapter(text="done", usage=Usage(input_tokens=10, output_tokens=0))
    orch = _build(fixture_repo, STEP, config=cfg, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_DONE
    assert orch.manifest.record("implement").status == M.DONE


# --- status surfacing of a usage_window park (FR-10.3 / FR-7) ----------------


def test_usage_window_park_classifies_and_offers_resume(fixture_repo):
    cfg = _window_cfg(budget=100, enforce=True, fallback=1000)
    adapter = FakeAdapter(text="done", usage=Usage(input_tokens=10, output_tokens=0))
    orch = _build(fixture_repo, STEP, config=cfg, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED

    rstate = operator.compute_run_state(orch.manifest, operator.LIVENESS_NONE)
    assert rstate.state == operator.STATE_PARKED_USAGE_WINDOW
    assert rstate.parked is not None
    assert rstate.parked.reason == M.PARKED_REASON_USAGE_WINDOW
    actions = operator.next_actions(orch.manifest, operator.LIVENESS_NONE)
    assert any("resume" in a.command for a in actions)

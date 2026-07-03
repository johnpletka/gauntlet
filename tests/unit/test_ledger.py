"""P10 — usage-window ledger + admission estimation (FR-10.1/10.2/10.3).

The ledger is a machine-global, content-free, append-only JSONL aggregating
per-step provider usage across runs/repos. These tests pin: idempotent appends
and backfill (de-dup by ``run_id::step_id``), the sliding-window per-provider sum,
the median estimator with a configured fallback, and the pre-step admission
decision (sufficient/insufficient + a deterministic replenishment projection).
Everything is exercised against a temp ledger path — no run drives here — so the
data layer is validated in isolation from the orchestrator wiring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gauntlet.engine import ledger as L
from gauntlet.engine.config import ProviderWindow, RunConfig
from gauntlet.engine.manifest import (
    Manifest,
    PipelineRef,
    StepRecord,
    UsageTotals,
)


def _now() -> datetime:
    return datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _row(
    *,
    run_id: str,
    step_id: str,
    provider: str = "anthropic",
    profile: str = "builder",
    step_type: str = "agent_task",
    ts: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None = None,
) -> L.LedgerRow:
    return L.LedgerRow(
        ts=ts,
        provider=provider,
        model="opus",
        profile=profile,
        step_type=step_type,
        repo="repohash",
        run_id=run_id,
        step_id=step_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


# --- content-free identifiers ------------------------------------------------


def test_repo_root_hash_is_stable_and_opaque(tmp_path: Path) -> None:
    h1 = L.repo_root_hash(tmp_path)
    h2 = L.repo_root_hash(tmp_path)
    assert h1 == h2  # stable for the same checkout
    assert h1 != L.repo_root_hash(tmp_path / "other")
    # Content-free: the project name never appears in the hash.
    assert tmp_path.name not in h1
    assert len(h1) == 64  # sha256 hex


def test_dedup_key_joins_run_and_step() -> None:
    assert L.dedup_key("run-1", "P3.2") == "run-1::P3.2"


def test_default_ledger_path_honors_env(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "scoped" / "ledger.jsonl"
    monkeypatch.setenv(L.LEDGER_PATH_ENV, str(target))
    assert L.default_ledger_path() == target


# --- append idempotency (FR-10.1) --------------------------------------------


def test_append_unique_dedups_within_batch_and_across_calls(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    now = _now()
    rows = [
        _row(run_id="run-a", step_id="s1", ts=_iso(now), input_tokens=100),
        _row(run_id="run-a", step_id="s1", ts=_iso(now), input_tokens=999),  # dup key
        _row(run_id="run-a", step_id="s2", ts=_iso(now), input_tokens=50),
    ]
    added, skipped = L.append_unique(rows, path=path)
    assert (added, skipped) == (2, 1)  # the in-batch duplicate is dropped

    # Re-appending the same rows adds nothing (idempotent across calls).
    added2, skipped2 = L.append_unique(rows, path=path)
    assert (added2, skipped2) == (0, 3)
    assert {r.key for r in L.load_rows(path)} == {"run-a::s1", "run-a::s2"}
    # The FIRST write of a key wins — the 999-token duplicate never landed.
    s1 = next(r for r in L.load_rows(path) if r.step_id == "s1")
    assert s1.input_tokens == 100


def test_load_rows_skips_torn_and_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    good = _row(run_id="run-a", step_id="s1", ts=_iso(_now()), input_tokens=10)
    path.write_text(
        good.model_dump_json() + "\n"
        + "\n"  # blank
        + "{not valid json\n"  # torn
    )
    rows = L.load_rows(path)
    assert len(rows) == 1 and rows[0].step_id == "s1"


# --- sliding-window sum (FR-10.2) --------------------------------------------


def test_window_usage_sums_only_in_window_provider_rows(tmp_path: Path) -> None:
    now = _now()
    rows = [
        _row(run_id="r1", step_id="a", ts=_iso(now - timedelta(hours=1)),
             input_tokens=100, output_tokens=20),
        _row(run_id="r2", step_id="b", ts=_iso(now - timedelta(hours=4)),
             input_tokens=200, output_tokens=0),
        # Outside the 5h window:
        _row(run_id="r3", step_id="c", ts=_iso(now - timedelta(hours=9)),
             input_tokens=1000),
        # Different provider — excluded:
        _row(run_id="r4", step_id="d", provider="openai",
             ts=_iso(now - timedelta(hours=1)), input_tokens=5000),
    ]
    total = L.window_usage(
        rows, provider="anthropic", window_hours=5, unit="tokens", now=now
    )
    assert total == 100 + 20 + 200  # only in-window anthropic rows


def test_window_usage_cost_unit_ignores_tokens_only_rows(tmp_path: Path) -> None:
    now = _now()
    rows = [
        _row(run_id="r1", step_id="a", ts=_iso(now), cost_usd=1.5),
        _row(run_id="r2", step_id="b", ts=_iso(now), cost_usd=None,
             input_tokens=999),  # tokens-only → contributes 0 to a cost window
    ]
    total = L.window_usage(
        rows, provider="anthropic", window_hours=5, unit="cost", now=now
    )
    assert total == 1.5


# --- estimator (FR-10.2) -----------------------------------------------------


def test_estimate_step_is_median_of_same_type_same_profile(tmp_path: Path) -> None:
    now = _now()
    rows = [
        _row(run_id="r1", step_id="a", ts=_iso(now), input_tokens=100),
        _row(run_id="r2", step_id="b", ts=_iso(now), input_tokens=300),
        _row(run_id="r3", step_id="c", ts=_iso(now), input_tokens=200),
        # Different profile — excluded from the builder estimate:
        _row(run_id="r4", step_id="d", profile="reviewer", ts=_iso(now),
             input_tokens=9999),
    ]
    est = L.estimate_step(
        rows, step_type="agent_task", profile="builder", unit="tokens",
        fallback=None,
    )
    assert est == 200  # median(100, 200, 300)


def test_estimate_step_uses_fallback_when_no_history() -> None:
    est = L.estimate_step(
        [], step_type="agent_task", profile="builder", unit="tokens",
        fallback=1234.0,
    )
    assert est == 1234.0
    # No fallback → unknown (None), which the admission treats as "do not block".
    assert L.estimate_step(
        [], step_type="agent_task", profile="builder", unit="tokens",
        fallback=None,
    ) is None


# --- admission decision (FR-10.2/10.3) ---------------------------------------


def test_admit_step_sufficient_when_estimate_fits_headroom() -> None:
    now = _now()
    rows = [
        _row(run_id="r1", step_id="a", ts=_iso(now - timedelta(hours=1)),
             input_tokens=100),
        _row(run_id="r2", step_id="b", ts=_iso(now - timedelta(hours=1)),
             input_tokens=100),
    ]
    window = ProviderWindow(window_hours=5, window_budget=1000)
    decision = L.admit_step(
        rows, window, provider="anthropic", step_type="agent_task",
        profile="builder", now=now,
    )
    assert decision.sufficient is True
    assert decision.spent == 200
    assert decision.headroom == 800
    assert decision.estimate == 100  # median of the two historical builder steps
    assert decision.replenish_at is None


def test_admit_step_insufficient_projects_replenishment() -> None:
    now = _now()
    oldest = now - timedelta(hours=4)
    rows = [
        _row(run_id="r1", step_id="a", ts=_iso(oldest), input_tokens=900),
        _row(run_id="r2", step_id="b", ts=_iso(now - timedelta(hours=1)),
             input_tokens=900),
    ]
    window = ProviderWindow(window_hours=5, window_budget=1000)
    decision = L.admit_step(
        rows, window, provider="anthropic", step_type="agent_task",
        profile="builder", now=now,
    )
    assert decision.sufficient is False  # est 900 > headroom (1000 - 1800 < 0)
    # Replenishment = oldest in-window row ts + window_hours (deterministic).
    assert decision.replenish_at == _iso(oldest + timedelta(hours=5))
    assert "replenishes" in decision.summary()


def test_admit_step_unknown_estimate_admits() -> None:
    """No history and no fallback ⇒ estimate unknown ⇒ never block (§4.2)."""
    now = _now()
    window = ProviderWindow(window_hours=5, window_budget=10)
    decision = L.admit_step(
        [], window, provider="anthropic", step_type="agent_task",
        profile="builder", now=now,
    )
    assert decision.estimate is None
    assert decision.sufficient is True


# --- provider window config validation (FR-10.2) -----------------------------


def test_provider_window_rejects_bad_unit() -> None:
    with pytest.raises(ValueError, match="budget_unit"):
        ProviderWindow(window_hours=5, window_budget=1, budget_unit="megatokens")


def test_provider_window_rejects_nonpositive_hours() -> None:
    with pytest.raises(ValueError, match="window_hours"):
        ProviderWindow(window_hours=0, window_budget=1)


def test_run_config_loads_providers_block() -> None:
    cfg = RunConfig.model_validate(
        {
            "agents": {},
            "providers": {
                "anthropic": {
                    "window_hours": 5,
                    "window_budget": 500000,
                    "enforce": True,
                }
            },
        }
    )
    assert cfg.providers["anthropic"].enforce is True
    assert cfg.providers["anthropic"].budget_unit == "tokens"


# --- backfill from manifests, idempotent (FR-10.1) ---------------------------


def _manifest_with_steps(run_id: str, steps: list[StepRecord]) -> Manifest:
    man = Manifest(
        run_id=run_id,
        slug="demo",
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="h"),
    )
    man.steps = steps
    return man


def _step(
    step_id: str, agent: str | None, in_toks: int, out_toks: int,
    *, step_type: str = "agent_task", started: str, ended: str,
) -> StepRecord:
    return StepRecord(
        id=step_id,
        type=step_type,
        agent=agent,
        started=started,
        ended=ended,
        usage=UsageTotals(input_tokens=in_toks, output_tokens=out_toks),
    )


def test_backfill_from_manifests_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "claude-code", "model": "opus"}}}
    )
    now = _now()
    ts = _iso(now - timedelta(hours=1))
    man1 = _manifest_with_steps(
        "run-1",
        [
            _step("impl", "builder", 100, 40, started=ts, ended=ts),
            # a gate step with no agent/usage contributes no row
            _step("gate", None, 0, 0, step_type="human_gate", started=ts, ended=ts),
        ],
    )
    man2 = _manifest_with_steps(
        "run-2", [_step("impl", "builder", 200, 60, started=ts, ended=ts)]
    )

    res = L.backfill_from_manifests(
        [man1, man2], repo_root=repo, config=cfg, path=path
    )
    assert res.manifests == 2
    assert res.rows_added == 2  # two agent steps with spend; gate skipped
    assert res.rows_skipped == 0

    # Sliding-window sum equals the expected per-provider total.
    rows = L.load_rows(path)
    total = L.window_usage(
        rows, provider="anthropic", window_hours=5, unit="tokens", now=now
    )
    assert total == (100 + 40) + (200 + 60)
    # Provider was resolved from the current config (claude-code → anthropic).
    assert all(r.provider == "anthropic" for r in rows)

    # A SECOND backfill over the same manifests adds zero rows and leaves the
    # window sum byte-for-byte identical (idempotent by run_id::step_id).
    before = path.read_bytes()
    res2 = L.backfill_from_manifests(
        [man1, man2], repo_root=repo, config=cfg, path=path
    )
    assert res2.rows_added == 0
    assert res2.rows_skipped == 2
    assert path.read_bytes() == before
    total2 = L.window_usage(
        L.load_rows(path), provider="anthropic", window_hours=5, unit="tokens",
        now=now,
    )
    assert total2 == total


def test_backfill_unknown_profile_records_null_provider(tmp_path: Path) -> None:
    """A run using a profile absent from the current config still records a row,
    with provider=None so it matches no window (FR-10.1)."""
    path = tmp_path / "ledger.jsonl"
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = RunConfig.model_validate({"agents": {}})  # no profiles at all
    ts = _iso(_now())
    man = _manifest_with_steps(
        "run-1", [_step("impl", "ghost-profile", 100, 0, started=ts, ended=ts)]
    )
    res = L.backfill_from_manifests([man], repo_root=repo, config=cfg, path=path)
    assert res.rows_added == 1
    assert L.load_rows(path)[0].provider is None

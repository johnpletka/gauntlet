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
    # The de-dup key is per-role (`run::step::profile`) so a compound step's roles
    # do not collide; these single-role rows carry the default `builder` profile.
    assert {r.key for r in L.load_rows(path)} == {
        "run-a::s1::builder", "run-a::s2::builder",
    }
    # The FIRST write of a key wins — the 999-token duplicate never landed.
    s1 = next(r for r in L.load_rows(path) if r.step_id == "s1")
    assert s1.input_tokens == 100


def test_append_unique_sees_external_appends(tmp_path: Path) -> None:
    # The incremental key cache scans only the new tail bytes, so rows another
    # process appended between our calls must still dedup (delta scan), while a
    # genuinely fresh row still lands.
    path = tmp_path / "ledger.jsonl"
    now = _now()
    L.append_unique([_row(run_id="run-a", step_id="s1", ts=_iso(now))], path=path)
    external = _row(run_id="run-b", step_id="s9", ts=_iso(now))
    with open(path, "a", encoding="utf-8") as fh:  # simulate another process
        fh.write(external.model_dump_json() + "\n")
    added, skipped = L.append_unique(
        [external, _row(run_id="run-a", step_id="s2", ts=_iso(now))], path=path
    )
    assert (added, skipped) == (1, 1)  # external row deduped; fresh row added
    assert {r.key for r in L.load_rows(path)} == {
        "run-a::s1::builder", "run-b::s9::builder", "run-a::s2::builder",
    }


def test_append_unique_reloads_after_truncation(tmp_path: Path) -> None:
    # size < consumed-offset means the file was rotated/truncated: the cache
    # must fully reload, so a re-appended key is NOT falsely deduped against
    # pre-truncation state.
    path = tmp_path / "ledger.jsonl"
    row = _row(run_id="run-a", step_id="s1", ts=_iso(_now()))
    L.append_unique([row], path=path)
    path.write_text("")  # rotate/truncate
    assert L.append_unique([row], path=path) == (1, 0)
    assert {r.key for r in L.load_rows(path)} == {"run-a::s1::builder"}


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


def test_estimate_step_scales_by_panel_count(tmp_path: Path) -> None:
    # pipeline-effectiveness FR-1.1 / plan-cycle-resp-2a: an ensemble review runs
    # `count` members on one profile, so its projected usage is count× a single
    # member's — a two- and three-member panel must estimate 2× and 3× a single
    # reviewer, asserted straight against the ledger admission estimate (P1-A8).
    now = _now()
    rows = [
        _row(run_id="r1", step_id="a", profile="reviewer",
             step_type="adversarial_cycle", ts=_iso(now), input_tokens=100),
        _row(run_id="r2", step_id="b", profile="reviewer",
             step_type="adversarial_cycle", ts=_iso(now), input_tokens=300),
    ]
    kw = dict(step_type="adversarial_cycle", profile="reviewer",
              unit="tokens", fallback=None)
    base = L.estimate_step(rows, **kw)
    assert base == 200  # median(100, 300)
    assert L.estimate_step(rows, count=2, **kw) == 400
    assert L.estimate_step(rows, count=3, **kw) == 600
    # scaling also applies to the fallback estimate (no history path)
    assert L.estimate_step([], step_type="adversarial_cycle", profile="reviewer",
                           unit="tokens", fallback=50.0, count=2) == 100.0
    # an unknown estimate stays unknown — scaling never manufactures a guess
    assert L.estimate_step([], step_type="adversarial_cycle", profile="reviewer",
                           unit="tokens", fallback=None, count=3) is None


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


def test_provider_window_rejects_negative_fallback_estimate() -> None:
    # A negative fallback would make an over-budget provider with no history read
    # as sufficient (estimate <= headroom trivially holds) — a fail-OPEN on a
    # safety gate. It must fail closed at load (F-003).
    with pytest.raises(ValueError, match="fallback_estimate"):
        ProviderWindow(window_hours=5, window_budget=1, fallback_estimate=-1)


def test_provider_window_accepts_null_or_nonnegative_fallback() -> None:
    # The two valid shapes: absent (None ⇒ unknown ⇒ admit) and non-negative.
    assert ProviderWindow(window_hours=5, window_budget=1).fallback_estimate is None
    assert (
        ProviderWindow(window_hours=5, window_budget=1, fallback_estimate=0)
        .fallback_estimate == 0
    )


def test_negative_fallback_cannot_fail_open_admission() -> None:
    # The safety property behind F-003: even if a negative fallback reached
    # admission, it would admit an already-over-budget provider. Assert the config
    # gate is what prevents that (construction raises before admit_step sees it).
    with pytest.raises(ValueError, match="fallback_estimate"):
        ProviderWindow(window_hours=5, window_budget=100, fallback_estimate=-50)


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


def test_run_manager_backfill_scans_run_root(tmp_path: Path, monkeypatch) -> None:
    """`gauntlet ledger backfill` reconstructs from run_root/*/*/manifest.json,
    idempotently, via RunManager (FR-10.1)."""
    from gauntlet.engine.run import RunManager

    ledger_path = tmp_path / "ledger.jsonl"
    monkeypatch.setenv(L.LEDGER_PATH_ENV, str(ledger_path))
    repo = tmp_path / "repo"
    (repo / "runs" / "demo" / "run-1").mkdir(parents=True)
    (repo / "runs" / "other" / "run-2").mkdir(parents=True)
    cfg = RunConfig.model_validate(
        {
            "run_root": "runs",
            "agents": {"builder": {"adapter": "claude-code", "model": "opus"}},
        }
    )
    now = _now()
    ts = _iso(now - timedelta(hours=1))
    m1 = _manifest_with_steps(
        "run-1", [_step("impl", "builder", 100, 40, started=ts, ended=ts)]
    )
    m2 = _manifest_with_steps(
        "run-2", [_step("impl", "builder", 200, 0, started=ts, ended=ts)]
    )
    m1.write_atomic(repo / "runs" / "demo" / "run-1" / "manifest.json")
    m2.write_atomic(repo / "runs" / "other" / "run-2" / "manifest.json")
    # A torn manifest must not abort the scan.
    (repo / "runs" / "demo" / "run-1" / "..torn").write_text("{bad")

    mgr = RunManager(repo, config=cfg)
    res = mgr.backfill_ledger()
    assert res.manifests == 2
    assert res.rows_added == 2
    total = L.window_usage(
        L.load_rows(ledger_path), provider="anthropic", window_hours=5,
        unit="tokens", now=now,
    )
    assert total == (100 + 40) + 200
    # Idempotent: a second backfill adds nothing.
    assert mgr.backfill_ledger().rows_added == 0


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


# --- per-role rows for compound (adversarial_cycle) steps (FR-10.1) ----------


def _multi_provider_config() -> RunConfig:
    """A config whose cycle roles bill DIFFERENT providers (codex→openai,
    claude-code→anthropic) — the case a single lumped cycle row can't attribute."""
    return RunConfig.model_validate(
        {
            "agents": {
                "reviewer": {"adapter": "codex", "model": "gpt5"},
                "fixer": {"adapter": "claude-code", "model": "opus"},
            }
        }
    )


def _cycle_step(step_id: str, agent_usage: dict[str, UsageTotals], *, ts: str) -> StepRecord:
    """An adversarial_cycle record: agent=None (no single profile), with a
    per-role usage split — exactly what the orchestrator now persists."""
    return StepRecord(
        id=step_id,
        type="adversarial_cycle",
        agent=None,
        started=ts,
        ended=ts,
        usage=UsageTotals(
            input_tokens=sum(u.input_tokens for u in agent_usage.values()),
            output_tokens=sum(u.output_tokens for u in agent_usage.values()),
        ),
        agent_usage=agent_usage,
    )


def test_rows_from_step_splits_cycle_into_per_role_rows() -> None:
    """A compound step yields one row PER ROLE, each attributed to that role's own
    provider — not one lumped (and, before this, dropped) row (FR-10.1)."""
    cfg = _multi_provider_config()
    ts = _iso(_now())
    rec = _cycle_step(
        "impl-cycle.0",
        {
            "reviewer": UsageTotals(input_tokens=100, output_tokens=20),
            "fixer": UsageTotals(input_tokens=300, output_tokens=50),
        },
        ts=ts,
    )
    rows = L.rows_from_step(rec, run_id="run-1", repo_hash="h", config=cfg)
    by_profile = {r.profile: r for r in rows}
    assert set(by_profile) == {"reviewer", "fixer"}
    # Each role attributed to ITS provider — the whole point of the split.
    assert by_profile["reviewer"].provider == "openai"
    assert by_profile["fixer"].provider == "anthropic"
    # Per-role de-dup keys so the two roles of one step never collide.
    assert {r.key for r in rows} == {
        "run-1::impl-cycle.0::reviewer",
        "run-1::impl-cycle.0::fixer",
    }
    # A role contributing no spend is dropped.
    rec2 = _cycle_step(
        "impl-cycle.1",
        {
            "reviewer": UsageTotals(input_tokens=10, output_tokens=1),
            "fixer": UsageTotals(),  # ran nothing this cycle
        },
        ts=ts,
    )
    assert {r.profile for r in L.rows_from_step(rec2, run_id="r", repo_hash="h", config=cfg)} == {
        "reviewer"
    }


def test_rows_from_step_single_agent_unchanged() -> None:
    """A single-agent step (no agent_usage split) still yields exactly one row
    from `agent` + `usage`, keyed `run::step` (no regression)."""
    cfg = RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "claude-code", "model": "opus"}}}
    )
    ts = _iso(_now())
    rec = _step("impl", "builder", 100, 40, started=ts, ended=ts)
    rows = L.rows_from_step(rec, run_id="run-1", repo_hash="h", config=cfg)
    assert len(rows) == 1
    assert rows[0].profile == "builder" and rows[0].provider == "anthropic"
    assert rows[0].key == "run-1::impl::builder"


def test_backfill_attributes_cycle_usage_per_provider(tmp_path: Path) -> None:
    """Regression: a run's adversarial_cycle usage — the bulk of its spend — is
    charged to each role's provider window instead of being dropped (the P10
    ledger-blindness finding). Also idempotent across a second backfill."""
    path = tmp_path / "ledger.jsonl"
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _multi_provider_config()
    now = _now()
    ts = _iso(now - timedelta(hours=1))
    man = _manifest_with_steps(
        "run-1",
        [
            _cycle_step(
                "impl-cycle.0",
                {
                    "reviewer": UsageTotals(input_tokens=1000, output_tokens=100),
                    "fixer": UsageTotals(input_tokens=4000, output_tokens=400),
                },
                ts=ts,
            )
        ],
    )
    res = L.backfill_from_manifests([man], repo_root=repo, config=cfg, path=path)
    assert res.rows_added == 2  # one row per role — cycle usage no longer dropped

    rows = L.load_rows(path)
    anthropic = L.window_usage(
        rows, provider="anthropic", window_hours=5, unit="tokens", now=now
    )
    openai = L.window_usage(
        rows, provider="openai", window_hours=5, unit="tokens", now=now
    )
    assert anthropic == 4000 + 400  # fixer (claude-code)
    assert openai == 1000 + 100  # reviewer (codex)

    # Idempotent: re-backfill adds nothing, sums unchanged.
    before = path.read_bytes()
    res2 = L.backfill_from_manifests([man], repo_root=repo, config=cfg, path=path)
    assert res2.rows_added == 0 and res2.rows_skipped == 2
    assert path.read_bytes() == before

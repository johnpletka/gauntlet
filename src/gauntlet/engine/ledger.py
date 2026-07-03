"""Machine-global usage ledger + window-admission estimation (FR-10).

The scarce resource on a laptop run is the builder's provider window (PRD §1.2),
and parallel runs across repos share one account window — so the data needed to
predict "will the next step fit?" is not in any single run's manifest. This
module keeps a **machine-global, append-only, content-free** ledger at
``~/.gauntlet/usage-ledger.jsonl`` aggregating per-step provider usage across
every run and repo, and answers two questions from it:

* sliding-window headroom — how much of a provider's window is already spent in
  the last ``window_hours`` (FR-10.2/10.3);
* per-step estimate — the median usage of historical same-type/same-profile
  steps (FR-10.2), with a configured fallback when there is no history.

Design (PRD §4.2 "Window management", §7):

* **Content-free.** A row carries counts, model/provider names, and a *hash* of
  the repo root — never a prompt, an output, or a project name — so the ledger
  leaks nothing about what any repo is doing.
* **Idempotent by construction.** Every row carries a stable de-dup key
  (``run_id ‖ step_id``); :func:`append_unique` and :func:`backfill_from_runs`
  both skip a row whose key is already present, so a re-run, a resumed step, or a
  repeated backfill append nothing and leave the window sums unchanged.
* **Advisory, not authoritative (fail toward the run).** The ledger cannot see
  non-gauntlet usage, so it under-counts; a wrong warn is noise and a wrong park
  is an annoyance, but a wrong *continue* is survivable via the reactive
  usage-limit park (FR-3). Callers therefore treat a ledger error as "no data",
  never as a run-halting fault.

The default location is overridable via ``GAUNTLET_LEDGER_PATH`` so tests (and an
operator who wants a scoped ledger) can point it elsewhere without touching the
real machine-global file.
"""

from __future__ import annotations

import hashlib
import json
import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # avoid import cycles at module load; only for type hints
    from gauntlet.engine.config import ProviderWindow, RunConfig
    from gauntlet.engine.manifest import Manifest, StepRecord

# Env override for the machine-global ledger path (§7). Set it to a scoped or
# throwaway path (tests do this so the suite never touches the real ledger).
LEDGER_PATH_ENV = "GAUNTLET_LEDGER_PATH"

# Budget units a provider window can be denominated in (FR-10.2). ``tokens`` sums
# input+output; ``cost`` sums cost_usd. Kept here (not config.py) so the estimator
# and the config validator agree on the one canonical set.
BUDGET_UNIT_TOKENS = "tokens"
BUDGET_UNIT_COST = "cost"
BUDGET_UNITS = frozenset({BUDGET_UNIT_TOKENS, BUDGET_UNIT_COST})

# Static adapter → provider map for the CLI adapters (their provider is fixed).
# The ``api`` adapter's provider is model-dependent, resolved via LiteLLM in
# :func:`profile_provider`; an unknown adapter yields ``None`` (no window match).
_STATIC_ADAPTER_PROVIDER = {"claude-code": "anthropic", "codex": "openai"}


def default_ledger_path() -> Path:
    """The machine-global ledger path, honoring ``GAUNTLET_LEDGER_PATH`` (§7)."""
    override = os.environ.get(LEDGER_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".gauntlet" / "usage-ledger.jsonl"


def repo_root_hash(repo_root: Path) -> str:
    """A content-free, stable identifier for a repo root (§7).

    The ledger is machine-global and shared across repos, so it records WHICH
    repo a row came from — but a project name would leak across contexts, so we
    store a SHA-256 of the resolved absolute path instead. Two rows from the same
    checkout hash equal; nothing about the project is recoverable from the hash.
    """
    return hashlib.sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()


def dedup_key(run_id: str, step_id: str) -> str:
    """The stable per-row de-dup key ``run_id ‖ step_id`` (FR-10.1).

    A ``::`` separator is unambiguous because neither a run id (``run-<ts>``) nor
    a rendered step id (``<id>`` / ``<id>.<iteration>``) contains it.
    """
    return f"{run_id}::{step_id}"


def _render_step_id(rec: "StepRecord") -> str:
    """A step's rendered id (``<id>`` / ``<id>.<iteration>``) — the ledger's
    step_id and half its de-dup key. Inlined (not imported from operator) so the
    ledger stays free of the status-rendering import graph."""
    return rec.id if rec.iteration is None else f"{rec.id}.{rec.iteration}"


class LedgerRow(BaseModel):
    """One content-free per-step usage record (PRD §6 ledger line + step_id).

    ``ts`` is the row's window timestamp (the step's ``ended``, else ``started``);
    the sliding-window query filters on it. ``step_id`` (the plan's mandated
    de-dup datum, beyond the PRD §6 example) makes ``key`` stable so re-appends
    and repeated backfills are idempotent. ``provider``/``model``/``profile`` may
    be ``None`` when unresolvable (e.g. an unknown adapter or a profile absent
    from the current config on backfill) — such a row still records honestly but
    never matches a provider window.
    """

    ts: str
    provider: str | None = None
    model: str | None = None
    profile: str | None = None
    step_type: str
    repo: str
    run_id: str
    step_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None
    started: str | None = None
    ended: str | None = None
    duration_s: float | None = None

    @property
    def key(self) -> str:
        return dedup_key(self.run_id, self.step_id)

    def value(self, unit: str) -> float | None:
        """The row's contribution in ``unit`` (FR-10.2): input+output tokens, or
        cost_usd. ``None`` for a cost query on a tokens-only row (excluded from
        aggregation/median), so a degraded-cost row never reads as free."""
        if unit == BUDGET_UNIT_COST:
            return self.cost_usd
        return (self.input_tokens or 0) + (self.output_tokens or 0)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-8601 stamp to an aware UTC datetime, or ``None`` (fail-safe).

    A naive stamp is assumed UTC; an unparseable stamp yields ``None`` so a torn
    or legacy row is simply excluded from a window rather than crashing the query.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _duration_s(started: str | None, ended: str | None) -> float | None:
    a, b = _parse_ts(started), _parse_ts(ended)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def row_from_step(
    rec: "StepRecord",
    *,
    run_id: str,
    repo_hash: str,
    provider: str | None,
    model: str | None,
) -> LedgerRow | None:
    """Build a ledger row from a manifest step, or ``None`` if it recorded no spend.

    The SAME conversion feeds both the live per-step append and the backfill from
    existing manifests, so a backfilled ledger is byte-shaped exactly like a
    live-written one for the same step. A step with no agent, or with zero tokens
    and no cost, contributes nothing (a human_gate, a skipped step) and is
    skipped — it would only add noise the estimator has to ignore anyway.
    """
    u = rec.usage
    has_spend = bool(u.input_tokens or u.output_tokens or u.cost_usd)
    if rec.agent is None or not has_spend:
        return None
    ts = rec.ended or rec.started
    if ts is None:
        return None
    return LedgerRow(
        ts=ts,
        provider=provider,
        model=model,
        profile=rec.agent,
        step_type=rec.type,
        repo=repo_hash,
        run_id=run_id,
        step_id=_render_step_id(rec),
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cached_input_tokens=u.cached_input_tokens or 0,
        cost_usd=u.cost_usd,
        started=rec.started,
        ended=rec.ended,
        duration_s=_duration_s(rec.started, rec.ended),
    )


def load_rows(path: Path | None = None) -> list[LedgerRow]:
    """Load every ledger row, tolerating blank/torn lines (fail-safe read).

    An unparseable line (a torn final append from a concurrent writer, or a
    schema-invalid legacy row) is skipped rather than aborting the read — the
    ledger is advisory, so a few unreadable rows degrade the estimate slightly,
    never break admission.
    """
    path = path or default_ledger_path()
    if not path.exists():
        return []
    rows: list[LedgerRow] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(LedgerRow.model_validate_json(line))
        except (ValueError, TypeError):
            continue
    return rows


def existing_keys(path: Path | None = None) -> set[str]:
    """The de-dup keys already present in the ledger (for idempotent appends)."""
    return {r.key for r in load_rows(path)}


def append_unique(
    rows: list[LedgerRow], *, path: Path | None = None
) -> tuple[int, int]:
    """Append rows whose de-dup key is not already present (FR-10.1 idempotency).

    Returns ``(added, skipped)``. Skips a row already in the ledger AND a
    duplicate within ``rows`` itself, so a caller that hands the same row twice
    (or backfill over overlapping manifests) can never double-count. The append
    is a single ``write`` under append mode, which POSIX makes atomic for a small
    payload, so concurrent runs' appends interleave line-cleanly.
    """
    if not rows:
        return (0, 0)
    path = path or default_ledger_path()
    seen = existing_keys(path)
    fresh: list[LedgerRow] = []
    for row in rows:
        if row.key in seen:
            continue
        seen.add(row.key)
        fresh.append(row)
    if not fresh:
        return (0, len(rows))
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(row.model_dump_json() + "\n" for row in fresh)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(payload)
    return (len(fresh), len(rows) - len(fresh))


# --- provider resolution -----------------------------------------------------


def profile_provider(config: "RunConfig", profile_name: str | None) -> str | None:
    """The provider name a profile bills against, or ``None`` (FR-10.2).

    ``claude-code`` → ``anthropic`` and ``codex`` → ``openai`` are fixed; the
    ``api`` adapter's provider is model-dependent, resolved via LiteLLM's offline
    ``get_llm_provider`` lookup. An unknown adapter, an absent profile, or an
    unresolvable model yields ``None`` — the step then matches no configured
    window (fail-safe: an unclassifiable step is never wrongly admission-checked).
    """
    if not profile_name:
        return None
    try:
        profile = config.profile(profile_name)
    except KeyError:
        return None
    adapter = profile.adapter
    if adapter in _STATIC_ADAPTER_PROVIDER:
        return _STATIC_ADAPTER_PROVIDER[adapter]
    if adapter == "api" and profile.model:
        try:
            import litellm

            result = litellm.get_llm_provider(profile.model)
        except Exception:
            return None
        # get_llm_provider returns (model, provider, ...); guard the shape.
        if isinstance(result, (tuple, list)) and len(result) >= 2:
            return result[1] or None
        return None
    return None


def profile_model(config: "RunConfig", profile_name: str | None) -> str | None:
    """The model id a profile is pinned to (for the content-free ledger row)."""
    if not profile_name:
        return None
    try:
        return config.profile(profile_name).model
    except KeyError:
        return None


# --- window headroom + per-step estimate -------------------------------------


def window_usage(
    rows: list[LedgerRow],
    *,
    provider: str,
    window_hours: float,
    unit: str,
    now: datetime,
) -> float:
    """Sum a provider's spend within the trailing ``window_hours`` (FR-10.2/10.3).

    Only rows whose ``provider`` matches AND whose ``ts`` falls in
    ``(now - window_hours, now]`` count. A row with an unparseable/absent ``ts``
    is excluded (it cannot be placed in the window). For a cost window, tokens-only
    rows (``cost_usd is None``) contribute nothing.
    """
    cutoff = now - timedelta(hours=window_hours)
    total = 0.0
    for row in rows:
        if row.provider != provider:
            continue
        ts = _parse_ts(row.ts)
        if ts is None or ts <= cutoff or ts > now:
            continue
        value = row.value(unit)
        if value is not None:
            total += value
    return total


def estimate_step(
    rows: list[LedgerRow],
    *,
    step_type: str,
    profile: str | None,
    unit: str,
    fallback: float | None,
) -> float | None:
    """Median historical usage of same-type/same-profile steps (FR-10.2).

    History is the WHOLE ledger (across runs/repos), not just the current window —
    the estimate answers "how big is a step like this?", independent of when it
    ran. Rows contributing ``None`` for the unit (a cost query on a tokens-only
    row) are excluded. With no usable history, returns ``fallback`` (which may be
    ``None`` → the caller treats the estimate as unknown and does not block).
    """
    values: list[float] = []
    for row in rows:
        if row.step_type != step_type or row.profile != profile:
            continue
        value = row.value(unit)
        if value is not None:
            values.append(value)
    if not values:
        return fallback
    return statistics.median(values)


def replenishment_at(
    rows: list[LedgerRow],
    *,
    provider: str,
    window_hours: float,
    now: datetime,
) -> str:
    """Projected UTC time headroom next frees up (FR-10.3), as an ISO stamp.

    When the window is full, headroom returns as its OLDEST in-window row ages
    past ``window_hours`` — so the projection is that row's ``ts`` + the window.
    With no in-window rows (the block came from the estimate alone), it is
    ``now + window_hours``. Deterministic given ``now``, so the parked step's
    recorded projection is stable in tests.
    """
    cutoff = now - timedelta(hours=window_hours)
    in_window = [
        ts
        for row in rows
        if row.provider == provider and (ts := _parse_ts(row.ts)) is not None
        and cutoff < ts <= now
    ]
    anchor = min(in_window) if in_window else now
    return (anchor + timedelta(hours=window_hours)).isoformat()


class AdmissionDecision(BaseModel):
    """The outcome of a pre-step window check (FR-10.2/10.3).

    ``sufficient`` True ⇒ launch normally. False ⇒ insufficient headroom: the
    caller warns (advisory) or parks ``usage_window`` (enforce). ``estimate`` /
    ``headroom`` / ``spent`` / ``budget`` are the numbers behind the decision (in
    ``unit``), and ``replenish_at`` is the projected clean-boundary retry time —
    all surfaced so the manifest note and status explain the decision without a
    transcript. ``estimate is None`` means no history and no fallback: unknown, so
    the step is admitted (``sufficient=True``) rather than blocked on a guess.
    """

    provider: str
    unit: str
    sufficient: bool
    spent: float
    budget: float
    headroom: float
    estimate: float | None
    replenish_at: str | None = None

    def summary(self) -> str:
        """One-line human/manifest explanation of the decision (FR-10.3)."""
        est = "unknown" if self.estimate is None else f"{self.estimate:g}"
        return (
            f"provider {self.provider!r} window: spent {self.spent:g}/"
            f"{self.budget:g} {self.unit} (headroom {self.headroom:g}), next "
            f"step est {est}"
            + ("" if self.sufficient else f"; replenishes ~{self.replenish_at}")
        )


def admit_step(
    rows: list[LedgerRow],
    window: "ProviderWindow",
    *,
    provider: str,
    step_type: str,
    profile: str | None,
    now: datetime,
) -> AdmissionDecision:
    """Decide whether the next step fits the provider window (FR-10.2/10.3).

    Pure over ``(rows, window, provider, step_type, profile, now)`` so admission
    is unit-testable with a synthetic ledger + stubbed clock. Insufficient iff a
    KNOWN estimate exceeds the remaining headroom; an unknown estimate (no history,
    no fallback) admits — a guess must never block a run (fail toward the run,
    PRD §4.2). The ``enforce`` flag is NOT read here (it governs warn-vs-park at
    the call site); this returns the raw sufficiency and the numbers behind it.
    """
    unit = window.budget_unit
    spent = window_usage(
        rows, provider=provider, window_hours=window.window_hours,
        unit=unit, now=now,
    )
    headroom = window.window_budget - spent
    estimate = estimate_step(
        rows, step_type=step_type, profile=profile, unit=unit,
        fallback=window.fallback_estimate,
    )
    sufficient = estimate is None or estimate <= headroom
    replenish = None if sufficient else replenishment_at(
        rows, provider=provider, window_hours=window.window_hours, now=now,
    )
    return AdmissionDecision(
        provider=provider,
        unit=unit,
        sufficient=sufficient,
        spent=spent,
        budget=window.window_budget,
        headroom=headroom,
        estimate=estimate,
        replenish_at=replenish,
    )


# --- backfill from existing run manifests ------------------------------------


def rows_from_manifest(
    man: "Manifest",
    *,
    repo_hash: str,
    config: "RunConfig",
) -> list[LedgerRow]:
    """Every ledger row a run manifest's per-step usage yields (FR-10.1 backfill).

    Provider/model are resolved from the CURRENT config's profile map — a profile
    absent from the current config (an old run using a since-removed profile)
    resolves to ``provider=None``, still recorded honestly but matching no window.
    """
    out: list[LedgerRow] = []
    for rec in man.steps:
        provider = profile_provider(config, rec.agent)
        model = profile_model(config, rec.agent)
        row = row_from_step(
            rec, run_id=man.run_id, repo_hash=repo_hash,
            provider=provider, model=model,
        )
        if row is not None:
            out.append(row)
    return out


class BackfillResult(BaseModel):
    """What a ``gauntlet ledger backfill`` did (FR-10.1): rows added vs. skipped
    as duplicates, and how many manifests it scanned."""

    manifests: int = 0
    rows_added: int = 0
    rows_skipped: int = 0


def backfill_from_manifests(
    manifests: list["Manifest"],
    *,
    repo_root: Path,
    config: "RunConfig",
    path: Path | None = None,
) -> BackfillResult:
    """Reconstruct the ledger from existing run manifests, idempotently (FR-10.1).

    Converts each manifest's per-step usage into the same content-free rows a live
    run appends, then :func:`append_unique` skips any row whose ``run_id::step_id``
    key is already present — so a second backfill over the same manifests adds
    zero rows and leaves the sliding-window sums byte-for-byte unchanged.
    """
    repo_hash = repo_root_hash(repo_root)
    all_rows: list[LedgerRow] = []
    for man in manifests:
        all_rows.extend(rows_from_manifest(man, repo_hash=repo_hash, config=config))
    added, skipped = append_unique(all_rows, path=path)
    return BackfillResult(
        manifests=len(manifests), rows_added=added, rows_skipped=skipped
    )

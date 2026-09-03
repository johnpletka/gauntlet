"""Run configuration: agent profiles, identities, policy (`.gauntlet/config.yaml`).

FR-2.1: every step's ``agent:`` references a named profile here, binding an
adapter + model + flags. FR-2.2: swapping builder/reviewer is a YAML edit, no
code change. The engine builds the actual adapter instance from a profile via
the entry-point registry (FR-2.4); the banned-flag lint (PRD §8) runs as a side
effect of constructing the CLI adapters and is invoked explicitly for ``api``.
"""

from __future__ import annotations

import inspect
import re
import warnings
from pathlib import Path
from typing import Any

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from gauntlet.adapters import get_adapter_class
from gauntlet.config import lint_flags
from gauntlet.engine.gitops import Identity
from gauntlet.engine.heartbeat import (
    DEFAULT_AGENT_SILENCE_S,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    DEFAULT_SUSPEND_CREDIT_CAP_S,
)
from gauntlet.logging.redact import RedactionSettings

# Auto-resume modes (FR-3.4). ``notify`` (default) parks + notifies with the
# reset time and never self-resumes; ``auto`` arms an in-process wait that
# performs the FR-3.3 continuation resume when the quota window replenishes.
# The same two modes govern ``resume_on_provider_unavailable`` (#134): a
# dependency park's backoff / Retry-After deadline is waited out the same way.
RESUME_ON_QUOTA_NOTIFY = "notify"
RESUME_ON_QUOTA_AUTO = "auto"
_RESUME_ON_QUOTA_MODES = frozenset({RESUME_ON_QUOTA_NOTIFY, RESUME_ON_QUOTA_AUTO})
# Aliases naming the shared mode set for the second knob.
AUTO_RESUME_NOTIFY = RESUME_ON_QUOTA_NOTIFY
AUTO_RESUME_AUTO = RESUME_ON_QUOTA_AUTO
_AUTO_RESUME_MODES = _RESUME_ON_QUOTA_MODES


def _validate_auto_resume_mode(field: str, v: str) -> str:
    """Shared validator for the two auto-resume knobs: ``notify``/``auto`` only,
    anything else fails closed (FR-3.4 / #134)."""
    name = (v or "").strip().lower()
    if name not in _AUTO_RESUME_MODES:
        raise ValueError(
            f"{field} must be one of {sorted(_AUTO_RESUME_MODES)}; got {v!r}"
        )
    return name

# Intra-phase checkpoint-commit disposition (harness-efficiency FR-11.1).
# ``keep`` (default) leaves ``PN wip:`` milestone commits in history; ``squash``
# collapses them into the phase-end ``PN:`` commit.
CHECKPOINT_COMMITS_KEEP = "keep"
CHECKPOINT_COMMITS_SQUASH = "squash"
_CHECKPOINT_COMMIT_MODES = frozenset(
    {CHECKPOINT_COMMITS_KEEP, CHECKPOINT_COMMITS_SQUASH}
)

# Interrupted-step disposition on resume of a dirty transaction boundary
# (F-003 / #72). ``park`` (default) parks for a human; ``reset_to_base`` backs
# up the partial work and rewinds to the latest committed checkpoint.
INTERRUPTED_STEP_PARK = "park"
INTERRUPTED_STEP_RESET = "reset_to_base"
_INTERRUPTED_STEP_MODES = frozenset(
    {INTERRUPTED_STEP_PARK, INTERRUPTED_STEP_RESET}
)

DEFAULT_CONFIG_PATH = Path(".gauntlet/config.yaml")


class ConfigNotFoundError(FileNotFoundError):
    """No run config where one is required — operational, not a bug (issue #21).

    Subclasses ``FileNotFoundError`` so every existing ``except
    FileNotFoundError`` caller keeps working; the CLI error boundary catches
    the subclass to print a one-line remedy instead of a traceback.
    """


def _format_validation_errors(errors: list[dict[str, Any]]) -> str:
    """Compact field-named rendering of pydantic errors for one-line CLI output
    (issue #21). An empty ``loc`` — a model-level/root error — renders as
    ``<config>`` rather than a bare leading colon (PR-54 review)."""
    lines = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<config>'}: {err['msg']}"
        for err in errors[:5]
    )
    more = "" if len(errors) <= 5 else f" (+{len(errors) - 5} more)"
    return f"{lines}{more}"


class ConfigLoadError(ValueError):
    """The run config exists but cannot be parsed/validated — operational.

    Subclasses ``ValueError`` for the same compatibility reason as
    :class:`ConfigNotFoundError`.
    """

# --- effort tiering (harness-efficiency FR-6.1) ------------------------------
# The NORMATIVE canonical effort enum. A profile/step `effort:` is drawn from
# this set; anything else is a config-load error (never a silent drop). The
# per-adapter *surface* below maps each canonical value onto the flag the
# adapter actually accepts — a static, pinned fact (the flags are live-verified
# in `.gauntlet/pins.yaml`), independent of the not-yet-ratified default effort
# *values* (Q2). P7 ships the plumbing; the shipped defaults are a later,
# measurement-gated config change.
CANONICAL_EFFORTS = ("minimal", "low", "medium", "high")

# adapter name -> {kwarg, accepted, remap}. ``kwarg`` is the adapter constructor
# parameter the mapped value lands on; ``accepted`` are the canonical values the
# adapter passes straight through; ``remap`` names a canonical value the adapter
# cannot express directly and the substitute it maps to (with a load warning).
# claude `--effort` accepts {low, medium, high} (pins.yaml), so canonical
# ``minimal`` maps to ``low``; codex `-c model_reasoning_effort=` and the api
# `reasoning_effort` param accept the full canonical set.
_EFFORT_SURFACE: dict[str, dict[str, Any]] = {
    "claude-code": {
        "kwarg": "effort",
        "accepted": frozenset({"low", "medium", "high"}),
        "remap": {"minimal": "low"},
    },
    "codex": {
        "kwarg": "reasoning_effort",
        "accepted": frozenset({"minimal", "low", "medium", "high"}),
        "remap": {},
    },
    "api": {
        "kwarg": "reasoning_effort",
        "accepted": frozenset({"minimal", "low", "medium", "high"}),
        "remap": {},
    },
}


def map_effort(adapter: str, canonical: str) -> tuple[str, str, str | None]:
    """Map a canonical effort onto an adapter's ``(kwarg, value, warning)`` (FR-6.1).

    Fail closed: raises :class:`ValueError` — a config-/pipeline-load error — when
    ``canonical`` is not a canonical value, the adapter has no known effort
    surface, or the adapter cannot express the value. The value is never silently
    dropped. Returns a non-``None`` ``warning`` when the canonical value had to be
    remapped to an adapter substitute (claude ``minimal`` → ``low``); callers
    surface it at load time.
    """
    if canonical not in CANONICAL_EFFORTS:
        raise ValueError(
            f"effort {canonical!r} is not a canonical effort value; use one of "
            f"{list(CANONICAL_EFFORTS)} (FR-6.1)"
        )
    surface = _EFFORT_SURFACE.get(adapter)
    if surface is None:
        raise ValueError(
            f"adapter {adapter!r} has no effort surface; `effort:` is supported "
            f"only for adapters {sorted(_EFFORT_SURFACE)} (FR-6.1)"
        )
    kwarg = surface["kwarg"]
    if canonical in surface["accepted"]:
        return kwarg, canonical, None
    remap = surface["remap"].get(canonical)
    if remap is not None:
        return kwarg, remap, (
            f"effort {canonical!r} is not accepted by the {adapter!r} adapter; "
            f"mapping it to {remap!r} (FR-6.1)"
        )
    raise ValueError(
        f"adapter {adapter!r} does not accept effort {canonical!r} "
        f"(accepted: {sorted(surface['accepted'])}); it is a canonical value the "
        "adapter cannot express — a config-load error, not a silent drop (FR-6.1)"
    )

# A POSIX environment-variable identifier. ``issue_tracker.api_key_env`` must
# match this — it names the env var holding the token, never the token (§7).
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_repo_relative(field: str, v: str) -> str:
    """Fail closed (PRD §2) on a repo-root-relative path that could escape.

    ``asset_root`` and ``run_root`` are both joined onto ``repo_root`` and then
    used as containment bases, so an absolute value (which discards repo_root),
    a ``..`` escape, or an empty value would breach the repo boundary. Reject
    those, and normalise spelling (``./.gauntlet`` → ``.gauntlet``, ``.`` stays
    ``.``) so the on-disk path join and the proposals allowlist agree (review
    F-006 / Copilot)."""
    raw = (v or "").strip()
    if not raw or raw.startswith("/") or raw.startswith("~"):
        raise ValueError(
            f"{field} must be a non-empty, repo-relative path; got {v!r} "
            "(absolute paths and ~ are rejected — they would escape the repo)"
        )
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(
            f"{field} must not contain '..' (it would escape the repo "
            f"boundary); got {v!r}"
        )
    return "/".join(parts) or "."


class AgentProfile(BaseModel):
    """One named agent profile (FR-2.1). Adapter-specific fields are passed
    through to the adapter constructor; engine-level budget guards (FR-3.3) are
    stripped out before construction."""

    # extra fields are allowed so a plugin adapter can declare its own flags
    # without a code change here (FR-2.4); they are filtered to the adapter's
    # constructor signature at build time.
    model_config = ConfigDict(extra="allow")

    adapter: str
    model: str | None = None

    # Canonical reasoning-effort tier (FR-6.1), drawn from CANONICAL_EFFORTS.
    # Mapped to the adapter's accepted flag at build time (claude `--effort`,
    # codex `-c model_reasoning_effort=`, api `reasoning_effort`); an unsupported
    # value fails at config load, never silently drops. Engine-level: stripped
    # from the generic kwargs and re-injected as the adapter's mapped parameter.
    effort: str | None = None

    # --- engine-level guards (FR-3.3); not passed to the adapter ---
    max_turns: int | None = None
    budget_usd: float | None = None
    step_timeout_s: float | None = None
    # Agent-liveness watchdog bound (FR-5.3, #103): how long the adapter wait
    # may sit on a child that is PROVABLY gone (leader reaped, process group
    # empty) with its output stream still open and silent, before the step
    # self-marks INTERRUPTED. Distinct from `step_timeout_s` (a wall-clock
    # budget on a live child, subject to FR-5.2 suspend credit). None → the
    # engine default (heartbeat.DEFAULT_AGENT_SILENT_TIMEOUT_S, 15 min); 0
    # disables. A live child is never affected, however silent.
    agent_silent_timeout_s: float | None = None

    @field_validator("effort")
    @classmethod
    def _validate_effort_enum(cls, v: str | None) -> str | None:
        """`effort` must be a canonical value (FR-6.1); anything else fails at load."""
        if v is None:
            return None
        name = str(v).strip().lower()
        if name not in CANONICAL_EFFORTS:
            raise ValueError(
                f"effort must be one of {list(CANONICAL_EFFORTS)} (canonical enum, "
                f"FR-6.1); got {v!r}"
            )
        return name

    @model_validator(mode="after")
    def _validate_effort_surface(self) -> AgentProfile:
        """Reject an effort the profile's adapter cannot express, at load (FR-6.1).

        The canonical enum is checked by the field validator; this cross-field
        check confirms the adapter accepts the value and warns on a remap (claude
        ``minimal`` → ``low``) so the substitution is visible, not silent."""
        if self.effort is not None:
            _kwarg, _value, warning = map_effort(self.adapter, self.effort)
            if warning:
                warnings.warn(warning, stacklevel=2)
        return self

    def adapter_class(self) -> type:
        return get_adapter_class(self.adapter)

    def _adapter_kwargs(self, effort_override: str | None = None) -> dict[str, Any]:
        guard_fields = {
            "max_turns", "budget_usd", "step_timeout_s",
            "agent_silent_timeout_s", "effort",
        }
        data = self.model_dump(exclude_none=True)
        data.pop("adapter", None)
        for f in guard_fields:
            data.pop(f, None)
        # FR-6.1: map the canonical effort onto the adapter's accepted parameter
        # (an override — e.g. a step-level `effort:` — wins over the profile's).
        effort = effort_override if effort_override is not None else self.effort
        if effort is not None:
            kwarg, value, _warning = map_effort(self.adapter, effort)
            data[kwarg] = value
        return data

    def build_adapter(self, *, effort: str | None = None) -> Any:
        """Construct the adapter, filtering kwargs to its constructor signature.

        Unknown profile keys (e.g. an adapter-specific flag the engine has never
        heard of) are dropped rather than crashing, keeping FR-2.4 plugins
        first-class. Banned flags are still rejected: the CLI adapters lint in
        ``__init__``; ``base_flags`` is linted here regardless of adapter.
        """
        cls = self.adapter_class()
        kwargs = self._adapter_kwargs(effort)
        base_flags = kwargs.get("base_flags")
        if isinstance(base_flags, list):
            lint_flags(base_flags)
        sig = inspect.signature(cls.__init__)
        accepted = {
            name
            for name in sig.parameters
            if name not in ("self",)
        }
        # If the constructor takes **kwargs we keep everything; otherwise filter.
        takes_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        )
        if takes_var_kw:
            return cls(**kwargs)
        return cls(**{k: v for k, v in kwargs.items() if k in accepted})

    def capabilities(self) -> Any:
        """Declared adapter capabilities (FR-2.3 load-time validation)."""
        return self.adapter_class().capabilities


class ProviderWindow(BaseModel):
    """One provider's usage-window budget (harness-efficiency FR-10.2).

    Declares the sliding window (``window_hours``) and the budget within it
    (``window_budget``, denominated in ``budget_unit`` — ``tokens`` sums
    input+output, ``cost`` sums cost_usd). Before an agent step billing this
    provider launches, the engine estimates the step's usage (median of historical
    same-type/same-profile steps from the ledger, else ``fallback_estimate``) and
    compares it to remaining headroom. With ``enforce: false`` (default) an
    insufficient-headroom check is a WARNING (advisory: the ledger cannot see
    non-gauntlet usage, so it under-counts); with ``enforce: true`` the run parks
    ``usage_window`` before the step starts. All defaults are opt-in — an absent
    ``providers:`` block leaves the run behaving exactly as before (FR-10.3).
    """

    model_config = ConfigDict(extra="allow")

    window_hours: float
    window_budget: float
    # Unit of ``window_budget`` and the per-step estimate; must agree with the
    # ledger's canonical set (imported so config and estimator never drift).
    budget_unit: str = "tokens"
    # Advisory by default: warn, don't park (FR-10.3). ``true`` parks pre-step.
    enforce: bool = False
    # Per-step estimate used when the ledger has no same-type/same-profile
    # history (FR-10.2 "configured fallback"). ``None`` ⇒ unknown ⇒ admit (a
    # missing estimate never blocks a run — fail toward the run, PRD §4.2).
    fallback_estimate: float | None = None

    @field_validator("budget_unit")
    @classmethod
    def _validate_budget_unit(cls, v: str) -> str:
        """``budget_unit`` must be a canonical ledger unit; else fail closed."""
        from gauntlet.engine.ledger import BUDGET_UNITS

        name = (v or "").strip().lower()
        if name not in BUDGET_UNITS:
            raise ValueError(
                f"providers.*.budget_unit must be one of {sorted(BUDGET_UNITS)}; "
                f"got {v!r} (FR-10.2)"
            )
        return name

    @field_validator("window_hours")
    @classmethod
    def _validate_window_hours(cls, v: float) -> float:
        """A window must span a positive number of hours (FR-10.2)."""
        if v <= 0:
            raise ValueError(
                f"providers.*.window_hours must be positive; got {v!r}"
            )
        return v

    @field_validator("window_budget")
    @classmethod
    def _validate_window_budget(cls, v: float) -> float:
        """A window budget must be non-negative (FR-10.2)."""
        if v < 0:
            raise ValueError(
                f"providers.*.window_budget must be non-negative; got {v!r}"
            )
        return v

    @field_validator("fallback_estimate")
    @classmethod
    def _validate_fallback_estimate(cls, v: float | None) -> float | None:
        """A fallback estimate must be null or non-negative (FR-10.2).

        Admission treats a KNOWN estimate as sufficient whenever
        ``estimate <= headroom`` (:func:`ledger.admit_step`). A NEGATIVE fallback
        would therefore make an over-budget provider with no history read as
        sufficient — a fail-OPEN on a safety gate. Reject it at load so a bad
        config fails closed instead (fail-closed posture, §2)."""
        if v is not None and v < 0:
            raise ValueError(
                f"providers.*.fallback_estimate must be null or non-negative; "
                f"got {v!r} (FR-10.2)"
            )
        return v


class IssueTrackerConfig(BaseModel):
    """The optional `issue_tracker:` config block (§6, FR-6.1/FR-6.2/FR-6.4).

    Selects the pluggable issue-tracker provider for the lightweight
    `gauntlet review` flow. Absent (or ``provider: none``), tracker resolution is
    disabled and `--issue` is rejected while `--intent`/`-m`/`$EDITOR` still work
    (FR-6.5). ``api_key_env`` is the **name** of the env var holding the token —
    never the token itself (FR-1.4 base spec, §7).
    """

    # ``hide_input_in_errors``: a rejected ``api_key_env`` may be a pasted token,
    # so never let pydantic echo the raw input back in the ValidationError text
    # (FR-1.4 base spec, §7; review F-001).
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    provider: str = "linear"
    api_key_env: str = "LINEAR_API_KEY"
    timeout_s: float = 10.0
    workspace: str | None = None

    @field_validator("api_key_env")
    @classmethod
    def _validate_api_key_env(cls, v: str) -> str:
        """``api_key_env`` is the NAME of the env var holding the token, never
        the token itself (FR-1.4 base spec, §7).

        Without this guard a pasted token would be persisted in repo config and
        later echoed back verbatim by ``gauntlet doctor``'s tracker check — a
        secret leak. Enforce a POSIX env-var identifier and reject the known
        Linear token shape (``lin_api_…``) so an actual token fails closed at
        load. The error never echoes the raw value (it may be a secret): it
        reports only the value's length/shape."""
        raw = (v or "").strip()
        if not raw:
            raise ValueError(
                "issue_tracker.api_key_env must be a non-empty env-var NAME "
                "(e.g. LINEAR_API_KEY), not the token itself"
            )
        if raw.lower().startswith("lin_api_") or not _ENV_NAME_RE.match(raw):
            raise ValueError(
                "issue_tracker.api_key_env must be a valid environment-variable "
                "NAME matching [A-Za-z_][A-Za-z0-9_]* (e.g. LINEAR_API_KEY); it "
                "holds the name of the env var, never the token itself "
                f"(got a {len(raw)}-character value that is not a valid name)"
            )
        return raw

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        """v1 accepts only a registered provider (FR-6.2). ``none`` disables the
        block; any other unregistered value fails closed at load, naming the
        supported set."""
        name = (v or "").strip().lower()
        if name == "none":
            return name
        from gauntlet.trackers import available_trackers

        known = sorted(available_trackers())
        if name not in known:
            raise ValueError(
                f"unsupported issue_tracker provider {v!r}; supported: "
                f"{known} (or 'none' to disable). github / jira are reserved but "
                "not implemented in v1."
            )
        return name

    @field_validator("timeout_s")
    @classmethod
    def _validate_timeout(cls, v: float) -> float:
        """Per-call tracker timeout (FR-6.4): default 10, minimum 1. A
        non-positive value — and anything below the stated 1s floor — fails
        closed at load."""
        if v < 1:
            raise ValueError(
                f"issue_tracker.timeout_s must be at least 1 second; got {v!r}"
            )
        return v

    @property
    def enabled(self) -> bool:
        """False when the block disables tracking (``provider: none``)."""
        return self.provider != "none"


class ReviewConfig(BaseModel):
    """The optional `review:` config block (§6, FR-8.1/FR-8.3).

    Controls where a lightweight `gauntlet review` run keeps its state
    (manifest, `intent.md`, transcripts, summary). ``state_dir`` defaults to
    ``null`` — the out-of-repo XDG location (``${XDG_STATE_HOME:-~/.local/state}
    /gauntlet/reviews/<repo-id>/<slug>/``) so a review leaves zero Git-status
    footprint. Set it (e.g. ``.gauntlet/runs``) to opt into an in-repo,
    gitignored layout for teams that want the review audit trail committable; an
    in-repo value that is not gitignored fails closed at resolution (FR-8.3).
    """

    model_config = ConfigDict(extra="allow")

    state_dir: str | None = None


class WorktreeConfig(BaseModel):
    """The optional `worktree:` block (P7c, spike §13).

    ``mode`` chooses which tree a run's agents edit and the engine commits in:

    * ``same_tree`` — the pre-P7 layout: the run drives the operator's own
      checkout. Every run started before P7c is this mode forever (spike
      §10/§16), and it stays the documented fallback for any adopter layout
      that cannot host a worktree. **Not removed by the flip** — selecting it
      explicitly is supported, and is the one way an adopter opts back out.
    * ``dedicated`` (**the default since P7g**) — the run gets its own linked
      worktree at the derived path
      ``<main-worktree>/.gauntlet/worktrees/<slug>/<run-id>`` (§6.2 as corrected
      by P7e; the ratified §6.2 location under the git dir is unusable because
      the `claude` CLI refuses to write any path carrying a ``.git`` segment —
      see `proposals/P7d-gate-blocker.md`). There is deliberately **no path
      knob**: a configurable root would need a ``resolve()``-based containment
      validator, and spike E9-C proves the string-based check this module
      already has is defeated by a symlink (§6.4, deferral D2).

    **P7g flipped the default**, which is the whole of that phase's production
    change. §13 made the flip a separate stage gated on a dogfood run; P7d ran
    it, found spike §6.2's root unwritable by the `claude` CLI, and halted. P7e
    relocated the root, P7f added the per-adapter writability preflight, and
    this line is what P7g changes. From here P7's acceptance criteria A1/A2/A3
    hold for runs **in general** rather than only for runs a human opted in.

    Setting a mode never moves an EXISTING run: `RunManager` reads this in
    exactly one place (`start`), records it on the manifest, and resolves every
    later verb from that record plus observed evidence. Flipping a default that
    silently relocated live runs would be the auto-migration spike §10 forbids.
    """

    model_config = ConfigDict(extra="allow")

    mode: str = "dedicated"

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        from gauntlet.engine.worktree import MODES

        value = (v or "").strip()
        if value not in MODES:
            raise ValueError(
                f"worktree.mode must be one of {sorted(MODES)}; got {v!r}"
            )
        return value


class CollectorConfig(BaseModel):
    """Per-collector enumeration overrides (FR-3.2, PR #59 review F4).

    ``command`` replaces the collector's derived/default enumeration command
    verbatim (string → shlex-split; list taken as argv). Operator-owned config —
    the acceptance gate never interpolates agent-authored text into it.
    """

    model_config = ConfigDict(extra="allow")

    command: str | list[str] | None = None


class RunConfig(BaseModel):
    """Top-level `.gauntlet/config.yaml` (FR-2.1, FR-9.1/9.7, F-003 policy)."""

    # ``hide_input_in_errors``: config load is the entry point that validates the
    # nested ``issue_tracker.api_key_env``; pydantic renders the whole
    # ValidationError with the top-level model's config, so this flag must live
    # here too or a token pasted into ``api_key_env`` would be echoed back in the
    # load error (FR-1.4 base spec, §7; review F-001). The bespoke field
    # validators already include any non-secret value in their own message.
    model_config = ConfigDict(extra="allow", hide_input_in_errors=True)

    # Branch a run is created from. A literal branch name (default "main"), or
    # the sentinel "current" meaning "branch from whatever branch is checked out
    # now" — so a run stacks on the integration branch you are on without a
    # per-run flag (the resolved name is recorded in the manifest).
    base_branch: str = "main"
    branch_prefix: str = "gauntlet/"
    # Single run-root for every artifact of a run (BOOTSTRAP-NOTES #2): plan,
    # transcripts, and manifests live under run_root/<slug>/. FR-4.1's
    # ".gauntlet/runs" is this same setting; the bootstrap pins it to "runs".
    run_root: str = "runs"
    # Repo-relative root under which the engine resolves tool ASSETS —
    # pipelines/, prompts/, schemas/, policy.yaml. Default "." = the repo root
    # (Gauntlet's own source layout, and backward-compatible: "." collapses in a
    # path join, so resolution is unchanged). `gauntlet init` scaffolds adopter
    # repos with `asset_root: .gauntlet` so every gauntlet-owned file lives under
    # one .gauntlet/ dir. Run output is `run_root` (a separate knob).
    asset_root: str = "."
    # Which tree a run's agents edit (P7c, spike §13). Defaults to `dedicated`
    # since P7g; `same_tree` stays selectable as the legacy/fallback mode. See
    # WorktreeConfig — the worktree PATH is derived and has no knob (§6.4).
    worktree: WorktreeConfig = Field(default_factory=WorktreeConfig)
    test_command: str = "uv run pytest"
    # Per-collector enumeration command overrides (FR-3.2, PR #59 review F4):
    # `collectors: {pytest: {command: "hatch run pytest"}}`. Absent, the pytest
    # collector derives its command from `test_command` so enumeration runs in
    # the project's own test environment (collectors.resolve_command).
    collectors: dict[str, CollectorConfig] = Field(default_factory=dict)
    agents: dict[str, AgentProfile] = Field(default_factory=dict)
    identities: dict[str, Identity] = Field(default_factory=dict)

    # --- usage-window admission (harness-efficiency FR-10.2/10.3) ------------
    # Optional per-provider window budgets keyed by provider name (e.g.
    # ``anthropic``). Empty by default → no admission check, so a run behaves
    # exactly as before. A step billing a window-constrained provider is estimated
    # against remaining headroom (from the machine-global ledger) before it
    # launches; short headroom warns (advisory) or, with ``enforce: true``, parks
    # the run ``usage_window`` at a clean boundary before any work is in flight.
    providers: dict[str, ProviderWindow] = Field(default_factory=dict)

    # Transaction-boundary policy on resume of a dirty interrupted step (F-003).
    interrupted_step: str = "park"  # park | reset_to_base

    # Reviewer-mutation policy (FR-9.6): commit | revert | halt.
    reviewer_mutation: str = "commit"

    # --- intra-phase checkpoint commits (harness-efficiency FR-11.1) ---------
    # How the phase-end `PN:` commit treats the builder's `PN wip:` milestone
    # commits: `keep` (default — the milestones are audit data, left in history;
    # if they already committed all of the phase's work, the `PN:` commit is an
    # empty marker so the reviewer handoff always lands on a `PN:` commit) or
    # `squash` (collapse the `wip:` commits into one non-empty `PN:` commit). The
    # reviewed range diff `base..<PN:>` is identical either way.
    checkpoint_commits: str = "keep"

    # --- suspend/sleep resilience (harness-efficiency FR-5) ------------------
    # The driver's heartbeat cadence (FR-5.1). Detection threshold is a fixed 2×
    # this in engine/heartbeat.py, so lengthening the cadence loosens detection.
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S
    # Max detected host-suspension credited back to a step deadline (FR-5.2);
    # past it the step halts with halt_reason=timeout. Default 12h.
    suspend_credit_cap_s: float = DEFAULT_SUSPEND_CREDIT_CAP_S
    # `agent_silent` threshold (FR-5.3): a healthy driver whose adapter child has
    # produced no output for longer than this is classified hung, not asleep.
    agent_silence_s: float = DEFAULT_AGENT_SILENCE_S
    # Keep the host awake for the driver's lifetime via `caffeinate -i` on darwin
    # (FR-5.4). Default TRUE since #134: host sleep was measured as a leading
    # source of park latency (58 min on one run, plus a driver wedge on another),
    # and the assertion is scoped to the driver's pid so nothing lingers. Set
    # `keep_awake: false` to opt out. Off darwin the knob is a no-op; a warning
    # is only raised when it was set explicitly.
    keep_awake: bool = True

    # --- usage-limit auto-resume (harness-efficiency FR-3.4) -----------------
    # `notify` (default): park on a usage limit + notify with the reset time; the
    # operator resumes. `auto`: the live driver waits until the projected reset
    # and performs the FR-3.3 continuation resume itself. `auto` needs the driver
    # to survive the wait — `keep_awake: true` or an external scheduler
    # (`external_scheduler: true` declares the operator re-invokes `gauntlet
    # resume` via cron/launchd); enabling `auto` with neither is a load warning.
    resume_on_quota: str = RESUME_ON_QUOTA_NOTIFY
    # The same policy for a `provider_unavailable` park (#134, rec. 1a): the
    # bounded in-process dependency retries (below) exhausted and the step
    # parked with a concrete backoff / Retry-After deadline. `notify` (default)
    # leaves it for the operator; `auto` has the live driver wait out that
    # deadline and perform the plain retry resume itself — the same wait loop,
    # survival requirement (keep_awake / external_scheduler) and shared attempt
    # ceiling as `resume_on_quota`. No provider health probe is attempted: the
    # only signal is the recorded deadline (fail-closed).
    resume_on_provider_unavailable: str = AUTO_RESUME_NOTIFY
    external_scheduler: bool = False
    # Spaced auto-resume attempts before falling back to a plain usage_limit /
    # provider_unavailable park with an exhaustion note (FR-3.4) — a persistent
    # limit or outage is not a hot loop. Shared by both `auto` knobs.
    max_auto_resume_attempts: int = 3

    # --- dependency retry policy (recovery-redesign plan §5.2, P5) -----------
    # Bounded in-process retries for typed transport/dependency failures
    # (timeout / connection / DNS / 5xx / overload) before the step parks
    # ``provider_unavailable``. The consumed budget is PERSISTED on
    # ``StepRecord.dependency_attempts`` write-ahead, so a crash between
    # retries never resets it. Delays are exponential (base * 2^attempt) with
    # deterministic jitter, honoring a structured Retry-After; a delay past
    # ``dependency_retry_max_delay_s`` parks immediately with that deadline
    # recorded instead of hot-waiting in process.
    dependency_retry_attempts: int = 3
    dependency_retry_base_s: float = 2.0
    dependency_retry_max_delay_s: float = 30.0

    # Live run observability (live-run-observability PRD, FR-6.1): stream each
    # CLI agent's NDJSON stdout to events.jsonl incrementally as it arrives,
    # instead of one buffered write at step end. Default OFF for all of v1 — the
    # buffered path is the proven fail-closed fallback; flipping the default is a
    # separate post-v1 decision after a soak (PRD OQ-5), never an acceptance
    # criterion of any phase here. An omitted field deserializes to False and the
    # flag round-trips through the same load/dump path as the gates above.
    stream_step_output: bool = False

    # Cycle convergence policy (FR-10.5; ratified 2026-06-12, BOOTSTRAP-NOTES
    # #30). What forces another review round:
    #   "blocking" (default) — only open BLOCKING findings loop (to max_rounds,
    #     then escalate). Major gets one fix attempt then is surfaced at the
    #     human gate; minor never loops. Rounds 2+ are regression-scoped.
    #   "strict" — any accepted-but-unresolved finding loops (the P4 original);
    #     higher fidelity, but oscillates on majors/minors.
    cycle_convergence: str = "blocking"

    # --- phase-size lint (FR-3.4) -------------------------------------------
    # The distinct-FR-reference budget a single plan phase may carry before the
    # `phase_lint` size lint fires (default 3). Oversized phases are where partial
    # delivery hides (#54 cause 4). The lint's disposition (warn vs park) is the
    # `phase_lint` step's own `size_lint:` option; this bound is a project-wide
    # config value because plan authoring is also handed it (FR-5.3, P7).
    max_frs_per_phase: int = 3

    # --- concurrent triage (harness-efficiency FR-9.1) ----------------------
    # Independent per-finding triage calls run on a bounded worker pool. Findings
    # are independent by design (point-by-point triage with untrusted-data
    # wrapping, PRD-gauntlet §8), so concurrency changes only latency and cost —
    # never an artifact byte: an all-success round's `triage.json` is byte-
    # identical to the sequential result (verdicts assembled in finding order).
    # The triager runs on the cheap unconstrained provider, so this pool does not
    # contend for the builder's constrained window. Default 4.
    triage_concurrency: int = 4

    # Configurable redaction list (FR-4.4), default-on; the transcript logger
    # builds its Redactor from this.
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)

    # Optional issue-tracker provider for the `gauntlet review` flow (§6,
    # FR-6.1). Absent => tracker resolution disabled; `--issue` is rejected while
    # `--intent`/`-m`/`$EDITOR` still work (FR-6.5).
    issue_tracker: IssueTrackerConfig | None = None

    # Optional `review:` block controlling the `gauntlet review` state location
    # (§6, FR-8.3). Absent => the out-of-repo XDG default (zero repo footprint).
    review: ReviewConfig = Field(default_factory=ReviewConfig)

    # NOTE: the optional console `web:` block (FR-9.4) is intentionally NOT a
    # field here — console settings stay above the orchestrator (plan ground
    # rules / review F-004). It rides on `extra="allow"` and is parsed/validated
    # by the console at serve time (`gauntlet.web.config.web_config_from`).

    @field_validator("asset_root")
    @classmethod
    def _validate_asset_root(cls, v: str) -> str:
        """asset_root is joined into every pipeline/prompt/schema/policy path;
        reject anything that could escape the repo boundary (PRD §2, F-006)."""
        return _validate_repo_relative("asset_root", v)

    @field_validator("run_root")
    @classmethod
    def _validate_run_root(cls, v: str) -> str:
        """run_root is joined onto repo_root and used as the containment base for
        every run/artifact path the console reads; an absolute or ``..``-escaping
        value would let ``gauntlet serve`` browse files outside the repo (review
        F-001). Same repo-relative containment as asset_root."""
        return _validate_repo_relative("run_root", v)

    @field_validator("resume_on_quota")
    @classmethod
    def _validate_resume_on_quota(cls, v: str) -> str:
        """Only ``notify``/``auto`` are valid; anything else fails closed (FR-3.4)."""
        return _validate_auto_resume_mode("resume_on_quota", v)

    @field_validator("resume_on_provider_unavailable")
    @classmethod
    def _validate_resume_on_provider_unavailable(cls, v: str) -> str:
        """Same closed mode set as ``resume_on_quota`` (#134)."""
        return _validate_auto_resume_mode("resume_on_provider_unavailable", v)

    @property
    def any_auto_resume(self) -> bool:
        """True when either auto-resume knob is ``auto`` (#134)."""
        return (
            self.resume_on_quota == AUTO_RESUME_AUTO
            or self.resume_on_provider_unavailable == AUTO_RESUME_AUTO
        )

    @field_validator("interrupted_step")
    @classmethod
    def _validate_interrupted_step(cls, v: str) -> str:
        """Only ``park``/``reset_to_base`` are valid; fail closed (F-003/#72).

        Previously unvalidated: any typo (``reset-to-base``, ``RESET_TO_BASE``)
        silently meant ``park`` at the single ``== "reset_to_base"`` check —
        the operator's configured recovery policy just didn't happen.
        """
        name = (v or "").strip().lower()
        if name not in _INTERRUPTED_STEP_MODES:
            raise ValueError(
                f"interrupted_step must be one of {sorted(_INTERRUPTED_STEP_MODES)}; "
                f"got {v!r}"
            )
        return name

    @field_validator("checkpoint_commits")
    @classmethod
    def _validate_checkpoint_commits(cls, v: str) -> str:
        """Only ``keep``/``squash`` are valid; anything else fails closed (FR-11.1)."""
        name = (v or "").strip().lower()
        if name not in _CHECKPOINT_COMMIT_MODES:
            raise ValueError(
                f"checkpoint_commits must be one of "
                f"{sorted(_CHECKPOINT_COMMIT_MODES)}; got {v!r}"
            )
        return name

    @field_validator("triage_concurrency")
    @classmethod
    def _validate_triage_concurrency(cls, v: int) -> int:
        """A pool of at least 1 worker; fail closed on a non-positive value (FR-9.1)."""
        if v < 1:
            raise ValueError(f"triage_concurrency must be >= 1; got {v!r}")
        return v

    @field_validator("max_frs_per_phase")
    @classmethod
    def _validate_max_frs_per_phase(cls, v: int) -> int:
        """The size-lint bound must admit at least one FR per phase (FR-3.4).

        A non-positive bound would flag every phase (a phase with zero FR refs is
        already degenerate); reject it at load so a mis-set bound fails closed
        rather than turning the advisory lint into a blanket park."""
        if v < 1:
            raise ValueError(f"max_frs_per_phase must be >= 1; got {v!r}")
        return v

    @model_validator(mode="after")
    def _warn_auto_resume_needs_survival(self) -> RunConfig:
        """Warn when ``auto`` cannot keep the driver alive across the wait (FR-3.4).

        ``auto`` performs an in-process wait, so the driver must survive it —
        either via ``keep_awake: true`` (caffeinate) or a declared external
        scheduler that re-invokes ``gauntlet resume``. With neither, ``auto`` will
        still reconcile a due schedule on the next manual resume, but its
        promise (self-resume without operator action) does not hold — surface
        that at load rather than silently."""
        if self.any_auto_resume and not self.keep_awake and not self.external_scheduler:
            knobs = [
                name for name in ("resume_on_quota", "resume_on_provider_unavailable")
                if getattr(self, name) == AUTO_RESUME_AUTO
            ]
            warnings.warn(
                f"{' / '.join(knobs)}: auto without keep_awake or an external "
                "scheduler — the driver may not survive the wait, so "
                "auto-resume falls back to reconciliation on the next manual "
                "`gauntlet resume` (FR-3.4 / #134).",
                stacklevel=2,
            )
        return self

    @model_validator(mode="after")
    def _redact_tracker_token(self) -> RunConfig:
        """Extend the redaction list with the tracker's ``api_key_env`` (§7).

        The env-name heuristic (KEY/TOKEN/SECRET/…) already masks the default
        ``LINEAR_API_KEY``, but a custom env-var name that misses the heuristic
        would otherwise leak. Adding it to ``extra_env_vars`` guarantees the
        resolved tracker token can never be written to any artifact, regardless
        of the chosen env-var name. Additive and idempotent."""
        tracker = self.issue_tracker
        if tracker is not None and tracker.enabled:
            name = tracker.api_key_env
            if name and name not in self.redaction.extra_env_vars:
                self.redaction.extra_env_vars.append(name)
        return self

    def profile(self, name: str) -> AgentProfile:
        try:
            return self.agents[name]
        except KeyError:
            raise KeyError(
                f"no agent profile named {name!r}; known: {sorted(self.agents)}"
            ) from None

    def identity(self, agent_name: str) -> Identity:
        """Commit identity for an agent (FR-9.7); falls back to a generic one."""
        if agent_name in self.identities:
            return self.identities[agent_name]
        return Identity(
            name=f"Gauntlet {agent_name}",
            email=f"{agent_name}@gauntlet.local",
        )

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> RunConfig:
        if not path.exists():
            raise ConfigNotFoundError(
                f"run config not found at {path}; `gauntlet init` scaffolds it (P6)"
            )
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            # PR-54 review: unreadable bytes (a mis-encoded file) or a
            # permission failure are operational config failures too — they
            # must reach the CLI boundary as one line, not escape as
            # UnicodeDecodeError/OSError.
            raise ConfigLoadError(f"{path} is not readable: {exc}") from exc
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise ConfigLoadError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigLoadError(
                f"{path} must be a YAML mapping, got {type(data).__name__}"
            )
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            # Operational, not a bug: name the offending fields so the operator
            # can fix the file, without a pydantic wall (issue #21).
            raise ConfigLoadError(
                f"invalid run config at {path}: "
                f"{_format_validation_errors(exc.errors())}"
            ) from exc

"""Run configuration: agent profiles, identities, policy (`.gauntlet/config.yaml`).

FR-2.1: every step's ``agent:`` references a named profile here, binding an
adapter + model + flags. FR-2.2: swapping builder/reviewer is a YAML edit, no
code change. The engine builds the actual adapter instance from a profile via
the entry-point registry (FR-2.4); the banned-flag lint (PRD §8) runs as a side
effect of constructing the CLI adapters and is invoked explicitly for ``api``.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gauntlet.adapters import get_adapter_class
from gauntlet.config import lint_flags
from gauntlet.engine.gitops import Identity
from gauntlet.logging.redact import RedactionSettings

DEFAULT_CONFIG_PATH = Path(".gauntlet/config.yaml")


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

    # --- engine-level guards (FR-3.3); not passed to the adapter ---
    max_turns: int | None = None
    budget_usd: float | None = None
    step_timeout_s: float | None = None

    def adapter_class(self) -> type:
        return get_adapter_class(self.adapter)

    def _adapter_kwargs(self) -> dict[str, Any]:
        guard_fields = {"max_turns", "budget_usd", "step_timeout_s"}
        data = self.model_dump(exclude_none=True)
        data.pop("adapter", None)
        for f in guard_fields:
            data.pop(f, None)
        return data

    def build_adapter(self) -> Any:
        """Construct the adapter, filtering kwargs to its constructor signature.

        Unknown profile keys (e.g. an adapter-specific flag the engine has never
        heard of) are dropped rather than crashing, keeping FR-2.4 plugins
        first-class. Banned flags are still rejected: the CLI adapters lint in
        ``__init__``; ``base_flags`` is linted here regardless of adapter.
        """
        cls = self.adapter_class()
        kwargs = self._adapter_kwargs()
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


class RunConfig(BaseModel):
    """Top-level `.gauntlet/config.yaml` (FR-2.1, FR-9.1/9.7, F-003 policy)."""

    model_config = ConfigDict(extra="allow")

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
    test_command: str = "uv run pytest"
    agents: dict[str, AgentProfile] = Field(default_factory=dict)
    identities: dict[str, Identity] = Field(default_factory=dict)

    # Transaction-boundary policy on resume of a dirty interrupted step (F-003).
    interrupted_step: str = "park"  # park | reset_to_base

    # Reviewer-mutation policy (FR-9.6): commit | revert | halt.
    reviewer_mutation: str = "commit"

    # Cycle convergence policy (FR-10.5; ratified 2026-06-12, BOOTSTRAP-NOTES
    # #30). What forces another review round:
    #   "blocking" (default) — only open BLOCKING findings loop (to max_rounds,
    #     then escalate). Major gets one fix attempt then is surfaced at the
    #     human gate; minor never loops. Rounds 2+ are regression-scoped.
    #   "strict" — any accepted-but-unresolved finding loops (the P4 original);
    #     higher fidelity, but oscillates on majors/minors.
    cycle_convergence: str = "blocking"

    # Configurable redaction list (FR-4.4), default-on; the transcript logger
    # builds its Redactor from this.
    redaction: RedactionSettings = Field(default_factory=RedactionSettings)

    @field_validator("asset_root")
    @classmethod
    def _validate_asset_root(cls, v: str) -> str:
        """Fail closed (PRD §2): asset_root is joined into every pipeline/prompt/
        schema/policy path, so an absolute value (which discards repo_root), a
        ``..`` escape, or an empty value would breach the repo boundary. Reject
        those, and normalise spelling (``./.gauntlet`` → ``.gauntlet``, ``.``
        stays ``.``) so the on-disk path join and the proposals allowlist agree
        (review F-006 / Copilot)."""
        raw = (v or "").strip()
        if not raw or raw.startswith("/") or raw.startswith("~"):
            raise ValueError(
                f"asset_root must be a non-empty, repo-relative path; got {v!r} "
                "(absolute paths and ~ are rejected — they would escape the repo)"
            )
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        if ".." in parts:
            raise ValueError(
                f"asset_root must not contain '..' (it would escape the repo "
                f"boundary); got {v!r}"
            )
        return "/".join(parts) or "."

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
            raise FileNotFoundError(
                f"run config not found at {path}; `gauntlet init` scaffolds it (P6)"
            )
        data = yaml.safe_load(path.read_text()) or {}
        if not isinstance(data, dict):
            raise ValueError(f"{path} must be a YAML mapping, got {type(data).__name__}")
        return cls.model_validate(data)

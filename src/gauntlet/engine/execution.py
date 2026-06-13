"""Step execution contract: context, result, and the step-type registry.

Step handlers receive a :class:`StepContext` (everything they may touch) and
return a :class:`StepResult` (what the orchestrator records). Control flow —
``on_fail`` routing, retries, parking, budget halts — is the orchestrator's
job, not the handler's; a handler just reports ``done``/``failed``/``parked``.

Step types register in :data:`BUILTIN_STEP_TYPES` and via the
``gauntlet.step_types`` entry point (FR-5.5); each carries the capability
metadata the load-time validator needs (FR-2.3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from gauntlet.adapters.base import AgentResult, Usage
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, StepRecord
from gauntlet.engine.pipeline import Pipeline, Step
from gauntlet.logging.redact import RedactingWriter

STEP_TYPE_ENTRY_POINT_GROUP = "gauntlet.step_types"

# StepResult.status values
DONE = "done"
FAILED = "failed"
PARKED = "parked"
HALTED = "halted"
SKIPPED = "skipped"
INTERRUPTED = "interrupted"  # killed mid-edit; record interrupted, park the run


@dataclass
class StepResult:
    status: str
    session_id: str | None = None
    usage: Usage | None = None
    commit_sha: str | None = None
    commit_phase: str | None = None
    # A multi-commit step (adversarial_cycle: fix rounds + reviewer-attributed
    # mutation commits) reports every (phase-prefix, sha) it created, in order.
    commits: list[tuple[str, str]] = field(default_factory=list)
    notes: str = ""
    # artifacts this step produced (artifact name -> path), merged into context
    artifact_writes: dict[str, Path] = field(default_factory=dict)
    # Per-agent-profile usage breakdown for this step (FR-3.2). Single-agent
    # steps report one entry; an adversarial_cycle splits across its roles.
    # The orchestrator merges this into the manifest's per-profile totals.
    usage_by_agent: dict[str, Usage] = field(default_factory=dict)
    # Structured outcome counts persisted onto the StepRecord for `--trend`
    # (FR-6.6): rounds, finding/verdict/confirm tallies an adversarial_cycle
    # emits so trend metrics come from the manifest (P7 test strategy).
    metrics: dict[str, Any] = field(default_factory=dict)


# An adapter factory lets unit tests inject fakes by agent-profile name without
# touching the entry-point registry (the orchestrator stays offline-testable).
AdapterFactory = Callable[[str], Any]


@dataclass
class StepContext:
    repo_root: Path
    run_dir: Path
    artifact_root: Path  # slug dir: prd.md / plan.md / step outputs live here
    config: RunConfig
    pipeline: Pipeline
    manifest: Manifest
    record: StepRecord
    writer: RedactingWriter
    judge_env: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, Path] = field(default_factory=dict)
    # repo-relative paths of the engine's own bookkeeping (review F-001); commit
    # and dirty checks exclude these but not real run artifacts.
    excludes: list[str] = field(default_factory=list)
    iteration_item: Any | None = None
    iteration_index: int | None = None
    adapter_factory: AdapterFactory | None = None

    def build_adapter(self, agent_name: str) -> Any:
        """Resolve an agent profile to an adapter instance (override in tests)."""
        if self.adapter_factory is not None:
            return self.adapter_factory(agent_name)
        return self.config.profile(agent_name).build_adapter()

    def steps_dir(self) -> Path:
        return self.run_dir / "steps"


Handler = Callable[[Step, StepContext], StepResult]


@dataclass(frozen=True)
class StepSpec:
    """Static metadata + handler for one step type."""

    type: str
    handler: Handler
    # FR-2.3 load-time capability checks:
    requires_repo_write: bool = False  # bound agent must be repo-write capable
    uses_schema: bool = False  # warn if the bound adapter is best-effort JSON
    needs_agent: bool = False  # must declare `agent:` (or message_agent)
    # F-003: record the step's base SHA before running it.
    touches_worktree: bool = False

    def step_requires_repo_write(self, step: Step) -> bool:
        # agent_task may opt out via `repo_write: false` (e.g. a doc reviewer).
        if self.type == "agent_task":
            return bool(step.get("repo_write", True))
        return self.requires_repo_write

    def step_touches_worktree(self, step: Step) -> bool:
        if self.type == "agent_task":
            return bool(step.get("repo_write", True))
        return self.touches_worktree


def builtin_specs() -> dict[str, StepSpec]:
    # Imported lazily to avoid a cycle (steptypes imports this module).
    from gauntlet.engine import steptypes

    return steptypes.SPECS


def step_specs() -> dict[str, StepSpec]:
    """All step specs: built-ins plus ``gauntlet.step_types`` entry points."""
    specs = dict(builtin_specs())
    for ep in entry_points(group=STEP_TYPE_ENTRY_POINT_GROUP):
        spec = ep.load()
        spec_obj = spec() if callable(spec) and not isinstance(spec, StepSpec) else spec
        if isinstance(spec_obj, StepSpec):
            specs[spec_obj.type] = spec_obj
    return specs


def get_spec(step_type: str) -> StepSpec:
    specs = step_specs()
    try:
        return specs[step_type]
    except KeyError:
        raise KeyError(
            f"unknown step type {step_type!r}; registered: {sorted(specs)}"
        ) from None


def usage_from_result(result: AgentResult) -> Usage | None:
    return result.usage


def run_bookkeeping_excludes(repo_root: Path, run_dir: Path, artifact_root: Path) -> list[str]:
    """Repo-relative paths of the engine's own live bookkeeping (review F-001).

    The run-instance dir (manifest/transcripts/steps/judge-audit) must be
    invisible to worktree-state checks and commits. Everything *else* under the
    run root (prd.md, plan.md, declared step outputs) is real work: tracked,
    detected by the transaction boundary, and committable. Narrowing to just
    this set is the F-001 fix — the prior code excluded the whole run root,
    hiding real partial effects from the dirty-base check.

    The active-run pointer is deliberately NOT listed here: it is ignored via a
    ``.gitignore`` rule (``runs/*/active-run.txt``, shipped by ``init``), so git
    already keeps it out of status and ``add``. Naming a gitignored path in an
    ``:(exclude)`` pathspec makes ``git add`` ERROR ("paths are ignored ... use
    -f"), which broke the commit step (BOOTSTRAP-NOTES #33). Letting gitignore
    own it is both correct and avoids the pathspec collision.

    ``PR.md`` (FR-9.8) IS listed: it is engine-drafted at final-gate pass but
    must never be auto-committed or pushed — opening the PR stays a human action
    (PRD §2.2). Excluding it keeps the engine's own commits and clean/rollback
    checks from sweeping it in, while leaving it plainly visible for the human to
    ``git add`` and commit deliberately (it is not gitignored, so no #33 clash).
    """
    excludes: list[str] = []
    root = repo_root.resolve()
    try:
        excludes.append(run_dir.resolve().relative_to(root).as_posix())
    except ValueError:
        pass
    try:
        excludes.append((artifact_root / "PR.md").resolve().relative_to(root).as_posix())
    except ValueError:
        pass
    return excludes

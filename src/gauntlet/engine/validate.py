"""Load-time pipeline validation (FR-5.3, FR-2.3, PRD §8).

Run once when a pipeline is loaded for a run. Three classes of check:

* **Dangling artifact dataflow** (FR-5.3): every ``inputs:``/``artifact:``
  reference must be a seed artifact (``prd.md``/``plan.md``) or produced by an
  earlier step's ``output:``.
* **Adapter capabilities** (FR-2.3): a step that writes the repo cannot bind an
  adapter that can't (e.g. ``api``); a step that needs a schema *warns* if its
  adapter only does best-effort JSON, and *errors* if it does none.
* **Banned flags** (§8): constructing each referenced adapter runs the
  permission-bypass / hook-disabling lint; a violation aborts the load.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gauntlet.config import BannedFlagError
from gauntlet.engine.config import RunConfig
from gauntlet.engine.execution import get_spec
from gauntlet.engine.pipeline import Pipeline, Step

SEED_ARTIFACTS = frozenset({"prd.md", "plan.md"})


class PipelineValidationError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("pipeline failed validation:\n- " + "\n- ".join(errors))
        self.errors = errors


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self) -> bool:
        return not self.errors


def validate_pipeline(
    pipeline: Pipeline, config: RunConfig, *, seeds: frozenset[str] = SEED_ARTIFACTS
) -> ValidationReport:
    report = ValidationReport()
    available: set[str] = set(seeds)
    all_ids = {step.id for step in pipeline.all_steps()}
    for stage in pipeline.stages:
        for step in stage.steps:
            _validate_step(step, config, available, report)
            if step.on_fail and step.on_fail.route_to not in all_ids:
                report.errors.append(
                    f"step {step.id!r} on_fail routes to unknown step "
                    f"{step.on_fail.route_to!r}"
                )
            output = step.get("output")
            if output:
                available.add(output)
    if report.errors:
        raise PipelineValidationError(report.errors)
    return report


def _validate_step(
    step: Step, config: RunConfig, available: set[str], report: ValidationReport
) -> None:
    # 1. step type resolves
    try:
        spec = get_spec(step.type)
    except KeyError as exc:
        report.errors.append(str(exc))
        return

    # 2. dangling artifact dataflow (FR-5.3)
    for name in (step.get("inputs", []) or []):
        if name not in available:
            report.errors.append(
                f"step {step.id!r} input {name!r} is not a seed artifact and is "
                "produced by no earlier step (dangling reference, FR-5.3)"
            )
    artifact = step.get("artifact")
    if artifact and artifact not in available:
        report.errors.append(
            f"step {step.id!r} artifact {artifact!r} is not available at this "
            "point (dangling reference, FR-5.3)"
        )

    # 3. agent profile resolution + capabilities (FR-2.3) + banned flags (§8)
    agent_refs = _agent_refs(step, spec.needs_agent)
    for ref in agent_refs:
        if ref not in config.agents:
            report.errors.append(
                f"step {step.id!r} references undefined agent profile {ref!r}"
            )
            continue
        profile = config.profile(ref)
        caps = profile.capabilities()
        if spec.step_requires_repo_write(step) and not caps.repo_write:
            report.errors.append(
                f"step {step.id!r} needs repo-write but agent {ref!r} "
                f"(adapter {profile.adapter!r}) cannot write the repo (FR-2.3)"
            )
        if _step_uses_schema(step, spec):
            if caps.structured_output == "none":
                report.errors.append(
                    f"step {step.id!r} needs structured output but agent {ref!r} "
                    "supports none (FR-2.3)"
                )
            elif caps.structured_output == "best_effort":
                report.warnings.append(
                    f"step {step.id!r} relies on schema output but agent {ref!r} "
                    "is best-effort JSON only; validate-and-retry applies (FR-2.3)"
                )
        try:
            profile.build_adapter()
        except BannedFlagError as exc:
            report.errors.append(
                f"step {step.id!r} agent {ref!r} uses a banned flag: {exc}"
            )
        except Exception as exc:  # construction issues surface as warnings
            report.warnings.append(
                f"step {step.id!r} agent {ref!r} could not be pre-constructed "
                f"({exc}); deferred to runtime"
            )


def _agent_refs(step: Step, needs_agent: bool) -> list[str]:
    refs: list[str] = []
    if step.agent:
        refs.append(step.agent)
    for key in ("message_agent", "reviewer", "triager", "fixer", "confirmer"):
        ref = step.get(key)
        if ref:
            refs.append(ref)
    return refs


def _step_uses_schema(step: Step, spec) -> bool:
    return bool(spec.uses_schema or step.get("findings_schema") or step.get("schema"))

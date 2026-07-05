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
from pathlib import Path

from gauntlet.config import BannedFlagError
from gauntlet.engine.config import RunConfig, map_effort
from gauntlet.engine.execution import get_spec
from gauntlet.engine.pipeline import (
    INPUT_MODE_INLINE,
    INPUT_MODE_PHASE,
    InputRef,
    Pipeline,
    Step,
    iter_inputs,
)
from gauntlet.engine.steptypes import HUMAN_RESPONSE_ARTIFACT

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
    pipeline: Pipeline,
    config: RunConfig,
    *,
    seeds: frozenset[str] = SEED_ARTIFACTS,
    repo_root: Path | None = None,
    artifact_root: Path | None = None,
) -> ValidationReport:
    """Validate a pipeline at load time.

    ``repo_root``/``artifact_root`` enable the FR-1.3 path checks for
    reference/`phase` inputs (containment + seed existence). When omitted (most
    unit tests), the capability and phase-mode-name checks still run; only the
    disk-path resolution is skipped — a real ``gauntlet run`` passes both so the
    fail-closed guarantee holds before any step executes.
    """
    report = ValidationReport()
    available: set[str] = set(seeds)
    # `produced` tracks only artifacts an EARLIER step writes (outputs + cycle
    # round artifacts) — a strict subset of `available` (which also holds seeds).
    # An FR-1.3 reference to a produced artifact skips the disk-existence check
    # (it will exist when the step runs); a reference to a seed must already be a
    # readable file. So the two sets must stay distinct.
    produced: set[str] = set()
    for stage in pipeline.stages:
        stage_ids = {step.id for step in stage.steps}
        for step in stage.steps:
            _validate_step(
                step, config, available, produced, report,
                repo_root=repo_root, artifact_root=artifact_root,
            )
            # on_fail routing is stage-local: the runtime jumps within the
            # current stage's step list, so a cross-stage target would validate
            # then crash at runtime (review F-005). Reject it at load.
            if step.on_fail and step.on_fail.route_to not in stage_ids:
                report.errors.append(
                    f"step {step.id!r} on_fail routes to {step.on_fail.route_to!r}, "
                    "which is not a step in the same stage (cross-stage routing "
                    "is unsupported, FR-5.4 / review F-005)"
                )
            output = step.get("output")
            if output:
                available.add(output)
                produced.add(output)
            if step.type == "adversarial_cycle":
                # the cycle registers its round outputs as named artifacts
                cyc = {"findings.json", "triage.json", "confirm.json"}
                available.update(cyc)
                produced.update(cyc)
    if report.errors:
        raise PipelineValidationError(report.errors)
    return report


def _validate_step(
    step: Step,
    config: RunConfig,
    available: set[str],
    produced: set[str],
    report: ValidationReport,
    *,
    repo_root: Path | None = None,
    artifact_root: Path | None = None,
) -> None:
    # 1. step type resolves
    try:
        spec = get_spec(step.type)
    except KeyError as exc:
        report.errors.append(str(exc))
        return

    # Normalize inputs once (FR-1.1): a malformed entry / unknown mode fails
    # closed at load rather than surfacing as a bad prompt mid-run.
    try:
        input_refs = iter_inputs(step)
    except ValueError as exc:
        report.errors.append(str(exc))
        input_refs = []

    # 1b. `max_turns` is unenforceable on the pinned CLIs (claude 2.1.172 has no
    # --max-turns; codex exec has no turn cap) — reject it rather than silently
    # ignore a claimed guard (review F-006). timeout_s + budget_usd are the
    # working per-step halts (FR-3.3).
    if step.max_turns is not None:
        report.errors.append(
            f"step {step.id!r} sets max_turns, but no installed adapter can honor "
            "it; use timeout_s / budget_usd (FR-3.3 / review F-006)"
        )

    # 1c. reserve the synthetic human-response artifact name (review F-002).
    # `--response` resumes inject the human-decision history under exactly
    # `human-response.md` into an invocation-local copy of the step's inputs. A
    # pipeline that ALSO declares that name (as an input, output, or artifact)
    # collides: the synthetic file would silently replace the declared one and
    # rendering would emit two identically-named blocks, breaking FR-4's
    # single-artifact / single-block premise. Fail closed at load.
    declared_reserved = [
        f"input {HUMAN_RESPONSE_ARTIFACT!r}"
        for ref in input_refs
        if ref.name == HUMAN_RESPONSE_ARTIFACT
    ] + [
        f"{field} {HUMAN_RESPONSE_ARTIFACT!r}"
        for field in ("output", "artifact")
        if step.get(field) == HUMAN_RESPONSE_ARTIFACT
    ]
    for where in declared_reserved:
        report.errors.append(
            f"step {step.id!r} {where} collides with the engine's reserved "
            "synthetic human-response artifact: that name is generated by a "
            "`--response` resume and must not be declared by a pipeline "
            "(FR-4 / review F-002); choose another name"
        )

    # 1d. `validate:` shape + capability (FR-2.1 / review F-003, F-004). The
    # named validator runs against the step's `output:` artifact and, on failure,
    # re-invokes the SAME agent session with the error (the bounded in-session
    # repair loop). Two ways to fail open, both rejected at load:
    #   * `validate:` without `output:` — the validator has no artifact to run
    #     against, so `handle_agent_task` would silently skip it (F-003).
    #   * `validate:` on an agent whose adapter cannot resume — the repair would
    #     be a fresh call lacking the authoring context, a non-session repair
    #     masquerading as the required in-session correction path (F-004).
    validate_name = step.get("validate")
    if validate_name and not step.get("output"):
        report.errors.append(
            f"step {step.id!r} declares `validate:` but no `output:`; the "
            "validator would be silently skipped (fail closed, FR-2.1 / F-003)"
        )
    if validate_name and step.agent and step.agent in config.agents:
        vprofile = config.profile(step.agent)
        if not vprofile.capabilities().resume:
            report.errors.append(
                f"step {step.id!r} declares `validate:` but agent {step.agent!r} "
                f"(adapter {vprofile.adapter!r}) cannot resume its session "
                "(FR-2.1 / F-004): the in-session repair loop re-invokes the same "
                "session with the validation error and requires resume support"
            )

    # 1e. step-level `effort:` (FR-6.1) is a canonical value that every adapter it
    # will drive must accept — fail closed at load rather than silently drop it or
    # raise mid-run. A step `effort:` overrides the profile's own effort (step wins
    # over profile). The adapters it must be accepted by depend on the step type:
    # an `agent_task` drives its own `agent`; an `adversarial_cycle` step effort
    # applies to every cycle role (reviewer/triager/fixer/confirmer/escalation),
    # so validate it against each role's adapter — otherwise a cycle effort a
    # role's adapter cannot express would be silently ignored (review F-004).
    # Deduped by adapter so shared surfaces warn once.
    step_effort = step.get("effort")
    if step_effort is not None:
        effort_norm = str(step_effort).strip().lower()
        seen_adapters: set[str] = set()
        for target in _effort_target_agents(step):
            if target not in config.agents:
                continue  # undefined profile — already reported in block 3
            adapter = config.profile(target).adapter
            if adapter in seen_adapters:
                continue
            seen_adapters.add(adapter)
            try:
                _kwarg, _value, warning = map_effort(adapter, effort_norm)
            except ValueError as exc:
                report.errors.append(f"step {step.id!r} effort: {exc}")
            else:
                if warning:
                    report.warnings.append(f"step {step.id!r} effort: {warning}")

    # 2. dangling artifact dataflow (FR-5.3)
    for ref in input_refs:
        if ref.name not in available:
            report.errors.append(
                f"step {step.id!r} input {ref.name!r} is not a seed artifact and is "
                "produced by no earlier step (dangling reference, FR-5.3)"
            )

    # 2a. scoped-context input modes (FR-1.3): reference/`phase` inputs are valid
    # only on a repo-reading profile and must resolve to a containment-valid path;
    # `phase` mode is plan.md-only. Fail closed at load so an unreadable-path
    # prompt never reaches an agent mid-run.
    for ref in input_refs:
        if ref.mode != INPUT_MODE_INLINE:
            _validate_scoped_input(
                step, ref, config, produced, report,
                repo_root=repo_root, artifact_root=artifact_root,
            )
    artifact = step.get("artifact")
    if artifact and artifact not in available:
        report.errors.append(
            f"step {step.id!r} artifact {artifact!r} is not available at this "
            "point (dangling reference, FR-5.3)"
        )

    # 2b. adversarial_cycle role bindings (FR-5.2): the three core roles are
    # required, and capability checks are role-aware — the FIXER writes the
    # repo (FR-2.3); the REVIEWER is intended read-only (FR-9.6), so a
    # repo-write check on it would be exactly backwards.
    if step.type == "adversarial_cycle":
        for role in ("reviewer", "triager", "fixer"):
            if not step.get(role):
                report.errors.append(
                    f"step {step.id!r} (adversarial_cycle) is missing required "
                    f"role {role!r} (FR-5.2)"
                )
        fixer = step.get("fixer")
        if fixer and fixer in config.agents:
            if not config.profile(fixer).capabilities().repo_write:
                report.errors.append(
                    f"step {step.id!r} fixer {fixer!r} cannot write the repo "
                    "(FR-2.3): fix rounds edit files"
                )
        if step.get("commit_each_fix_round") is False:
            report.errors.append(
                f"step {step.id!r} sets commit_each_fix_round=false, which "
                "breaks the clean-handoff invariant (FR-9.3/9.4); unsupported"
            )

    # 2c. retrospective role bindings (FR-6.2): at least one self-critiquing
    # agent is required; the proposer is optional (no proposer → self-critique
    # only, generation skipped and recorded).
    if step.type == "retrospective":
        if not (step.get("agents") or []):
            report.errors.append(
                f"step {step.id!r} (retrospective) needs a non-empty `agents:` "
                "list to self-critique (FR-6.2)"
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
        if profile.max_turns is not None:
            report.errors.append(
                f"agent profile {ref!r} sets max_turns, unenforceable on the "
                "pinned CLIs; use step_timeout_s / budget_usd (review F-006)"
            )
        caps = profile.capabilities()
        # Repo-write applies to the step's PRIMARY agent only — auxiliary emitters
        # (message_agent, disposition_agent) never touch the worktree, so a cheap
        # non-writing profile (api) is valid there (FR-6.3). The cycle's writing
        # role (fixer) is checked in block 2b.
        if ref == step.agent and spec.step_requires_repo_write(step) and not caps.repo_write:
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


def _validate_scoped_input(
    step: Step,
    ref: InputRef,
    config: RunConfig,
    produced: set[str],
    report: ValidationReport,
    *,
    repo_root: Path | None,
    artifact_root: Path | None,
) -> None:
    """Fail-closed checks for a reference/`phase` input (FR-1.3).

    Three enforcement points, all at load:
      * capability — the step's profile must declare ``reads_repo`` true (the api
        adapter cannot read the repo, so a path reference would be unreadable);
      * `phase` mode is plan.md-only (FR-1.1);
      * path — when ``repo_root``/``artifact_root`` are known, the artifact must
        resolve under the repo root, and a *seed* (not produced by an earlier
        step) must already be a readable file.
    """
    # capability: the profile's declared effective reads_repo must be true.
    agent = step.agent
    if agent and agent in config.agents:
        profile = config.profile(agent)
        if not getattr(profile.capabilities(), "reads_repo", False):
            report.errors.append(
                f"step {step.id!r} input {ref.name!r} uses {ref.mode!r} context "
                f"mode, but agent {agent!r} (adapter {profile.adapter!r}) cannot "
                "read the repo (reads_repo=false): reference/phase context injects "
                "a path the agent must open itself (FR-1.3)"
            )
    # `phase` mode is plan.md-only (FR-1.1).
    if ref.mode == INPUT_MODE_PHASE and ref.name != "plan.md":
        report.errors.append(
            f"step {step.id!r} input {ref.name!r} uses `phase` mode, which is "
            "valid only for plan.md (FR-1.1)"
        )
    # path containment + seed existence (only checkable with a resolved root).
    if repo_root is not None and artifact_root is not None:
        err = _scoped_path_error(ref, produced, repo_root, artifact_root)
        if err is not None:
            report.errors.append(f"step {step.id!r} input {ref.name!r}: {err} (FR-1.3)")


def _scoped_path_error(
    ref: InputRef, produced: set[str], repo_root: Path, artifact_root: Path
) -> str | None:
    """The FR-1.3 path failure for a reference/`phase` input, or None if valid.

    Containment first (a path escaping the repo root is caught regardless of
    existence); then, for a seed the step reads by reference, existence-as-a-file
    (a produced artifact is skipped — it will exist when the step runs).
    """
    root = repo_root.resolve()
    candidate = (artifact_root / ref.name).resolve()
    if root != candidate and root not in candidate.parents:
        return (
            f"repo-relative path {ref.name!r} resolves outside the repo root "
            f"({candidate})"
        )
    if ref.name in produced:
        return None  # written by an earlier step; present by the time it runs
    if not candidate.exists():
        return f"repo-relative path {ref.name!r} does not resolve to a file under the repo root"
    if not candidate.is_file():
        return f"repo-relative path {ref.name!r} is not a file"
    return None


def _effort_target_agents(step: Step) -> list[str]:
    """Agent profiles a step-level ``effort:`` must be accepted by (FR-6.1).

    An ``adversarial_cycle`` step effort overrides every bound cycle role's own
    effort (step wins), so it must map onto each role's adapter; any other step's
    effort targets its own ``agent`` (the ``agent_task`` case). Returns only
    the roles the step actually declares (``confirmer`` defaults to ``reviewer``,
    which is already covered)."""
    if step.type == "adversarial_cycle":
        roles = ("reviewer", "triager", "fixer", "confirmer", "escalation_agent")
        return [a for a in (step.get(r) for r in roles) if a]
    return [step.agent] if step.agent else []


def _agent_refs(step: Step, needs_agent: bool) -> list[str]:
    refs: list[str] = []
    if step.agent:
        refs.append(step.agent)
    for key in ("message_agent", "disposition_agent", "reviewer", "triager",
                "fixer", "confirmer", "escalation_agent", "proposer"):
        ref = step.get(key)
        if ref:
            refs.append(ref)
    # The retrospective step fans out over a list of self-critiquing agents
    # (FR-6.2); each must resolve to a profile like any other agent reference.
    for ref in step.get("agents", []) or []:
        refs.append(ref)
    return refs


def _step_uses_schema(step: Step, spec) -> bool:
    return bool(spec.uses_schema or step.get("findings_schema") or step.get("schema"))

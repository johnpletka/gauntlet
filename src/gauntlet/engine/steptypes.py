"""Built-in step types: agent_task, shell, human_gate, commit (FR-5, FR-9.2).

The ``adversarial_cycle`` step type (the review→triage→fix→confirm primitive)
is a P4 deliverable and registers there; P3 ships the four primitives the
crash test and switchover need. Control flow (routing, retries, parking,
budget halts) is the orchestrator's; handlers report status only.

Trust model (plan §0 / review F-001): ``shell`` commands come **only** from
human-committed pipeline/config YAML — :func:`render_shell_command` refuses any
template token that is not a ``{{config.*}}`` reference, so agent-authored text
can never be substituted into a command line.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from gauntlet.adapters.base import (
    AdapterError,
    AgentFailedError,
    SessionNotFoundError,
)
from gauntlet.engine.commit_format import (
    HEADER_MAX,
    header_prefix,
    validate_commit_message,
)
from gauntlet.engine.config import CHECKPOINT_COMMITS_SQUASH
from gauntlet.engine.execution import (
    DONE,
    FAILED,
    HALTED,
    PARKED,
    StepContext,
    StepResult,
    StepSpec,
)
from gauntlet.engine import gitops
from gauntlet.engine.timing import record_invocation
from gauntlet.engine.manifest import (
    HALT_REASON_ADAPTER_ERROR,
    HALT_REASON_JUDGE_DENY,
    HALT_REASON_PRECONDITION,
    HALT_REASON_TIMEOUT,
    PARKED_REASON_ARTIFACT_INVALID,
    PARKED_REASON_GATE,
    PARKED_REASON_PROVIDER_UNAVAILABLE,
    PARKED_REASON_RESPONSE,
    PARKED_REASON_USAGE_LIMIT,
    RESPONSE_CONSUMED,
    RESPONSE_PENDING,
    RevalidationRecord,
)
from gauntlet.engine.pipeline import (
    INPUT_MODE_PHASE,
    INPUT_MODE_REFERENCE,
    InputRef,
    Step,
    iter_inputs,
)
from gauntlet.engine.collectors import (
    CollectorEnumerationError,
    get_collector,
    is_registered,
    resolve_command,
)
from gauntlet.engine.deferrals import (
    SIZE_LINT_MODES,
    SIZE_LINT_PARK,
    SIZE_LINT_WARN,
    Deferral,
    deferrals_from_map,
    distinct_fr_refs,
    open_deferrals_for,
    parse_body_deferrals,
    phantom_deferrals,
)
from gauntlet.engine.planphases import (
    PlanPhasesError,
    acceptance_clause_errors,
    extract_phases,
    load_plan_phases,
    missing_phase_sections,
    phase_section,
)
from gauntlet.engine.validators import validate_artifact
from gauntlet.engine import verify
from gauntlet.logging.transcript import StepLogger

# FR-2.1: how many in-session repair attempts an invalid `output:` artifact gets
# before the step parks `artifact_invalid` (FR-2.2). Two, per the PRD acceptance
# ("succeeds on attempt 2") and §9 metric (≥80% repaired within 2 attempts).
_MAX_ARTIFACT_REPAIRS = 2

_CONFIG_TOKEN_RE = re.compile(r"\{\{\s*config\.([a-zA-Z0-9_]+)\s*\}\}")
_ANY_TOKEN_RE = re.compile(r"\{\{.*?\}\}")

# Canonical FR-10.4 halt marker. Only a `halt_on:` whose value is *exactly* this
# marker sets the conflict-park discriminator (FR-2.1); a step configured with a
# different `halt_on:` marker parks with `parked_reason` unset. Pipelines use
# this string verbatim (pipelines/standard.yaml `halt_on: "UPSTREAM CONFLICT"`).
UPSTREAM_CONFLICT_MARKER = "UPSTREAM CONFLICT"

# FR-3.3 continuation prompt: sent (instead of the full original prompt) when a
# usage-limit park is resumed against a preserved CLI session. Short by design —
# the session already holds the task context; re-sending the full prompt would
# waste the very usage budget the resume exists to conserve.
_CONTINUATION_PROMPT = (
    "You were interrupted by a provider usage limit before finishing this task. "
    "Continue from where you left off and complete it. The worktree is exactly "
    "as you left it — your prior edits are intact. Do not restart from scratch "
    "or redo work you already completed."
)

# FR-4: the single, fixed-name synthetic artifact that carries the full
# chronological human-decision history into a `--response` resume. There is
# exactly one file with this name; each resume regenerates it from the manifest
# (so repeated resumes never accumulate differently-named files / collide).
HUMAN_RESPONSE_ARTIFACT = "human-response.md"

# FR-10: the structured-disposition schema the builder must emit on a `--response`
# resume. It is bound INVOCATION-LOCALLY (only while a step consumes a pending
# response) rather than added to the approved pipeline definition — the
# `implement` step carries no `schema:` field and the approved snapshot must not
# be mutated (FR-4.1). Lives under the configured asset_root, like every schema.
RESUME_DISPOSITION_SCHEMA = "schemas/resume-disposition.json"

# FR-3 / FR-5 / FR-10: how a builder's structured `disposition` drives the step
# outcome on a `--response` resume. The enum maps 1:1 to the FR-3 categories —
# proceed_* completes the step (DONE → commit); amendment_required / new_conflict
# re-park it for a human (parked_reason=response, the FR-10.4 gate). This
# structured signal — not the textual UPSTREAM CONFLICT marker — is authoritative
# once a response is being consumed (the marker is only the FIRST-conflict signal,
# before any response exists).
_DISPOSITION_OUTCOMES: dict[str, tuple[str, str | None]] = {
    "proceed_in_place": (DONE, None),
    "proceed_with_deviation": (DONE, None),
    "amendment_required": (PARKED, PARKED_REASON_RESPONSE),
    "new_conflict": (PARKED, PARKED_REASON_RESPONSE),
}


# --- shell -------------------------------------------------------------------
def render_shell_command(template: str, config) -> str:
    """Substitute only ``{{config.<key>}}`` tokens; reject anything else.

    Refusing non-config tokens is the engine-side enforcement of the trust
    model: no agent-authored artifact may be interpolated into a shell command.
    """
    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        value = getattr(config, key, None)
        if value is None:
            raise ValueError(
                f"shell template references unknown config key {key!r}"
            )
        return str(value)

    rendered = _CONFIG_TOKEN_RE.sub(_sub, template)
    leftover = _ANY_TOKEN_RE.search(rendered)
    if leftover:
        raise ValueError(
            f"shell command may only reference {{{{config.*}}}}; refusing "
            f"to substitute {leftover.group(0)!r} (trust model / review F-001)"
        )
    return rendered


def resolve_step_timeout_s(step: Step, agent_name: str | None, config) -> float | None:
    """Effective step deadline (FR-3.3) — the single precedence rule.

    Per-step ``timeout_s`` wins; else an agent step falls back to its profile's
    ``step_timeout_s``; else ``None`` (unbounded). Shared by the ``agent_task``
    handler (which arms the adapter with it) and the read-only status path (which
    renders ``current_step_timeout_remaining_s`` from it), so the reported deadline
    is the real one — not a profile-only guess that misses a per-step override or a
    shell step's own ``timeout_s`` (F-003). A shell step has no agent, so it never
    picks up the profile fallback: it reports its own ``timeout_s`` or null.
    """
    timeout = step.timeout_s
    if timeout is None and agent_name and agent_name in config.agents:
        timeout = config.profile(agent_name).step_timeout_s
    return timeout


def handle_shell(step: Step, ctx: StepContext) -> StepResult:
    template = step.get("run")
    if not template:
        return StepResult(
            status=FAILED,
            halt_reason=HALT_REASON_PRECONDITION,
            notes="shell step has no `run:` command",
        )
    command = render_shell_command(template, ctx.config)
    timeout = step.timeout_s  # per-step guard (FR-3.3); None => unbounded
    try:
        proc = subprocess.run(
            command,
            shell=True,
            # The tree this step acts on (P7a): a shell step runs the repo's
            # own tests/tooling against the work tree, not the repository.
            cwd=ctx.work_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _write_step_log(ctx, "output.txt", f"$ {command}\n--- TIMEOUT after {timeout}s ---\n")
        # Halt at a checkpoint rather than letting a stuck command burn on.
        return StepResult(
            status=HALTED,
            halt_reason=HALT_REASON_TIMEOUT,
            notes=f"shell timeout halt (FR-3.3): `{command}` exceeded {timeout}s",
        )
    _write_step_log(ctx, "output.txt", _proc_log(command, proc))
    if proc.returncode != 0:
        # The command ran and reported failure (e.g. a failing test suite): a
        # terminal execution failure, not a fail-closed precondition guard (FR-7.2).
        return StepResult(
            status=FAILED,
            halt_reason=HALT_REASON_ADAPTER_ERROR,
            notes=f"`{command}` exited {proc.returncode}",
        )
    return StepResult(status=DONE, notes=f"`{command}` exited 0")


# --- human_gate --------------------------------------------------------------
def handle_human_gate(step: Step, ctx: StepContext) -> StepResult:
    show = step.get("show", []) or []
    return StepResult(
        status=PARKED,
        parked_reason=PARKED_REASON_GATE,  # FR-7.2: a gate park stamps `gate`
        notes=f"awaiting human decision; review: {', '.join(show) or '(nothing listed)'}",
    )


# --- phase_lint --------------------------------------------------------------
def _phase_lint_park(artifact: str, text: str, diagnostic: str) -> StepResult:
    """An ``artifact_invalid`` park for a plan.md structural defect (§5.1, P5).

    One coherent transition carrying artifact + validator + verbatim diagnostic
    + content fingerprint, replacing the pre-P5 HALTED/precondition halt (which
    issue #64 showed re-lints the unchanged artifact in a zero-cost loop and
    renders under the wrong meaning line). The revalidation record's hash is
    what folds into the progress fingerprint, so an unchanged plain resume
    exits nonzero (R5) while an edited plan re-lints and continues.
    """
    return StepResult(
        status=PARKED,
        parked_reason=PARKED_REASON_ARTIFACT_INVALID,
        revalidation=RevalidationRecord(
            artifact=artifact,
            hash_at_park=_sha256(text),
            validator="phase_lint",
            diagnostic=diagnostic,
        ),
        notes=(
            f"phase lint: {diagnostic}\n"
            f"Parked artifact_invalid (plan §5.1): a PRE-approval plan defect. "
            f"Fix {artifact} (a hand-edit is sanctioned and audited via the "
            "revalidation hashes) or reject the plan gate to route the defect "
            "list back into the plan cycle's author/fix loop. A plain "
            "`gauntlet resume` re-runs ONLY this lint against the edited "
            "bytes; an unchanged artifact exits nonzero (no-progress) instead "
            "of re-linting in a loop (issue #64)."
        ),
    )


def handle_phase_lint(step: Step, ctx: StepContext) -> StepResult:
    """Structurally validate plan.md's ``gauntlet-phases`` block at the plan gate.

    The plan-cycle reviewer reads plan.md as *prose* — it never parses the
    fenced ``gauntlet-phases`` block — so a structurally broken block (e.g. an
    unquoted ``schema:`` colon that YAML reads as a nested mapping) sails through
    review and only detonates later, when the phases stage tries to fan out over
    ``plan.phases`` and :func:`load_plan_phases` raises. This deterministic,
    no-agent check closes that gap: it runs the *same* parser the foreach uses,
    so a plan can only pass the gate if the engine can actually execute it.

    Fail closed (CLAUDE.md §2): a missing/empty/malformed block parks the run
    ``artifact_invalid`` (plan §5.1, P5) — one coherent step/run transition
    recording the artifact, the validator, the exact diagnostic, and a content
    fingerprint — rather than letting a known-unrunnable plan reach human
    approval. This is a PRE-approval defect: the note routes it back into the
    artifact's author loop (hand-edit sanctioned; or reject the plan gate so
    the plan cycle re-runs with the defect list injected). A plain resume
    re-runs ONLY this deterministic lint against the edited bytes; an
    UNCHANGED artifact exits nonzero through the R5 no-progress guard instead
    of re-linting in a zero-cost loop (issue #64).
    """
    artifact = step.get("artifact", "plan.md")
    path = ctx.artifact_root / artifact
    if not path.exists():
        return _phase_lint_park(
            artifact, "", f"{artifact} is missing at the plan gate"
        )
    try:
        text = path.read_text()
    except (OSError, ValueError) as exc:
        # P5.1 review F-006: an unreadable/wrong-kind artifact (permissions, a
        # directory where a file belongs, undecodable bytes) is a REPAIRABLE
        # artifact defect, not a terminal handler fault — a bare exception here
        # would land FAILED on a non-respondable step whose only exit is
        # abort, wedging a run a file repair should resume.
        return _phase_lint_park(
            artifact, "", f"{artifact} is unreadable at the plan gate: {exc}"
        )
    try:
        phases = extract_phases(text)
    except PlanPhasesError as exc:
        return _phase_lint_park(
            artifact, text, f"{artifact} gauntlet-phases block is invalid — {exc}"
        )
    if not phases:
        return _phase_lint_park(
            artifact, text,
            f"{artifact} declares no gauntlet-phases block; the phases stage "
            "would have nothing to fan out over (FR-5.1)",
        )
    # FR-1.1: the implement step slices each phase's prose section out of plan.md
    # by its ATX heading (`phase`-mode context). A phase declared in the list but
    # lacking a locatable `## <id> …` heading would silently lose its excerpt at
    # render time — a fail-open on scoped-context quality. Halt at the gate (same
    # fail-closed path as a malformed block) so an unrunnable-for-phase-mode plan
    # never reaches human approval.
    missing = missing_phase_sections(text, phases)
    if missing:
        return _phase_lint_park(
            artifact, text,
            f"{artifact} has no locatable prose section for phase(s) "
            f"{', '.join(missing)}; every phase in the gauntlet-phases list "
            "needs a matching '## <id> …' heading so `phase`-mode context can "
            "slice it (FR-1.1)",
        )
    # FR-3.1: every phase must carry a well-formed `acceptance:` list of testable
    # clauses (the acceptance_gate's input). A clause-less/malformed phase fails
    # closed here — same fail-closed path as a malformed block — so an
    # unmappable-at-gate plan never reaches human approval.
    acc_errors = acceptance_clause_errors(phases)
    if acc_errors:
        return _phase_lint_park(
            artifact, text,
            f"{artifact} has acceptance-clause defects — "
            + "; ".join(acc_errors) + " (FR-3.1)",
        )
    # FR-3.4: the phase-size lint. A phase carrying more than `max_frs_per_phase`
    # (default 3) distinct FR references is oversized — the scope where partial
    # delivery hides (#54 cause 4). Counted from the phase's DECLARED `frs:` list
    # when it carries one (the authoritative scope, #66 — a prose sweep counts
    # incidental cross-references and parent-vs-child FRs as scope); pre-`frs`
    # plans fall back to sweeping the phase's prose section. The disposition is
    # the step's `size_lint:` option — warn (default; surface, do not block) or
    # park (fail closed at the plan gate).
    size_mode = step.get("size_lint", SIZE_LINT_WARN)
    if size_mode not in SIZE_LINT_MODES:
        return StepResult(
            status=HALTED,
            halt_reason=HALT_REASON_PRECONDITION,
            notes=(
                f"phase lint: unknown size_lint mode {size_mode!r}; must be one of "
                f"{sorted(SIZE_LINT_MODES)} (FR-3.4)"
            ),
        )
    bound = ctx.config.max_frs_per_phase
    oversized: list[str] = []
    for phase in phases:
        declared = phase.get("frs")
        if declared is not None:  # shape-validated by extract_phases; never empty
            refs = set(declared)
        else:
            section = phase_section(text, phase["id"]) or ""
            refs = distinct_fr_refs(section)
        if len(refs) > bound:
            oversized.append(
                f"{phase['id']} carries {len(refs)} distinct FR refs "
                f"({', '.join(sorted(refs))})"
            )
    ids = ", ".join(p["id"] for p in phases)
    if oversized:
        detail = (
            f"{len(oversized)} phase(s) exceed max_frs_per_phase={bound}: "
            + "; ".join(oversized)
            + " — oversized phases hide partial delivery (FR-3.4)"
        )
        if size_mode == SIZE_LINT_PARK:
            return _phase_lint_park(artifact, text, detail)
        # warn mode: not a blocker — surface the finding in the notes so it lands
        # in RUN.md / status without stopping the plan gate.
        return StepResult(
            status=DONE,
            notes=(
                f"phase lint: {len(phases)} phase(s) valid ({ids}); "
                f"WARNING — {detail}"
            ),
        )
    return StepResult(
        status=DONE, notes=f"phase lint: {len(phases)} phase(s) valid ({ids})"
    )


# --- acceptance_gate ---------------------------------------------------------
# The acceptance-map artifact the implement step produces (§6). Read directly
# from disk (not declared as a dataflow `artifact:`/`inputs:` reference — the
# builder writes it with its file tools, so it is not an agent `output:` the
# loader tracks), and fail closed if it is absent.
_ACCEPTANCE_MAP_DEFAULT = "artifacts/acceptance-map.json"
_ACCEPTANCE_MAP_SCHEMA = "schemas/acceptance-map.json"


def _acceptance_gate_halt(notes: str) -> StepResult:
    """A fail-closed acceptance_gate park: HALTED → RUN_PARKED for a human.

    Mirrors ``phase_lint``'s precondition-halt path (a deterministic gate that
    parks the run when it cannot pass) so an incomplete/uncheckable phase never
    advances to the review cycle.
    """
    return StepResult(status=HALTED, halt_reason=HALT_REASON_PRECONDITION, notes=notes)


def handle_acceptance_gate(step: Step, ctx: StepContext) -> StepResult:
    """Deterministically prove every plan-phase acceptance clause maps to a real,
    collector-enumerated test id (FR-3.2) — the structural close of the #54 class.

    One instance per distinct collector (``collector:``). It proves *citation +
    existence*: every clause is mapped, and every id this collector is cited for
    appears in that collector's side-effect-free enumeration. It does **not**
    prove the cited test meaningfully exercises the clause — sufficiency stays the
    spec-coverage review lens's job (G2 scoped accordingly).

    Fail closed at every step: a missing/unparseable/schema-invalid map, an
    unmapped clause, a cited id absent from the enumeration, or a failed/timed-out
    enumeration all **park** — an absent or failed check is never read as "passed".
    Enumeration is a bounded engine subprocess in a DISPOSABLE COPY with a
    stripped env and a project-resolved command — deterministic, no LLM in the
    evidence path (PR #59 review F3/F4/F7; see collectors.py).
    """
    collector_kind = step.get("collector")
    if not collector_kind:
        return _acceptance_gate_halt(
            "acceptance gate: step declares no `collector:` (FR-3.2)"
        )
    # Defense in depth: pipeline load already rejects an unregistered collector
    # (validate.py), but a hand-built/bypassed pipeline fails closed here too.
    if not is_registered(collector_kind):
        return _acceptance_gate_halt(
            f"acceptance gate: collector {collector_kind!r} has no registered "
            "collector (rejected at load; unsupported collector, FR-3.2)"
        )

    phase = ctx.iteration_item
    if not isinstance(phase, dict) or not phase.get("id"):
        return _acceptance_gate_halt(
            "acceptance gate: no current phase in context; this step runs inside "
            "the `foreach: plan.phases` loop (FR-3.2)"
        )
    phase_id = phase["id"]
    clauses = phase.get("acceptance") or []
    if not clauses:
        # phase_lint already fails closed on a clause-less phase (FR-3.1); reaching
        # the gate with none means the lint was bypassed — fail closed here too.
        return _acceptance_gate_halt(
            f"acceptance gate: phase {phase_id} carries no acceptance clauses "
            "(FR-3.1 lint should have parked upstream)"
        )
    clause_ids = [c["id"] for c in clauses]

    # 1. Load + parse + schema-validate the acceptance map. A schema-invalid map —
    # including one whose evidence declares an unregistered collector kind (the
    # schema's `kind` enum is closed) — is rejected HERE, at map load, before any
    # enumeration runs. It never "parks closed after running an unsupported
    # collector" (FR-3.2 / P2-A5).
    map_name = step.get("map", _ACCEPTANCE_MAP_DEFAULT)
    # In the WORK tree, not the operator's checkout (P7g). Unlike prd.md/plan.md
    # this artifact has no authoring surface: the block comment above
    # `_ACCEPTANCE_MAP_DEFAULT` records that "the builder writes it with its file
    # tools", and a builder's cwd IS the work tree — the whole point of P7. Read
    # from `artifact_root` this gate halted every `dedicated` run with "no
    # acceptance map", pointing at a path the builder was never asked to write.
    map_path = ctx.artifact_root_in_work / map_name
    if not map_path.exists():
        return _acceptance_gate_halt(
            f"acceptance gate: phase {phase_id} has no acceptance map at "
            f"{map_name} (fail closed — an absent map is not 'all clauses mapped', "
            "FR-3.2)"
        )
    try:
        mapping = json.loads(map_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return _acceptance_gate_halt(
            f"acceptance gate: acceptance map {map_name} is not parseable JSON: "
            f"{exc} (fail closed)"
        )
    schema_err = _validate_acceptance_map_schema(mapping, ctx)
    if schema_err is not None:
        return _acceptance_gate_halt(
            f"acceptance gate: acceptance map {map_name} is schema-invalid — "
            f"{schema_err} (rejected at load, FR-3.2)"
        )

    # 1b. Phase scoping: the acceptance map is a phase-scoped artifact (schema
    # `phase`). A map whose `phase` is not this phase's id — a stale or
    # wrong-phase acceptance-map.json — is rejected here (review F-001). Reusing
    # clause ids such as `A1` across phases must NOT let a prior phase's map
    # satisfy this gate; the map must declare it covers THIS phase (fail closed,
    # FR-3.2).
    map_phase = mapping.get("phase")
    if map_phase != phase_id:
        return _acceptance_gate_halt(
            f"acceptance gate: acceptance map {map_name} covers phase "
            f"{map_phase!r}, not the current phase {phase_id} — a stale or "
            "wrong-phase map is not this phase's completion artifact (fail "
            "closed, FR-3.2)"
        )

    # 1c. Exact map: the artifact must map exactly this phase's clauses — no
    # extras. A clause id in the map but absent from the plan phase is a
    # stale/unrelated entry that would let incorrect evidence ride in the audit
    # artifact consumed by P3 deferral reconciliation; reject it (review F-002,
    # FR-3.2).
    clause_id_set = set(clause_ids)
    extra_ids = sorted(
        {c["id"] for c in mapping["clauses"] if c["id"] not in clause_id_set}
    )
    if extra_ids:
        return _acceptance_gate_halt(
            f"acceptance gate: phase {phase_id} acceptance map {map_name} carries "
            f"clause id(s) not in the plan phase: {', '.join(extra_ids)} — the map "
            "must be an exact map of the current phase's acceptance list (fail "
            "closed, FR-3.2)"
        )

    # 1d. FR-3.3 deferral reconciliation: a deferral (in this phase's acceptance
    # map `deferrals[]` or in its commit body — the CLAUDE.md §7 "Deferred to
    # P<N>" convention) that points to a phase the plan does not contain lands the
    # deferred work nowhere. Reconciled HERE, at the phase that authored the
    # deferral, against the plan's actual phase ids — a phantom target parks the
    # phase closed (a deferral that points nowhere is silently-dropped work). Open
    # deferrals themselves are injected into the target phase's implement prompt at
    # render time (`_render_prompt`); this gate is the fail-closed existence check.
    deferrals = deferrals_from_map(mapping, source=f"acceptance-map:{phase_id}")
    for rec in ctx.manifest.commits:
        if rec.phase == phase_id or rec.phase.startswith(f"{phase_id}."):
            try:
                body = gitops.commit_message(ctx.repo_root, rec.sha)
            except gitops.GitError:
                continue
            deferrals.extend(
                parse_body_deferrals(body, source=f"commit:{rec.sha[:10]}")
            )
    # Only touch the plan when there is something to reconcile: a phase with no
    # deferral needs no phase-list load (and must not fail closed for lack of one).
    if deferrals:
        known_ids = _plan_phase_ids(ctx)
        if known_ids is None:
            return _acceptance_gate_halt(
                f"acceptance gate: phase {phase_id} declares deferral(s) but the "
                "plan's phase list cannot be loaded from plan.md to reconcile them "
                "(fail closed, FR-3.3)"
            )
        phantom = phantom_deferrals(deferrals, known_ids)
        if phantom:
            named = "; ".join(f"{d.to_phase} ({d.source})" for d in phantom)
            return _acceptance_gate_halt(
                f"acceptance gate: phase {phase_id} defers work to nonexistent "
                f"phase(s): {named} — a deferral must target a real plan phase "
                "(fail closed, FR-3.3)"
            )

    # 2. Mapping completeness: every phase clause must have >=1 evidence entry.
    mapped_ids = {c["id"] for c in mapping["clauses"] if c.get("evidence")}
    unmapped = [cid for cid in clause_ids if cid not in mapped_ids]
    if unmapped:
        return _acceptance_gate_halt(
            f"acceptance gate: phase {phase_id} has unmapped acceptance "
            f"clause(s): {', '.join(unmapped)} — every clause must cite >=1 test "
            "(fail closed, FR-3.2)"
        )

    # 3. Existence: every id cited for THIS collector must appear in the
    # collector's side-effect-free enumeration (run under the interim posture).
    cited = {
        ev["id"]
        for c in mapping["clauses"]
        for ev in c.get("evidence", [])
        if ev.get("kind") == collector_kind
    }
    if not cited:
        # No evidence for this collector at all, yet every clause is mapped (step 2)
        # — the map cites only other collectors. In v1 the only registered kind is
        # pytest and the schema forbids any other, so this is unreachable in a
        # valid v1 map; nothing to enumerate, so this collector's gate is a no-op.
        return StepResult(
            status=DONE,
            notes=(
                f"acceptance gate ({collector_kind}): phase {phase_id} — no "
                f"{collector_kind} evidence to check; all {len(clause_ids)} clause(s) "
                "mapped"
            ),
        )
    collector = get_collector(collector_kind)
    # Enumeration posture (PR #59 review F3/F7, superseding the P5 LLM-mediated
    # design): a bounded ENGINE SUBPROCESS in a DISPOSABLE COPY of the worktree.
    # Deterministic — no LLM in the evidence path (the agent-echo design could
    # truncate a large id list into a chronic false park, or fabricate ids into
    # a false pass, defeating the gate's whole premise). `pytest --collect-only`
    # still executes branch-authored conftest/import-time code, so it runs with
    # the verifier's STRIPPED env, cwd pinned to the copy (import-time writes
    # land in a discarded tree), and wall-clock + rlimit bounds. The command is
    # project-resolved (collectors.resolve_command): the operator's
    # `collectors.<kind>.command` override, else the project's pytest-shaped
    # `test_command` env, else the engine interpreter.
    command = resolve_command(collector, ctx.config)
    try:
        copy = verify.make_disposable_copy(ctx.work_root)
    except verify.CopyCreationError as exc:
        return _acceptance_gate_halt(
            f"acceptance gate ({collector_kind}): could not create a disposable "
            f"copy to enumerate phase {phase_id} in — {exc} (fail closed)"
        )
    try:
        enumerated = collector.enumerate(
            worktree=copy.path,
            judge_env={},
            command=command,
        )
    except CollectorEnumerationError as exc:
        return _acceptance_gate_halt(
            f"acceptance gate ({collector_kind}): enumeration failed for phase "
            f"{phase_id} — {exc}"
        )
    finally:
        verify.discard_disposable_copy(ctx.work_root, copy)
    missing_ids = sorted(cited - enumerated)
    if missing_ids:
        return _acceptance_gate_halt(
            f"acceptance gate ({collector_kind}): phase {phase_id} cites test id(s) "
            f"absent from the collector enumeration: {', '.join(missing_ids)} "
            "(fail closed — a cited id must exist, FR-3.2)"
        )
    return StepResult(
        status=DONE,
        notes=(
            f"acceptance gate ({collector_kind}): phase {phase_id} — all "
            f"{len(clause_ids)} clause(s) mapped, {len(cited)} cited id(s) exist "
            "(citation + existence proven; sufficiency is the review lens's job)"
        ),
    )


def _plan_phase_ids(ctx: StepContext) -> set[str] | None:
    """The plan's actual phase ids (P1, P2…) for deferral reconciliation (FR-3.3).

    Returns ``None`` when plan.md is absent or its ``gauntlet-phases`` block is
    unparseable — the caller fails closed (a deferral cannot be reconciled without
    the phase list). ``phase_lint`` already rejects a malformed plan at the plan
    gate, so this is the last-ditch guard for a bypassed plan.
    """
    try:
        phases = load_plan_phases(ctx.artifact_root / "plan.md")
    except PlanPhasesError:
        return None
    if not phases:
        return None
    return {p["id"] for p in phases if isinstance(p, dict) and p.get("id")}


def _acceptance_map_relpath(ctx: StepContext) -> str | None:
    """Repo-relative POSIX path of the default acceptance map, for `git show`.

    Deferral injection (FR-3.3) reads each prior phase's committed
    ``acceptance-map.json`` out of history (the live on-disk file is the *current*
    phase's map), which needs the path relative to the repo root. ``None`` when it
    resolves outside the repo (defensive; the map lives under the artifact root).
    """
    path = (ctx.artifact_root / _ACCEPTANCE_MAP_DEFAULT).resolve()
    try:
        return path.relative_to(ctx.repo_root.resolve()).as_posix()
    except ValueError:
        return None


class DeferralCollectionError(Exception):
    """A prior phase's committed deferral data could not be recovered (FR-3.3).

    Raised when a commit that *tracks* the acceptance map cannot have its
    structured ``deferrals[]`` read/parsed. It fails the prompt render closed so
    the phase is never built with an open deferral silently dropped (review
    F-001); the handler turns it into a precondition halt for a human to resolve.
    """


def _collect_run_deferrals(ctx: StepContext, *, map_relpath: str | None) -> list[Deferral]:
    """All deferrals recorded across this run's phase commits (FR-3.3).

    Two durable sources per recorded commit: the commit BODY ("Deferred to P<N>:"
    prose, CLAUDE.md §7) and the ``acceptance-map.json`` committed at that sha (its
    structured ``deferrals[]``, read out of history because the live file on disk
    is the current phase's map).

    The structured source fails CLOSED (review F-001): a commit that *tracks* the
    acceptance map must have its ``deferrals[]`` recovered, so a git read failure
    or an unparseable committed map on such a commit raises
    :class:`DeferralCollectionError` rather than degrading to "no deferrals here".
    Silently reducing an unrecoverable committed map to absent data would drop the
    obligation a prior phase handed forward — the exact silently-lost-work failure
    FR-3.3 exists to prevent, and a violation of the fail-closed / data-over-
    inference principles (CLAUDE.md §2). A commit that simply does not carry the
    map (the ordinary case for every non-phase commit) is not an error and is
    skipped. Commit-body prose stays best-effort: it is a convention, not a
    committed structured artifact, so an unreadable message yields no prose
    deferrals without halting.
    """
    out: list[Deferral] = []
    seen: set[str] = set()
    for rec in ctx.manifest.commits:
        sha = rec.sha
        if not sha or sha in seen:
            continue
        seen.add(sha)
        try:
            body = gitops.commit_message(ctx.repo_root, sha)
        except gitops.GitError:
            body = ""
        out.extend(parse_body_deferrals(body, source=f"commit:{sha[:10]}"))
        if not map_relpath:
            continue
        # Distinguish "this commit has no map" (fine — skip) from "the map is there
        # but unrecoverable" (fail closed). `git show <sha>:<path>` collapses both
        # to a non-zero exit, so a tracked-ness probe is what separates them.
        try:
            carries_map = gitops.any_tracked_at(ctx.repo_root, sha, [map_relpath])
        except gitops.GitError as exc:
            raise DeferralCollectionError(
                f"cannot determine whether commit {sha[:10]} carries {map_relpath} "
                f"for open-deferral reconciliation: {exc}"
            ) from exc
        if not carries_map:
            continue
        raw = gitops.file_at_commit(ctx.repo_root, sha, map_relpath)
        if raw is None:
            raise DeferralCollectionError(
                f"acceptance map {map_relpath} is committed at {sha[:10]} but could "
                "not be read out of history for open-deferral reconciliation"
            )
        try:
            mapping = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeferralCollectionError(
                f"acceptance map {map_relpath} committed at {sha[:10]} is not valid "
                f"JSON, so its structured deferrals cannot be recovered: {exc}"
            ) from exc
        out.extend(deferrals_from_map(mapping, source=f"acceptance-map@{sha[:10]}"))
    return out


def _render_open_deferrals(ctx: StepContext) -> str | None:
    """The verbatim open-deferral block for the current phase's prompt (FR-3.3).

    A prior phase that explicitly deferred work to THIS phase must not have the
    obligation silently dropped: the builder receives each deferral's text
    verbatim so it implements or explicitly re-defers it. Returns ``None`` when
    there is no current phase or no open deferral targeting it (the ordinary
    first-phase / no-deferral case, where no block is injected).

    Raises :class:`DeferralCollectionError` (via :func:`_collect_run_deferrals`)
    when a prior phase's committed acceptance map cannot be recovered — the render
    fails closed rather than omit a block whose absence is indistinguishable from
    "no deferral" (review F-001).
    """
    phase_id = _iteration_phase(ctx)
    if not phase_id:
        return None
    all_deferrals = _collect_run_deferrals(ctx, map_relpath=_acceptance_map_relpath(ctx))
    open_ = open_deferrals_for(phase_id, all_deferrals)
    if not open_:
        return None
    lines = "\n".join(d.render() for d in open_)
    return (
        f"\n\n--- open deferrals targeting {phase_id} (FR-3.3) ---\n"
        "A prior phase explicitly deferred the following work to THIS phase. "
        "Implement each, or re-defer it explicitly (do not silently drop it):\n"
        f"{lines}\n"
    )


# The plan-author prompt template (basename). The trend-history block (FR-5.3,
# P7) is injected only into this step's prompt — the plan author is the sole
# consumer of measured phase-cost history.
_PLAN_AUTHOR_PROMPT = "plan-author.md"


def _render_plan_author_history(step: Step, ctx: StepContext) -> str | None:
    """Measured phase-cost history + the size bound for the plan-author prompt.

    FR-5.3 (P7): the plan author sizes phases, and without measured history it
    sizes blind. This appends the repo's completed-run cost/duration distributions
    by step type, the ``max_frs_per_phase`` bound, and any provider window budget
    to the plan-author input as advisory data (the plan stays human-ratified;
    nothing auto-tunes). Returns ``None`` for every other step — the block is
    scoped to the plan-author template — and a non-empty block (stats or the
    explicit no-history notice, never silence) for the plan-author step.
    """
    ref = step.get("prompt")
    if not ref or Path(ref).name != _PLAN_AUTHOR_PROMPT:
        return None
    from gauntlet.engine.trend import render_plan_author_history

    run_root = ctx.repo_root / ctx.config.run_root
    return render_plan_author_history(
        run_root,
        max_frs_per_phase=ctx.config.max_frs_per_phase,
        providers=ctx.config.providers,
    )


def _deferral_collection_halt(exc: DeferralCollectionError) -> StepResult:
    """Fail-closed halt when open-deferral injection cannot recover committed data.

    A dropped open deferral is silently-lost work (FR-3.3), so we do not render the
    phase prompt without it — we halt for a human, exactly like any other
    unsatisfied precondition (review F-001).
    """
    return StepResult(
        status=FAILED,
        halt_reason=HALT_REASON_PRECONDITION,
        notes=(
            "open-deferral injection failed closed: a prior phase's committed "
            f"acceptance map could not be recovered — {exc}. The phase prompt is "
            "not rendered without its open deferrals (FR-3.3 / review F-001)."
        ),
    )


def _validate_acceptance_map_schema(mapping: object, ctx: StepContext) -> str | None:
    """Validate the acceptance map against ``schemas/acceptance-map.json``.

    Returns ``None`` when valid, else the schema error string. The schema's
    ``kind`` enum is closed (registered collectors only), so an evidence entry
    naming an unregistered collector is rejected here — schema-invalid, not a
    runtime surprise (FR-3.2 / P2-A5).
    """
    from gauntlet.adapters._structured import validate_schema

    # The schema lives under the configured asset_root alongside the other
    # schemas/*.json (asset_root is "." in gauntlet's own repo), resolved the same
    # way `validate:` schema refs are (validators.py).
    asset_root = getattr(ctx.config, "asset_root", ".")
    schema_file = (ctx.repo_root / asset_root / _ACCEPTANCE_MAP_SCHEMA).resolve()
    if not schema_file.exists():
        return f"schema {_ACCEPTANCE_MAP_SCHEMA} not found under the asset root"
    try:
        schema = json.loads(schema_file.read_text())
        validate_schema(mapping, schema)
    except ValueError as exc:
        return str(exc)
    except (OSError, json.JSONDecodeError) as exc:
        return f"schema {_ACCEPTANCE_MAP_SCHEMA} is unreadable: {exc}"
    return None


# --- agent_task --------------------------------------------------------------
def handle_agent_task(step: Step, ctx: StepContext) -> StepResult:
    agent_name = step.agent
    if not agent_name:
        return StepResult(
            status=FAILED,
            halt_reason=HALT_REASON_PRECONDITION,
            notes="agent_task step has no `agent:`",
        )
    # FR-2.1 (review F-003): `validate:` runs against the step's `output:`
    # artifact, so a `validate:` with no `output:` would silently validate
    # nothing — a fail-OPEN skip. The loader rejects this shape at load time
    # (engine/validate.py); this runtime precondition is defense in depth for a
    # hand-built / bypassed pipeline, failing closed before the adapter is built.
    if step.get("validate") and not step.get("output"):
        return StepResult(
            status=FAILED,
            halt_reason=HALT_REASON_PRECONDITION,
            notes=(
                "agent_task declares `validate:` without `output:`; the validator "
                "would be silently skipped — failing closed (FR-2.1 / review F-003)"
            ),
        )
    # Snapshot the append-only judge audit before this invocation. The delta is
    # the adapter-independent record of its PreToolUse allow/deny decisions
    # (Claude/Codex expose different and sometimes incomplete event shapes).
    from gauntlet.engine.judgeaudit import audit_offset

    judge_audit = ctx.run_dir / "judge-audit.jsonl"
    judge_offset = audit_offset(judge_audit) if ctx.judge_env else 0
    # FR-2.2: a plain `gauntlet resume` of an artifact_invalid park re-runs ONLY
    # the validator against the (possibly hand-edited) on-disk artifact — no
    # adapter invocation. Done here, before the adapter is even built, so a
    # hand-edit-then-resume never re-runs the author. `parked_reason` is still the
    # park's value at handler time (the orchestrator clears it only in _finalize),
    # exactly like the usage-limit resume discriminator below. Scoped to a step
    # that OWNS a validator (P5, plan §5.1): a drive-level plan-parse park
    # (`_park_plan_artifact_invalid`) can legitimately land on an agent_task
    # with no `validate:`/`output:` — the stage walk already re-validated the
    # plan before re-reaching this handler, so that step runs normally.
    if (
        ctx.record.parked_reason == PARKED_REASON_ARTIFACT_INVALID
        and step.get("validate")
        and step.get("output")
    ):
        return _revalidate_on_resume(step, ctx, agent_name)
    # FR-10: while this invocation is consuming a pending `--response`, bind the
    # resume-disposition schema invocation-locally and let the structured
    # disposition drive the outcome — without touching the approved pipeline.
    consuming_response = _consuming_response(ctx)
    # FR-6.3: the resume-disposition emission is a mechanical structured
    # classification (schema-bound + fail-closed engine checks), so a shipped
    # pipeline can route it to a cheap `disposition_agent` profile instead of
    # spending the builder's constrained window on it. Only on a `--response`
    # resume; a different emitter runs a fresh sessionless call (a cheap `api`
    # profile has no session to continue). The primary `agent` still owns every
    # non-disposition invocation.
    emit_agent = agent_name
    disposition_agent = step.get("disposition_agent")
    if consuming_response and disposition_agent and disposition_agent != agent_name:
        emit_agent = disposition_agent
    # FR-6.1: a step-level `effort:` overrides the profile's effort — but only for
    # the step's own agent, never a substituted disposition emitter (which uses
    # its own profile's effort).
    effort_override = step.get("effort") if emit_agent == agent_name else None
    adapter = ctx.build_adapter(emit_agent, effort=effort_override)
    # FR-3.3: a usage-limit resume continues the persisted CLI session with a
    # SHORT continuation prompt instead of re-sending the full original prompt.
    # The record still carries parked_reason=usage_limit (the orchestrator clears
    # it only when this run finalizes), so it uniquely identifies the resume; it
    # is never a `--response` resume (a usage_limit park needs no decision).
    is_quota_resume = bool(
        ctx.record.parked_reason == PARKED_REASON_USAGE_LIMIT and ctx.record.session_id
    )
    try:
        prompt = _CONTINUATION_PROMPT if is_quota_resume else _render_prompt(step, ctx)
    except DeferralCollectionError as exc:
        return _deferral_collection_halt(exc)
    schema = (
        _resume_disposition_schema(ctx)
        if consuming_response
        else _load_schema(step, ctx)
    )
    # A substituted disposition emitter (emit_agent != agent_name) has no session
    # to continue — the persisted session_id belongs to the primary agent — so it
    # runs a fresh call. The primary agent (disposition or quota resume) continues
    # its own session as before.
    emit_session = ctx.record.session_id if emit_agent == agent_name else None
    # Per-step timeout overrides the profile's step_timeout_s, which overrides
    # the adapter default (FR-3.3). A timeout raises AgentTimeoutError, which the
    # orchestrator turns into a HALTED checkpoint. The status path resolves the
    # SAME value via `resolve_step_timeout_s` so the reported deadline matches.
    timeout = resolve_step_timeout_s(step, agent_name, ctx.config)
    if timeout is not None and hasattr(adapter, "timeout_s"):
        adapter.timeout_s = timeout
    # Agent-liveness watchdog bound (FR-5.3, #103), armed the same way from the
    # profile; unset keeps the adapter's default (engine default bound). A
    # vanished child raises AgentVanishedError → the orchestrator parks the
    # step INTERRUPTED for a plain resume, never a silent 2h wait.
    if agent_name and agent_name in ctx.config.agents:
        watchdog = ctx.config.profile(agent_name).agent_silent_timeout_s
        if watchdog is not None and hasattr(adapter, "watchdog_silence_s"):
            adapter.watchdog_silence_s = watchdog
    logger = step_logger(ctx)

    def _invoke(call_prompt: str, session: str | None, *, log_suffix: str = ""):
        """One adapter call with FR-4 lossless logging + FR-6 streaming.

        Factored so a usage-limit resume can fall back to a second, full-prompt
        call when the stored session is gone (FR-3.3), and so an FR-2.1 repair
        re-invocation gets its OWN evidence files. The prompt is persisted before
        the call (survives a crash); a per-attempt stream is opened/closed here so
        the events file reflects the current attempt. ``log_suffix`` names a
        distinct attempt (e.g. ``-repair1``) so a repair never overwrites the
        initial attempt's prompt/events (lossless, FR-4).
        """
        from gauntlet.engine import depretry

        logger.log_text(f"prompt{log_suffix}.md", call_prompt)
        while True:
            # Live-observability streaming (live-run-observability FR-2): when
            # enabled and the adapter is line-streamable, thread a per-line sink
            # so the events file grows during the step. sink is passed ONLY when
            # streaming — the buffered path's call shape (and existing fakes)
            # stay untouched. The suffix keeps a repair attempt's stream off the
            # initial attempt's file. Opened fresh per dependency retry.
            stream = open_step_stream(ctx, adapter, logger, suffix=log_suffix)
            kwargs: dict = {
                # The tree the agent edits (P7a).
                "session": session, "schema": schema, "cwd": ctx.work_root,
            }
            if stream is not None:
                kwargs["sink"] = stream.append_line
            try:
                # Clock-time evidence (engine-measured, adapter-agnostic): one
                # Invocation per call, labelled by its evidence-file suffix.
                with record_invocation(
                    ctx, agent=emit_agent, label=f"call{log_suffix}",
                    adapter=adapter, effort=effort_override,
                ):
                    return adapter.run(call_prompt, **kwargs)
            except AdapterError as exc:
                # FR-4.2 is lossless for failures too (P4.r1 F-007): persist
                # whatever partial evidence the adapter salvaged before it is
                # re-raised (the orchestrator classifies transient-vs-terminal,
                # FR-3.1).
                if exc.partial is not None:
                    logger.log_result(exc.partial, suffix=f"{log_suffix}-failed")
                logger.log_text(f"failure{log_suffix}.txt", str(exc))
                # P5 (plan §5.2): a typed transport/dependency failure gets a
                # bounded, PERSISTED in-process retry with backoff + jitter
                # before it can park — the budget lives on the step record and
                # is flushed write-ahead, so a crash between retries never
                # resets it.
                info = getattr(exc, "failure_info", None)
                if isinstance(exc, AgentFailedError) and depretry.is_dependency_failure(info):
                    delay = depretry.consume_retry(
                        ctx, info, site=f"{ctx.record.id}{log_suffix}"
                    )
                    if delay is not None:
                        depretry.wait(delay)
                        continue
                # Authoritative failure evidence in THIS step's dir (issue
                # #63): the files `gauntlet logs` reads must name the failure,
                # never fall back to a sibling. A SessionNotFoundError is not
                # a failure — the caller falls back to a full re-run (FR-3.3).
                if not isinstance(exc, SessionNotFoundError):
                    logger.log_failure(
                        error=str(exc),
                        agent=ctx.record.agent,
                        failure_kind=info.kind if info is not None else None,
                        marker=info.marker if info is not None else None,
                        partial_events=(
                            exc.partial.raw_events if exc.partial else None
                        ),
                    )
                raise
            finally:
                # A streaming sink fault surfaces as a StreamSinkError (not an
                # AdapterError) that propagates past the except above; the
                # orchestrator records the step FAILED (fail closed, FR-6.2).
                # Close the stream either way so it is never left half-open.
                if stream is not None:
                    stream.close()

    fallback_note = ""
    try:
        result = _invoke(prompt, emit_session)
    except SessionNotFoundError as exc:
        # FR-3.3: the stored session is gone — on a usage-limit resume, fall back
        # to a full re-run with no session (recoverable, not a run-halting fault)
        # and record the fallback. Off the quota-resume path a SessionNotFoundError
        # is unexpected, so re-raise it to fail closed like any other adapter error.
        if not is_quota_resume:
            raise
        logger.log_text("session-expired.txt", str(exc))
        fallback_note = (
            "usage-limit resume: stored session was unknown/expired; fell back "
            "to a full re-run with no session (FR-3.3)"
        )
        try:
            prompt = _render_prompt(step, ctx)
        except DeferralCollectionError as exc:
            return _deferral_collection_halt(exc)
        result = _invoke(prompt, None)
    logger.log_result(result)  # transcript.md + events.jsonl (+ structured)
    # Attribute usage to the agent that actually ran (the disposition emitter on a
    # routed `--response` resume, else the step's agent) so `agent_usage` reflects
    # the cheap profile's spend, not the builder's (FR-3.2/FR-6.3).
    usage_by_agent = {emit_agent: result.usage} if result.usage else {}

    if consuming_response and emit_agent != agent_name:
        # FR-6.3 two-phase resume: the cheap `disposition_agent` CLASSIFIED the
        # response, but it cannot do the step's work. Its verdict gates whether the
        # primary agent runs at all:
        #   * a re-park (amendment_required/new_conflict) or a fail-closed
        #     disposition lands nothing — return it now, so the builder's
        #     constrained window is never touched for a conflict that resolves to
        #     "amend the artifact" or "still ambiguous" (the common case).
        #   * a proceed means the conflict is resolved and the phase must actually
        #     be implemented, which only the primary agent can do — re-drive it
        #     exactly like an unrouted resume (full prompt + disposition schema +
        #     its preserved session), and let ITS authoritative disposition drive
        #     the outcome below.
        classified = _resume_disposition_result(
            emit_agent, result, usage_by_agent, ctx.record.human_responses
        )
        if classified.status != DONE:
            return classified
        # proceed: re-drive the primary agent to implement. Account BOTH the cheap
        # classification and the builder's implementation as real spend (FR-3.2),
        # split per profile — the classification is not free just because it was
        # cheap.
        spend = _UsageAccumulator()
        spend.add(result.usage, agent=emit_agent)  # the disposition_agent's spend
        # `_invoke` closes over the adapter AND its invocation provenance. Switch
        # all three together before launching the primary turn; otherwise the
        # builder's clock record is frozen under the cheap disposition profile.
        effort_override = step.get("effort")
        adapter = ctx.build_adapter(agent_name, effort=effort_override)
        emit_agent = agent_name
        # FR-3.3: re-apply the resolved step timeout to this freshly-built primary
        # adapter. The `timeout` above was applied to the phase-1 disposition
        # adapter (a different, often cheaper profile); this new adapter defaults to
        # the adapter's DEFAULT_TIMEOUT_S and would otherwise halt a long builder
        # step mid-phase on a `--response` resume — the fresh-launch path applies it,
        # so the two-phase re-drive must too.
        if timeout is not None and hasattr(adapter, "timeout_s"):
            adapter.timeout_s = timeout
        try:
            result = _invoke(prompt, ctx.record.session_id, log_suffix="-implement")
        except SessionNotFoundError as exc:
            # Same audit contract as the quota-resume fallback (FR-3.3): the
            # session loss and the sessionless re-drive must be visible in the
            # step evidence, not silent.
            logger.log_text("session-expired-implement.txt", str(exc))
            result = _invoke(prompt, None, log_suffix="-implement")
        logger.log_result(result, suffix="-implement")
        spend.add(result.usage, agent=agent_name)
        result = result.model_copy(update={"usage": spend.result()})
        usage_by_agent = spend.by_agent()

    from gauntlet.engine.judgeaudit import JudgeToolCounts, counts_since

    judge_counts = (
        counts_since(judge_audit, offset=judge_offset, step_id=ctx.record.id)
        if ctx.judge_env
        else JudgeToolCounts()
    )
    # An exit-0 agent whose every requested tool was denied did not complete an
    # implementation turn. Marking it DONE launders a judge/config failure into
    # a vacuous green and defers the real cause to phase-commit (issue #83).
    if judge_counts.all_denied:
        if judge_counts.fail_closed_denied == judge_counts.denied:
            headline = "judge cannot evaluate tool calls"
        else:
            headline = "judge denied every tool call"
        reason = (
            f" Last denial: {judge_counts.denial_reasons[-1]}"
            if judge_counts.denial_reasons else ""
        )
        return StepResult(
            status=FAILED,
            session_id=result.session_id,
            usage=result.usage,
            usage_by_agent=usage_by_agent,
            halt_reason=HALT_REASON_JUDGE_DENY,
            judge_tool_calls_allowed=judge_counts.allowed,
            judge_tool_calls_denied=judge_counts.denied,
            notes=(
                f"{headline}: 0 allowed, {judge_counts.denied} denied during "
                f"agent {agent_name!r}; refusing to record a successful step."
                f"{reason}"
            ),
        )

    # Issue #101 (sibling of #83): the guard above is defeated by a single
    # allowed READ-ONLY call. Classify per-tool for a repo-write agent task —
    # an invocation whose every MUTATING call (Write/Edit/Bash/...) was denied
    # made zero observable changes through judged tools, so `done` would be
    # vacuous: tests then pass vacuously and only phase-commit fails loud on
    # the clean tree. Disposition mirrors the #89 cycle-side precedent
    # (cycle.py fix-turn park): denials that are ALL `source: fail-closed`
    # are a judge-infrastructure outage → park provider_unavailable
    # (plain-resumable, concrete deadline); any real policy deny in the mix
    # stays terminal exactly like the #83 guard — the judge worked, the agent
    # attempted something it should not, and a human decision is at stake.
    if bool(step.get("repo_write", True)) and judge_counts.all_mutating_denied:
        evidence = "; ".join(judge_counts.denial_reasons) or "(none recorded)"
        if judge_counts.mutating_fail_closed_denied == judge_counts.mutating_denied:
            from gauntlet.engine import depretry

            return StepResult(
                status=PARKED,
                parked_reason=PARKED_REASON_PROVIDER_UNAVAILABLE,
                session_id=result.session_id,
                usage=result.usage,
                usage_by_agent=usage_by_agent,
                backoff_s=depretry.park_deadline_s(ctx.record, ctx.config, None),
                judge_tool_calls_allowed=judge_counts.allowed,
                judge_tool_calls_denied=judge_counts.denied,
                notes=(
                    f"judge could not evaluate agent {agent_name!r}'s mutating "
                    f"tool calls: all {judge_counts.mutating_denied} denied "
                    "fail-closed (judge infrastructure error, not policy) and "
                    "0 mutating calls allowed "
                    f"({judge_counts.allowed} read-only call(s) allowed), so "
                    "the step made no observable change — refusing to record "
                    "a vacuous `done` (#101). Parking provider_unavailable: a "
                    "plain `gauntlet resume` retries the step once the judge's "
                    "dependency recovers — no `--response` decision is at "
                    f"stake. Denial evidence: {evidence}"
                ),
            )
        return StepResult(
            status=FAILED,
            session_id=result.session_id,
            usage=result.usage,
            usage_by_agent=usage_by_agent,
            halt_reason=HALT_REASON_JUDGE_DENY,
            judge_tool_calls_allowed=judge_counts.allowed,
            judge_tool_calls_denied=judge_counts.denied,
            notes=(
                "judge denied every mutating tool call during agent "
                f"{agent_name!r}: {judge_counts.mutating_denied} denied, 0 "
                f"allowed ({judge_counts.allowed} read-only call(s) allowed). "
                "A repo-write step with no allowed mutating call implemented "
                "nothing; refusing to record a successful step (#101). "
                f"Denial evidence: {evidence}"
            ),
        )

    # FR-3/FR-5/FR-10: on a `--response` resume the STRUCTURED disposition is
    # authoritative for the outcome, not the textual `halt_on` marker (which only
    # signals the FIRST conflict, before any response). Map it to the step status
    # here so a schema-valid `new_conflict` re-parks instead of being marked DONE;
    # the FR-3.0 classification itself lives in the prompt.
    if consuming_response:
        outcome = _resume_disposition_result(
            emit_agent, result, usage_by_agent, ctx.record.human_responses
        )
        # A re-park (amendment_required/new_conflict) or a fail-closed disposition
        # lands nothing: return immediately, skipping completion-signal handling
        # and the `output:` artifact write (review F-004 — a re-park must not
        # produce the step's declared artifact).
        if outcome.status != DONE:
            outcome.judge_tool_calls_allowed = judge_counts.allowed
            outcome.judge_tool_calls_denied = judge_counts.denied
            return outcome
        # proceed_*: the structured disposition resolved the conflict and is
        # authoritative, so the obsolete textual UPSTREAM CONFLICT marker is
        # suppressed below (check_halt=False). But a proceed completes the step
        # NORMALLY (FR-5 / FR-1.1): fall through so `require_signal` is still
        # honored and the declared `output:` artifact is still written (F-004).

    # Completion-signal contract (BOOTSTRAP-NOTES #32): a headless agent that
    # exits 0 may still have *halted* — surfaced an FR-10.4 upstream conflict
    # instead of doing the work. Exit code alone read that as `done` and the
    # engine marched on to a doomed commit. Opt-in per step: when `halt_on:` is
    # set and its marker is *signalled* (line-leading, per `_marker_signalled`),
    # park for a human (fail closed, never DONE); when `require_signal:` is set
    # and absent, fail closed. Document-authoring tasks must not carry `halt_on:`
    # — their output legitimately quotes such markers as prose (see plan-author
    # in pipelines/standard.yaml); the line-leading match is the second guard.
    # On a proceed-disposition resume, halt_on is suppressed (the structured
    # disposition already governed the conflict) while require_signal still binds.
    signal = _completion_signal(step, result.text, check_halt=not consuming_response)
    if signal is not None:
        status, note, parked_reason, halt_reason = signal
        return StepResult(
            status=status, session_id=result.session_id, usage=result.usage,
            usage_by_agent=usage_by_agent, notes=note,
            parked_reason=parked_reason, halt_reason=halt_reason,
            judge_tool_calls_allowed=judge_counts.allowed,
            judge_tool_calls_denied=judge_counts.denied,
        )

    artifact_writes: dict[str, Path] = {}
    output = step.get("output")
    validate_name = step.get("validate")
    final_usage = result.usage
    commit_sha = commit_phase = None
    if output:
        out_path = ctx.artifact_root / output
        ctx.writer.write_text(out_path, result.text)
        # FR-2.1: validate the freshly written artifact in-step, with a bounded
        # in-session repair loop; on exhaustion park artifact_invalid (FR-2.2).
        # Runs BEFORE any commit_output below, so an invalid artifact is never
        # committed — it stays on disk (dirty) for the sanctioned hand-edit path.
        if validate_name:
            park, result, summed = _validate_output(
                step, ctx, _invoke, logger, validate_name, output, out_path, result,
            )
            # Repair attempts are part of the same step invocation; refresh the
            # append-only delta so the persisted counts include them too.
            if ctx.judge_env:
                judge_counts = counts_since(
                    judge_audit, offset=judge_offset, step_id=ctx.record.id
                )
            final_usage = summed
            usage_by_agent = {agent_name: summed} if summed else {}
            if park is not None:
                park.session_id = result.session_id
                park.usage = summed
                park.usage_by_agent = usage_by_agent
                park.judge_tool_calls_allowed = judge_counts.allowed
                park.judge_tool_calls_denied = judge_counts.denied
                return park
        artifact_writes[output] = out_path
        # P7g: publish the validated bytes into the tree the run branch commits
        # in. Same-tree this is a no-op; under `dedicated` it is the only thing
        # that puts a mid-drive artifact where `commit_output` (and the
        # downstream cycle's baseline commit and reviewer) can see it. Runs
        # AFTER validation, so an invalid artifact is never published — it
        # stays in the operator's checkout for the sanctioned hand-edit.
        ctx.publish_artifact(output)
        # Prevent-at-source (report #3): a producer that opts in commits its own
        # declared deliverable as it finalizes, so HEAD advances at production
        # time. The deliverable then survives a crash before the next step AND
        # cannot be conflated with unrelated dirt at the downstream cycle's
        # clean-handoff guard — which previously skipped its baseline commit when
        # any second path was dirty and failed the next step with a misleading
        # "failed upstream". Commits ONLY the output path; unrelated dirt is left
        # for the handoff guard to surface (now with the offending paths named).
        if step.get("commit_output"):
            outcome = _commit_output_artifact(step, ctx, agent_name, output, out_path)
            if isinstance(outcome, StepResult):  # fail-closed format/commit error
                outcome.judge_tool_calls_allowed = judge_counts.allowed
                outcome.judge_tool_calls_denied = judge_counts.denied
                return outcome
            commit_sha, commit_phase = outcome
    return StepResult(
        status=DONE,
        session_id=result.session_id,
        usage=final_usage,
        usage_by_agent=usage_by_agent,
        artifact_writes=artifact_writes,
        commit_sha=commit_sha,
        commit_phase=commit_phase,
        notes=(
            f"agent {agent_name!r} completed\n{fallback_note}"
            if fallback_note else f"agent {agent_name!r} completed"
        ),
        judge_tool_calls_allowed=judge_counts.allowed,
        judge_tool_calls_denied=judge_counts.denied,
    )


# --- in-step artifact validation + repair (FR-2.1/2.2) -----------------------
def _sha256(text: str) -> str:
    """Content hash of an artifact's bytes for the revalidation pair (§6)."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _repair_prompt(output: str, error: str, attempt: int) -> str:
    """The in-session correction prompt fed to the same agent on a repair (FR-2.1).

    Short and directive — the session already holds the authoring context; this
    just names the concrete validation failure and asks for a full rewrite of the
    one artifact, mirroring the proven schema-retry re-ask in ``cycle.py``.
    """
    return (
        f"The `{output}` artifact you just wrote failed validation "
        f"(repair attempt {attempt} of {_MAX_ARTIFACT_REPAIRS}):\n\n{error}\n\n"
        f"Rewrite `{output}` so it passes this check. Return only the full "
        "corrected artifact as your response — no commentary, no code fences "
        "around it unless the artifact itself requires them."
    )


def _validate_output(step, ctx, invoke, logger, validate_name, output, out_path, result):
    """Validate ``output`` in-step with a bounded in-session repair loop (FR-2.1).

    ``invoke(prompt, session)`` is ``handle_agent_task``'s per-call closure (it
    logs the prompt, streams, and persists partial-failure evidence). Returns
    ``(park_or_none, result, summed_usage)``:

    * ``(None, valid_result, usage)`` — the artifact validated immediately or
      after ≤``_MAX_ARTIFACT_REPAIRS`` repairs; ``valid_result`` is the
      authoritative :class:`AgentResult` and ``out_path`` holds the valid bytes.
    * ``(park_result, last_result, usage)`` — repairs exhausted → a PARKED
      ``artifact_invalid`` :class:`StepResult` carrying the verbatim validator
      error (FR-2.2) and the ``hash_at_park`` content hash; the caller stamps its
      session/usage.

    ``usage`` sums the initial call and every repair attempt — each is real spend
    (FR-3.2) — or ``None`` when no attempt reported usage. Each repair result is
    logged with a ``-repair<n>`` suffix so both attempts survive in the transcript.
    An :class:`UnknownValidatorError` from a misconfigured ``validate:`` name
    propagates (fail closed → the step FAILs), never a repairable park.
    """
    total = _UsageAccumulator()
    total.add(result.usage)
    error = validate_artifact(
        validate_name, out_path.read_text(),
        repo_root=ctx.repo_root, asset_root=ctx.config.asset_root,
    )
    attempt = 0
    while error is not None and attempt < _MAX_ARTIFACT_REPAIRS:
        attempt += 1
        suffix = f"-repair{attempt}"
        result = invoke(
            _repair_prompt(output, error, attempt), result.session_id,
            log_suffix=suffix,
        )
        logger.log_result(result, suffix=suffix)
        total.add(result.usage)
        ctx.writer.write_text(out_path, result.text)
        error = validate_artifact(
            validate_name, out_path.read_text(),
            repo_root=ctx.repo_root, asset_root=ctx.config.asset_root,
        )
    summed = total.result()
    if error is None:
        return None, result, summed
    park = StepResult(
        status=PARKED,
        parked_reason=PARKED_REASON_ARTIFACT_INVALID,
        revalidation=RevalidationRecord(
            artifact=output, hash_at_park=_sha256(out_path.read_text()),
            validator=validate_name, diagnostic=error,
        ),
        notes=(
            f"artifact {output!r} failed validation ({validate_name}) after "
            f"{_MAX_ARTIFACT_REPAIRS} in-session repair attempts (FR-2.2); parked "
            f"for a hand-edit-then-`gauntlet resume`. Validator error:\n{error}"
        ),
    )
    return park, result, summed


def _revalidate_on_resume(step: Step, ctx: StepContext, agent_name: str) -> StepResult:
    """Re-run ONLY the validator on a plain resume of an ``artifact_invalid`` park.

    No adapter invocation (FR-2.2): validate the on-disk artifact — which a human
    may have hand-edited while the run was parked — and record the revalidation
    content-hash pair so the hand-edit is auditable rather than off-book file
    surgery (PRD §7). On pass → DONE (committing the now-valid ``output`` when the
    step opted into ``commit_output``, since the normal path commits only on
    validity); still invalid → re-park ``artifact_invalid`` with refreshed hashes.
    """
    output = step.get("output")
    validate_name = step.get("validate")
    # An artifact_invalid park is only ever written for a step with both `output`
    # and `validate` (see _validate_output). Missing either → inconsistent
    # manifest; fail closed rather than silently completing (CLAUDE.md §2).
    if not output or not validate_name:
        return StepResult(
            status=FAILED,
            halt_reason=HALT_REASON_PRECONDITION,
            notes=(
                "artifact_invalid resume on a step with no `output`/`validate` "
                "(inconsistent manifest); failing closed (FR-2.2)"
            ),
        )
    out_path = ctx.artifact_root / output
    try:
        text = out_path.read_text() if out_path.exists() else ""
    except (OSError, ValueError) as exc:
        # P5.1 review F-006: an unreadable/wrong-kind artifact on the
        # revalidation path re-parks artifact_invalid (repairable, resumable)
        # instead of surfacing as a terminal handler fault.
        return StepResult(
            status=PARKED,
            parked_reason=PARKED_REASON_ARTIFACT_INVALID,
            revalidation=RevalidationRecord(
                artifact=output, hash_at_park=_sha256(""),
                validator=validate_name,
                diagnostic=f"artifact is unreadable: {exc}",
            ),
            notes=(
                f"artifact {output!r} is unreadable on resume ({exc}); repair "
                "the file, then `gauntlet resume` re-runs the validator "
                "(FR-2.2)"
            ),
        )
    hash_at_resume = _sha256(text)
    prior = ctx.record.revalidation
    hash_at_park = prior.hash_at_park if prior is not None else hash_at_resume
    changed = hash_at_resume != hash_at_park
    error = validate_artifact(
        validate_name, text, repo_root=ctx.repo_root, asset_root=ctx.config.asset_root
    )
    if error is not None:
        # Re-park on the CURRENT (still-invalid) on-disk bytes (review F-001): the
        # new park pair must baseline against `hash_at_resume` — what is actually
        # parked now — NOT the original `hash_at_park`. Reusing the prior park hash
        # would keep comparing every later resume against stale bytes, so an invalid
        # hand-edit (A→B) followed by a resume with no further edit would wrongly
        # report `changed_while_parked=True` against B, contradicting the P4 audit
        # contract. Resume-side fields reset to their park defaults; the note below
        # still describes THIS resume's transition for the transcript.
        return StepResult(
            status=PARKED,
            parked_reason=PARKED_REASON_ARTIFACT_INVALID,
            revalidation=RevalidationRecord(
                artifact=output, hash_at_park=hash_at_resume,
                validator=validate_name, diagnostic=error,
            ),
            notes=(
                f"artifact {output!r} still fails validation ({validate_name}) on "
                f"resume ({'edited' if changed else 'unchanged'} while parked); "
                f"hand-edit it and `gauntlet resume` again (FR-2.2). Validator "
                f"error:\n{error}"
            ),
        )
    # Passed: the full audit pair documents the sanctioned hand-edit (park bytes
    # → resume bytes → changed? → passed) that resolved the park.
    reval = RevalidationRecord(
        artifact=output,
        hash_at_park=hash_at_park,
        hash_at_resume=hash_at_resume,
        changed_while_parked=changed,
        passed_on_resume=True,
        validator=validate_name,
    )
    # Valid on resume — complete the step with no adapter call. Commit the
    # now-valid deliverable if the step opted into commit_output (the normal path
    # committed only on validity; the resume path must too, to keep the
    # clean-handoff invariant for the downstream cycle).
    commit_sha = commit_phase = None
    # P7g: the hand-edit landed in the operator's checkout (the playbook's one
    # sanctioned edit, and it says to make it there). Publish it into the work
    # tree whether or not this step commits, so the downstream reviewer is
    # handed the bytes the human actually wrote.
    ctx.publish_artifact(output)
    if step.get("commit_output"):
        outcome = _commit_output_artifact(step, ctx, agent_name, output, out_path)
        if isinstance(outcome, StepResult):  # fail-closed format/commit error
            return outcome
        commit_sha, commit_phase = outcome
    edited = "hand-edited while parked" if changed else "unchanged since park"
    return StepResult(
        status=DONE,
        revalidation=reval,
        artifact_writes={output: out_path},
        commit_sha=commit_sha,
        commit_phase=commit_phase,
        notes=(
            f"artifact {output!r} passed validation ({validate_name}) on resume "
            f"({edited}); step completed with no agent re-invocation (FR-2.2)"
        ),
    )


def _commit_output_artifact(step: Step, ctx: StepContext, agent_name: str,
                            output: str, out_path: Path):
    """Commit ONLY the producer's freshly written `output:` artifact.

    Returns ``(sha, phase)`` on a commit, ``(None, None)`` when the artifact is
    already current (nothing to commit — never an empty commit), or a terminal
    ``StepResult`` on a missing-phase / format / git error (fail closed).

    Stages exactly the one path (never ``git add -A``), so an unrelated dirty
    file is neither swept into this commit nor able to defeat it — it stays
    uncommitted for the downstream clean-handoff guard to name.

    ``out_path`` is the AUTHORITY (the operator's checkout, §4.4); what gets
    committed is its work-tree counterpart, which
    :meth:`StepContext.publish_artifact` has already materialised. Relativising
    the authority against ``work_root`` is what produced "resolves outside the
    repo" for every `dedicated` producer step (P7g).
    """
    phase = step.get("phase")
    if not phase:
        return StepResult(
            status=FAILED,
            notes=f"step {step.get('id')!r} sets commit_output but no `phase:`; "
            "the producer commit needs a phase prefix (e.g. PLAN) for the "
            "enforced header format",
        )
    in_work = ctx.publish_artifact(output)
    try:
        rel = in_work.resolve().relative_to(ctx.work_root.resolve()).as_posix()
    except ValueError:
        return StepResult(
            status=FAILED,
            notes=(
                f"commit_output: artifact {output!r} resolves to {in_work}, "
                f"which is outside the tree this run commits in "
                f"({ctx.work_root})"
            ),
        )
    dirty = set(gitops.dirty_paths(ctx.work_root, exclude=ctx.excludes))
    if rel not in dirty:
        return None, None  # identical to HEAD — nothing to commit, no empty commit
    message = (
        f"{phase}: Author {output} for adversarial review\n\n"
        f"The {phase} artifact ({output}) was authored by the {agent_name} and is "
        "committed here, by the producing step, as the clean reviewable baseline. "
        "The clean-handoff invariant (FR-9.3) requires a committed worktree when "
        "control passes to the reviewer; committing at production time also lets "
        "the deliverable survive a crash before the review cycle. Engine-composed; "
        "no agent call.\n"
    )
    err = validate_commit_message(message)
    if err is not None:  # engine-composed; a violation here is a bug
        return StepResult(
            status=FAILED,
            notes=f"producer-commit message invalid: {err.reason}",
        )
    # Fail closed with an actionable note (review): a git failure here (hook,
    # identity, lock) must surface the cause + the offending path/phase, not
    # bubble out as the orchestrator's generic "handler error: ...".
    try:
        sha = gitops.commit_paths(
            ctx.work_root, message, [rel], identity=ctx.config.identity(agent_name),
        )
    except gitops.GitError as exc:
        return StepResult(
            status=FAILED,
            notes=f"producer-commit of {rel!r} (phase {phase}) failed: {exc}",
        )
    return sha, phase


def _completion_signal(step: Step, text: str, *, check_halt: bool = True):
    """Read an agent_task's final output for a halt/completion contract (#32).

    Returns ``None`` to proceed normally, or ``(status, note, parked_reason,
    halt_reason)`` to short-circuit. Both checks are opt-in (absent keys → no
    contract), so existing steps and the document-authoring tasks keep their plain
    exit-code semantics. Exactly one of ``parked_reason`` / ``halt_reason`` is set
    (FR-7.2 disjointness): the halt_on park carries ``parked_reason`` and a null
    ``halt_reason``; the require_signal failure carries ``halt_reason`` and a null
    ``parked_reason``.

    A ``halt_on`` park ALWAYS carries ``parked_reason=PARKED_REASON_RESPONSE``
    (FR-7.2 park invariant): the agent deliberately halted for a human decision,
    which is the PRD ``response`` park kind regardless of the marker text. The
    canonical :data:`UPSTREAM_CONFLICT_MARKER` and any custom ``halt_on`` marker
    alike route by step type (``RESPONDABLE_STEP_TYPES``), so a single reason
    serves both and no park is ever written with a null ``parked_reason`` (a null
    would classify as ``unknown`` — unexplainable from status JSON).

    ``check_halt=False`` suppresses only the ``halt_on`` check (review F-004): on a
    proceed-disposition `--response` resume the textual UPSTREAM CONFLICT marker is
    obsolete — the structured disposition already governed the conflict — but
    ``require_signal`` still binds, so the completion contract is preserved.
    """
    halt_on = step.get("halt_on")
    if check_halt and halt_on and _marker_signalled(halt_on, text):
        # Every halt_on park is a human-decision park → PRD `response` reason
        # (FR-7.2 park invariant, F-001): the agent halted for a human, and
        # re-driving without a decision would only re-halt into the same wall.
        # The builder-conflict vs cycle-escalation distinction is recovered from
        # the step type (RESPONDABLE_STEP_TYPES), not this value, so one reason
        # serves the canonical UPSTREAM CONFLICT marker and any custom marker
        # alike. Never null (a null park classifies as `unknown`).
        return PARKED, (
            f"agent signalled {halt_on!r} (FR-10.4 upstream conflict / halt); "
            "parked for a human instead of marking the step done (#32)"
        ), PARKED_REASON_RESPONSE, None
    require = step.get("require_signal")
    if require and not _marker_signalled(require, text):
        # The agent ran but did not satisfy the completion contract — a terminal
        # adapter/output failure (FR-7.2), not a fail-closed precondition guard.
        return FAILED, (
            f"agent did not emit the required completion signal {require!r}; "
            "failing closed rather than advancing on a silent non-completion (#32)"
        ), None, HALT_REASON_ADAPTER_ERROR
    return None


def _marker_signalled(marker: str, text: str) -> bool:
    """True when *marker* appears as a deliberate line-leading signal in *text*.

    The contract (implement-phase.md) tells the agent to emit the marker as a
    *clearly marked block* — i.e. at the start of its own line, optionally behind
    Markdown decoration (``#``/``*``/``>``/`` ` ``/``-``). Matching only there,
    not anywhere in the body, is what keeps a document that merely *discusses*
    the marker in prose from being read as a genuine signal: a plan that quotes
    the FR-10.4 protocol verbatim ("…is an **UPSTREAM CONFLICT** (FR-10.4)…")
    used to false-positive the substring check, park the step, and lose the
    authored ``output:`` (the write happens only on the non-signal path). This
    stays fail-closed on a real signal (a marker on its own line still matches)
    while refusing to invent one from incidental text.
    """
    if not marker:
        return False
    # The marker must OWN its line, not merely begin it (review F-002). A
    # prefix-only match also fired on lines that extend the token into a
    # different word or sentence — "UPSTREAM CONFLICTS: none" (plural) or
    # "UPSTREAM CONFLICT resolved" — parking a step that emitted no genuine
    # signal. After the leading decoration + marker, allow only: a trailing
    # field colon ("MARKER: <reason>", the compact one-line form), or closing
    # Markdown decoration (`*`/`` ` ``/`#`) and whitespace to end-of-line.
    pattern = re.compile(
        rf"^[ \t#*>`\-]*{re.escape(marker)}(?=:|[ \t]*[*`#]*[ \t]*$)",
        re.MULTILINE,
    )
    return pattern.search(text or "") is not None


def _render_prompt(step: Step, ctx: StepContext) -> str:
    template_ref = step.get("prompt")
    if template_ref:
        template_path = ctx.repo_root / ctx.config.asset_root / template_ref
        base = template_path.read_text()
    else:
        base = step.get("prompt_text", "") or ""
    # FR-4: feed the human-decision history (if any) to the builder via the
    # EXISTING input-artifact path — no new `{{}}` interpolation. The synthetic
    # `human-response.md` is added to an INVOCATION-LOCAL copy of the inputs list
    # and an invocation-local artifact-path map; `step.inputs`, the pipeline
    # definition, and manifest.json are never mutated (FR-4.1). The artifact is
    # rebuilt fresh from `human_responses` on every render (chronological), so
    # repeated resumes regenerate one file rather than accumulating files.
    # FR-1.1: each input carries a mode — `inline` (default; embed the body),
    # `reference` (inject the repo-relative path, the agent reads it), or `phase`
    # (plan.md only; inject the current phase's section + the full-doc path). The
    # modes were fail-closed-validated at load (engine/validate.py) — an unknown
    # mode / non-reading profile / escaping path never reaches here.
    input_refs = iter_inputs(step)
    artifacts = dict(ctx.artifacts)
    parts = [base]
    # FR-11.2: a reset_to_base recovery that rewound to an intra-phase checkpoint
    # names it here, so the re-run knows its completed milestones were preserved
    # and it is continuing from that checkpoint, not restarting the phase.
    checkpoint = ctx.record.resumed_from_checkpoint
    if checkpoint:
        parts.append(
            "\n\n--- recovery: resuming from an intra-phase checkpoint ---\n"
            "The previous attempt was interrupted and the worktree was rewound to "
            f"your last passing-test checkpoint commit: {checkpoint!r}. That "
            "milestone's work is committed and preserved; only the uncommitted "
            "edits after it were discarded. Continue the phase from that "
            "checkpoint — do not redo the committed milestones.\n"
        )
    for ref in input_refs:
        parts.append(_render_input(ref, ctx, artifacts))
    # FR-3.3: inject any open deferrals a prior phase pushed to THIS phase, so the
    # builder cannot silently drop the obligation. Placed after the plan-phase
    # excerpt (near the scoped context it relates to) and before the human-decision
    # history. No-op for a phase with no incoming deferral (the ordinary case).
    deferral_block = _render_open_deferrals(ctx)
    if deferral_block is not None:
        parts.append(deferral_block)
    # FR-5.3 (P7): inject measured phase-cost history + the size bound into the
    # plan-author input so phase sizing is grounded in observed costs, not blind.
    # No-op (None) for every non-plan-author step.
    history_block = _render_plan_author_history(step, ctx)
    if history_block is not None:
        parts.append(history_block)
    # FR-1 verbatim requirement (review F-001): the builder must receive the
    # human-decision history EXACTLY as recorded. The on-disk copy is written
    # through the RedactingWriter (credential-shaped substrings become
    # placeholders), so re-reading it for the prompt would feed the adapter a
    # non-verbatim, redacted version that also diverges from the manifest record.
    # Inject the unmodified rendered text directly; the redacted copy stays on
    # disk only for the audit trail. The history artifact is always inline (it is
    # never committed to the repo, so it has no readable path to reference).
    history_text = _write_human_response_artifact(ctx)
    if history_text is not None:
        parts.append(
            f"\n\n--- input artifact: {HUMAN_RESPONSE_ARTIFACT} ---\n{history_text}"
        )
    if ctx.iteration_item is not None:
        item = ctx.iteration_item
        rendered = item if isinstance(item, str) else json.dumps(item, indent=2)
        parts.append(f"\n\n--- foreach item [{ctx.iteration_index}] ---\n{rendered}")
    return "".join(parts)


def _artifact_path(name: str, ctx: StepContext, artifacts: dict) -> Path:
    """Resolve an input artifact to its on-disk path (produced path or default)."""
    return Path(artifacts.get(name) or (ctx.artifact_root / name))


def _repo_relative(path: Path, repo_root: Path) -> str:
    """The artifact's repo-relative POSIX path for a `reference` prompt (FR-1.1).

    Reference/phase paths are containment-validated at load, so the artifact is
    under the repo root; fall back to the bare name only for defensiveness.
    """
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.name


def _render_input(ref: InputRef, ctx: StepContext, artifacts: dict) -> str:
    """Render one input per its mode (FR-1.1): inline / reference / phase.

    * ``reference`` — inject the repo-relative path + a read-it instruction; the
      body never enters the prompt (the agent reads the file itself, FR-1.3).
    * ``phase`` — inject the current `foreach` phase's section of plan.md plus
      the full-document path; anything outside the phase is read on demand.
    * ``inline`` — today's behavior: embed the whole document body.
    """
    name = ref.name
    path = _artifact_path(name, ctx, artifacts)
    if ref.mode == INPUT_MODE_REFERENCE:
        rel = _repo_relative(path, ctx.repo_root)
        return (
            f"\n\n--- input artifact (by reference): {name} ---\n"
            f"Read this file yourself from the repository — it is provided by path, "
            f"not inlined, to keep this prompt small.\nPath: {rel}\n"
        )
    if ref.mode == INPUT_MODE_PHASE:
        rel = _repo_relative(path, ctx.repo_root)
        excerpt = _phase_excerpt(name, path, ctx)
        return (
            f"\n\n--- input artifact (current-phase excerpt): {name} ---\n"
            f"Below is only THIS phase's section of {name}. Read the full document "
            f"at the path for anything outside this phase.\nPath: {rel}\n\n{excerpt}\n"
        )
    content = path.read_text() if path.exists() else ""
    return f"\n\n--- input artifact: {name} ---\n{content}"


def _phase_excerpt(name: str, path: Path, ctx: StepContext) -> str:
    """The current `foreach` phase's section of plan.md — or fail closed.

    Fail closed (§2): P6 requires the implement prompt to carry THIS phase's plan
    section (FR-1.1), so a missing locatable section is a defect, not a
    degrade-and-continue. The plan validators (``plan_phases`` / ``phase_lint``)
    reject a plan whose phases lack locatable `## <id> …` headings before
    approval, so this raise is the last-ditch guard for a hand-built / bypassed
    plan; it halts the step rather than shipping an implement prompt that quietly
    omits its scoped context. Determinism: the slice is a pure heading scan
    (:func:`phase_section`), never a summary.
    """
    phase_id = _iteration_phase(ctx)
    text = path.read_text() if path.exists() else ""
    if phase_id and text:
        section = phase_section(text, phase_id)
        if section:
            return section
    raise ValueError(
        f"`phase`-mode context for {name}: no locatable section for phase "
        f"{phase_id or '?'} — the plan must carry a '## {phase_id or '<id>'} …' "
        "heading so this phase's excerpt can be sliced (FR-1.1). Fail closed "
        "rather than ship an implement prompt missing its scoped context."
    )


def render_human_responses(responses) -> str:
    """Render the full ordered human-decision history in the FR-4 block format.

    One block per recorded response, oldest first, under a single heading. Pure
    and derived: the manifest's ``human_responses`` array is the only source, so
    the rendered file is fully reconstructible and a stale on-disk copy is
    harmless (FR-4.1). Kept as a standalone function so it is unit-testable
    without driving a whole resume.
    """
    parts = ["# Human decisions (chronological)\n"]
    for r in responses:
        parts.append(
            f"## Response {r.response_id} — attempt {r.response_attempt}\n"
            f"Response: {r.response_text}\n"
            f"Timestamp: {r.timestamp}\n"
            f"User: {r.user}\n"
        )
    return "\n".join(parts)


def _write_human_response_artifact(ctx: StepContext) -> str | None:
    """Rebuild ``human-response.md`` from the manifest (FR-4); return it verbatim.

    Returns the rendered history text — the EXACT string the builder must
    receive (FR-1, review F-001) — or ``None`` when the step carries no recorded
    responses (the ordinary first-run / non-conflict case, where no block is
    injected). A *redacted* copy is also written under the step's log dir —
    inside the gitignored live run dir — for the audit trail: never committed as
    a step artifact, overwritten on the next resume, and harmless if left stale
    because it is fully derived from ``human_responses`` (FR-4.1). The returned
    value is the pre-redaction text so the invocation prompt stays verbatim even
    when a response contains credential-shaped substrings.
    """
    responses = ctx.record.human_responses
    if not responses:
        return None
    rendered = render_human_responses(responses)
    path = step_log_dir(ctx) / HUMAN_RESPONSE_ARTIFACT
    ctx.writer.write_text(path, rendered)  # redacted on disk; audit trail only
    return rendered


def _load_schema(step: Step, ctx: StepContext) -> dict | None:
    ref = step.get("findings_schema") or step.get("schema")
    if not ref:
        return None
    return json.loads((ctx.repo_root / ctx.config.asset_root / ref).read_text())


def _consuming_response(ctx: StepContext) -> bool:
    """True when this invocation is consuming a pending `--response` (FR-5/FR-10).

    The latest `human_responses` entry is ``pending`` only while a `--response`
    resume re-executes the parked step; ``Orchestrator._finalize`` flips it to
    ``consumed`` on the terminal outcome. Keying on that same discriminator means
    the schema binding and disposition mapping fire on exactly the invocations
    that carry a human decision — and never on an ordinary first run (no
    responses) or a non-conflict park.
    """
    responses = ctx.record.human_responses
    return bool(responses) and responses[-1].state == RESPONSE_PENDING


def _resume_disposition_schema(ctx: StepContext) -> dict:
    """Load the invocation-local resume-disposition schema (FR-10).

    Bound only while consuming a response, so the adapter validates the builder's
    disposition through the existing structured-output path without the approved
    pipeline definition ever gaining a ``schema:`` field (FR-4.1).
    """
    path = ctx.repo_root / ctx.config.asset_root / RESUME_DISPOSITION_SCHEMA
    return json.loads(path.read_text())


def _resume_disposition_result(
    agent_name, result, usage_by_agent, human_responses
) -> StepResult:
    """Map a builder's structured `disposition` to the step outcome (FR-3/FR-5).

    proceed_* → DONE (the run proceeds to commit); amendment_required /
    new_conflict → PARKED with ``parked_reason=response`` so P1's
    current-state ``_finalize`` records the re-park and the human is asked for the
    next decision (FR-3(b)/FR-10.4 gate). Fail closed (CLAUDE.md §2): a missing or
    unrecognized disposition is NEVER read as success — it fails the step rather
    than letting a malformed resume silently land work.

    Two semantic rules the schema cannot express are enforced here, both
    fail-closed (review F-001/F-003): the disposition must reference the consumed
    (pending) response and only known response_ids — a response-unaware result is
    rejected rather than allowed past the conflict gate — and an
    amendment_required must name a non-empty approved artifact (FR-3(b)).
    """
    structured = result.structured
    disposition = _disposition_value(structured)
    outcome = _DISPOSITION_OUTCOMES.get(disposition)
    if outcome is None:
        return _resume_failure(
            result,
            usage_by_agent,
            f"resume disposition missing or unrecognized ({disposition!r}); "
            "failing closed rather than advancing on an unparseable resume (FR-10)",
        )
    # FR-10 (BOOTSTRAP-NOTES #46): the conflict object-vs-null discriminator used
    # to be a top-level `allOf` in the schema, but the Anthropic API rejects a
    # top-level oneOf/allOf/anyOf in a structured-output input_schema, so the rule
    # moved engine-side. A re-park must carry a conflict object; a proceed must
    # carry a null conflict — fail closed on a mismatch the bound schema no longer
    # catches (the schema only constrains conflict to object-or-null).
    conflict_error = _conflict_shape_error(disposition, outcome[0], structured)
    if conflict_error is not None:
        return _resume_failure(result, usage_by_agent, conflict_error)
    # FR-1/FR-5/FR-10 (review F-001): the disposition must be a function of the
    # response it consumed. A result that omits the pending response, or names an
    # unknown/duplicate response_id, is response-unaware — fail closed rather than
    # let it pass the conflict gate.
    responses_error = _validate_responses_considered(structured, human_responses)
    if responses_error is not None:
        return _resume_failure(
            result,
            usage_by_agent,
            f"{responses_error}; failing closed rather than advancing on a "
            "response-unaware resume (FR-1/FR-5/FR-10)",
        )
    # FR-3(b) (review F-003): an amendment_required must name the approved
    # artifact it diverges from; a null/empty target is malformed → fail closed.
    if disposition == "amendment_required" and not _amendment_artifact(structured):
        return _resume_failure(
            result,
            usage_by_agent,
            "amendment_required disposition names no approved artifact "
            "(conflict.artifact null or empty); failing closed (FR-3(b))",
        )
    status, parked_reason = outcome
    return StepResult(
        status=status,
        session_id=result.session_id,
        usage=result.usage,
        usage_by_agent=usage_by_agent,
        parked_reason=parked_reason,
        notes=f"resume disposition: {disposition} (FR-3/FR-5/FR-10)",
    )


def _resume_failure(result, usage_by_agent, note: str) -> StepResult:
    """A fail-closed resume outcome (FR-10): FAILED, carrying the agent's cost.

    Stamps ``halt_reason=adapter_error`` (FR-7.2): the agent ran but emitted a
    missing/unrecognized/malformed disposition — a terminal output-contract
    failure, not a fail-closed precondition guard.
    """
    return StepResult(
        status=FAILED,
        halt_reason=HALT_REASON_ADAPTER_ERROR,
        session_id=result.session_id,
        usage=result.usage,
        usage_by_agent=usage_by_agent,
        notes=note,
    )


def _validate_responses_considered(structured, human_responses) -> str | None:
    """Check ``responses_considered`` against the recorded history; None if valid.

    Returns a short failure reason (review F-001) when the array is missing/
    malformed, names an unknown or duplicated response_id, or omits the consumed
    (pending) response — the latest ``human_responses`` entry, which is the one
    this invocation is processing. ``human_responses`` is non-empty here: this
    runs only while a pending response is being consumed.
    """
    considered = structured.get("responses_considered") if isinstance(structured, dict) else None
    if not isinstance(considered, list) or not all(isinstance(x, str) for x in considered):
        return "resume disposition carries no valid responses_considered list"
    known = {r.response_id for r in human_responses}
    pending_id = human_responses[-1].response_id  # the response being consumed
    seen: set[str] = set()
    for rid in considered:
        if rid in seen:
            return f"responses_considered repeats response id {rid!r}"
        seen.add(rid)
        if rid not in known:
            return f"responses_considered names unknown response id {rid!r}"
    if pending_id not in seen:
        return f"responses_considered omits the consumed response {pending_id!r}"
    return None


def _conflict_shape_error(disposition, status, structured) -> str | None:
    """Enforce the conflict object-vs-null discriminator (FR-10); None if valid.

    Re-park dispositions (amendment_required/new_conflict, ``status == PARKED``)
    must carry a non-null ``conflict`` object; proceed dispositions must carry a
    null/absent ``conflict``. This was a top-level ``allOf``/``if``/``then``/
    ``else`` in the schema until it had to move engine-side: the Anthropic API
    forbids a top-level ``oneOf``/``allOf``/``anyOf`` in the structured-output
    ``input_schema`` (BOOTSTRAP-NOTES #46). The bound schema still constrains
    ``conflict`` to object-or-null, so only the per-disposition rule is checked
    here, fail closed (CLAUDE.md §2).
    """
    conflict = structured.get("conflict") if isinstance(structured, dict) else None
    if status == PARKED:
        if not isinstance(conflict, dict):
            return (
                f"{disposition} disposition carries no conflict object "
                "(required when re-parking); failing closed (FR-10)"
            )
    elif conflict is not None:
        return (
            f"{disposition} disposition carries a non-null conflict "
            "(must be null when proceeding); failing closed (FR-10)"
        )
    return None


def _amendment_artifact(structured) -> bool:
    """True when ``conflict.artifact`` is a non-empty string (FR-3(b), F-003)."""
    conflict = structured.get("conflict") if isinstance(structured, dict) else None
    artifact = conflict.get("artifact") if isinstance(conflict, dict) else None
    return isinstance(artifact, str) and bool(artifact.strip())


def _disposition_value(structured) -> str | None:
    """Pull the `disposition` enum off the adapter's structured output, or None.

    The adapter already validated `structured` against the bound schema, so a
    well-formed resume carries a dict with a string `disposition`. Anything else
    (None, non-dict, missing key) returns None and the caller fails closed.
    """
    if isinstance(structured, dict):
        value = structured.get("disposition")
        return value if isinstance(value, str) else None
    return None


# --- commit (FR-9.2/9.7) -----------------------------------------------------
def handle_commit(step: Step, ctx: StepContext) -> StepResult:
    # The tree the phase was built in (P7a). The commit-message drafter already
    # reads this tree; the commit itself must land in the same one.
    repo = ctx.work_root
    # Narrow exclusion (review F-001): commit real artifacts (plan.md, outputs);
    # keep only the engine's own bookkeeping out of the commit and the checks.
    exclude = ctx.excludes
    # PRD §8 / appendix: a phase commit that lands after a `gauntlet resume
    # --response` must reference the human decision(s) it implements, linking the
    # committed code back to the ratifying response in git history. The consumed
    # responses are passed into message generation (so a drafted body can cite
    # them) AND an audit trailer is appended deterministically below — data over
    # inference: the linkage never depends on the drafter remembering to add it.
    consumed = _consumed_responses(step, ctx)
    # FR-11.1: the builder commits at each passing-test milestone as `P<N> wip:`.
    # Discover the trailing run of such checkpoint commits at the branch tip —
    # the set this phase commit collapses (squash) or lists in an empty marker
    # (keep). Empty when the builder made none (today's single-commit phase).
    #
    # Discovery is SCOPED to THIS phase's prefix (review F-001): an explicit
    # `phase:` wins, else the `foreach: plan.phases` iteration id (P1, P2…). The
    # scope keeps a wrong-phase `P<N> wip:` from being squashed into this phase
    # and fails closed if one sits in the trailing run. Only a numeric `P<N>`
    # prefix scopes; stage labels (PRD/PLAN/REVIEW) carry no checkpoints, so they
    # discover unscoped (matching nothing, as before). The walk is transparent to
    # engine bookkeeping commits so checkpoints preserved beneath a recovery
    # rewind are still found (review F-002).
    phase_prefix = step.get("phase") or _iteration_phase(ctx)
    wip_scope = (
        phase_prefix if phase_prefix and re.fullmatch(r"P\d+", phase_prefix) else None
    )
    try:
        wips = gitops.wip_checkpoints(repo, phase=wip_scope)
    except gitops.WrongPhaseCheckpointError as exc:
        return StepResult(status=FAILED, notes=f"checkpoint discovery failed closed: {exc}")
    squash = (
        ctx.config.checkpoint_commits == CHECKPOINT_COMMITS_SQUASH and bool(wips)
    )
    # The squash base is the parent of the OLDEST checkpoint: where the collapsed
    # `P<N>:` commit lands. Also the drafting diff base when checkpoints exist —
    # so the drafter sees the cumulative phase diff, not an empty residual tree.
    squash_base = gitops.commit_parent(repo, wips[-1][0]) if wips else None
    # Drafting side-notes (#134): "diff handed by reference", "header
    # bounded inline for tool-less drafting" — surfaced on the step record so
    # an operator reading `gauntlet status` sees WHY a commit message looks the
    # way it does (data over inference), never inferred from the message.
    draft_notes: list[str] = []
    message, draft_usage, draft_session, drafter = _commit_message(
        step, ctx, consumed, diff_base=(squash_base if wips else None),
        notes=draft_notes,
    )
    if consumed:
        message = _append_response_trailer(message, [r.response_id for r in consumed])
    if wips:
        # Chronological (oldest first) milestone list in the body — engine-
        # appended (data over inference) so the milestones are always recorded,
        # whether the drafter mentioned them or not.
        subjects = [subject for _sha, subject in reversed(wips)]
        message = _append_checkpoint_trailer(message, subjects, squashed=squash)
    usage_by_agent = {drafter: draft_usage} if draft_usage and drafter else {}
    err = validate_commit_message(message)
    if err is not None:
        # message_agent drafting includes a bounded redraft loop in _draft;
        # a literal/exhausted message that still fails is a hard error. The
        # failure names the operator's verbatim override (#134): every park
        # here used to cost a guess at what `--response` would do with the
        # text — it is used AS the message when it is itself a valid one.
        return StepResult(
            status=FAILED,
            usage=draft_usage,
            usage_by_agent=usage_by_agent,
            session_id=draft_session,
            notes=_join_notes(
                f"commit message invalid: {err.reason}. "
                f"Override: {commit_override_hint(ctx)}",
                draft_notes,
            ),
        )
    prefix = header_prefix(message)

    # Commit AUTHORSHIP is the implementer's, never the message drafter's
    # (FR-9.7, review F-003): a phase commit records the builder's work, so the
    # message_agent (typically `triage`) drafting the text must not bleed into
    # the commit identity — that mislabels implementation work as triage-
    # authored and breaks the builder/triage provenance split. An explicit
    # `agent:` on the commit step overrides; otherwise the builder authors it.
    agent_name = step.agent or "builder"
    identity = ctx.config.identity(agent_name)

    # SQUASH (FR-11.1): soft-reset to the squash base so every `wip:` change (and
    # any residual) stages together, then commit once. The reviewer handoff SHA
    # is a single non-empty `P<N>:` commit; the reviewed range diff base..<PN:>
    # is unchanged from the keep case (same base, same final tree).
    if squash:
        gitops.reset_soft(repo, squash_base)
        # The soft reset re-stages every commit in squash_base..old-HEAD, which
        # can include an engine bookkeeping commit swept in by a checkpoint-
        # preserving recovery (FR-11.2). Unstage the run-bookkeeping paths so the
        # collapsed `P<N>:` commit carries only implementation, never manifest/
        # RUN.md state (review F-002). `commit_all`'s own `--exclude` then leaves
        # them unstaged rather than re-adding them from the worktree.
        gitops.unstage(repo, exclude)
        sha = gitops.commit_all(repo, message, identity=identity, exclude=exclude)
        return StepResult(
            status=DONE, commit_sha=sha, commit_phase=prefix,
            usage=draft_usage, usage_by_agent=usage_by_agent,
            session_id=draft_session,
            notes=_join_notes(
                f"squashed {len(wips)} checkpoint(s) into {sha[:10]}", draft_notes
            ),
        )

    # Mid-commit resume reconciliation (review F-003): if a prior attempt
    # already created the commit (HEAD moved off the recorded base) but died
    # before recording the SHA, adopt that commit rather than double-committing.
    # A `wip:` commit is never adopted here — `header_prefix` returns None for a
    # `P<N> wip:` subject (no `:` immediately after the prefix), so only a real
    # `P<N>:` phase commit matches.
    base = ctx.record.base_sha
    if base and gitops.head_sha(repo) != base and gitops.is_clean(repo, exclude=exclude):
        existing = gitops.head_sha(repo)
        if header_prefix(gitops.commit_message(repo, existing)) == prefix:
            return StepResult(
                status=DONE,
                commit_sha=existing,
                commit_phase=prefix,
                usage=draft_usage,
                usage_by_agent=usage_by_agent,
                session_id=draft_session,
                notes="reconciled pre-existing commit after mid-commit interruption",
            )
        # The phase's `P<N>:` commit may sit BEHIND engine bookkeeping commits
        # (`gauntlet: response … consumed`, run-bookkeeping flushes) AND outside
        # this step's own `base..HEAD` window: when `resume` ADOPTS operator
        # commits that already carried the phase work — e.g. FR-9.3 clean-handoff
        # pre-commits made by an operator filling human evidence before the
        # reviewer handoff — the step's `base_sha` is re-anchored to the adopted
        # tip, which is PAST the `P<N>:` commit. HEAD is then a bookkeeping commit
        # and `base` is later still, so neither the HEAD match above nor a
        # `base..HEAD` scan sees it. Walk back from HEAD instead — but BOUNDED to
        # the run's own commits (`HEAD ^base_branch`): the `P<N>` prefix is only
        # unique WITHIN a run, and the base branch's pre-run history can hold a
        # same-prefix commit from an earlier run/PRD, which must never be adopted
        # as this phase's deliverable. A genuinely empty phase therefore still
        # falls through to the loud FAILED below, as does any walk the repo
        # cannot answer (missing base ref → fail closed, no adoption) (#124).
        # Adoption is skipped when `P<N> wip:` checkpoints exist: the KEEP branch
        # below must still land its empty `P<N>:` marker at the tip so the
        # checkpointed work is covered by the handoff commit.
        if not wips:
            try:
                candidates = gitops.commits_from_head(
                    repo, existing,
                    exclude_reachable_from=ctx.manifest.base_branch,
                )
            except gitops.GitError:
                candidates = []
            for sha, subject in candidates:
                if header_prefix(subject) != prefix:
                    continue
                try:
                    if gitops.is_ancestor(repo, sha, base):
                        # Restore `base_sha`'s meaning ("the state this attempt
                        # started from"): adoption re-anchored it AT or PAST the
                        # phase commit, which would hand review-diff consumers a
                        # reversed or empty `base..commit` range. The phase's
                        # work began at the adopted commit's parent.
                        ctx.record.base_sha = gitops.commit_parent(repo, sha)
                except gitops.GitError:
                    pass  # repair is best-effort; adoption itself stands
                return StepResult(
                    status=DONE,
                    commit_sha=sha,
                    commit_phase=prefix,
                    usage=draft_usage,
                    usage_by_agent=usage_by_agent,
                    session_id=draft_session,
                    notes=(
                        f"reconciled pre-existing {prefix}: commit {sha[:10]} "
                        "reachable from HEAD; worktree already clean"
                    ),
                )

    if gitops.is_clean(repo, exclude=exclude):
        if wips:
            # KEEP, no residual: the `wip:` commits already carry all of the
            # phase's work. The reviewer handoff must STILL land on a `P<N>:`
            # commit (git-history contract, CLAUDE.md §1), so record an explicit
            # empty marker (`--allow-empty`) whose body lists the milestones. The
            # range diff base..<marker> equals the cumulative `wip:` diff.
            sha = gitops.commit_all(
                repo, message, identity=identity, allow_empty=True, exclude=exclude
            )
            return StepResult(
                status=DONE, commit_sha=sha, commit_phase=prefix,
                usage=draft_usage, usage_by_agent=usage_by_agent,
                session_id=draft_session,
                notes=_join_notes(
                    f"empty P<N>: marker over {len(wips)} checkpoint(s): "
                    f"{sha[:10]}",
                    draft_notes,
                ),
            )
        return StepResult(
            status=FAILED,
            usage=draft_usage,
            usage_by_agent=usage_by_agent,
            session_id=draft_session,
            notes="commit step found a clean worktree with nothing to commit",
        )

    # KEEP with residual (or no checkpoints at all): commit the remaining work as
    # the `P<N>:` phase commit on top of any `wip:` commits.
    sha = gitops.commit_all(repo, message, identity=identity, exclude=exclude)
    return StepResult(
        status=DONE, commit_sha=sha, commit_phase=prefix,
        usage=draft_usage, usage_by_agent=usage_by_agent,
        session_id=draft_session,
        notes=_join_notes(f"committed {sha[:10]}", draft_notes),
    )


def _join_notes(primary: str, extra: list[str] | None) -> str:
    """One step-notes string: the outcome first, drafting side-notes after."""
    return "; ".join([primary, *(extra or [])])


def commit_override_hint(ctx) -> str:
    """The operator's verbatim commit-message override, spelled out (#134).

    Named in every terminal commit-format failure so the recovery is a
    copy-paste, not a guess: a `--response` that is itself a valid commit
    message is used AS the message (:func:`_commit_message`), with no redraft.
    """
    slug = getattr(getattr(ctx, "manifest", None), "slug", None) or "<slug>"
    return (
        f"`gauntlet resume {slug} --response '<full message>'` uses your text "
        f"verbatim when it is itself a valid commit message (P<N>: header "
        f"≤{HEADER_MAX} chars, blank line, body)"
    )


def _commit_message(
    step: Step, ctx: StepContext, consumed=(), *, diff_base=None, notes=None
):
    """Return ``(message, usage, session_id, drafter)``; usage/session/drafter
    are None for a literal message (no model call). ``notes`` (a list the
    caller owns) collects drafting side-notes for the step record (#134)."""
    literal = step.get("message")
    if literal:
        return literal, None, None, None  # human-authored YAML; still validated
    # Operator commit-recovery (FR-9.2 recovery): a commit step whose drafter
    # could not produce a legal header fails terminally with no re-draft lever.
    # `gauntlet resume --response` re-runs it with the human decision pending on
    # THIS step's record (not yet in `consumed`, which is prior-step CONSUMED
    # only). If that decision is itself a valid commit message, use it verbatim —
    # a deterministic override; otherwise fold it into the redraft as guidance.
    pending = _pending_response(ctx)
    if pending is not None:
        text = (pending.response_text or "").strip()
        if text and validate_commit_message(text) is None:
            return text, None, None, None
        consumed = list(consumed) + [pending]
    if notes is None:
        # Pre-#134 call shape (also what test doubles of the drafter accept).
        return _draft_commit_message(step, ctx, consumed, diff_base=diff_base)
    return _draft_commit_message(
        step, ctx, consumed, diff_base=diff_base, notes=notes
    )


def _pending_response(ctx: StepContext):
    """This step's own still-`pending` `--response` decision, if any (else None).

    The commit-recovery override reads it directly from the step record because
    :func:`_consumed_responses` returns CONSUMED entries only, and the decision
    being applied to a just-resumed commit step is still `pending` while the
    handler runs (finalize flips it to `consumed` on the terminal outcome)."""
    responses = ctx.record.human_responses
    if responses and responses[-1].state == RESPONSE_PENDING:
        return responses[-1]
    return None


# Headroom under an adapter's declared input cap for everything a prompt
# carries besides its main payload (template, plan section, response section)
# plus CLI envelope overhead. 64 KiB is deliberately generous: switching to
# by-reference a little early costs the agent a few git reads; switching late
# costs the whole invocation (`input_too_large`). Shared by the review /
# confirm prompt builders (cycle.py) and the commit-message drafter (#134).
PROMPT_INPUT_HEADROOM = 65_536

# The commit-message drafter's inline-diff ceiling when the drafter's adapter
# declares NO input cap (#134). Fail closed against the unknown: a claude-code
# drafter has no declared cap, yet a phase that minted a few MB of model files
# (a ~2.5M-token diff, observed live) fails `phase-commit` terminally on every
# model when the whole diff is inlined. Above this many chars the diff goes by
# reference regardless of what the adapter would accept.
DRAFT_INLINE_DIFF_MAX = 400_000


def profile_input_cap(ctx: StepContext, profile: str) -> tuple[int | None, bool]:
    """``(max_input_chars, reads_repo)`` declared by ``profile``'s adapter class.

    THE capability path every by-reference switch resolves through (review and
    confirm panels via ``cycle._panel_input_cap``; the commit-message drafter,
    #134): the profile's configured adapter CLASS, not a built instance — a
    test double injected via ``adapter_factory`` has no config profile, and an
    unresolvable profile means ``(None, False)``: unknown cap, no repo access.
    """
    try:
        capabilities = ctx.config.profile(profile).adapter_class().capabilities
    except Exception:
        return None, False
    return capabilities.max_input_chars, bool(capabilities.reads_repo)


def _draft_commit_message(
    step: Step, ctx: StepContext, consumed=(), *, diff_base=None, notes=None
):
    """Draft a commit message via the message_agent with bounded redraft.

    The agent sees the change as data — both the tracked diff AND the untracked
    files `git add -A` will sweep in (review F-008: a new-file phase otherwise
    drafts from an empty diff) — plus an optional plan section and, after a
    `--response` resume, the human decision(s) being implemented (PRD §8). The
    engine validates the format and asks for a redraft on violation (FR-9.2).
    Returns ``(message, usage, session_id, drafter)`` so the commit step records
    the drafter's cost (FR-3.2/§7).

    Oversize changes use git references only for adapters that can read the
    repository. Tool-less adapters receive bounded, explicitly partial diff
    excerpts. Invalid drafts exhaust the ordinary redraft loop and fail closed.
    """
    agent_name = step.get("message_agent")
    if not agent_name:
        raise ValueError("commit step needs either `message:` or `message_agent:`")
    if notes is None:
        notes = []
    adapter = ctx.build_adapter(agent_name)
    base_prompt = (
        (ctx.repo_root / ctx.config.asset_root / step.get("prompt")).read_text()
        if step.get("prompt")
        else _DEFAULT_COMMIT_PROMPT
    )
    # Phase prefix: an explicit `phase:` wins; otherwise, inside the
    # `foreach: plan.phases` fan-out, the iteration's phase id (P1, P2…) is the
    # required prefix, so each phase commit is labelled from the plan, not
    # left for the drafter to guess (FR-5.1 / FR-9.2).
    phase_hint = step.get("phase") or _iteration_phase(ctx)
    plan_section = _plan_section(step, ctx)
    header = (
        f"{base_prompt}\n\nRequired header phase prefix: {phase_hint or '(infer PN)'}\n"
        f"{plan_section}{_response_section(consumed)}"
    )
    change, diff_len = _change_context(ctx, diff_base=diff_base)
    cap, reads_repo = profile_input_cap(ctx, agent_name)
    if cap is not None:
        # Same rule as the review/confirm prompts: an over-cap prompt is
        # rejected WHOLESALE by the adapter, so never build one.
        oversize = len(header) + len(change) > cap - PROMPT_INPUT_HEADROOM
        why = f"{diff_len} chars vs the {cap}-char input limit"
    else:
        # Unknown cap: fail closed on the diff alone (see DRAFT_INLINE_DIFF_MAX).
        oversize = diff_len > DRAFT_INLINE_DIFF_MAX
        why = (
            f"{diff_len} chars with no declared input limit; inline ceiling "
            f"{DRAFT_INLINE_DIFF_MAX} chars"
        )
    if oversize:
        if reads_repo:
            change = _change_context_by_reference(ctx, diff_base=diff_base, why=why)
            notes.append(f"diff handed to the drafter by reference ({why})")
        else:
            budget = min(40_000, cap - PROMPT_INPUT_HEADROOM - len(header) - 2048) if cap else 40_000
            if budget < 1024:
                raise ValueError("commit drafting instructions leave no room for change evidence")
            change = _bounded_draft_evidence(change, budget=budget)
            notes.append(f"drafter received bounded inline diff excerpts ({why})")
    prompt = f"{header}\n{change}\n"
    max_redrafts = max(0, int(step.get("max_redrafts", 2)))
    message = ""
    err = None
    usage = _UsageAccumulator()  # sum across ALL draft attempts, incl. rejected
    session_id = None
    for _attempt in range(1 + max_redrafts):
        # The commit-message drafter reads the staged diff of the tree the
        # phase was built in (P7a).
        with record_invocation(
            ctx, agent=agent_name, label="commit-message", adapter=adapter
        ):
            result = adapter.run(prompt, cwd=ctx.work_root)
        usage.add(result.usage)  # a redraft's cost is real spend (F-008 round 2)
        session_id = result.session_id
        message = result.text.strip()
        err = validate_commit_message(message)
        if err is None:
            return message, usage.result(), session_id, agent_name
        # Echo the offending header WITH its exact count (#134): "header is 78
        # chars" alone left models re-submitting 74; the line they wrote and
        # the prefix-inclusive rule make the fix arithmetic, not a guess.
        offending = message.split("\n", 1)[0]
        prompt = (
            f"{header}\n\nYour previous draft was rejected: {err.reason}. "
            f"Its header line was {offending!r} — {len(offending)} characters; "
            f"the limit is {HEADER_MAX} counting the 'P<N>: ' prefix. "
            f"Return only the corrected commit message.\n{change}\n"
        )
    return message, usage.result(), session_id, agent_name


def _bounded_draft_evidence(change: str, *, budget: int) -> str:
    """Distribute a fixed evidence budget across files for tool-less drafting.

    This is message drafting, not code review: omitted evidence is explicit,
    and the model must limit its claims to the visible excerpts and phase plan.
    """
    parts = re.split(r"(?=^diff --git )", change, flags=re.MULTILINE)
    header = (
        "--- PARTIAL DIFF EXCERPTS (bounded inline evidence) ---\n"
        "You have no repository tools. These excerpts omit content. Use only "
        "the visible evidence and phase plan; do not claim to have inspected "
        "omitted changes.\n"
    )
    allowance = max(1, (budget - len(header) - 128) // len(parts))
    excerpts = []
    omitted = 0
    for part in parts:
        if len(part) > allowance:
            omitted += len(part) - allowance
            part = part[:allowance]
        excerpts.append(part)
    result = header + "\n".join(excerpts)
    suffix = f"\n[omitted {omitted} source characters across {len(parts)} sections]\n"
    return result[:budget - len(suffix)] + suffix


def _iteration_phase(ctx: StepContext) -> str:
    """The phase id (P1, P2…) of the current foreach item, if it carries one."""
    item = ctx.iteration_item
    if isinstance(item, dict):
        return str(item.get("id", "") or "")
    return ""


class _UsageAccumulator:
    """Sum Usage across calls so rejected drafts / sub-agent calls still count.

    Optionally tracks a per-agent breakdown (FR-3.2): pass ``agent=`` to
    :meth:`add` and the cycle's grand total and its per-profile split fall out
    of one accumulator (F-008 for redraft sums; per-agent for `gauntlet report`).
    """

    def __init__(self) -> None:
        self._in = 0
        self._out = 0
        self._cached = 0
        self._cache_w = 0
        self._reasoning = 0
        self._cost: float | None = None
        self._seen = False
        self._by_agent: dict[str, _UsageAccumulator] = {}

    def add(self, usage, *, agent: str | None = None) -> None:
        if usage is None:
            return
        self._seen = True
        self._in += usage.input_tokens or 0
        self._out += usage.output_tokens or 0
        self._cached += usage.cached_input_tokens or 0
        self._cache_w += getattr(usage, "cache_creation_input_tokens", None) or 0
        self._reasoning += getattr(usage, "reasoning_output_tokens", None) or 0
        if usage.cost_usd is not None:
            self._cost = (self._cost or 0.0) + usage.cost_usd
        if agent is not None:
            self._by_agent.setdefault(agent, _UsageAccumulator()).add(usage)

    def result(self):
        from gauntlet.adapters.base import Usage

        if not self._seen:
            return None
        return Usage(
            input_tokens=self._in,
            output_tokens=self._out,
            cached_input_tokens=self._cached,
            cost_usd=self._cost,
            cache_creation_input_tokens=self._cache_w or None,
            reasoning_output_tokens=self._reasoning or None,
        )

    def merge(self, other: "_UsageAccumulator") -> None:
        """Fold another accumulator's totals + per-agent split into this one.

        Concurrent triage (FR-9.1) gives each per-finding call its OWN
        accumulator (``_UsageAccumulator.add`` is not thread-safe), then merges
        them back into the round accumulator in a deterministic finding order —
        so the grand total and per-profile split are identical whether triage ran
        sequentially or concurrently. A failed call's partial spend is merged too
        (its accumulator carries the partial usage `_run_sub` recorded), keeping
        the F-008 "failed attempts still count" property under concurrency."""
        if not other._seen:
            return
        self._seen = True
        self._in += other._in
        self._out += other._out
        self._cached += other._cached
        self._cache_w += other._cache_w
        self._reasoning += other._reasoning
        if other._cost is not None:
            self._cost = (self._cost or 0.0) + other._cost
        for name, acc in other._by_agent.items():
            self._by_agent.setdefault(name, _UsageAccumulator()).merge(acc)

    def by_agent(self) -> dict:
        """Per-agent-profile Usage (FR-3.2); empty when no agent was tagged."""
        out = {}
        for name, acc in self._by_agent.items():
            r = acc.result()
            if r is not None:
                out[name] = r
        return out


def _change_context(ctx: StepContext, *, diff_base=None) -> tuple[str, int]:
    """The change a commit is about to record, as data for the drafter (F-008),
    and the raw diff's length (the by-reference switch's input, #134).

    Normally the tracked diff vs HEAD plus the untracked files staging will add.
    When the phase already landed `P<N> wip:` checkpoint commits (FR-11.1), the
    tip may be clean while the real change is the cumulative diff since the phase
    base — pass ``diff_base`` (the squash base) so the drafted message reflects
    the whole phase, not an empty residual tree.
    """
    repo = ctx.work_root
    if diff_base:
        diff = gitops.diff_worktree_vs(repo, diff_base, exclude=ctx.excludes)
        label = f"diff (tracked, vs phase base {diff_base[:10]})"
    else:
        diff = gitops.diff_head(repo, exclude=ctx.excludes)
        label = "diff (tracked, vs HEAD)"
    status = gitops.status_porcelain(repo, exclude=ctx.excludes)
    return (
        f"--- git status (incl. untracked) ---\n{status}\n"
        f"\n--- {label} ---\n{diff}"
    ), len(diff)


def _change_context_by_reference(ctx: StepContext, *, diff_base=None, why: str) -> str:
    """:func:`_change_context` with the diff BY REFERENCE (#134).

    The status (incl. untracked files) and the ``diff --stat`` change map stay
    inline; the hunks do not. The drafter runs inside the worktree and reads
    the per-file diffs it needs with its own git — the same transport the
    review and confirm prompts use for an oversize range (cycle.py), and like
    them never a truncation: a clipped diff would silently narrow what the
    message claims to describe.
    """
    repo = ctx.work_root
    base = diff_base or "HEAD"
    label = (
        f"vs phase base {diff_base[:10]}" if diff_base else "vs HEAD"
    )
    status = gitops.status_porcelain(repo, exclude=ctx.excludes)
    stat = gitops.diff_stat(repo, base, exclude=ctx.excludes)
    return (
        f"--- git status (incl. untracked) ---\n{status}\n"
        f"\n--- diff --stat (tracked, {label}) ---\n{stat}\n"
        f"\n--- diff (tracked, {label}): BY REFERENCE — too large to inline "
        f"({why}) ---\n"
        f"You are running inside the repository worktree. Read the change "
        f"yourself with git (read-only), e.g.:\n"
        f"  git diff --stat {base}      # the change map — shown above\n"
        f"  git diff {base} -- <path>   # per-file, for the files the message "
        f"must explain\n"
        f"Untracked files (`??` in the status) are not in the diff; read them "
        f"directly. Draft for the ENTIRE change exactly as if the diff were "
        f"inlined here, and keep the body free of the diff itself.\n"
    )


def _consumed_responses(step: Step, ctx: StepContext) -> list:
    """The `--response` decisions consumed in this commit's stage (PRD §8).

    A phase commit follows the agent_task(s) it commits the work of; scope the
    audit linkage to the stage containing this commit step, matching iteration so
    a `foreach: plan.phases` fan-out references only its own phase's responses.
    Consumed-state only — a still-`pending` entry has no committed outcome yet.
    Returns the `HumanResponse` entries in execution order (oldest first).
    """
    stage = next(
        (s for s in ctx.pipeline.stages if any(st.id == step.id for st in s.steps)),
        None,
    )
    if stage is None:
        return []
    consumed: list = []
    for st in stage.steps:
        rec = ctx.manifest.record(st.id, ctx.record.iteration)
        if rec is None:
            continue
        consumed.extend(
            r for r in rec.human_responses if r.state == RESPONSE_CONSUMED
        )
    return consumed


def _append_response_trailer(message: str, response_ids: list[str]) -> str:
    """Append the consumed-response audit trailer to a phase commit body (PRD §8).

    Engine-appended (not left to the message drafter) so the link from the
    committed code to the ratifying human decision is deterministic and always
    present — fail closed, data over inference. A git-trailer-shaped line keeps
    the reference machine-greppable in history.
    """
    body = message.rstrip("\n")
    return f"{body}\n\nGauntlet-Response: {', '.join(response_ids)}\n"


def _append_checkpoint_trailer(
    message: str, wip_subjects: list[str], *, squashed: bool
) -> str:
    """List the intra-phase checkpoint milestones in the phase commit body (FR-11.1).

    Engine-appended (not left to the message drafter), so the milestones a phase
    was built from are always recorded — in the squash case where the `wip:`
    commits are collapsed and their subjects would otherwise be lost, and in the
    keep/empty-marker case where the marker commit summarizes the phase. Data
    over inference; keeps the git-history contract auditable from the `P<N>:`
    commit alone.
    """
    body = message.rstrip("\n")
    label = (
        "Squashed checkpoint milestones:"
        if squashed
        else "Checkpoint milestones:"
    )
    lines = "\n".join(f"- {subject}" for subject in wip_subjects)
    return f"{body}\n\n{label}\n{lines}\n"


def _response_section(consumed) -> str:
    """An optional commit-draft section naming the human decision(s) implemented.

    Lists only the response_id(s), not the verbatim response text: the text may
    be credential-shaped (it reaches the builder verbatim but the on-disk audit
    copy is redacted), and it must not bleed into a commit message. Gives the
    message_agent enough to cite the decision; the audit link itself is
    guaranteed by the engine-appended trailer regardless of drafting.
    """
    if not consumed:
        return ""
    ids = ", ".join(r.response_id for r in consumed)
    return (
        "\n--- human decision(s) this commit implements ---\n"
        f"This commit lands work directed by `gauntlet resume --response`. "
        f"Reference the consumed response id(s) in the body: {ids}\n"
    )


def _plan_section(step: Step, ctx: StepContext) -> str:
    """Optional plan excerpt the message_agent drafts from (FR-9.2)."""
    ref = step.get("plan_section")
    if not ref:
        return ""
    path = ctx.artifacts.get(ref) or (ctx.artifact_root / ref)
    if Path(path).exists():
        return f"\n--- plan section: {ref} ---\n{Path(path).read_text()}\n"
    return ""


_DEFAULT_COMMIT_PROMPT = (
    "Draft a git commit message for the staged changes. Line 1: an imperative "
    "header prefixed with the phase, e.g. 'P3: <summary>', under 72 characters. "
    "Then a blank line, then a body explaining what changed and why, the plan "
    "assumption validated, and relevant FR references."
)
# The prompt asks for < 72 (the git convention) while the engine enforces
# HEADER_MAX (100) — the gap is deliberate (#142): a drafter told the exact
# enforced limit aims at it and lands a few characters over, burning every
# redraft and parking the run over a cosmetic rule.


# --- helpers -----------------------------------------------------------------
def _proc_log(command: str, proc: subprocess.CompletedProcess) -> str:
    return (
        f"$ {command}\n--- exit {proc.returncode} ---\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
    )


def step_log_dir(ctx: StepContext) -> Path:
    iteration = ctx.record.iteration
    leaf = ctx.record.id if iteration is None else f"{ctx.record.id}.{iteration}"
    return ctx.steps_dir() / leaf


def step_logger(ctx: StepContext, *subdir: str) -> StepLogger:
    """FR-4 logger for this step (or a sub-step, e.g. a cycle round's review)."""
    return StepLogger(ctx.writer, step_log_dir(ctx).joinpath(*subdir))


def open_step_stream(ctx: StepContext, adapter, logger: StepLogger, *, suffix: str = ""):
    """Open a live ``events<suffix>.jsonl`` stream, or return ``None`` for the
    buffered path (live-run-observability FR-2/FR-6.1).

    ``suffix`` isolates a repair re-invocation's stream (FR-2.1) so it never
    truncates the initial attempt's events file.

    Returns a :class:`StepStream` (whose ``append_line`` is threaded into
    ``adapter.run`` as the per-line sink) only when **both** the run-level flag
    is on **and** the bound adapter declares itself line-streamable for its
    current output mode (``streams_to_sink``). Gating on the adapter's own
    qualification honors FR-2.8: a non-qualified adapter (or the API adapter,
    which has no such method) never opens a stream at all — no file is created
    and no sink is passed, so the buffered path runs exactly as today (FR-6.1).
    """
    if not getattr(ctx.config, "stream_step_output", False):
        return None
    streams = getattr(adapter, "streams_to_sink", None)
    if not callable(streams) or not streams():
        return None
    return logger.open_stream(suffix=suffix)


def _write_step_log(ctx: StepContext, name: str, text: str) -> None:
    ctx.writer.write_text(step_log_dir(ctx) / name, text)


SPECS: dict[str, StepSpec] = {
    "agent_task": StepSpec(
        type="agent_task",
        handler=handle_agent_task,
        needs_agent=True,
        # repo_write / touches_worktree are decided per-step (default True)
    ),
    "shell": StepSpec(
        type="shell",
        handler=handle_shell,
        touches_worktree=True,  # a test/build step can mutate the tree
    ),
    "human_gate": StepSpec(
        type="human_gate",
        handler=handle_human_gate,
    ),
    "phase_lint": StepSpec(
        type="phase_lint",
        handler=handle_phase_lint,  # read-only: parses plan.md, touches nothing
    ),
    "acceptance_gate": StepSpec(
        type="acceptance_gate",
        handler=handle_acceptance_gate,  # deterministic: reads the map, enumerates
    ),
    "commit": StepSpec(
        type="commit",
        handler=handle_commit,
        touches_worktree=True,
    ),
}


def _register_builtins() -> None:
    # Imported at the bottom: cycle.py / retro.py use this module's helpers
    # lazily, but registering here keeps adversarial_cycle and retrospective
    # built-ins (PRD §4.1 v1 step set).
    from gauntlet.engine.cycle import SPEC as _CYCLE_SPEC
    from gauntlet.engine.retro import SPEC as _RETRO_SPEC

    for spec in (_CYCLE_SPEC, _RETRO_SPEC):
        SPECS[spec.type] = spec


_register_builtins()

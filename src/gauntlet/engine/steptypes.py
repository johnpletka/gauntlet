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

import json
import re
import subprocess
from pathlib import Path

from gauntlet.adapters.base import AdapterError
from gauntlet.engine.commit_format import header_prefix, validate_commit_message
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
from gauntlet.engine.manifest import (
    PARKED_REASON_UPSTREAM_CONFLICT,
    RESPONSE_CONSUMED,
    RESPONSE_PENDING,
)
from gauntlet.engine.pipeline import Step
from gauntlet.engine.planphases import PlanPhasesError, extract_phases
from gauntlet.logging.transcript import StepLogger

_CONFIG_TOKEN_RE = re.compile(r"\{\{\s*config\.([a-zA-Z0-9_]+)\s*\}\}")
_ANY_TOKEN_RE = re.compile(r"\{\{.*?\}\}")

# Canonical FR-10.4 halt marker. Only a `halt_on:` whose value is *exactly* this
# marker sets the conflict-park discriminator (FR-2.1); a step configured with a
# different `halt_on:` marker parks with `parked_reason` unset. Pipelines use
# this string verbatim (pipelines/standard.yaml `halt_on: "UPSTREAM CONFLICT"`).
UPSTREAM_CONFLICT_MARKER = "UPSTREAM CONFLICT"

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
# re-park it for a human (parked_reason=upstream_conflict, the FR-10.4 gate). This
# structured signal — not the textual UPSTREAM CONFLICT marker — is authoritative
# once a response is being consumed (the marker is only the FIRST-conflict signal,
# before any response exists).
_DISPOSITION_OUTCOMES: dict[str, tuple[str, str | None]] = {
    "proceed_in_place": (DONE, None),
    "proceed_with_deviation": (DONE, None),
    "amendment_required": (PARKED, PARKED_REASON_UPSTREAM_CONFLICT),
    "new_conflict": (PARKED, PARKED_REASON_UPSTREAM_CONFLICT),
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


def handle_shell(step: Step, ctx: StepContext) -> StepResult:
    template = step.get("run")
    if not template:
        return StepResult(status=FAILED, notes="shell step has no `run:` command")
    command = render_shell_command(template, ctx.config)
    timeout = step.timeout_s  # per-step guard (FR-3.3); None => unbounded
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=ctx.repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _write_step_log(ctx, "output.txt", f"$ {command}\n--- TIMEOUT after {timeout}s ---\n")
        # Halt at a checkpoint rather than letting a stuck command burn on.
        return StepResult(
            status=HALTED,
            notes=f"shell timeout halt (FR-3.3): `{command}` exceeded {timeout}s",
        )
    _write_step_log(ctx, "output.txt", _proc_log(command, proc))
    if proc.returncode != 0:
        return StepResult(
            status=FAILED,
            notes=f"`{command}` exited {proc.returncode}",
        )
    return StepResult(status=DONE, notes=f"`{command}` exited 0")


# --- human_gate --------------------------------------------------------------
def handle_human_gate(step: Step, ctx: StepContext) -> StepResult:
    show = step.get("show", []) or []
    return StepResult(
        status=PARKED,
        notes=f"awaiting human decision; review: {', '.join(show) or '(nothing listed)'}",
    )


# --- phase_lint --------------------------------------------------------------
def handle_phase_lint(step: Step, ctx: StepContext) -> StepResult:
    """Structurally validate plan.md's ``gauntlet-phases`` block at the plan gate.

    The plan-cycle reviewer reads plan.md as *prose* — it never parses the
    fenced ``gauntlet-phases`` block — so a structurally broken block (e.g. an
    unquoted ``schema:`` colon that YAML reads as a nested mapping) sails through
    review and only detonates later, when the phases stage tries to fan out over
    ``plan.phases`` and :func:`load_plan_phases` raises. This deterministic,
    no-agent check closes that gap: it runs the *same* parser the foreach uses,
    so a plan can only pass the gate if the engine can actually execute it.

    Fail closed (CLAUDE.md §2): a missing/empty/malformed block HALTS — which
    parks the run for a human (HALTED -> RUN_PARKED) with the precise reason —
    rather than letting a known-unrunnable plan reach human approval.
    """
    artifact = step.get("artifact", "plan.md")
    path = ctx.artifact_root / artifact
    if not path.exists():
        return StepResult(
            status=HALTED,
            notes=f"phase lint: {artifact} is missing at the plan gate",
        )
    try:
        phases = extract_phases(path.read_text())
    except PlanPhasesError as exc:
        return StepResult(
            status=HALTED,
            notes=f"phase lint: {artifact} gauntlet-phases block is invalid — {exc}",
        )
    if not phases:
        return StepResult(
            status=HALTED,
            notes=(
                f"phase lint: {artifact} declares no gauntlet-phases block; the "
                "phases stage would have nothing to fan out over (FR-5.1)"
            ),
        )
    ids = ", ".join(p["id"] for p in phases)
    return StepResult(
        status=DONE, notes=f"phase lint: {len(phases)} phase(s) valid ({ids})"
    )


# --- agent_task --------------------------------------------------------------
def handle_agent_task(step: Step, ctx: StepContext) -> StepResult:
    agent_name = step.agent
    if not agent_name:
        return StepResult(status=FAILED, notes="agent_task step has no `agent:`")
    adapter = ctx.build_adapter(agent_name)
    prompt = _render_prompt(step, ctx)
    # FR-10: while this invocation is consuming a pending `--response`, bind the
    # resume-disposition schema invocation-locally and let the structured
    # disposition drive the outcome — without touching the approved pipeline.
    consuming_response = _consuming_response(ctx)
    schema = (
        _resume_disposition_schema(ctx)
        if consuming_response
        else _load_schema(step, ctx)
    )
    # Per-step timeout overrides the profile's step_timeout_s, which overrides
    # the adapter default (FR-3.3). A timeout raises AgentTimeoutError, which the
    # orchestrator turns into a HALTED checkpoint.
    timeout = step.timeout_s
    if timeout is None and agent_name in ctx.config.agents:
        timeout = ctx.config.profile(agent_name).step_timeout_s
    if timeout is not None and hasattr(adapter, "timeout_s"):
        adapter.timeout_s = timeout
    logger = step_logger(ctx)
    logger.log_prompt(prompt)  # before the call: the prompt survives a crash
    try:
        result = adapter.run(
            prompt,
            session=ctx.record.session_id,
            schema=schema,
            cwd=ctx.repo_root,
        )
    except AdapterError as exc:
        # FR-4.2 is lossless for failures too (P4.r1 F-007): persist whatever
        # partial evidence the adapter salvaged before the orchestrator
        # classifies the error.
        if exc.partial is not None:
            logger.log_result(exc.partial, suffix="-failed")
        logger.log_text("failure.txt", str(exc))
        raise
    logger.log_result(result)  # transcript.md + events.jsonl (+ structured)
    usage_by_agent = {agent_name: result.usage} if result.usage else {}

    # FR-3/FR-5/FR-10: on a `--response` resume the builder's STRUCTURED
    # disposition is authoritative for the outcome, not the textual `halt_on`
    # marker (which only signals the FIRST conflict, before any response). Map it
    # to the step status here so a schema-valid `new_conflict` re-parks instead of
    # being marked DONE; the FR-3.0 classification itself lives in the prompt.
    if consuming_response:
        outcome = _resume_disposition_result(
            agent_name, result, usage_by_agent, ctx.record.human_responses
        )
        # A re-park (amendment_required/new_conflict) or a fail-closed disposition
        # lands nothing: return immediately, skipping completion-signal handling
        # and the `output:` artifact write (review F-004 — a re-park must not
        # produce the step's declared artifact).
        if outcome.status != DONE:
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
        status, note, parked_reason = signal
        return StepResult(
            status=status, session_id=result.session_id, usage=result.usage,
            usage_by_agent=usage_by_agent, notes=note,
            parked_reason=parked_reason,
        )

    artifact_writes: dict[str, Path] = {}
    output = step.get("output")
    if output:
        out_path = ctx.artifact_root / output
        ctx.writer.write_text(out_path, result.text)
        artifact_writes[output] = out_path
    return StepResult(
        status=DONE,
        session_id=result.session_id,
        usage=result.usage,
        usage_by_agent=usage_by_agent,
        artifact_writes=artifact_writes,
        notes=f"agent {agent_name!r} completed",
    )


def _completion_signal(step: Step, text: str, *, check_halt: bool = True):
    """Read an agent_task's final output for a halt/completion contract (#32).

    Returns ``None`` to proceed normally, or ``(status, note, parked_reason)`` to
    short-circuit. Both checks are opt-in (absent keys → no contract), so
    existing steps and the document-authoring tasks keep their plain exit-code
    semantics.

    ``parked_reason`` is ``PARKED_REASON_UPSTREAM_CONFLICT`` only when the matched
    ``halt_on`` marker is *exactly* the canonical :data:`UPSTREAM_CONFLICT_MARKER`
    (FR-2.1) — a step parking on a *different* ``halt_on`` marker, or failing on a
    missing ``require_signal``, carries no ``parked_reason``.

    ``check_halt=False`` suppresses only the ``halt_on`` check (review F-004): on a
    proceed-disposition `--response` resume the textual UPSTREAM CONFLICT marker is
    obsolete — the structured disposition already governed the conflict — but
    ``require_signal`` still binds, so the completion contract is preserved.
    """
    halt_on = step.get("halt_on")
    if check_halt and halt_on and _marker_signalled(halt_on, text):
        parked_reason = (
            PARKED_REASON_UPSTREAM_CONFLICT
            if halt_on == UPSTREAM_CONFLICT_MARKER
            else None
        )
        return PARKED, (
            f"agent signalled {halt_on!r} (FR-10.4 upstream conflict / halt); "
            "parked for a human instead of marking the step done (#32)"
        ), parked_reason
    require = step.get("require_signal")
    if require and not _marker_signalled(require, text):
        return FAILED, (
            f"agent did not emit the required completion signal {require!r}; "
            "failing closed rather than advancing on a silent non-completion (#32)"
        ), None
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
    inputs = list(step.get("inputs", []) or [])
    artifacts = dict(ctx.artifacts)
    # FR-1 verbatim requirement (review F-001): the builder must receive the
    # human-decision history EXACTLY as recorded. The on-disk copy is written
    # through the RedactingWriter (credential-shaped substrings become
    # placeholders), so re-reading it for the prompt would feed the adapter a
    # non-verbatim, redacted version that also diverges from the manifest record.
    # Inject the unmodified rendered text directly; the redacted copy stays on
    # disk only for the audit trail.
    history_text = _write_human_response_artifact(ctx)
    verbatim: dict[str, str] = {}
    if history_text is not None:
        inputs.append(HUMAN_RESPONSE_ARTIFACT)
        verbatim[HUMAN_RESPONSE_ARTIFACT] = history_text
    parts = [base]
    for name in inputs:
        if name in verbatim:
            content = verbatim[name]
        else:
            path = artifacts.get(name) or (ctx.artifact_root / name)
            content = Path(path).read_text() if Path(path).exists() else ""
        parts.append(f"\n\n--- input artifact: {name} ---\n{content}")
    if ctx.iteration_item is not None:
        item = ctx.iteration_item
        rendered = item if isinstance(item, str) else json.dumps(item, indent=2)
        parts.append(f"\n\n--- foreach item [{ctx.iteration_index}] ---\n{rendered}")
    return "".join(parts)


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
    new_conflict → PARKED with ``parked_reason=upstream_conflict`` so P1's
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
    """A fail-closed resume outcome (FR-10): FAILED, carrying the agent's cost."""
    return StepResult(
        status=FAILED,
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
    repo = ctx.repo_root
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
    message, draft_usage, draft_session, drafter = _commit_message(step, ctx, consumed)
    if consumed:
        message = _append_response_trailer(message, [r.response_id for r in consumed])
    usage_by_agent = {drafter: draft_usage} if draft_usage and drafter else {}
    err = validate_commit_message(message)
    if err is not None:
        # message_agent drafting includes a bounded redraft loop in _draft;
        # a literal/exhausted message that still fails is a hard error.
        return StepResult(
            status=FAILED,
            usage=draft_usage,
            usage_by_agent=usage_by_agent,
            session_id=draft_session,
            notes=f"commit message invalid: {err.reason}",
        )
    prefix = header_prefix(message)

    # Mid-commit resume reconciliation (review F-003): if a prior attempt
    # already created the commit (HEAD moved off the recorded base) but died
    # before recording the SHA, adopt that commit rather than double-committing.
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

    if gitops.is_clean(repo, exclude=exclude):
        return StepResult(
            status=FAILED,
            usage=draft_usage,
            usage_by_agent=usage_by_agent,
            session_id=draft_session,
            notes="commit step found a clean worktree with nothing to commit",
        )

    # Commit AUTHORSHIP is the implementer's, never the message drafter's
    # (FR-9.7, review F-003): a phase commit records the builder's work, so the
    # message_agent (typically `triage`) drafting the text must not bleed into
    # the commit identity — that mislabels implementation work as triage-
    # authored and breaks the builder/triage provenance split. An explicit
    # `agent:` on the commit step overrides; otherwise the builder authors it.
    agent_name = step.agent or "builder"
    identity = ctx.config.identity(agent_name)
    sha = gitops.commit_all(repo, message, identity=identity, exclude=exclude)
    return StepResult(
        status=DONE, commit_sha=sha, commit_phase=prefix,
        usage=draft_usage, usage_by_agent=usage_by_agent,
        session_id=draft_session, notes=f"committed {sha[:10]}",
    )


def _commit_message(step: Step, ctx: StepContext, consumed=()):
    """Return ``(message, usage, session_id, drafter)``; usage/session/drafter
    are None for a literal message (no model call)."""
    literal = step.get("message")
    if literal:
        return literal, None, None, None  # human-authored YAML; still validated
    return _draft_commit_message(step, ctx, consumed)


def _draft_commit_message(step: Step, ctx: StepContext, consumed=()):
    """Draft a commit message via the message_agent with bounded redraft.

    The agent sees the change as data — both the tracked diff AND the untracked
    files `git add -A` will sweep in (review F-008: a new-file phase otherwise
    drafts from an empty diff) — plus an optional plan section and, after a
    `--response` resume, the human decision(s) being implemented (PRD §8). The
    engine validates the format and asks for a redraft on violation (FR-9.2).
    Returns ``(message, usage, session_id, drafter)`` so the commit step records
    the drafter's cost (FR-3.2/§7).
    """
    agent_name = step.get("message_agent")
    if not agent_name:
        raise ValueError("commit step needs either `message:` or `message_agent:`")
    adapter = ctx.build_adapter(agent_name)
    change = _change_context(ctx)
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
    prompt = f"{header}\n{change}\n"
    max_redrafts = int(step.get("max_redrafts", 2))
    message = ""
    usage = _UsageAccumulator()  # sum across ALL draft attempts, incl. rejected
    session_id = None
    for _attempt in range(1 + max_redrafts):
        result = adapter.run(prompt, cwd=ctx.repo_root)
        usage.add(result.usage)  # a redraft's cost is real spend (F-008 round 2)
        session_id = result.session_id
        message = result.text.strip()
        if validate_commit_message(message) is None:
            return message, usage.result(), session_id, agent_name
        prompt = (
            f"{header}\n\nYour previous draft was rejected: "
            f"{validate_commit_message(message).reason}. "
            f"Return only the corrected commit message.\n{change}\n"
        )
    return message, usage.result(), session_id, agent_name


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
        )

    def by_agent(self) -> dict:
        """Per-agent-profile Usage (FR-3.2); empty when no agent was tagged."""
        out = {}
        for name, acc in self._by_agent.items():
            r = acc.result()
            if r is not None:
                out[name] = r
        return out


def _change_context(ctx: StepContext) -> str:
    """The diff vs HEAD plus the untracked files staging will add (F-008)."""
    repo = ctx.repo_root
    diff = gitops.diff_head(repo, exclude=ctx.excludes)
    status = gitops.status_porcelain(repo, exclude=ctx.excludes)
    return (
        f"--- git status (incl. untracked) ---\n{status}\n"
        f"\n--- diff (tracked, vs HEAD) ---\n{diff}"
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
    "header prefixed with the phase, e.g. 'P3: <summary>', at most 72 chars. "
    "Then a blank line, then a body explaining what changed and why, the plan "
    "assumption validated, and relevant FR references."
)


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

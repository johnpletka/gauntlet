"""Run manifest: the state-machine checkpoint (§7, FR-8.2, review F-003).

The manifest is written **write-ahead** — before and after every step — and
atomically (write-temp + ``os.replace``), so a ``kill -9`` at any instant
leaves either the prior or the next consistent state on disk, never a torn
file. The side-effect transaction boundary (review F-003) lives here too: each
worktree-touching step records its **base SHA** before running, so resume can
tell a clean re-entry from a step that died mid-edit.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

# --- park reasons (PRD FR-7.2 enum) ------------------------------------------
# The canonical ``parked_reason`` enum a PARKED step carries (harness-efficiency
# FR-7.2, PRD §6). Exactly one is stamped per park; it is DISJOINT from
# ``halt_reason`` (never both set) and CURRENT-STATE (cleared on any non-park
# finalization). ``status --json`` and ``schemas/status.json`` emit/accept only
# these five values (never a legacy value or a null-reason on a park).

# A step (agent_task or adversarial_cycle) interrupted by a provider usage limit
# / rate limit / overload (FR-3.2). Needs NO human decision — the worktree and
# CLI session are preserved and a PLAIN `gauntlet resume` continues the persisted
# session with a short continuation prompt (FR-3.3). Deliberately NOT in
# RESPONSE_RESOLVABLE_PARK_REASONS.
PARKED_REASON_USAGE_LIMIT = "usage_limit"
# A pre-step park because the provider usage window has insufficient headroom for
# the next step (FR-10.3, ``enforce: true``). P3 defines the value; P10 wires the
# admission behavior. A plain `gauntlet resume` retries once the window replenishes.
PARKED_REASON_USAGE_WINDOW = "usage_window"
# A park because an agent-authored structured artifact failed validation after the
# bounded in-session repair loop (FR-2.2). P3 defines the value; P4 wires the
# validator/repair/park behavior. A plain `gauntlet resume` re-runs ONLY the
# validator against the (possibly hand-edited) artifact.
PARKED_REASON_ARTIFACT_INVALID = "artifact_invalid"
# A human-decision park: the builder halted on an UPSTREAM CONFLICT (an
# ``agent_task`` park) OR a reviewer/triager escalated an ``adversarial_cycle``
# finding no configured agent could settle. Both are resolved by ``gauntlet
# resume --response`` (FR-10.4); the agent_task-vs-cycle distinction that governs
# HOW the decision is re-driven is recovered from the step ``type``
# (RESPONDABLE_STEP_TYPES), so one value suffices for BOTH.
PARKED_REASON_RESPONSE = "response"
# A park at a human ratification gate (``human_gate``): awaiting approve/reject.
PARKED_REASON_GATE = "gate"
# A transport/dependency park (plan §5.2, P5): the provider itself was
# unreachable or failing — typed timeout / connection / DNS / 5xx /
# service-unavailable / overload — and the bounded in-process dependency
# retries (persisted on ``StepRecord.dependency_attempts``) were exhausted.
# DISTINCT from ``usage_limit`` (a quota wall): no human decision is at stake
# and a PLAIN ``gauntlet resume`` retries the step — NEVER ``--response``
# (R7; issue #63). The park always records a concrete backoff/Retry-After
# deadline on ``quota_reset_at`` so it is a legitimate wait, not a wedge
# (P4.1 F-006).
PARKED_REASON_PROVIDER_UNAVAILABLE = "provider_unavailable"

# The complete PRD parked_reason enum (the only values written / emitted).
# ``provider_unavailable`` was APPENDED by P5 (additive; schema_version stays 1).
PARKED_REASONS = frozenset(
    {
        PARKED_REASON_USAGE_LIMIT,
        PARKED_REASON_USAGE_WINDOW,
        PARKED_REASON_ARTIFACT_INVALID,
        PARKED_REASON_RESPONSE,
        PARKED_REASON_GATE,
        PARKED_REASON_PROVIDER_UNAVAILABLE,
    }
)

# --- legacy parked_reason values (READ-SIDE ONLY, FR-7.2 legacy contract) -----
# Pre-P3 runs persisted two current-state discriminators absent from the PRD
# enum, and stamped a null parked_reason on a ``human_gate`` park. They are never
# WRITTEN or EMITTED again; they enter only through :func:`normalize_parked_reason`
# on read, which maps both to ``response`` (their agent_task-vs-cycle distinction
# is recovered from the step type). Kept as named constants so a test can build a
# legacy fixture and so the mapper is self-documenting.
PARKED_REASON_UPSTREAM_CONFLICT = "upstream_conflict"  # legacy → response
PARKED_REASON_CYCLE_ESCALATION = "cycle_escalation"  # legacy → response
_LEGACY_PARKED_REASON_MAP = {
    PARKED_REASON_UPSTREAM_CONFLICT: PARKED_REASON_RESPONSE,
    PARKED_REASON_CYCLE_ESCALATION: PARKED_REASON_RESPONSE,
}


def normalize_parked_reason(
    reason: str | None, step_type: str | None, status: str | None = None
) -> str | None:
    """Map any persisted ``parked_reason`` to the PRD FR-7.2 enum (read-side).

    The single normalization point (FR-7.2 legacy contract, point 2): every read
    surface — the status classifier, `--response` resume routing, status/schema
    rendering — passes a persisted value through here so all operate on the PRD
    enum set. Legacy manifests on disk are read through this mapper, never
    rewritten in place.

    * A PRD value passes through unchanged.
    * A legacy value (``upstream_conflict`` / ``cycle_escalation``) maps to
      ``response`` — the two collapse because the resume-routing distinction they
      carried is recovered from the step ``type``.
    * A ``None`` reason on a PARKED ``human_gate`` step (the pre-P3 gate shape)
      maps to ``gate``. ``None`` on any other step (a non-park / cleared record)
      stays ``None``. ``status`` is optional so a caller that has already
      established the step is parked (the classifier, which only reaches here for
      the unique PARKED step) can omit it and still get the gate mapping.
    * An unrecognized non-null value is returned UNCHANGED, so it fails closed
      downstream (the classifier maps it to ``unknown``; the schema enum rejects
      it at emission) rather than being silently coerced into a PRD value — the
      complete set of engine-produced current-state values is verified to map
      1:1, so a non-mapping value can only be a corrupt manifest (legacy-contract
      point 5).
    """
    if reason in PARKED_REASONS:
        return reason
    if reason in _LEGACY_PARKED_REASON_MAP:
        return _LEGACY_PARKED_REASON_MAP[reason]
    if reason is None:
        gate_shape = step_type == "human_gate" and (status in (None, PARKED))
        return PARKED_REASON_GATE if gate_shape else None
    return reason  # unrecognized → fail closed downstream, never coerced


# Park reasons a human resolves by supplying a `gauntlet resume --response`
# decision (FR-1.1 / FR-10.4): resuming such a park WITHOUT `--response` errors
# and asks for one, instead of silently re-running into the same wall. Collapsed
# to the single PRD value ``response``; the two legacy values are accepted only
# on read (through :func:`normalize_parked_reason`), never compared directly.
RESPONSE_RESOLVABLE_PARK_REASONS = frozenset({PARKED_REASON_RESPONSE})
# Step types that accept a `--response` decision when parked OR failed. An
# `agent_task` (the builder halting on UPSTREAM CONFLICT) re-runs with the
# decision injected into its prompt; an `adversarial_cycle` re-drives with it
# injected into the reviewer/triager so they re-evaluate the parked finding. A
# `commit` step whose message-drafting failed the FR-9.2 format check (a benign,
# otherwise-unrecoverable terminal failure) re-runs consuming the decision: a
# response that is itself a valid commit message is used verbatim (a
# deterministic operator override for a drafter that could not produce a legal
# header), else it steers the redraft.
RESPONDABLE_STEP_TYPES = frozenset({"agent_task", "adversarial_cycle", "commit"})

# --- halt reasons (terminal HALTED/FAILED/INTERRUPTED discriminator) ---------
# The disjoint sibling of ``parked_reason`` (harness-efficiency FR-7.2, PRD §6):
# a terminal step carries a ``halt_reason`` while ``parked_reason`` is null, and
# a PARKED step the reverse — the two are never both set. P2 introduced the field
# with the single value it needed — ``timeout`` — for the suspend-cap timeout
# halt (FR-5.2); P3 completes the enum and the full disjointness invariant.
# Current-state like ``parked_reason``: set on the just-finished execution and
# cleared on any finalization that does not re-set it. The applicable value is
# mandatory on every engine-written non-DONE terminal step (older manifests
# predating P3 carry ``None``, which the schema tolerates as nullable).
HALT_REASON_TIMEOUT = "timeout"  # step/adapter deadline (incl. suspend-cap, FR-5.2)
HALT_REASON_BUDGET = "budget"  # per-step/profile cost budget guard (FR-3.3)
HALT_REASON_JUDGE_DENY = "judge_deny"  # a judge deny terminated the step
HALT_REASON_SIGNAL_KILL = "signal_kill"  # killed mid-step by a signal / crash
HALT_REASON_ADAPTER_ERROR = "adapter_error"  # a terminal adapter/handler failure
HALT_REASON_PRECONDITION = "precondition"  # a fail-closed guard fired (nothing ran)
HALT_REASON_OPERATOR_RECOVER = "operator_recover"  # `gauntlet recover` ended it

# The complete PRD halt_reason enum.
HALT_REASONS = frozenset(
    {
        HALT_REASON_TIMEOUT,
        HALT_REASON_BUDGET,
        HALT_REASON_JUDGE_DENY,
        HALT_REASON_SIGNAL_KILL,
        HALT_REASON_ADAPTER_ERROR,
        HALT_REASON_PRECONDITION,
        HALT_REASON_OPERATOR_RECOVER,
    }
)


def reason_fields_disjoint(
    halt_reason: str | None, parked_reason: str | None
) -> bool:
    """The FR-7.2 disjointness invariant: never both reason fields set at once.

    A single pure predicate (the plan's "validator asserting disjointness"): a
    non-DONE step is EITHER terminal (``halt_reason`` set, ``parked_reason`` null)
    OR parked (``parked_reason`` set, ``halt_reason`` null). Both-set is a
    contract violation the engine must never persist.
    """
    return not (halt_reason is not None and parked_reason is not None)

# --- failure kinds (current-state discriminator on a FAILED step) ------------
# Most FAILED steps are TERMINAL: re-running them only re-invokes the adapter and
# repeats the same failure (`_is_terminal_failure`). A PRECONDITION failure is the
# exception — it fired BEFORE any adapter call (no cost, `agent` null), because an
# upstream invariant the step depends on was not satisfied. The canonical case is
# the FR-9.3 round-1 clean-handoff guard: control may not pass to a reviewer on a
# dirty worktree. Such a failure is safely re-runnable once the operator fixes the
# precondition (commits/cleans the tree), so a plain `gauntlet resume` re-executes
# the step's guard rather than no-op'ing as a terminal failure would. Like
# `parked_reason`, this is CURRENT-STATE on the record: `_finalize` copies the
# just-finished execution's `failure_kind`, so any non-precondition finalization
# clears a stale value.
FAILURE_KIND_CLEAN_HANDOFF = "clean_handoff_precondition"
# An UNKNOWN adapter failure whose attempt provably produced no Git/worktree
# side effects (plan §5.2, P5): the engine assessed the tree against the
# attempt's ``base_sha`` — not the exception name — and found it unchanged, so
# re-running cannot re-run over partial work. Such a failure is plain-resumable
# (no ``--response``, R7); a deterministic repeat then trips the R5 no-progress
# guard rather than looping. A side-effecting unknown failure stays terminal —
# its recovery goes through the snapshot/reconciliation paths (P3 executor).
FAILURE_KIND_SIDE_EFFECT_FREE = "side_effect_free_unknown"
# Failure kinds a plain (response-less) `gauntlet resume` may safely re-execute
# once the operator has fixed the named precondition — they cost nothing and
# re-run the guard, not the adapter — plus the P5 side-effect-free unknown
# failure, whose retry provably cannot re-run over partial work.
RERUNNABLE_FAILURE_KINDS = frozenset(
    {FAILURE_KIND_CLEAN_HANDOFF, FAILURE_KIND_SIDE_EFFECT_FREE}
)

# --- human-response lifecycle states (FR-2, FR-7.1) --------------------------
# A `--response` entry is born ``pending`` (appended before the agent launches)
# and flips to ``consumed`` once the resumed agent reaches a terminal outcome
# (proceeds, re-parks, or fails). The ``state`` field is the single source of
# truth for idempotent crash recovery: a recovered ``pending`` entry is
# re-launched, never re-appended; a ``consumed`` entry is never re-executed.
RESPONSE_PENDING = "pending"
RESPONSE_CONSUMED = "consumed"

# --- step lifecycle states ---------------------------------------------------
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
INTERRUPTED = "interrupted"  # killed mid-step, dirty worktree (F-003)
PARKED = "parked"  # human_gate / interrupted-park awaiting a human
HALTED = "halted"  # a terminal guard halt; the cause is the step's halt_reason (HALT_REASON_*)
SKIPPED = "skipped"  # `when:` false

# --- run lifecycle states ----------------------------------------------------
RUN_RUNNING = "running"
RUN_PARKED = "parked"
RUN_DONE = "done"
RUN_ABORTED = "aborted"
RUN_FAILED = "failed"


class PipelineRef(BaseModel):
    name: str
    version: int
    hash: str


class UsageTotals(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float | None = None  # None until at least one priced call (§12 Q3)
    # Raw provider counters (see ``adapters.base.Usage``): prompt-cache writes
    # and reasoning output. Additive — older manifests load with 0.
    cache_creation_input_tokens: int = 0
    reasoning_output_tokens: int = 0

    def add(self, usage: Any | None) -> None:
        if usage is None:
            return
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cached_input_tokens += usage.cached_input_tokens or 0
        if usage.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + usage.cost_usd
        self.cache_creation_input_tokens += (
            getattr(usage, "cache_creation_input_tokens", None) or 0
        )
        self.reasoning_output_tokens += getattr(usage, "reasoning_output_tokens", None) or 0


class HumanResponse(BaseModel):
    """One `gauntlet resume --response` decision, appended for the audit trail.

    Append-only (FR-2): once written, an entry's ``response_id`` never changes —
    it is how the builder references a response (FR-5/FR-10) and how crash
    recovery deduplicates (FR-7.1). ``state`` drives that idempotent recovery:
    ``pending`` immediately after append (before the agent launches), flipped to
    ``consumed`` once the resumed agent reaches a terminal outcome. The append /
    consume wiring lands in P3; P1 only defines the durable shape.
    """

    response_id: str
    response_text: str
    timestamp: str
    user: str
    response_attempt: int
    state: Literal["pending", "consumed"]
    # SHA-256 of the governed artifact's bytes at the moment the response was
    # recorded (#79). The vacuous-convergence guard compares it against the
    # artifact at a zero-findings convergence on the re-drive: byte-identical
    # means the response produced no revision. ``None`` when the responding
    # step has no governed artifact (code_review mode) or on entries predating
    # the field — the guard skips rather than guesses. Additive/nullable.
    artifact_fingerprint: str | None = None


class ScheduledResume(BaseModel):
    """An armed auto-resume schedule on a usage-limit or provider-unavailable
    park (FR-3.4; generalized by #134).

    Persisted on the parked step BEFORE the run parks, so a process death
    between scheduling and ``attempt_at`` loses nothing — the next driver start
    or ``gauntlet resume`` reconciles from disk. ``attempt_at`` is the absolute
    UTC time to resume (``now + retry_after_s``, else the quota reset time; for
    a ``provider_unavailable`` park the recorded backoff / Retry-After
    deadline); ``attempts`` counts spaced attempts made; once ``attempts >=
    max_attempts`` the step re-parks plain (no schedule) with an exhaustion
    note. ``reason`` records which park reason armed the schedule
    (``usage_limit`` / ``provider_unavailable``) so the wait loop can consult
    the matching config knob; ``None`` on manifests written before #134 (an
    unstamped schedule is read as the step's own park reason). Additive/nullable.
    """

    attempt_at: str
    attempts: int = 0
    max_attempts: int = 3
    reason: str | None = None


class RevalidationRecord(BaseModel):
    """The content-hash pair recorded on an ``artifact_invalid`` park (FR-7.2/§6).

    P3 defines the shape; P4 populates it. ``hash_at_park`` is the SHA-256 of the
    invalid artifact at park time; on a plain ``gauntlet resume`` the validator
    re-runs against the (possibly hand-edited) on-disk bytes and P4 records
    ``hash_at_resume``, whether it ``changed_while_parked``, and whether it
    ``passed_on_resume`` — so a sanctioned hand-edit is auditable rather than
    off-book file surgery (PRD §7). Additive/nullable: absent on every park that
    is not an ``artifact_invalid`` park, so older manifests load unchanged.
    """

    artifact: str
    hash_at_park: str
    hash_at_resume: str | None = None
    changed_while_parked: bool = False
    passed_on_resume: bool = False
    # P5 (plan §5.1, issue #63/#64 class): WHICH validator judged the artifact
    # invalid (``plan_phases``, ``phase_lint``, a ``schema:<ref>``, or the
    # drive-level ``gauntlet-phases`` parse) and its exact diagnostic, so the
    # park is self-explaining without a transcript read and the audit trail
    # records the precise check a hand-edit answered. Additive/nullable —
    # pre-P5 manifests load unchanged.
    validator: str | None = None
    diagnostic: str | None = None


class Suspension(BaseModel):
    """A detected host-suspension interval (FR-5.1), appended to the manifest.

    ``gap_s`` is the wallclock width of the interval; ``start``/``end`` are the
    straddling heartbeats' wallclock stamps. Additive/append-only — older
    manifests load with an empty list.
    """

    start: str
    end: str
    gap_s: int


# --- adversarial-cycle sub-step checkpoints (FR-4.1) -------------------------
# Named sub-steps of one cycle round, in execution order. The per-finding triage
# batch is checkpointed as ONE ``triage`` sub-step in P5; P11 refines it to a
# per-finding failure-path fragment.
CHECKPOINT_SUBSTEPS = ("review", "triage", "fix", "confirm")


class Checkpoint(BaseModel):
    """A completed adversarial-cycle sub-step, recorded write-ahead (FR-4.1).

    Each cycle sub-step (review, per-finding triage batch, fix, confirm) appends
    one of these the instant it finishes AND flushes the manifest, so a
    ``kill -9`` or usage-limit park mid-round leaves a durable record of exactly
    which sub-steps completed. On a plain ``gauntlet resume`` the cycle loads the
    completed sub-steps from ``artifact`` (a round-scoped copy under the run dir)
    instead of re-invoking the agent, re-entering the round at the first sub-step
    with no checkpoint — re-running zero completed work (PRD G1).

    ``handoff_sha`` is the round's review-handoff SHA and the FR-4.2 guard key:
    if the worktree/handoff moved since the checkpoint (e.g. manual commits during
    a park), reuse is invalidated and the round restarts. ``artifact`` is the
    run-dir-relative path of the sub-step's persisted output
    (``artifacts/r1/findings.json`` etc.); it is ``None`` for the ``fix``
    sub-step, whose product is a commit — that commit's SHA is ``result_sha``
    (also the next round's handoff), so a reused fix advances the loop without
    re-committing. Additive/append-only — older manifests load with an empty list.
    """

    sub_step: str  # one of CHECKPOINT_SUBSTEPS
    round: int
    handoff_sha: str
    artifact: str | None = None
    result_sha: str | None = None


# --- per-invocation clock-time evidence (cost-metrics: timing) ---------------
# Outcome vocabulary for one adapter call, as the engine observed it. ``ok`` is
# a returned result (the caller may still reject its structured output later);
# the rest map the adapter error the call raised. Written by
# :func:`gauntlet.engine.timing.record_invocation`, never inferred afterwards.
INVOCATION_OUTCOMES = (
    "ok", "malformed", "failed", "timeout", "vanished", "session_not_found",
    "error",
)


class Invocation(BaseModel):
    """One adapter call the engine made on behalf of a step — clock-time evidence.

    ``started``/``ended`` are the engine's own UTC stamps around the call and
    ``wall_s`` its monotonic wall-clock width, so the record is identical in
    shape and trustworthiness for every adapter: a CLI that exports no timing
    of its own (codex) is measured exactly like one that does (claude-code).
    ``label`` names the work the call did — a cycle sub-step (``r1-review``,
    ``r1-triage``, ``r1-fix``, ``r1-confirm``, ``r1-verify``, an ensemble
    member's ``r1-review-<member>``), a single-agent step's ``call`` (with the
    repair/retry suffix of its evidence files), or ``commit-message`` for the
    phase-commit drafter. ``attempt`` is the step attempt the call ran under.
    ``adapter`` / ``model`` / ``effort`` are FROZEN from the profile (and the
    built adapter) at call time, so a later config edit can never re-attribute
    a past call: the report and ledger read the model that ran, not the one
    configured today.
    Append-only across attempts, resumes and re-drives: a failed or wasted
    call's time is real time, so ``gauntlet report`` can show where a run's
    clock went. Additive — older manifests load with an empty list.
    """

    agent: str | None = None
    label: str
    started: str
    ended: str
    wall_s: float
    outcome: str = "ok"
    attempt: int = 0
    adapter: str | None = None
    model: str | None = None
    effort: str | None = None


class StepRecord(BaseModel):
    id: str
    type: str
    status: str = PENDING
    agent: str | None = None
    session_id: str | None = None
    started: str | None = None
    ended: str | None = None
    attempts: int = 0
    # Transaction boundary (F-003): HEAD before the step touched the worktree.
    base_sha: str | None = None
    # Park discriminator (FR-7.2), one of ``PARKED_REASONS``. CURRENT-STATE, not a
    # latch: ``_finalize`` copies the just-finished execution's value and clears it
    # (back to None) on any non-park finalization, so a stale value can never cause
    # a later terminal/DONE step to be misclassified as a park. DISJOINT from
    # ``halt_reason`` (never both set — :func:`reason_fields_disjoint`). Only PRD
    # enum values are written; legacy on-disk values are mapped on read by
    # :func:`normalize_parked_reason`, never rewritten in place.
    parked_reason: str | None = None
    # Terminal halt discriminator (harness-efficiency FR-7.2), one of
    # ``HALT_REASONS`` and DISJOINT from ``parked_reason``: a HALTED/FAILED/
    # INTERRUPTED step carries ``halt_reason`` with ``parked_reason=None``, and a
    # PARKED step the reverse. Current-state: set on the just-finished execution,
    # cleared on any finalization that does not re-set it. Mandatory on every
    # engine-written non-DONE terminal step; additive/nullable so manifests
    # predating P3 (which carry ``None``) still load and validate.
    halt_reason: str | None = None
    # Failure-kind discriminator on a FAILED step (current-state, like
    # ``parked_reason``): ``FAILURE_KIND_CLEAN_HANDOFF`` when this execution failed
    # a re-runnable PRECONDITION guard (no adapter invoked, no cost) rather than
    # in execution. ``_is_terminal_failure`` consults it so a plain ``resume``
    # re-runs such a step once the precondition is fixed instead of treating it as
    # terminal. ``None`` for every other outcome; cleared on any finalization that
    # does not re-set it (so a stale value can never mislabel a later failure).
    failure_kind: str | None = None
    # Usage-limit park stamps (harness-efficiency FR-3.2). Set only when this step
    # parked with ``parked_reason == PARKED_REASON_USAGE_LIMIT``: ``retry_after_s``
    # is the provider's structured retry hint (never scraped from prose; ``None``
    # when the envelope carried no structured field), and ``quota_reset_at`` is the
    # absolute UTC reset time derived from it (``None`` when no hint was reported).
    # Additive/nullable, so older manifests load unchanged; full status surfacing
    # of these is P3.
    retry_after_s: int | None = None
    quota_reset_at: str | None = None
    # Vacuous-convergence guard state (#79): the response_attempt and artifact
    # fingerprint of the LAST fail-closed vacuous park on this step. The
    # proceed-with-warning bypass applies ONLY to the direct reply to that park
    # (attempt + 1) with the artifact still byte-identical — never as a
    # standing latch, so a later, independent gate rejection gets a fresh
    # fail-closed park (PR #93 review F-001). Additive/nullable.
    vacuous_park_response_attempt: int | None = None
    vacuous_park_fingerprint: str | None = None
    # Which adversarial_cycle sub-step produced the preserved ``session_id`` on a
    # usage-limit park (FR-3.3): e.g. ``"r1-review"``, ``"r1-fix"``. Current-state
    # like ``parked_reason`` — set on a usage-limit park, cleared on any other
    # finalization. The cycle resume continues the preserved session only when it
    # re-reaches this exact sub-step, so a fixer/triager session is never
    # misrouted into the round-1 reviewer. Additive/nullable: older manifests load
    # unchanged.
    parked_substep: str | None = None
    # Armed auto-resume schedule on a usage-limit or provider-unavailable park
    # (FR-3.4 / #134). Set only when ``resume_on_quota: auto`` (usage_limit) or
    # ``resume_on_provider_unavailable: auto`` (provider_unavailable) parked
    # this step; ``None`` otherwise and after a successful resume.
    # Additive/nullable — older manifests load unchanged.
    scheduled_resume: ScheduledResume | None = None
    # Content-hash pair recorded on an ``artifact_invalid`` park (FR-7.2/§6). P3
    # defines the shape here; P4 populates it on the validator-repair park path.
    # Additive/nullable — ``None`` on every other outcome, so older manifests load
    # unchanged.
    revalidation: RevalidationRecord | None = None
    # Orthogonal recovery taxonomy (plan §4.1/§6 P5): WHY this step needs
    # recovery (a ``RecoveryCause`` value) and WHAT strategy applies (a
    # ``RecoveryDisposition`` value), stamped by ``_finalize`` on every
    # terminal/parked outcome from the outcome's own evidence
    # (``recovery_exec.outcome_classification``). CURRENT-STATE like the reason
    # fields: cleared (None) on DONE/SKIPPED. The planner treats a recorded pair
    # as refining evidence over the coarse state→cause map; a pre-P5 manifest
    # carries ``None`` and classifies exactly as before (plan §8 — additive,
    # nullable, never required).
    recovery_cause: str | None = None
    recovery_disposition: str | None = None
    # Persisted dependency-retry budget (plan §5.2, P5): how many bounded
    # in-process retries this step's CURRENT failure episode has consumed for
    # transport/dependency failures (timeout / connection / 5xx / overload).
    # Incremented and flushed WRITE-AHEAD before each retry sleep, so a crash
    # between retries never resets the budget; reset to 0 when a new episode
    # starts (a plain resume of a ``provider_unavailable`` park) and on any
    # finalization that is not a ``provider_unavailable`` park. Additive —
    # older manifests load with 0.
    dependency_attempts: int = 0
    # Append-only audit trail of human `--response` decisions on this step
    # (FR-2). Recording/consume wiring is P3; P1 carries the schema only.
    human_responses: list[HumanResponse] = Field(default_factory=list)
    usage: UsageTotals = Field(default_factory=UsageTotals)
    # Per-role usage split for a COMPOUND step (adversarial_cycle): profile → its
    # own usage, mirroring the run-level `Manifest.agent_usage` but scoped to this
    # step (FR-3.2). Empty for a single-agent step (its one profile is `agent` +
    # `usage`). The usage-ledger reads this to attribute a cycle's spend to each
    # role's own provider (FR-10.1); without it a cycle's usage — the bulk of a
    # run's — has no per-provider attribution.
    agent_usage: dict[str, UsageTotals] = Field(default_factory=dict)
    notes: str | None = None
    # foreach binding key, when this record is one iteration of a fan-out step
    iteration: str | None = None
    # Step-emitted structured outcome counts for `gauntlet report --trend`
    # (FR-6.6): an adversarial_cycle records rounds, finding/verdict/confirm
    # tallies here so trend math reads the manifest, never the log dirs (the
    # plan's P7 test strategy is "trend-metric math from fixture manifests").
    metrics: dict[str, Any] = Field(default_factory=dict)
    # Current invocation's PreToolUse authorization counts (issue #83). Kept as
    # explicit data so status can expose a bricked/all-denied agent without an
    # operator mining its transcript. Older manifests load with zeroes.
    judge_tool_calls_allowed: int = 0
    judge_tool_calls_denied: int = 0
    # Write-ahead adversarial-cycle sub-step checkpoints (harness-efficiency
    # FR-4.1). Appended (and the manifest flushed) as each round sub-step
    # completes, so a mid-round interruption — a usage-limit park (P1) or a kill —
    # resumes at the first INCOMPLETE sub-step instead of re-deriving the whole
    # round (PRD G1). A fresh (non-reuse) drive rebuilds them from empty; the SHA
    # guard (FR-4.2) invalidates and clears them when the handoff moved. Left in
    # place on a converged/terminal cycle as a truthful record of the rounds it
    # ran. Additive/append-only — older manifests load with an empty list.
    checkpoints: list[Checkpoint] = Field(default_factory=list)
    # The intra-phase checkpoint commit a reset_to_base recovery rewound to
    # (harness-efficiency FR-11.2): the ``P<N> wip:`` subject the re-run resumes
    # from, so the re-run prompt can name it and the audit trail records that
    # completed milestones were preserved (not discarded to base_sha). ``None``
    # when recovery fell back to ``base_sha`` (no checkpoint existed) or the step
    # was never checkpoint-recovered. Additive/nullable — older manifests load
    # unchanged.
    resumed_from_checkpoint: str | None = None
    # Per-adapter-call clock-time evidence (see :class:`Invocation`): every
    # call the engine made for this step, in the order it finished, across
    # every attempt. ``gauntlet report`` aggregates these into per-step,
    # per-profile and per-activity clock time. Additive/append-only — older
    # manifests load with an empty list.
    invocations: list[Invocation] = Field(default_factory=list)


class CommitRecord(BaseModel):
    step_id: str
    phase: str  # the PN[.x] prefix
    sha: str


# --- evidence-tiered gates (pipeline-effectiveness FR-4, P8) ------------------
# A per-phase code gate's policy. ``always`` (default) is today's behavior — the
# gate parks for a human. ``auto_when_clean`` lets the gate auto-approve, but
# only when the strict §4.2 clean-signal predicate holds; any predicate miss
# parks exactly as ``always`` would (fail closed). PRD/plan (document) gates
# reject ``auto_when_clean`` at pipeline load — document ratification stays
# unconditionally human (§2.2).
GATE_POLICY_ALWAYS = "always"
GATE_POLICY_AUTO_WHEN_CLEAN = "auto_when_clean"
GATE_POLICIES = frozenset({GATE_POLICY_ALWAYS, GATE_POLICY_AUTO_WHEN_CLEAN})

# The verifier-result values an auto-approval evidence snapshot records (PRD §6).
# Only ``clean`` satisfies the predicate; every other value — including
# ``not_configured``, recorded when a phase that PASSED load (a verifier was
# configured, or the run predates the verifier) has no clean verifier result at
# runtime — is a predicate miss that parks closed (FR-4.1, review F-008).
VERIFIER_CLEAN = "clean"
VERIFIER_FINDINGS = "findings"
VERIFIER_FAILED = "failed"
VERIFIER_NOT_CONFIGURED = "not_configured"


class AutoApproval(BaseModel):
    """One auto-approved code gate + its durable evidence snapshot (FR-4.1/§6).

    Appended to :attr:`Manifest.auto_approvals` the instant an ``auto_when_clean``
    gate clears the strict clean-signal predicate, so the run's final PR can
    enumerate every auto-approval for the human's collective ratification at the
    audit boundary (FR-4.2). Append-only; a later human reversal never deletes
    the record — it stamps ``reversed_*`` on it and flips the run's effective
    policy (:attr:`Manifest.auto_approval_disabled`).

    ``evidence`` carries the full §6 snapshot (rounds, blocking/major finding
    counts, escalations, reviewer mutations, acceptance-gate result, verifier
    result, test summary) so the decision is reconstructable without transcript
    access. Additive — older manifests load with an empty ``auto_approvals``.
    """

    gate_id: str
    iteration: str | None = None
    phase: str | None = None
    policy: str = GATE_POLICY_AUTO_WHEN_CLEAN
    evidence: dict[str, Any] = Field(default_factory=dict)
    at: str
    # A recorded human reversal (FR-4.2): stamped when a `gauntlet rollback` past
    # this gate's phase boundary reverses the auto-approval. Additive/nullable.
    reversed_at: str | None = None
    reversed_by: str | None = None
    reversal_notes: str | None = None


# --- review-run intent provenance (lightweight `gauntlet review`, §6/FR-2.6) --
# A review run resolves its problem statement (`intent.md`) from one of four
# sources and tags it with a provenance on an independence axis. The block is
# persisted here so provenance and — for a non-independent statement — the
# human ratification that made its circularity tolerable (FR-2.5) are auditable,
# and so the reviewer prompt can be told how the intent was sourced (FR-2.2).

# The single intent source that produced the statement (§6 manifest block).
INTENT_SOURCE_ISSUE = "issue"
INTENT_SOURCE_INTENT_FILE = "intent-file"
INTENT_SOURCE_MESSAGE = "message"
INTENT_SOURCE_EDITOR = "editor"
INTENT_SOURCE_CODE_ONLY = "code-only"

# Provenance on the independence axis (FR-2.1a). `tracker` is the only
# independent value; the rest are the author's own framing of the problem.
PROVENANCE_TRACKER = "tracker"
PROVENANCE_TRACKER_SESSION = "tracker-session"
PROVENANCE_AUTHOR_SESSION_SUMMARY = "author-session-summary"
PROVENANCE_NONE = "none"  # `--code-only`: there is no intent to place on the axis

# Manual `--intent-provenance` choices (declared by the operator; FR-1.1). The
# tracker/none provenances are never operator-declared — they are derived from
# the source.
MANUAL_PROVENANCES = frozenset(
    {PROVENANCE_TRACKER, PROVENANCE_TRACKER_SESSION, PROVENANCE_AUTHOR_SESSION_SUMMARY}
)

# How a non-independent intent was ratified before the cycle (FR-2.5/FR-2.6).
RATIFICATION_INTERACTIVE = "interactive-confirm"
RATIFICATION_APPROVED_FLAG = "approved-intent-flag"


class RatificationRecord(BaseModel):
    """The pre-run human ratification of a non-independent intent (FR-2.5/2.6).

    Present only when ``IntentRecord.independent`` is ``False``. ``method`` is
    ``interactive-confirm`` (a TTY operator confirmed/edited the statement) or
    ``approved-intent-flag`` (``--approved-intent`` asserted an out-of-band
    ratification, e.g. a skill's in-session review). ``user`` is the resolved
    human operator identity; ``timestamp`` is UTC ISO-8601.
    """

    method: str
    user: str
    timestamp: str


class IntentRecord(BaseModel):
    """The resolved intent's provenance for a review run (§6 manifest block).

    ``independent`` is derived from ``provenance`` (``tracker`` ⇒ ``True``, every
    other value ⇒ ``False``); it is stored explicitly so a reader need not
    re-derive it. ``ratification`` is present iff ``independent`` is ``False``.
    For a ``code-only`` run ``source`` is ``code-only``, ``provenance`` is
    ``none``, ``independent`` is ``False``, and there is no ratification (there
    is no intent to ratify).

    ``repo_exclude`` is the repo-relative path of an in-repo, untracked
    ``--intent`` file (FR-2.4): it is persisted so a ``resume`` re-applies the
    same worktree exclusion the fresh run derived, keeping the user's intent
    file out of the clean-handoff checks and every ``REVIEW.x`` fix commit.
    Absent (``None``) whenever the intent did not come from an in-repo untracked
    file (a tracker issue, ``-m`` text, the ``$EDITOR`` template, an out-of-repo
    or tracked ``--intent`` path, or ``--code-only``).
    """

    source: str
    provenance: str
    independent: bool
    ratification: RatificationRecord | None = None
    repo_exclude: str | None = None


class ReviewPrRecord(BaseModel):
    """PR-mode facts a review's terminal summary needs (§6, FR-4.3/FR-4.4).

    Persisted at the pre-cycle boundary so a review that parks/fails and is later
    resumed still renders the chosen linked ticket, the ignored secondary refs,
    and (for a fork PR) the "push-back is manual" note. On a fresh drive these
    live only in the in-memory ``ReviewResolution``; without persistence a resume
    would default them away and silently drop mandated summary facts.

    Absent (``None``) for every branch-mode review and every heavyweight run —
    the field is additive, so older/non-PR manifests load unchanged.
    """

    number: str | None = None
    url: str | None = None
    is_fork: bool = False
    chosen_ref: str | None = None
    ignored_refs: list[str] = Field(default_factory=list)


# --- recovery audit (operator-aids P4, FR-5.3 / §6.4) ------------------------
# `gauntlet recover` terminates a verified wedged driver. Which signal ended the
# recorded process group — or that it was already gone by the time we signalled.
SIGNAL_TERMINATED_SIGTERM = "terminated_sigterm"
SIGNAL_TERMINATED_SIGKILL = "terminated_sigkill"
SIGNAL_ALREADY_DEAD = "already_dead"


class RecoveryRecord(BaseModel):
    """One `gauntlet recover` event, APPENDED to ``Manifest.recoveries`` (§6.4).

    Append-only: a prior record is never overwritten, so repeated recoveries (a
    run re-wedged after a resume) accumulate a complete audit trail. The
    ``lock_nonce``/``pid``/``pgid``/``proc_identity``/``host`` are the verified
    prior-lock identity datums (the FR-5.1 gate inputs), so the record proves
    *which* process was killed; ``prior_*``/``resulting_*`` record the exact
    step/run transition the recovery effected.
    """

    ts: str
    actor: str
    actor_source: str
    reason: str | None = None
    lock_nonce: str
    pid: int
    pgid: int
    proc_identity: dict | None = None
    host: str
    signal_outcome: str  # terminated_sigterm | terminated_sigkill | already_dead
    prior_step_id: str
    prior_step_status: str
    prior_run_status: str
    resulting_step_status: str
    resulting_run_status: str
    # Branch↔manifest reconciliation evidence (#72): the run-branch tip at
    # recovery time, and — when that tip is strictly ahead of the manifest's
    # last recorded commit (a builder killed after committing but before a
    # manifest flush) — the unmanifested ``last..tip`` commit list, one line
    # per commit. ``None`` on pre-#72 records or when git was unavailable.
    branch_head: str | None = None
    unmanifested_range: str | None = None


class Manifest(BaseModel):
    """The persisted run state (§7)."""

    run_id: str
    slug: str
    branch: str
    base_branch: str
    pipeline: PipelineRef
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    status: str = RUN_RUNNING
    current_step: str | None = None
    # Non-fatal anomalies surfaced rather than swallowed (data over inference) —
    # e.g. a required final-gate artifact (FR-9.8 PR.md) that could not be
    # rendered. Recorded so a completed run never silently hides a missing
    # deliverable (review F-005).
    warnings: list[str] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    # Detected host-suspension intervals (FR-5.1), drained from the driver's
    # heartbeat writer at safe points. Append-only; additive, so older manifests
    # load with an empty list.
    suspensions: list[Suspension] = Field(default_factory=list)
    commits: list[CommitRecord] = Field(default_factory=list)
    # Auto-approved code gates + evidence snapshots (pipeline-effectiveness FR-4,
    # P8). Append-only; enumerated in the final PR for collective ratification
    # (FR-4.2). Additive — older manifests load with an empty list.
    auto_approvals: list[AutoApproval] = Field(default_factory=list)
    # Reversal circuit breaker (FR-4.2 / §9): flipped True by a recorded human
    # reversal of any auto-approved gate. Once set, the clean predicate fails
    # closed for the REMAINDER of the run — every later code gate parks even if
    # its evidence is clean, because a human has signalled distrust. Additive —
    # older manifests load with the default (auto-approval enabled).
    auto_approval_disabled: bool = False
    totals: UsageTotals = Field(default_factory=UsageTotals)
    # Per-agent-profile usage (FR-3.2): `gauntlet report` needs the spend split
    # by profile, not just by step — a single adversarial_cycle step bills the
    # reviewer, triager, fixer, and escalation profiles, so step-level totals
    # alone cannot answer "is triage < 5% of run cost?" (FR-3 acceptance).
    agent_usage: dict[str, UsageTotals] = Field(default_factory=dict)
    # Append-only `gauntlet recover` audit (operator-aids P4, FR-5.3/§6.4). One
    # record per recovery; never overwritten, so a run re-wedged after a resume
    # accumulates the full history. Defaults empty, so older manifests load
    # unchanged (additive — the field is absent on pre-P4 runs).
    recoveries: list[RecoveryRecord] = Field(default_factory=list)
    # Resolved intent provenance for a lightweight `gauntlet review` run (§6,
    # FR-2.6). Absent (``None``) for every heavyweight `gauntlet run` — the field
    # is additive, so older manifests and non-review runs load unchanged.
    intent: IntentRecord | None = None
    # PR-mode summary facts for a `gauntlet review --pr` run (§6, FR-4.3/4.4).
    # Persisted so a resumed PR review still renders the chosen/ignored refs and
    # the fork manual-push note. Absent (``None``) for branch mode / heavyweight
    # runs; additive, so older manifests load unchanged.
    pr: ReviewPrRecord | None = None
    # Which tree layout this run was BORN into (P7c, spike §13). Set once by
    # `start()` from `config.worktree.mode` and never rewritten by a later
    # verb. Additive and optional, so every pre-P7c manifest loads unchanged
    # with ``None`` — which resolves to `same_tree`, the legacy population's
    # mode forever (§16).
    #
    # This field exists to make auto-migration STRUCTURALLY impossible. A run's
    # effective mode is resolved from evidence and from THIS record, never from
    # the live config (`RunManager._effective_worktree_mode`): otherwise an
    # operator flipping `worktree.mode: dedicated` on a repo with existing runs
    # would silently move every one of them into a worktree on its next resume,
    # which is exactly the auto-migration spike §10 forbids. `config` is read in
    # exactly one place — `start()`, choosing what a NEW run is born as.
    worktree_mode: str | None = None

    @field_validator("worktree_mode")
    @classmethod
    def _worktree_mode(cls, v: str | None) -> str | None:
        """Reject a mode this engine cannot act on (F-006, fail closed).

        Validated at LOAD as well as at construction, because the dangerous
        direction is reading a manifest someone else wrote — a corrupt file, or
        one from a forward version that knows a third mode. `None` stays valid:
        it is every pre-P7c run.
        """
        if v is None:
            return None
        from gauntlet.engine.worktree import MODES

        if v not in MODES:
            raise ValueError(
                f"worktree_mode must be one of {sorted(MODES)} or absent; got "
                f"{v!r} — refusing to load a manifest whose tree layout this "
                "engine cannot determine"
            )
        return v

    # ---- record lookup -------------------------------------------------------
    def record(self, step_id: str, iteration: str | None = None) -> StepRecord | None:
        for rec in self.steps:
            if rec.id == step_id and rec.iteration == iteration:
                return rec
        return None

    def upsert(self, rec: StepRecord) -> StepRecord:
        for i, existing in enumerate(self.steps):
            if existing.id == rec.id and existing.iteration == rec.iteration:
                self.steps[i] = rec
                return rec
        self.steps.append(rec)
        return rec

    # ---- atomic persistence (FR-8.2; journaled write-ahead since P6) --------
    def write_atomic(self, path: Path) -> None:
        """Persist the manifest as one durable, journaled transition.

        This is the single atomic-persist primitive every write-ahead site
        uses, so P6 anchors the authoritative state journal here (plan §4.6):
        the exact payload about to land is first appended to the append-only
        journal under the run-instance state dir (``journal.record_transition``
        — write-ahead, the journal is the authority), then the projection file
        is atomically replaced (:func:`_replace_atomic`). One logical
        transition, two ordered durable writes, no new kill window: a kill
        between them leaves ``manifest.json`` exactly one journaled state
        behind, which the next mutating contact catches up idempotently
        (``journal.reconcile_projection``) — never a torn file, never an
        unclassifiable state.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump_json(indent=2)
        # Lazy import: journal is stdlib-only and imports nothing from this
        # module, but the low-level model module must not pull it (or anything)
        # in at import time.
        from gauntlet.engine import journal

        journal.record_transition(path, payload, validate=validate_projection_text)
        _replace_atomic(path, payload)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_text())


def validate_projection_text(text: str) -> bool:
    """Can the engine actually LOAD this manifest text? (P6.1, review F-002)

    The journal's injected state validator: only bytes that pass the full
    model validation may become an authoritative journal state (a genesis
    migration) or be reasoned about as a candidate projection. Bytes that
    merely happen to be JSON would otherwise wedge a run — every later
    ``Manifest.load`` of the authoritative head would raise, with nothing
    valid left to rebuild from. Kept here (not in :mod:`journal`) so the
    journal module stays free of engine imports; injected at every call site
    that can promote bytes to authority.
    """
    try:
        Manifest.model_validate_json(text)
    except ValueError:
        return False
    return True


def _replace_atomic(path: Path, payload: str) -> None:
    """Atomically replace ``path`` with ``payload`` (temp + fsync + replace).

    ``os.replace`` is atomic on POSIX within a filesystem, so a reader (or a
    resume after kill) always sees a whole manifest — the prior one until the
    instant the new one lands. ``fsync`` before replace so the bytes are
    durable, not just in the page cache, before the rename is visible. Module
    level (not a method) so the P6 crash harness can kill a real process at
    the exact event-append/projection-write boundary.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".manifest-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # On any failure (including KeyboardInterrupt) leave the prior
        # manifest untouched and clean up the temp file.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

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

from pydantic import BaseModel, Field

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

# The complete PRD parked_reason enum (the only values written / emitted).
PARKED_REASONS = frozenset(
    {
        PARKED_REASON_USAGE_LIMIT,
        PARKED_REASON_USAGE_WINDOW,
        PARKED_REASON_ARTIFACT_INVALID,
        PARKED_REASON_RESPONSE,
        PARKED_REASON_GATE,
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
# Step types that accept a `--response` decision when parked. An `agent_task`
# (the builder halting on UPSTREAM CONFLICT) re-runs with the decision injected
# into its prompt; an `adversarial_cycle` re-drives with it injected into the
# reviewer/triager so they re-evaluate the parked finding.
RESPONDABLE_STEP_TYPES = frozenset({"agent_task", "adversarial_cycle"})

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
# Failure kinds a plain (response-less) `gauntlet resume` may safely re-execute
# once the operator has fixed the named precondition — they cost nothing and
# re-run the guard, not the adapter.
RERUNNABLE_FAILURE_KINDS = frozenset({FAILURE_KIND_CLEAN_HANDOFF})

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
HALTED = "halted"  # budget/timeout guard tripped (FR-3.3)
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

    def add(self, usage: Any | None) -> None:
        if usage is None:
            return
        self.input_tokens += usage.input_tokens or 0
        self.output_tokens += usage.output_tokens or 0
        self.cached_input_tokens += usage.cached_input_tokens or 0
        if usage.cost_usd is not None:
            self.cost_usd = (self.cost_usd or 0.0) + usage.cost_usd


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


class ScheduledResume(BaseModel):
    """An armed auto-resume schedule on a usage-limit park (FR-3.4).

    Persisted on the parked step BEFORE the run parks, so a process death
    between scheduling and ``attempt_at`` loses nothing — the next driver start
    or ``gauntlet resume`` reconciles from disk. ``attempt_at`` is the absolute
    UTC time to resume (``now + retry_after_s``, else the quota reset time);
    ``attempts`` counts spaced attempts made; once ``attempts >= max_attempts``
    the step re-parks plain (no schedule) with an exhaustion note.
    """

    attempt_at: str
    attempts: int = 0
    max_attempts: int = 3


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


class Suspension(BaseModel):
    """A detected host-suspension interval (FR-5.1), appended to the manifest.

    ``gap_s`` is the wallclock width of the interval; ``start``/``end`` are the
    straddling heartbeats' wallclock stamps. Additive/append-only — older
    manifests load with an empty list.
    """

    start: str
    end: str
    gap_s: int


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
    # Which adversarial_cycle sub-step produced the preserved ``session_id`` on a
    # usage-limit park (FR-3.3): e.g. ``"r1-review"``, ``"r1-fix"``. Current-state
    # like ``parked_reason`` — set on a usage-limit park, cleared on any other
    # finalization. The cycle resume continues the preserved session only when it
    # re-reaches this exact sub-step, so a fixer/triager session is never
    # misrouted into the round-1 reviewer. Additive/nullable: older manifests load
    # unchanged.
    parked_substep: str | None = None
    # Armed auto-resume schedule on a usage-limit park (FR-3.4). Set only when
    # ``resume_on_quota: auto`` parked this step; ``None`` otherwise and after a
    # successful resume. Additive/nullable — older manifests load unchanged.
    scheduled_resume: ScheduledResume | None = None
    # Content-hash pair recorded on an ``artifact_invalid`` park (FR-7.2/§6). P3
    # defines the shape here; P4 populates it on the validator-repair park path.
    # Additive/nullable — ``None`` on every other outcome, so older manifests load
    # unchanged.
    revalidation: RevalidationRecord | None = None
    # Append-only audit trail of human `--response` decisions on this step
    # (FR-2). Recording/consume wiring is P3; P1 carries the schema only.
    human_responses: list[HumanResponse] = Field(default_factory=list)
    usage: UsageTotals = Field(default_factory=UsageTotals)
    notes: str | None = None
    # foreach binding key, when this record is one iteration of a fan-out step
    iteration: str | None = None
    # Step-emitted structured outcome counts for `gauntlet report --trend`
    # (FR-6.6): an adversarial_cycle records rounds, finding/verdict/confirm
    # tallies here so trend math reads the manifest, never the log dirs (the
    # plan's P7 test strategy is "trend-metric math from fixture manifests").
    metrics: dict[str, Any] = Field(default_factory=dict)


class CommitRecord(BaseModel):
    step_id: str
    phase: str  # the PN[.x] prefix
    sha: str


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

    # ---- atomic persistence (FR-8.2) ----------------------------------------
    def write_atomic(self, path: Path) -> None:
        """Write the manifest atomically: temp file in the same dir + replace.

        ``os.replace`` is atomic on POSIX within a filesystem, so a reader (or a
        resume after kill) always sees a whole manifest — the prior one until
        the instant the new one lands. ``fsync`` before replace so the bytes are
        durable, not just in the page cache, before the rename is visible.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.model_dump_json(indent=2)
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

    @classmethod
    def load(cls, path: Path) -> Manifest:
        return cls.model_validate_json(path.read_text())

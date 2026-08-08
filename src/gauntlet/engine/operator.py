"""Operator observability core: driver liveness + composite run-state (P1).

The single, pure, deterministic computation behind the self-describing
``gauntlet status`` footer (P1, FR-1/FR-2), the ``status --json`` contract (P3,
FR-4), and — read-only — the report half of crash reconciliation (FR-5.6). It
reads the on-disk drive lock + the OS process-identity primitives and renders a
truthful liveness signal and the correct next action; it **never** writes, and
it **never** trusts ``manifest.status`` for liveness.

Three layers, all pure:

* :func:`driver_info` / :func:`driver_liveness` — the FR-2.4 total failure-mode
  table over the drive lock + ``procident`` primitives (rows a–h). It probes PID
  liveness and process identity *separately* (never via
  ``procident.process_is_alive``, which collapses "dead" and
  "identity-unverifiable" the wrong way for this purpose) so a live-but-
  unverifiable driver reads ``indeterminate``, never a false ``orphaned``.
* :func:`compute_run_state` / :func:`next_actions` — the §6.3 + §6.3a total
  decision table mapping ``(run_status, liveness, descriptor)`` to one of the
  eleven composite classes and the structured next action(s). Both the human
  footer and ``--json`` render *this one* return value, so they can never
  disagree.
* run-instance / step / transcript-leaf resolution (FR-3.1a) + the read-only
  recovery-intent parser (FR-5.6 report half) — metadata-driven, never mtime.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from datetime import datetime, timezone

from gauntlet.engine import heartbeat as HB
from gauntlet.engine import locking
from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import Manifest, StepRecord
from gauntlet.engine.pipeline import Pipeline, upstream_cycle_id_for_gate
from gauntlet.engine.run import (
    DRIVING_LOCK_NAME,
    RECOVERY_INTENT_NAME,
    UnsafeRunSegment,
    _LockRecord,
    safe_run_segment,
)
from gauntlet.logging.transcript import FAILURE_MARKER_NAME, STREAM_MARKER_SUFFIX
from gauntlet.procident import ProcessIdentity, read_process_identity

if TYPE_CHECKING:
    from gauntlet.logging.redact import RedactionSettings

# --- liveness values (FR-2.4) ------------------------------------------------
LIVENESS_ALIVE = "alive"
LIVENESS_ORPHANED = "orphaned"
LIVENESS_INDETERMINATE = "indeterminate"
LIVENESS_NONE = "none"

# --- composite run-state classes (§6.3, the eleven-class total set) ----------
# P4 (plan §4.2 / R4): the classification core and the state → mutating-action
# table live in `recovery_exec` — the SAME module the mutating resume path and
# the recovery planner consume — so this read-only surface and the mutating
# verbs cannot drift. The names are re-exported here unchanged; the values are
# the §6.1 `state` enum verbatim. Per-state semantics:
#   parked_usage_limit — a provider usage-limit park (FR-3.2); NO human
#     decision — a plain `gauntlet resume` continues the session (FR-3.3).
#   parked_usage_window — an enforce-mode pre-step window park (FR-10.3); a
#     plain `resume` retries once the window replenishes.
#   parked_artifact_invalid — a validated artifact failed its repair loop
#     (FR-2.2); a plain `resume` re-runs ONLY the validator (hand-edit
#     sanctioned).
from gauntlet.engine.recovery_exec import (  # noqa: E402  (grouped re-export)
    STATE_ABORTED,
    STATE_DONE,
    STATE_FAILED,
    STATE_HALTED,
    STATE_INDETERMINATE,
    STATE_INTERRUPTED,
    STATE_IN_PROGRESS,
    STATE_ORPHANED,
    STATE_PARKED_ARTIFACT_INVALID,
    STATE_PARKED_FOR_RESPONSE,
    STATE_PARKED_GATE,
    STATE_PARKED_PROVIDER_UNAVAILABLE,
    STATE_PARKED_USAGE_LIMIT,
    STATE_PARKED_USAGE_WINDOW,
    STATE_UNKNOWN,
)
from gauntlet.engine import journal as JR  # noqa: E402  (P6 projection view)
from gauntlet.engine import recovery_exec as RX  # noqa: E402
from gauntlet.engine import recovery as RXM  # noqa: E402  (P1 models)

# The failure-status set and the park-reason normalization now live with the
# classification core in `recovery_exec` (P4/R4); nothing here keys on them
# directly any more.

# The failing-leaf evidence marker (P5, issue #63) — the single name is owned
# by the transcript logger; re-bound here for the leaf-selection reads.
_FAILURE_NAME = FAILURE_MARKER_NAME

# Composite step types whose evidence lives in role sub-directories, not a
# direct ``steps/<leaf>/transcript.md`` (FR-3.1a). Mirrors the cycle/retro
# registrations in :data:`gauntlet.engine.steptypes.SPECS`; every other type
# (including an unrecognized one) is treated as atomic and falls back to the
# direct transcript path, so a new step type can never silently misresolve.
_COMPOSITE_STEP_TYPES = frozenset({"adversarial_cycle", "retrospective"})

# A short, human "what this state means" line for the status footer (FR-1.1).
_MEANING: dict[str, str] = {
    STATE_IN_PROGRESS: "driver alive and working — observe only, no action needed",
    STATE_ORPHANED: "manifest says running but the driver is gone; the lock is reclaimable",
    STATE_INDETERMINATE: "cannot prove the driver is alive or dead — inspect read-only before acting",
    STATE_PARKED_GATE: "awaiting a human decision at a gate",
    STATE_PARKED_FOR_RESPONSE: "awaiting a `resume --response` decision",
    STATE_PARKED_USAGE_LIMIT: "paused by a provider usage limit — `resume` continues the session",
    STATE_PARKED_USAGE_WINDOW: "parked before a step to stay within the provider usage window — `resume` retries once it replenishes",
    STATE_PARKED_ARTIFACT_INVALID: "a validated artifact is malformed — hand-edit it, then `resume` re-runs the validator",
    STATE_PARKED_PROVIDER_UNAVAILABLE: "a provider/transport failure exhausted the bounded retries — `resume` retries after the recorded deadline (no decision needed)",
    STATE_FAILED: "a step failed",
    STATE_HALTED: "a guard halted the step (reason unrecorded — see steps[].halt_reason in status --json)",
    STATE_INTERRUPTED: "the run was killed mid-step",
    STATE_DONE: "run complete",
    STATE_ABORTED: "run aborted by an operator",
    STATE_UNKNOWN: "unrecognized or contradictory run state — inspect read-only only",
}

# A halted run's footer meaning, keyed by the halted step's ``halt_reason``
# (#64: every HALTED run used to render as "the budget/timeout guard tripped",
# misdirecting the operator for the five other reasons). The `_MEANING` entry
# above is the fallback for a null/unknown reason (pre-P3 manifests).
_HALT_REASON_MEANING: dict[str, str] = {
    M.HALT_REASON_TIMEOUT: "a step timeout tripped — inspect logs, then `resume` re-runs the step",
    M.HALT_REASON_BUDGET: "the cost-budget guard tripped — raise the budget or trim scope, then `resume`",
    M.HALT_REASON_JUDGE_DENY: "a judge deny terminated the step — review the judge decision before resuming",
    M.HALT_REASON_SIGNAL_KILL: "the step was killed by a signal/crash mid-flight",
    M.HALT_REASON_ADAPTER_ERROR: "a terminal adapter/handler failure ended the step",
    M.HALT_REASON_PRECONDITION: "a fail-closed precondition guard rejected the step before anything ran — fix the named defect (see the step notes), then `resume` re-runs the check",
    M.HALT_REASON_OPERATOR_RECOVER: "`gauntlet recover` finalized the step",
}


class RunResolutionError(RuntimeError):
    """Run-instance/step selection could not resolve deterministically (FR-3.1a)."""


class StatusContractError(RuntimeError):
    """A persisted manifest/lock value cannot be rendered as a §6.1 status
    payload, so we fail closed (FR-4.3) rather than emit a contract-violating
    object.

    Raised for: a non-canonical step ``iteration`` (so ``current_step`` and
    ``steps[].iteration`` can never disagree — F-001); a step ``id`` that is not
    a single safe path segment (so ``failure.evidence_path`` can never escape
    ``run_root`` — F-002); and a completed payload that fails validation against
    the committed ``schemas/status.json`` (so an unconstrained persisted value —
    e.g. an out-of-enum step status or a non-string lock field — can never reach
    a consumer as schema-invalid JSON — F-003)."""


# --- structured action + state records --------------------------------------
@dataclass
class Action:
    """A structured, safely-executable next action (FR-4.2).

    ``argv`` is fully split and resolved (no shell quoting/interpolation);
    ``command`` is the human-display rendering and is **never** executed.
    ``executable`` is ``True`` only when ``required_inputs`` is empty and
    ``argv`` is complete and safe to run as-is.
    """

    label: str
    kind: str  # observe | decide | control | recover
    argv: list[str]
    required_inputs: list[str]
    executable: bool
    command: str
    # One-line "what this does when taken" (FR-8.2). Populated for gate decisions
    # (approve → what proceeds; reject → which cycle re-runs with the notes
    # injected); None for actions with no distinct consequence to spell out.
    consequence: str | None = None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "kind": self.kind,
            "argv": list(self.argv),
            "required_inputs": list(self.required_inputs),
            "executable": self.executable,
            "command": self.command,
            "consequence": self.consequence,
        }


@dataclass
class ParkedDescriptor:
    step_id: str
    type: str
    reason: str | None


@dataclass
class FailureDescriptor:
    step_id: str
    status: str  # failed | halted | interrupted
    # The FAILED step's ``failure_kind`` (current-state): a re-runnable
    # PRECONDITION failure (e.g. FR-9.3 clean-handoff) takes plain `resume` once
    # the operator fixes the precondition, while a terminal failure needs a
    # `resume --response` decision — so the next-action recommendation differs.
    failure_kind: str | None = None
    # The step's ``halt_reason`` (HALT_REASON_*), when recorded. Drives the
    # halted footer's reason-specific meaning line (#64); not part of the
    # ``--json`` failure object (``steps[].halt_reason`` already carries it).
    halt_reason: str | None = None


@dataclass
class DriverInfo:
    """The rendered driver-liveness view (§6.1 ``driver`` object)."""

    state: str  # one of the LIVENESS_* values
    pid: int | None
    host: str | None
    since: str | None


@dataclass
class RunState:
    """The single computed state both the footer and ``--json`` render."""

    state: str
    slug: str
    current_step: str | None
    parked: ParkedDescriptor | None
    failure: FailureDescriptor | None
    next_actions: list[Action] = field(default_factory=list)


@dataclass
class Reconciliation:
    """Report-only notice that a recovery intent survives (§6.1, FR-5.6).

    Produced by read-only ``status`` detection; ``status`` never finalizes.
    """

    intent_step_id: str
    nonce_matches_lock: bool
    recommended_command: str

    def to_dict(self) -> dict:
        return {
            "intent_step_id": self.intent_step_id,
            "nonce_matches_lock": self.nonce_matches_lock,
            "recommended_command": self.recommended_command,
        }


# --- patchable OS primitives (so tests can drive the FR-2.4 rows) ------------
def _probe_pid(pid: int) -> str:
    """Return ``dead`` | ``alive`` | ``unknown`` from ``os.kill(pid, 0)``.

    ``dead`` is the only *proof* of absence (``ProcessLookupError``); a
    permission error means the pid exists but is owned by another user (alive,
    identity decides); any other ``OSError`` cannot prove either way and is
    reported ``unknown`` → mapped to ``indeterminate`` by the caller (fail
    closed).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


def _this_host() -> str:
    return socket.gethostname()


# --- the drive lock (the single read path) -----------------------------------
def _lock_file_state(path: Path) -> tuple[str, _LockRecord | None]:
    """Read one lockfile → ``(kind, record)``.

    ``kind`` is ``absent`` (no file), ``malformed`` (unreadable or unparseable
    / missing required field — FR-2.4 row g, fail closed), or ``present``.

    Delegates to :func:`locking.read_lock_state` (review F-002): this
    tri-state distinction used to exist ONLY here, while the mutating acquire
    path collapsed malformed into absent and deleted the file. One primitive,
    so the read-only view and the mutating verbs cannot disagree (R4).
    """
    return locking.read_lock_state(path)


def _lock_state(
    run_root: Path, run_instance_dir: Path | None = None
) -> tuple[str, _LockRecord | None]:
    """The drive lock for a run: per-run path first, worktree-global second (P7b).

    P7b splits one lockfile into two scopes and publishes ONE record at both,
    so for a run driven by a P7b engine either file answers. Reading the
    per-run path first, and the worktree-global path only when the per-run one
    is ``absent``, is what makes three populations readable by one function:

    * a **P7b run** — answered by its own per-run lock;
    * a **legacy run** started by a pre-P7b engine, which only ever wrote
      ``<run_root>/.driving.lock`` — answered by the fallback (spike §10:
      legacy locks are read so a half-migrated machine cannot double-drive);
    * **another slug driving this shared tree** — no per-run lock for the slug
      being asked about, and a worktree-global lock naming someone else, which
      is FR-2.4 row b.

    A per-run lock that is present but ``malformed`` does NOT fall through: an
    unreadable lock for exactly this run is row g (indeterminate, fail closed),
    and silently consulting a different file would downgrade that to a
    confident answer.

    ``run_instance_dir=None`` reads the worktree-global path alone — the
    pre-P7b behaviour, and what callers that have no resolved instance (or that
    are deliberately asking "who owns this tree?") get.
    """
    return _lock_state_scoped(run_root, run_instance_dir)[:2]


# Which file answered, so the caller can tell "another slug owns the shared
# TREE" (legitimate, FR-2.4 row b) from "another slug's record is sitting at
# THIS RUN's authoritative path" (inconsistent evidence — review F-003).
_SCOPE_RUN = "run"
_SCOPE_SLUG = "slug"  # the #86 per-slug minting lock
_SCOPE_TREE = "tree"


def _lock_state_scoped(
    run_root: Path, run_instance_dir: Path | None = None, *,
    slug: str | None = None,
) -> tuple[str, _LockRecord | None, str]:
    """:func:`_lock_state` plus which scope answered.

    ``slug`` (#86) inserts the per-slug minting lock between the per-run and
    tree scopes: during `start`'s minting window no run instance exists yet, so
    without this scope the answer for a run being minted would be "no driver" —
    exactly the wrong moment for that answer.
    """
    if run_instance_dir is not None:
        kind, rec = _lock_file_state(Path(run_instance_dir) / DRIVING_LOCK_NAME)
        if kind != locking.LOCK_ABSENT:
            return (kind, rec, _SCOPE_RUN)
    if slug is not None:
        kind, rec = _lock_file_state(Path(run_root) / slug / DRIVING_LOCK_NAME)
        if kind != locking.LOCK_ABSENT:
            return (kind, rec, _SCOPE_SLUG)
    return (*_lock_file_state(Path(run_root) / DRIVING_LOCK_NAME), _SCOPE_TREE)


def _liveness_for_record(rec: _LockRecord) -> str:
    """Apply the FR-2.4 present-lock outcomes (rows c–h) to a parsed record.

    Probes liveness (``os.kill``) and identity (``read_process_identity``)
    **separately** — never via ``process_is_alive`` — so an alive-but-
    unverifiable driver maps to ``indeterminate`` (row f), never a false
    ``orphaned``.
    """
    probe = _probe_pid(rec.pid)
    if probe == "dead":
        return LIVENESS_ORPHANED  # row c — proven dead
    if probe == "unknown":
        return LIVENESS_INDETERMINATE  # cannot prove alive or dead → fail closed
    # PID is live. Identity decides ownership / PID-reuse.
    recorded = ProcessIdentity.from_dict(rec.proc_identity)
    fresh = read_process_identity(rec.pid)
    if recorded is None or fresh is None:
        return LIVENESS_INDETERMINATE  # row f — identity unobtainable
    if not recorded.same_process(fresh):
        return LIVENESS_ORPHANED  # row d — both present and unequal → PID reuse
    # Identities present and equal → row e/h by host equality.
    if rec.host and rec.host == _this_host():
        return LIVENESS_ALIVE  # row e
    return LIVENESS_INDETERMINATE  # row h — foreign-host (or unrecorded) host


def driver_info(
    run_root: Path, slug: str, *, run_instance_dir: Path | None = None
) -> DriverInfo:
    """The full driver-liveness view for ``slug`` (FR-2.4 total table).

    ``pid``/``host``/``since`` are populated from the lock only when a parsed
    record for this slug yields a non-``none`` liveness; they are ``None``
    (the §6.1 nullable contract) for the no-lock, foreign, and malformed cases.

    ``run_instance_dir`` (P7b) is the run whose driver is being asked about. It
    must be a resolved, containment-checked instance — ``resolve_run_instance``
    plus the two-link chain in ``cli._resolve_run_instance_dir``, never a raw
    ``run_root/slug/<name>`` join — because these bytes ultimately come from
    ``active-run.txt``. Omitting it reads the worktree-global lock alone, which
    stays correct (it is where a legacy run's lock lives, and where a foreign
    slug's tree hold shows up) but cannot distinguish two P7b runs of one slug.

    The table stays TOTAL and every row stays reachable:

    * rows a/c–h are reached from whichever lockfile ``_lock_state`` answered;
    * row b (**foreign lock → none**) is reached from the WORKTREE-GLOBAL lock,
      which genuinely names another slug whenever that slug is driving this
      shared tree, and from a legacy lock left by another slug's run. It is
      *not* legacy-only in P7b. "No driver for this slug" is the true answer
      there: the other slug's hold is transient contention, not evidence about
      this run.

    A foreign record at the **per-run** path is a different thing entirely and
    is NOT row b (review F-003). That path is this run's own authoritative
    evidence; a record naming someone else there is inconsistent — the file was
    misplaced, hand-copied, or corrupted. Reporting ``none`` would tell the
    operator "no driver, go ahead and resume" while the mutating path refuses
    the very same lock as a live foreign holder, which is precisely the R4
    disagreement ("the read-only view cannot ... recommend a resume that the
    mutating path will reject"). It is ``indeterminate``: fail closed, offer
    read-only actions, and let the operator look.
    """
    kind, rec, scope = _lock_state_scoped(run_root, run_instance_dir, slug=slug)
    if kind == locking.LOCK_ABSENT:
        return DriverInfo(LIVENESS_NONE, None, None, None)  # row a
    if kind == locking.LOCK_MALFORMED:
        return DriverInfo(LIVENESS_INDETERMINATE, None, None, None)  # row g
    assert rec is not None
    if rec.slug != slug:
        if scope in (_SCOPE_RUN, _SCOPE_SLUG):
            # Inconsistent evidence at this run's/slug's own path → fail closed
            # (#86: a foreign record at THIS slug's mint path is misplacement,
            # not another slug's legitimate tree hold).
            return DriverInfo(LIVENESS_INDETERMINATE, None, None, None)
        return DriverInfo(LIVENESS_NONE, None, None, None)  # row b — foreign tree hold
    state = _liveness_for_record(rec)
    return DriverInfo(state, rec.pid, rec.host or None, rec.started_at or None)


def driver_liveness(
    run_root: Path, slug: str, *, run_instance_dir: Path | None = None
) -> str:
    """Just the FR-2.4 liveness value (``alive``/``orphaned``/``indeterminate``/``none``)."""
    return driver_info(run_root, slug, run_instance_dir=run_instance_dir).state


# --- structured next actions (FR-4.2 object shape) ---------------------------
def _observe_logs(slug: str) -> Action:
    return Action("logs", "observe", ["gauntlet", "logs", slug], [], True,
                  f"gauntlet logs {slug}")


def _observe_status_json(slug: str) -> Action:
    return Action("status (json)", "observe",
                  ["gauntlet", "status", slug, "--json"], [], True,
                  f"gauntlet status {slug} --json")


def _control_resume(slug: str) -> Action:
    return Action("resume", "control", ["gauntlet", "resume", slug], [], True,
                  f"gauntlet resume {slug}")


def _decide_approve(slug: str, *, gate_cycle_id: str | None = None) -> Action:
    # FR-8.2 consequence: approving ratifies the artifact and the run proceeds
    # past the gate to the next stage/phase.
    consequence = "continues the run past this gate to the next stage"
    return Action("approve", "decide", ["gauntlet", "approve", slug], [], True,
                  f"gauntlet approve {slug}", consequence=consequence)


def _decide_reject(slug: str, *, gate_cycle_id: str | None = None) -> Action:
    # `--notes` is a flag with no value here; the operator supplies the reason,
    # so the action is non-executable and `command` carries a placeholder.
    # FR-8.2 consequence: a reject downstream of an adversarial_cycle injects the
    # notes into that cycle and re-runs it (reject_gate); with no upstream cycle to
    # iterate it is a terminal reject.
    if gate_cycle_id:
        consequence = (
            f"re-runs the '{gate_cycle_id}' adversarial cycle with your notes "
            "injected as a new round"
        )
    else:
        consequence = "terminally rejects the gate (no upstream cycle to re-run)"
    return Action("reject", "decide", ["gauntlet", "reject", slug, "--notes"],
                  ["notes"], False,
                  f'gauntlet reject {slug} --notes "<your reason>"',
                  consequence=consequence)


def _decide_resume_response(slug: str) -> Action:
    return Action("resume --response", "decide",
                  ["gauntlet", "resume", slug, "--response"], ["response"], False,
                  f'gauntlet resume {slug} --response "<your decision>"')


def _control_reset_interrupted(slug: str) -> Action:
    # #72: the sanctioned resolution for an interrupted park — a plain resume
    # re-parks when the tree/branch is dirty vs the step's base, and the only
    # alternative used to be forbidden git surgery. This verb discards the
    # interrupted attempt non-destructively (backup ref first, committed
    # checkpoints preserved) and re-runs the step.
    consequence = (
        "preserves the interrupted attempt's partial work as a recovery "
        "snapshot under refs/gauntlet/recovery/, rewinds to the latest "
        "committed checkpoint, and re-runs the step cleanly (one-shot; "
        "config policy unchanged)"
    )
    return Action("resume --reset-interrupted", "control",
                  ["gauntlet", "resume", slug, "--reset-interrupted"], [], True,
                  f"gauntlet resume {slug} --reset-interrupted",
                  consequence=consequence)


# The observe-only prefix rows per composite state — a rendering concern, not a
# recovery decision, so they stay here while the mutating rows derive from the
# shared `recovery_exec` table (P4/R4).
_OBSERVE_PREFIX: dict[str, tuple[str, ...]] = {
    STATE_IN_PROGRESS: ("logs", "json"),
    STATE_INDETERMINATE: ("logs", "json"),
    STATE_UNKNOWN: ("logs", "json"),
    STATE_PARKED_ARTIFACT_INVALID: ("logs",),
    STATE_PARKED_PROVIDER_UNAVAILABLE: ("logs",),
    STATE_FAILED: ("logs",),
    STATE_HALTED: ("logs",),
    STATE_INTERRUPTED: ("logs",),
}


def _control_abort(slug: str) -> Action:
    # F-007: the executable exit for a terminal failure resume cannot re-run
    # and `--response` cannot target (a non-respondable step type). Evidence
    # and snapshots are retained.
    return Action(
        "abort", "control", ["gauntlet", "abort", slug], [], True,
        f"gauntlet abort {slug}",
        consequence="aborts the run, retaining every snapshot and all evidence",
    )


def _actions_for(
    state: str, slug: str, failure: "FailureDescriptor | None" = None,
    *, gate_cycle_id: str | None = None, failure_step_type: str | None = None,
) -> list[Action]:
    """The §6.3 next-action column for a composite ``state`` (total).

    P4 (R4): the mutating rows are DERIVED from
    :func:`recovery_exec.mutating_action_kinds` — the same table the resume
    path chooses its default action from — so the read-only view can never
    recommend a verb the mutating path rejects (or vice versa). The rendering
    per kind: RETRY → plain ``resume`` (FR-3.3: a usage park's resume
    continues the preserved session; FR-2.2: an artifact park's resume re-runs
    only the validator; a re-runnable FR-9.3 precondition failure re-runs the
    guard); HUMAN_DECISION → the gate approve/reject pair (FR-8.2, with the
    reject consequence naming the cycle it re-drives) or ``resume
    --response``; SNAPSHOT_AND_RESTART → ``resume --reset-interrupted`` (#72);
    ABORT → ``gauntlet abort`` (F-007: a terminally failed non-respondable
    step's only executable exit). Indeterminate and unknown states expose
    read-only inspection only.
    """
    actions = _render_observe_prefix(state, slug)
    failure_kind = failure.failure_kind if failure is not None else None
    for kind in RX.mutating_action_kinds(
        state, failure_kind=failure_kind, step_type=failure_step_type
    ):
        if kind is RXM.RecoveryActionKind.RETRY:
            actions.append(_control_resume(slug))
        elif kind is RXM.RecoveryActionKind.HUMAN_DECISION:
            if state == STATE_PARKED_GATE:
                actions.append(_decide_approve(slug, gate_cycle_id=gate_cycle_id))
                actions.append(_decide_reject(slug, gate_cycle_id=gate_cycle_id))
            else:
                actions.append(_decide_resume_response(slug))
        elif kind is RXM.RecoveryActionKind.SNAPSHOT_AND_RESTART:
            actions.append(_control_reset_interrupted(slug))
        elif kind is RXM.RecoveryActionKind.ABORT:
            actions.append(_control_abort(slug))
    return actions


def _render_observe_prefix(state: str, slug: str) -> list[Action]:
    tokens = _OBSERVE_PREFIX.get(state, ())
    out: list[Action] = []
    for token in tokens:
        out.append(_observe_logs(slug) if token == "logs" else _observe_status_json(slug))
    return out


def render_assessment_actions(
    assessment: "RXM.RecoveryAssessment",
    state: str,
    slug: str,
    *,
    gate_cycle_id: str | None = None,
) -> list[Action]:
    """Render the planner's ``safe_actions`` as §6.1 CLI action rows (P4/R4).

    The read-only surface renders EXACTLY what the mutating path will do: for
    the base composite states the planner's actions come from the same table
    :func:`_actions_for` consumes (identical rows), and for the
    branch-relation refinements (plan §5.4) the adopted/checkpoint/recovery-
    branch actions render with their consequence spelled out. Every rendered
    row is additive against the v1 ``next_actions`` contract (same object
    shape; `kind: recover` is already in the schema enum).
    """
    actions = _render_observe_prefix(state, slug)
    for act in assessment.safe_actions:
        kind = act.kind
        if kind is RXM.RecoveryActionKind.RETRY:
            actions.append(_control_resume(slug))
        elif kind is RXM.RecoveryActionKind.HUMAN_DECISION:
            if state == STATE_PARKED_GATE:
                actions.append(_decide_approve(slug, gate_cycle_id=gate_cycle_id))
                actions.append(_decide_reject(slug, gate_cycle_id=gate_cycle_id))
            else:
                actions.append(_decide_resume_response(slug))
        elif kind is RXM.RecoveryActionKind.SNAPSHOT_AND_RESTART:
            actions.append(_control_reset_interrupted(slug))
        elif kind in (
            RXM.RecoveryActionKind.RESTART_FROM_CHECKPOINT,
            RXM.RecoveryActionKind.ADOPT_COMMITS,
        ):
            # Plain resume IS the adoption vehicle (plan §5.4/R6): the row
            # renders the same executable command with the adoption spelled
            # out as its consequence.
            resume = _control_resume(slug)
            resume.consequence = act.description
            actions.append(resume)
        elif kind is RXM.RecoveryActionKind.CONTINUE_ON_RECOVERY_BRANCH:
            actions.append(_restore_ref_action(act))
        elif kind is RXM.RecoveryActionKind.ABORT:
            actions.append(
                Action(
                    "abort", "control", ["gauntlet", "abort", slug], [], True,
                    f"gauntlet abort {slug}", consequence=act.description,
                )
            )
        elif kind is RXM.RecoveryActionKind.REBUILD_PROJECTION:
            # P6 (plan §5.5): a plain resume IS the rebuild vehicle — it
            # reconciles the projection through the shared executor action
            # before driving. The row renders the same executable command
            # with the rebuild spelled out as its consequence (R4).
            actions.append(projection_rebuild_action(slug, act))
        # Kinds the planner does not emit (resume_session, edit_then_retry,
        # restore_snapshot) have no rendering yet; adding one later is
        # additive.
    return actions


def _restore_ref_action(act: "RXM.ContinueOnRecoveryBranchAction") -> Action:
    """The §6.1 row for a non-destructive ref restoration (plan §5.4).

    Two shapes, and both are guarded rather than trusted (post-review F-003).

    * ``via="ff_merge"`` — the operator is standing ON the run branch, where a
      forced ref move is invalid. A pure fast-forward merge is git's own guard:
      it refuses anything that is not a fast-forward, so nothing can be
      discarded whatever the ref does between the assessment and the command.
    * ``via="branch_force"`` — every other case, rendered as a **compare-and-swap**
      ``git update-ref <ref> <new> <expected>`` rather than ``git branch -f``.

    Why the change of verb. An assessment is a photograph, and the command it
    advertises runs whenever the operator gets to it. ``git branch -f`` applies
    unconditionally, so a resume (or a second operator) that advanced the ref in
    that gap gets rewound by a decision taken before it happened, and the
    commits it made are orphaned with nothing in the run's evidence explaining
    it. ``update-ref``'s third argument is the guard git's own ref transaction
    enforces atomically: the empty string means "must not exist" — the
    missing-branch and fork-preservation forms, which create and never overwrite
    — and a SHA means "must still be exactly this". A ref that moved therefore
    fails loudly and leaves everything intact, which is the fail-closed
    direction and what lets plan §5.4's "every action is non-destructive" hold
    as a property of the command rather than of the moment it was computed.

    This is the guard, not a lock: it makes the mutation refuse-or-apply against
    the observed value, which is exactly what an operator running a git command
    by hand can be given. It does not make the restoration a driven Gauntlet
    transaction with an intent and a journal record — that is a new verb and a
    third executor plane, deferred and tracked in `FUTURE.md` (PR #87 review).

    One property ``branch -f`` had that ``update-ref`` does not is git's refusal
    to move the CHECKED-OUT branch. It is not lost where it matters: the only
    form that moves an *existing* ref is the behind-relation one, and a behind
    run branch the operator is standing on renders ``ff_merge`` instead. The
    residue is an operator who checks the run branch out in the gap — there the
    guarded move still applies, because the ref is where it was observed, and
    it is a fast-forward: every commit stays reachable and the reflog records
    the pre-state, so the visible cost is a stale index one ``git reset --hard``
    corrects. That is a different failure class from orphaning commits.

    The displayed command and ``argv`` must stay interchangeable — an operator
    copies the string. The create-only form therefore renders the empty
    old-value as a quoted ``''``: dropping it would silently turn a
    compare-and-swap back into the unconditional update this exists to remove.
    """
    ref = f"refs/heads/{act.branch_name}"
    if act.via == "ff_merge":
        argv = ["git", "merge", "--ff-only", act.start_sha]
        command = f"git merge --ff-only {act.start_sha[:10]}"
        guard = (
            f"; refuses unless {act.branch_name} is still an ancestor of it "
            "(fast-forward only, so nothing can be discarded)"
        )
    elif act.expected_sha is not None:
        argv = ["git", "update-ref", ref, act.start_sha, act.expected_sha]
        command = (
            f"git update-ref {ref} {act.start_sha[:10]} {act.expected_sha[:10]}"
        )
        guard = (
            f"; refuses unless {act.branch_name} is still at "
            f"{act.expected_sha[:10]} (compare-and-swap)"
        )
    else:
        argv = ["git", "update-ref", ref, act.start_sha, ""]
        command = f"git update-ref {ref} {act.start_sha[:10]} ''"
        guard = f"; refuses if {act.branch_name} exists by then (create-only)"
    return Action(
        "restore run branch",
        "recover",
        argv,
        [],
        True,
        command,
        consequence=act.description + guard,
    )


def projection_catchup_action(slug: str, detail: str | None) -> Action:
    """The §6.1 action row for a stale projection catch-up (P6, R8).

    A plain resume's projection reconciliation rewrites ``manifest.json``
    from the journal head before anything else runs — the branch-reset /
    kill-window repair, loudly audited.
    """
    return Action(
        "catch up manifest projection",
        "recover",
        ["gauntlet", "resume", slug],
        [],
        True,
        f"gauntlet resume {slug}",
        consequence=detail
        or "rewrites the stale manifest.json from the authoritative journal head",
    )


def projection_rebuild_action(slug: str, act: "RXM.RebuildProjectionAction") -> Action:
    """The §6.1 action row for a pending manifest-projection rebuild (P6).

    Rendered by the read-only surface from the SAME
    :func:`RX.projection_rebuild_assessment` the mutating verbs apply
    (R4): the executable command is a plain ``gauntlet resume``, whose
    projection reconciliation applies exactly this action through
    :meth:`RecoveryExecutor.apply_rebuild` before anything else runs.
    """
    return Action(
        "rebuild manifest projection",
        "recover",
        ["gauntlet", "resume", slug],
        [],
        True,
        f"gauntlet resume {slug}",
        consequence=act.description,
    )


def migrate_worktree_action(slug: str) -> Action:
    """The §6.1 action row offering an eligible run its own worktree (P7c-2).

    Spike §10's second table row: a run that predates the dedicated layout
    "keeps driving in `same_tree` mode; `status` surfaces an *optional* action
    `gauntlet migrate-worktree <slug>`". Optional is the operative word — the
    operator chooses, and the run is fully drivable either way, so this is an
    ADDITIONAL action appended after the ones that move the run forward, never
    a replacement for them.

    The eligibility decision is deliberately NOT made here. It is
    ``RunManager.migration_blocker``, the negation of the single mode-resolution
    rule, so the read-only surface can never offer a migration the verb would
    refuse (R4). This function only renders it.

    P7c-1 shipped the `worktree` status object able to express this case
    already (mode `same_tree`, nothing registered), which is why P7c-2 adds an
    action row and no schema field — the object reports observed facts, never a
    recommendation (`proposals/P7c-split-seam.md` §5).
    """
    return Action(
        "move this run into its own worktree",
        "control",
        ["gauntlet", "migrate-worktree", slug],
        [],
        True,
        f"gauntlet migrate-worktree {slug}",
        consequence=(
            "Optional. Creates a dedicated worktree for this run and drives "
            "there from now on, so the run stops editing your checkout. The "
            "branch, the journal and the run dir do not move; undo with "
            f"`gauntlet migrate-worktree {slug} --rollback`."
        ),
    )


def compute_status_assessment(
    repo_root: Path,
    man: Manifest,
    liveness: str,
    *,
    run_instance_dir: Path,
    work_root: Path | None = None,
) -> "RXM.RecoveryAssessment | None":
    """Build the shared recovery assessment for the read-only status surface.

    The single I/O point behind ``status → assess → render`` (plan §4.2): the
    same ``observe_git`` / ``ProgressFingerprint`` machinery the mutating
    verbs consume, evaluated read-only. Fail-soft: any observation failure
    (no repo, unreadable refs, a merge inside the inventoried range) returns
    ``None`` and the caller renders the pure state table instead — status
    must never crash or mutate because advisory git evidence was
    unavailable.

    ``work_root`` (F-007) is the tree this run actually drives — the run
    worktree for a `dedicated` run, the operator's checkout otherwise. R4 says
    the read-only view and the mutating path must not disagree, and they
    observe INDEX and WORKTREE planes: assessing the operator's checkout for a
    dedicated run would let the operator's own uncommitted edits change the
    recommendation while the run tree's real dirt stayed invisible, so `status`
    and `resume` would describe different trees and recommend accordingly.
    Defaults to ``repo_root``, which is the same-tree answer and every
    pre-P7c caller's behaviour.
    """
    from gauntlet.engine import gitops
    from gauntlet.engine.execution import (
        engine_bookkeeping_candidates,
        governed_artifact_paths,
        run_bookkeeping_excludes,
    )

    work = work_root or repo_root
    try:
        artifact_root = run_instance_dir.parent
        # The bookkeeping/artifact paths are the same RELATIVE strings in both
        # trees (the run worktree mirrors the operator layout), so they are
        # computed once against the operator checkout — where the run-instance
        # dir actually lives — and applied to whichever tree is observed.
        excludes = run_bookkeeping_excludes(
            repo_root, run_instance_dir, artifact_root
        )
        record = RX._attempt_record(man)
        fingerprint = RX.build_progress_fingerprint(
            work, manifest=man, record=record, excludes=excludes
        )
    except (gitops.GitError, RX.RecoveryExecError, OSError, ValueError):
        return None  # unobservable environment: render the pure state table
    try:
        git_obs = RX.observe_git(
            work,
            run_branch=man.branch,
            recorded_sha=RX.reconciliation_boundary(man),
            excludes=excludes,
            bookkeeping_candidates=engine_bookkeeping_candidates(
                repo_root, run_instance_dir
            ),
            approved_artifacts=governed_artifact_paths(repo_root, artifact_root),
        )
    except RX.RecoveryObservationError as exc:
        # F-004: the repository state EXISTS but cannot be represented by the
        # recovery contracts (e.g. a merge commit inside the inventoried
        # range). Resume fails closed on the identical observation, so status
        # must not fall back to the mutating base table — render a fail-closed
        # assessment whose only mutating action is an evidence-retaining abort.
        return RXM.RecoveryAssessment(
            cause=RXM.RecoveryCause.STATE_INCONSISTENT,
            disposition=RXM.RecoveryDisposition.ABORT_ONLY,
            evidence=(
                f"recovery observation failed closed: {exc}",
                "resume/rollback refuse on the same evidence; reconcile the "
                "named state manually, or abort retaining all evidence",
            ),
            safe_actions=(
                RXM.AbortAction(
                    description=(
                        f"`gauntlet abort {man.slug}` aborts the run, "
                        "retaining every snapshot and all evidence"
                    ),
                    reason="the repository state cannot be assessed for recovery",
                ),
            ),
            recommended_action=RXM.RecoveryActionKind.ABORT,
            progress_fingerprint=fingerprint.digest,
        )
    except (gitops.GitError, OSError, ValueError):
        return None
    try:
        return RX.RecoveryPlanner(repo_root).assess(
            manifest=man,
            liveness=liveness,
            git_obs=git_obs,
            fingerprint=fingerprint,
        )
    except (gitops.GitError, RX.RecoveryExecError, OSError, ValueError):
        return None


# --- P6: read-only journal/projection view (plan §4.6/§5.5, R4/R8) -----------


@dataclass
class ProjectionView:
    """What read-only status knows about journal ↔ projection agreement.

    ``manifest`` is the AUTHORITATIVE state to classify from: the on-disk
    projection when healthy (or when the run predates the journal), else the
    journal-head state parsed in memory — so a branch reset or kill window
    that left the projection stale/corrupt/missing never makes ``status``
    disagree with what a mutating verb will do (R4). ``None`` only when
    neither source yields a loadable manifest (a pre-P6 run with a corrupt
    manifest — exactly the pre-P6 failure, reported as before).

    ``assessment``/``action`` carry the shared rebuild plan
    (:func:`RX.projection_rebuild_assessment`) when a rebuild is pending, so
    the rendered action and the applied action have one construction point.
    """

    manifest: "Manifest | None"
    health: str  # journal.HEALTH_*: ok | no_journal | stale | unjournaled | corrupt | missing
    journal_seq: int | None
    rebuild_pending: bool
    assessment: "RXM.RecoveryAssessment | None" = None
    action: "RXM.RebuildProjectionAction | None" = None
    detail: str | None = None

    def payload_block(self) -> dict | None:
        """The additive §6.1 ``projection`` object (null when healthy)."""
        if self.health in (JR.HEALTH_OK, JR.HEALTH_NO_JOURNAL):
            return None
        return {
            "health": self.health,
            "journal_seq": self.journal_seq,
            "rebuild_pending": self.rebuild_pending,
        }


def load_projection_view(
    repo_root: Path, run_instance_dir: Path, *, slug: str | None = None
) -> ProjectionView:
    """Load the run's authoritative state, read-only (P6, plan §4.6).

    Never writes: no quarantine, no genesis, no projection rewrite — those
    happen on the next mutating verb. The journal head is compared against
    the on-disk projection; when the projection is stale (an older journaled
    state — a kill window or a branch reset, R8) or missing/corrupt, the
    head state is parsed in memory and classification proceeds from it, with
    the pending reconciliation reported via :meth:`ProjectionView.payload_block`
    and — for missing/corrupt — the shared rebuild assessment (R4).
    """
    manifest_path = run_instance_dir / "manifest.json"
    status = JR.projection_status(
        run_instance_dir, mutate=False, validate=M.validate_projection_text
    )

    def _head_manifest() -> "Manifest | None":
        if status.head_state_json is None:
            return None
        try:
            return Manifest.model_validate_json(status.head_state_json)
        except ValueError:
            return None

    if status.health in (JR.HEALTH_OK, JR.HEALTH_NO_JOURNAL):
        # Healthy, or a pre-P6 run with no journal: classify from the on-disk
        # projection, exactly as before P6.
        try:
            man: "Manifest | None" = Manifest.load(manifest_path)
        except (OSError, ValueError):
            man = _head_manifest()
        return ProjectionView(
            manifest=man,
            health=status.health if man is not None else JR.HEALTH_NO_JOURNAL,
            journal_seq=status.head_seq,
            rebuild_pending=False,
        )

    if status.health in (JR.HEALTH_STALE, JR.HEALTH_UNJOURNALED):
        # The journal is the authority in BOTH shapes (post-P6 review F-001):
        # a stale projection is an older journaled state, and an unjournaled
        # one was written outside the journaled path (a branch reset onto a
        # pre-journal committed manifest, a stale driver, or a hand-edit) —
        # neither redefines authority, so status classifies from the head,
        # exactly as the next mutating command will resolve it (R4).
        if status.health == JR.HEALTH_STALE:
            detail = (
                "manifest.json holds an older journaled state (kill window "
                "or branch reset, R8); the next mutating command catches it "
                f"up from the journal head (seq {status.head_seq})"
            )
        else:
            detail = (
                "manifest.json holds a state the journal has never recorded "
                "(an out-of-band write — a branch reset onto a pre-journal "
                "committed manifest, a stale driver, or a hand-edit); the "
                "next mutating command preserves those bytes as evidence and "
                f"restores the journal head (seq {status.head_seq}). Nothing "
                "is discarded"
            )
        return ProjectionView(
            manifest=_head_manifest(),
            health=status.health,
            journal_seq=status.head_seq,
            rebuild_pending=True,
            detail=detail,
        )

    # corrupt (unparseable OR schema-invalid — review F-002) / missing: the
    # shared rebuild assessment (plan §5.5, R4).
    planned = RX.projection_rebuild_assessment(
        repo_root, run_instance_dir, slug=slug
    )
    assessment = action = None
    if planned is not None:
        assessment, action = planned
    return ProjectionView(
        manifest=_head_manifest(),
        health=status.health,
        journal_seq=status.head_seq,
        rebuild_pending=planned is not None,
        assessment=assessment,
        action=action,
        detail=(
            f"manifest.json is {status.health}; a plain resume rebuilds it "
            f"from the journal head (seq {status.head_seq}), preserving the "
            "malformed original as evidence (plan §5.5)"
        ),
    )


# --- step / run-instance resolution (FR-3.1a) --------------------------------
# A canonical non-negative decimal: "0", or a non-zero leading digit followed by
# more digits — no leading zero, sign, or surrounding whitespace. The engine
# always writes ``iteration`` as ``str(idx)`` for a non-negative index
# (orchestrator._run_stage), so a real manifest always matches; anything else is
# a corrupt manifest (F-001).
_CANONICAL_ITERATION_RE = re.compile(r"0|[1-9][0-9]*")


def _canonical_iteration(iteration: str | None) -> int | None:
    """Parse a step iteration into the §6.1 canonical ``integer|null``.

    ``None`` stays ``None``; a canonical non-negative decimal string converts to
    its int. Any other value (a leading-zero form like ``"01"``, a sign,
    whitespace, or a non-numeric string) is a manifest-contract violation →
    :class:`StatusContractError` (fail closed). This single canonical
    representation feeds BOTH :func:`render_step_id` (which renders
    ``current_step`` and every parked/failure descriptor) and
    :func:`_iteration_for_json` (``steps[].iteration``), so the rendered id and
    the serialized iteration can never disagree (F-001).
    """
    if iteration is None:
        return None
    if not _CANONICAL_ITERATION_RE.fullmatch(iteration):
        raise StatusContractError(
            f"non-canonical step iteration {iteration!r}: expected a canonical "
            "non-negative decimal string (e.g. '0', '1', '12')"
        )
    return int(iteration)


def render_step_id(rec: StepRecord) -> str:
    """The rendered step id used everywhere a leaf is named (``id`` / ``id.it``).

    The iteration is rendered through :func:`_canonical_iteration`, the same
    canonical integer the JSON serializer uses, so a rendered ``current_step``
    always matches its ``steps[]`` entry exactly (F-001)."""
    it = _canonical_iteration(rec.iteration)
    return rec.id if it is None else f"{rec.id}.{it}"


def select_default_step(man: Manifest) -> StepRecord | None:
    """The FR-3.1a default step: the last record whose status ∉ {done,skipped}.

    Selection is by **manifest step order** (authoritative), never directory
    mtime; the last matching record is the highest iteration of an iterated
    step because iterations are appended in order. If every step is
    done/skipped, the last done step is returned (else ``None``).
    """
    non_terminal = [s for s in man.steps if s.status not in (M.DONE, M.SKIPPED)]
    if non_terminal:
        return non_terminal[-1]
    done = [s for s in man.steps if s.status == M.DONE]
    return done[-1] if done else None


def list_run_instances(slug_dir: Path) -> list[str]:
    """Sorted ``run-<ts>`` instance dir names under ``slug_dir`` (chronological)."""
    if not slug_dir.exists():
        return []
    return sorted(
        p.name for p in slug_dir.iterdir() if p.is_dir() and p.name.startswith("run-")
    )


def resolve_run_instance(slug_dir: Path) -> Path:
    """The FR-3.1a authoritative run instance: ``active-run.txt`` else greatest.

    The instance named in ``active-run.txt`` (when present and it exists), else
    the lexicographically-greatest ``run-<ts>`` dir. If ``active-run.txt`` names
    a missing instance, error and list the available ones rather than guessing.
    """
    pointer = slug_dir / "active-run.txt"
    if pointer.exists():
        name = pointer.read_text().strip()
        try:
            safe_run_segment(name, kind="run_id")
        except UnsafeRunSegment as exc:
            raise RunResolutionError(str(exc)) from exc
        inst = slug_dir / name
        if not inst.is_dir():
            avail = list_run_instances(slug_dir)
            raise RunResolutionError(
                f"active-run.txt names instance {name!r}, which does not exist; "
                f"available instances: {avail or '(none)'}"
            )
        return inst
    instances = list_run_instances(slug_dir)
    if not instances:
        raise RunResolutionError(f"no run instances under {slug_dir}")
    return slug_dir / instances[-1]


def step_dir_for(run_instance_dir: Path, rec: StepRecord) -> Path:
    """The ``steps/<leaf>/`` dir for a step record (mirrors ``step_log_dir``)."""
    return run_instance_dir / "steps" / render_step_id(rec)


def _subdirs(path: Path) -> list[Path]:
    """Immediate real sub-directories of ``path``, never following symlinks.

    A symlinked child is excluded so evidence-dir resolution and the
    available-steps enumeration can never recurse out of the run tree (FR-3.3).
    """
    if not path.is_dir() or path.is_symlink():
        return []
    return [c for c in path.iterdir() if c.is_dir() and not c.is_symlink()]


def _round_count(rec: StepRecord, step_dir: Path) -> int | None:
    """Authoritative round count for a cycle: ``metrics["rounds"]`` else greatest
    ``r<N>-*`` sub-dir prefix present (FR-3.1a). ``None`` when neither exists."""
    rounds = rec.metrics.get("rounds") if rec.metrics else None
    if isinstance(rounds, bool):  # bool is an int subclass — exclude explicitly
        rounds = None
    if isinstance(rounds, int) and rounds > 0:
        return rounds
    nums: list[int] = []
    for p in _subdirs(step_dir):
        m = re.match(r"r(\d+)-", p.name)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None


def resolve_transcript_dir(run_instance_dir: Path, rec: StepRecord) -> Path:
    """The directory holding the step's authoritative ``transcript.md`` (FR-3.1a).

    Atomic step types write ``steps/<leaf>/transcript.md`` directly. Composite
    step types (``adversarial_cycle``, ``retrospective``) write **no** direct
    transcript; their evidence lives in role sub-directories, and the default
    leaf is the most-recently-executed role of the highest round — resolved from
    metadata + the fixed reverse-execution role order, never directory mtime.
    An unrecognized type is treated as atomic (the missing-artifact path in P2
    then handles an absent transcript rather than crashing).
    """
    step_dir = step_dir_for(run_instance_dir, rec)
    if rec.type not in _COMPOSITE_STEP_TYPES:
        return step_dir
    if rec.type == "retrospective":
        synth = step_dir / "synthesis"
        if synth.is_dir():
            return synth
        retros = sorted(p for p in _subdirs(step_dir) if p.name.startswith("retro-"))
        return retros[-1] if retros else step_dir
    # adversarial_cycle: highest round, then reverse-execution role order.
    rnd = _round_count(rec, step_dir)
    if rnd is None:
        return step_dir
    for role in (f"r{rnd}-confirm", f"r{rnd}-fix"):
        if (step_dir / role).is_dir():
            return step_dir / role
    triage = step_dir / f"r{rnd}-triage"
    if triage.is_dir():
        findings = sorted(_subdirs(triage))
        if findings:
            # P5 (plan §5.2, issue #63): a FAILING leaf — one carrying the
            # failure-evidence marker `log_failure` writes — wins over the
            # most-recent sibling, so `gauntlet logs` never surfaces a
            # successful leaf's transcript in place of the failure. Multiple
            # failing leaves resolve to the lexicographically-greatest
            # (deterministic; each remains addressable via `--step`).
            failing = [d for d in findings if (d / _FAILURE_NAME).is_file()]
            if failing:
                return failing[-1]
            return findings[-1]  # lexicographically-greatest finding-id
    review = step_dir / f"r{rnd}-review"
    if review.is_dir():
        return review
    return step_dir


# --- composite run-state classification (§6.3 + §6.3a, total) ----------------
def _classify(man: Manifest, liveness: str) -> tuple[str, ParkedDescriptor | None, FailureDescriptor | None]:
    """The total ``(run_status, liveness, descriptor) -> state`` function.

    P4 (R4): the classification core is :func:`recovery_exec.classify_composite`
    — the SAME function the mutating resume/recover paths consume — so the two
    surfaces cannot disagree. This wrapper only renders the returned raw step
    records into the §6.1 descriptors (a corrupt iteration still fails closed
    through :func:`render_step_id`). Any unrecognized ``run_status`` or an
    internally contradictory manifest maps to ``unknown`` → read-only
    inspection only; the historical kill-window shape (``RUN_RUNNING`` + one
    ended interrupted/halted/failed step) maps to the corresponding
    RECOVERABLE state by liveness (plan §5.3), never ``unknown``.
    """
    state, parked_rec, failure_rec = RX.classify_composite(man, liveness)
    parked = None
    failure = None
    if parked_rec is not None:
        # The descriptor carries the NORMALIZED park reason (FR-7.2), so
        # `status --json` never emits a legacy on-disk value.
        reason = M.normalize_parked_reason(
            parked_rec.parked_reason, parked_rec.type, parked_rec.status
        )
        parked = ParkedDescriptor(render_step_id(parked_rec), parked_rec.type, reason)
    if failure_rec is not None:
        failure = FailureDescriptor(
            render_step_id(failure_rec),
            failure_rec.status,
            # failure_kind matters only on a FAILED step (it selects the
            # re-runnable vs terminal next action); halted/interrupted never
            # carry one.
            failure_rec.failure_kind if failure_rec.status == M.FAILED else None,
            halt_reason=failure_rec.halt_reason,
        )
    return state, parked, failure


def _current_step(
    man: Manifest,
    state: str,
    parked: ParkedDescriptor | None,
    failure: FailureDescriptor | None,
) -> str | None:
    """The §6.1 ``current_step`` — a rendered id that matches exactly one step,
    or ``None``. Derived; ``steps[]`` stays authoritative."""
    if parked is not None:
        return parked.step_id
    if failure is not None:
        return failure.step_id
    if state in (STATE_IN_PROGRESS, STATE_ORPHANED, STATE_INDETERMINATE):
        rec = select_default_step(man)
        return render_step_id(rec) if rec is not None else None
    return None


# Sentinel distinguishing "caller resolved the gate cycle from the pipeline (use
# it, even if None → terminal reject)" from "caller did not resolve; fall back to
# the pure manifest heuristic". A plain None default cannot express both (F-001).
_UNRESOLVED: Any = object()


def compute_run_state(
    man: Manifest,
    liveness: str,
    *,
    gate_cycle_id: str | None = _UNRESOLVED,
    assessment: "RXM.RecoveryAssessment | None" = None,
) -> RunState:
    """The single computed composite state both the footer and ``--json`` render.

    ``gate_cycle_id`` (FR-8.2) is the ``adversarial_cycle`` a reject of the parked
    gate would re-drive, which the reject action's consequence names. The
    authoritative resolution is over the run's *pipeline snapshot* (same rule as
    ``Orchestrator._upstream_cycle_for_gate``), so the I/O-bearing caller resolves
    it via :func:`gauntlet.engine.pipeline.upstream_cycle_id_for_gate` and threads
    it in — passing ``None`` when a reject is terminal (a foreach gate, or a gate
    with no same-stage cycle). Left unset, this function falls back to a pure
    manifest-order heuristic (``_upstream_cycle_record``) so pure/no-pipeline
    callers still get a shaped consequence; the CLI/web always resolve from the
    pipeline so their surfaces name the cycle a reject *actually* re-runs (F-001).
    """
    state, parked, failure = _classify(man, liveness)
    if state == STATE_PARKED_GATE and parked is not None:
        if gate_cycle_id is _UNRESOLVED:
            gate_rec = next(
                (r for r in man.steps if render_step_id(r) == parked.step_id), None
            )
            cyc = (
                _upstream_cycle_record(man, gate_rec)
                if gate_rec is not None else None
            )
            gate_cycle_id = cyc.id if cyc is not None else None
    else:
        gate_cycle_id = None
    # P4 (plan §4.2): with a threaded-in recovery assessment (the CLI status
    # path), render ITS safe actions — the same actions the mutating verbs
    # will take, including the branch-relation refinements. Without one
    # (pure callers, web store), the fallback table derives from the SAME
    # shared kind table, so the two renderings agree on every base state.
    if assessment is not None:
        next_actions = render_assessment_actions(
            assessment, state, man.slug, gate_cycle_id=gate_cycle_id
        )
    else:
        # F-007: the failed step's TYPE selects between `resume --response`
        # (respondable types) and abort (a shell step / terminally rejected
        # gate, where resume's validators reject --response).
        failure_step_type = None
        if failure is not None:
            frec = next(
                (r for r in man.steps if render_step_id(r) == failure.step_id),
                None,
            )
            failure_step_type = frec.type if frec is not None else None
        next_actions = _actions_for(
            state, man.slug, failure, gate_cycle_id=gate_cycle_id,
            failure_step_type=failure_step_type,
        )
    return RunState(
        state=state,
        slug=man.slug,
        current_step=_current_step(man, state, parked, failure),
        parked=parked,
        failure=failure,
        next_actions=next_actions,
    )


def next_actions(man: Manifest, liveness: str) -> list[Action]:
    """The structured next action(s) for a manifest + liveness (FR-1.2/FR-4)."""
    return compute_run_state(man, liveness).next_actions


def composite_state(man: Manifest, liveness: str) -> str:
    """Just the composite ``state`` class for a manifest + liveness (§6.3)."""
    return compute_run_state(man, liveness).state


# --- read-only recovery-intent parser (FR-5.6 report half) -------------------
# The single source of the intent filename is `run.RECOVERY_INTENT_NAME`; the P4
# writer and this read-only parser must agree, so it is imported, not re-literal'd.
_RECOVERY_INTENT_NAME = RECOVERY_INTENT_NAME


def _within(child: Path, ancestor: Path) -> bool:
    """True iff ``child`` (already ``realpath``-resolved) is at/under ``ancestor``."""
    try:
        child.relative_to(ancestor)
        return True
    except ValueError:
        return False


def read_recovery_intent(
    run_root: Path, run_instance_dir: Path, slug: str
) -> tuple[Reconciliation | None, str | None]:
    """Detect a surviving ``.recovery-intent.json`` (FR-5.6 report-only).

    Returns ``(reconciliation, anomaly_note)``:

    * absent intent → ``(None, None)`` (no note).
    * well-formed intent (parses, has ``step_id`` + ``lock_nonce``) →
      ``(Reconciliation(...), None)``. ``nonce_matches_lock`` is computed
      read-only against the current drive lock: ``True`` when the lock is absent
      **or** its nonce equals the intent's (the finalize branch); ``False`` when
      the lock is present with a differing nonce, or is itself unreadable
      (fail closed — never claim finalize-safe).
    * malformed / incomplete / unreadable intent, or a path escaping the run
      tree → ``(None, anomaly_note)`` (a human-footer note; ``--json`` keeps
      ``reconciliation: null`` rather than fabricating a step id).

    This is **detection only**: nothing is signalled, unlinked, or written.
    Until P4 writes intents, this always finds none and returns ``(None, None)``.
    """
    anomaly = (
        "unreadable recovery-intent present; run `gauntlet recover " + slug
        + "` or `gauntlet logs " + slug + "` to inspect"
    )
    path = run_instance_dir / _RECOVERY_INTENT_NAME
    if not path.is_symlink() and not path.exists():
        return (None, None)
    # Containment: a symlink escaping the run tree is refused with no read.
    # `resolve()` can itself raise on a self-referential symlink (RuntimeError)
    # or an otherwise unresolvable target (OSError); fail closed to the anomaly
    # notice rather than crashing `gauntlet status` (FR-5.6).
    try:
        real = path.resolve()
        if not _within(real, run_instance_dir.resolve()):
            return (None, anomaly)
    except (OSError, RuntimeError):
        return (None, anomaly)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return (None, anomaly)
    if not isinstance(data, dict):
        return (None, anomaly)
    step_id = data.get("step_id")
    lock_nonce = data.get("lock_nonce")
    if not isinstance(step_id, str) or not step_id or not isinstance(lock_nonce, str):
        return (None, anomaly)
    kind, rec = _lock_state(run_root, run_instance_dir)
    if kind == locking.LOCK_ABSENT:
        nonce_matches = True  # finalize branch — verified target already gone
    elif kind == locking.LOCK_PRESENT and rec is not None:
        nonce_matches = rec.nonce == lock_nonce
    else:  # malformed/unreadable lock → fail closed
        nonce_matches = False
    return (
        Reconciliation(
            intent_step_id=step_id,
            nonce_matches_lock=nonce_matches,
            recommended_command=f"gauntlet recover {slug}",
        ),
        None,
    )


# --- machine-readable status payload (P3, FR-4 / §6.1) -----------------------
SCHEMA_VERSION = 1  # §6.1/§6.5 — the major version of the status.json contract

# The §6.1 contract, embedded so emission-time validation works in any repo
# (the committed ``schemas/status.json`` lives at the run root and is NOT packaged
# in the wheel). This is a byte-equivalent mirror of that committed file —
# drift-guarded by tests/unit/test_status_json.py — mirroring the
# ``skill.SKILL_FRONTMATTER_SCHEMA`` pattern (F-003).
_STATUS_SCHEMA_JSON = r'''{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "gauntlet/schemas/status.json",
  "title": "gauntlet status --json contract (PRD operator-aids, §6.1)",
  "description": "The stable machine contract emitted by `gauntlet status <slug> --json` (FR-4). It is a single rendering of the same computation behind the human footer (operator.compute_run_state / driver_info / next_actions), so the two surfaces can never diverge. `additionalProperties: false` is set top-level and on every nested object: an unknown field is a validation failure, not silently accepted. This strictness is scoped to the CURRENT schema_version — it describes exactly what the current Gauntlet emits. All listed properties are required; a nullable field is always PRESENT and explicitly `null` when not applicable (never omitted).",
  "$comment": "Compatibility policy (§6.5 / harness-efficiency FR-7.1): schema_version starts at 1 and identifies the MAJOR version. This committed schema is the single living source for that major version — updated additively IN PLACE (new optional/always-present fields, appended enum members) without bumping schema_version. Any field removal, type change, or required-field addition is a BREAKING change that bumps schema_version. Harness-efficiency FR-7 adds fields (current_step_elapsed_s, current_step_timeout_remaining_s, run_elapsed_s, totals, agent_usage, quota, and per-step duration_s/notes/halt_reason/parked_reason) additively and keeps schema_version=1; FR-8 additively populates the `gate` object body and adds `next_actions[].consequence`, likewise keeping schema_version=1; FR-10.3 adds the always-present `warnings` array additively, likewise keeping schema_version=1; recovery-redesign P5 appends the `parked_provider_unavailable` state, the `provider_unavailable` park reason, the remaining built-in step types to `parked.type`, and the always-present per-step `recovery_cause`/`recovery_disposition` fields, likewise keeping schema_version=1; recovery-redesign P6 adds the always-present nullable top-level `projection` object (journal-vs-projection agreement, plan §4.6/R8) additively, likewise keeping schema_version=1. A strict-validating consumer MUST validate against the committed schema at the payload's schema_version OR NEWER, never a private frozen copy (an additive field/enum is correctly rejected by an older snapshot under additionalProperties:false — the documented re-pin cost, not a break). A consumer that cannot track the committed schema must instead tolerate unknown object properties and unknown enum members defensively.",
  "type": "object",
  "additionalProperties": false,
  "$defs": {
    "usage_totals": {
      "type": "object",
      "additionalProperties": false,
      "required": ["input_tokens", "output_tokens", "cached_input_tokens", "cost_usd"],
      "description": "Aggregated provider usage (harness-efficiency FR-7.1). cost_usd is null on the degraded tokens-only path (a subscription-auth CLI may not report cost); the token counts are always integers.",
      "properties": {
        "input_tokens": {"type": "integer", "description": "Fresh (uncached) input tokens."},
        "output_tokens": {"type": "integer", "description": "Output tokens."},
        "cached_input_tokens": {"type": "integer", "description": "Cache-read input tokens."},
        "cost_usd": {"type": ["number", "null"], "description": "Cost in USD, or null when unpriced."}
      }
    }
  },
  "required": [
    "schema_version",
    "slug",
    "run_id",
    "run_status",
    "state",
    "current_step",
    "current_step_elapsed_s",
    "current_step_timeout_remaining_s",
    "run_elapsed_s",
    "totals",
    "agent_usage",
    "warnings",
    "quota",
    "driver",
    "parked",
    "failure",
    "reconciliation",
    "current_step_freshness",
    "suspension",
    "gate",
    "projection",
    "worktree",
    "steps",
    "next_actions"
  ],
  "properties": {
    "schema_version": {
      "type": "integer",
      "const": 1,
      "description": "Major schema version. 1 for v1; see §6.5 compatibility policy."
    },
    "slug": {
      "type": "string",
      "pattern": "^[a-z0-9][a-z0-9-]*$",
      "description": "The run slug; matches the slug-validation pattern."
    },
    "run_id": {
      "type": "string",
      "description": "The selected run-instance id."
    },
    "run_status": {
      "type": "string",
      "description": "Raw manifest run_status (running|parked|done|aborted|failed). Left an unconstrained string deliberately: an unrecognized value does not fail the schema, it maps to composite state `unknown` (§6.3)."
    },
    "state": {
      "type": "string",
      "enum": [
        "in_progress",
        "orphaned",
        "indeterminate",
        "parked_gate",
        "parked_for_response",
        "parked_usage_limit",
        "parked_usage_window",
        "parked_artifact_invalid",
        "failed",
        "halted",
        "interrupted",
        "done",
        "aborted",
        "unknown",
        "parked_provider_unavailable"
      ],
      "description": "The computed composite run-state class (§6.3) — a total function of (run_status x driver liveness x descriptor). `parked_provider_unavailable` (recovery-redesign P5, plan §5.2) was APPENDED additively: a transport/dependency park after the bounded persisted retries; a plain `resume` retries after the recorded deadline."
    },
    "current_step": {
      "type": ["string", "null"],
      "description": "Rendered id of the active/most-recent non-terminal step, or null. When non-null it MUST equal the rendered id of exactly one steps[] entry (`<id>` or `<id>.<iteration>`). Derived convenience; steps[] is authoritative."
    },
    "current_step_elapsed_s": {
      "type": ["number", "null"],
      "description": "Wall-clock seconds the current step has been running (harness-efficiency FR-7.1): now - started for a running step, or ended - started for a finished one. null when there is no current step or its timestamps are absent/unparseable."
    },
    "current_step_timeout_remaining_s": {
      "type": ["number", "null"],
      "description": "Best-effort seconds remaining before the current running step's deadline (harness-efficiency FR-7.1), computed as the resolved effective timeout minus elapsed and clamped to 0 (never negative). null when there is no running step or no resolvable timeout. Advisory — suspend credit (FR-5.2) is not folded in here."
    },
    "run_elapsed_s": {
      "type": ["number", "null"],
      "description": "Wall-clock seconds from the first step start to now (a live run) or to the last step end (a finished run) (harness-efficiency FR-7.1). null when no step has started."
    },
    "totals": {
      "$ref": "#/$defs/usage_totals",
      "description": "Run-level aggregated provider usage incl. cost (harness-efficiency FR-7.1). Always present."
    },
    "agent_usage": {
      "type": "object",
      "additionalProperties": {"$ref": "#/$defs/usage_totals"},
      "description": "Per-agent-profile usage totals keyed by profile name (harness-efficiency FR-7.1). Always present; an empty object when no per-profile usage was recorded."
    },
    "warnings": {
      "type": "array",
      "items": {"type": "string"},
      "description": "Non-fatal run anomalies surfaced rather than swallowed (harness-efficiency FR-10.3): each an already-redacted human-readable string. Includes advisory usage-window shortfalls (`[<step>] usage-window admission (FR-10.3): …`, the warn-don't-park default) so a live shortfall is visible without opening the manifest, plus any other recorded warning (e.g. an unrenderable final-gate artifact). Always present; an empty array when none."
    },
    "quota": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["reset_at"],
      "description": "Provider usage-window reset info (harness-efficiency FR-7.1/FR-10.3), non-null only when parked on a usage_limit park (provider reset time), a usage_window park (projected replenishment time), or a provider_unavailable park (the recorded backoff/Retry-After retry deadline, recovery-redesign P5); null otherwise.",
      "properties": {
        "reset_at": {
          "type": ["string", "null"],
          "description": "Absolute UTC reset time (ISO-8601): a usage_limit park's provider reset (null when no structured retry hint), or a usage_window park's projected window-replenishment time."
        }
      }
    },
    "driver": {
      "type": "object",
      "additionalProperties": false,
      "required": ["state", "pid", "since", "host"],
      "description": "The computed driver-liveness view (FR-2.4). Always present.",
      "properties": {
        "state": {
          "type": "string",
          "enum": ["alive", "orphaned", "indeterminate", "none"],
          "description": "Liveness value (FR-2.4)."
        },
        "pid": {
          "type": ["integer", "null"],
          "description": "Recorded pid, or null when liveness is `none`."
        },
        "since": {
          "type": ["string", "null"],
          "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}$",
          "description": "The lock's started_at verbatim (%Y-%m-%dT%H-%M-%S UTC, hyphen-delimited, no offset); opaque, never reformatted; null when `none`. The anchored pattern enforces exactly the §6.1 format (rejecting an ISO offset, colon-delimited time, or any other string); it constrains only the string form, so null still passes."
        },
        "host": {
          "type": ["string", "null"],
          "description": "The lock's recorded host, or null when `none`."
        }
      }
    },
    "parked": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["step_id", "type", "reason"],
      "description": "Present (object) iff state in {parked_gate, parked_for_response, parked_usage_limit, parked_usage_window, parked_artifact_invalid, parked_provider_unavailable}, else null (enforced by the state-coupling allOf below).",
      "properties": {
        "step_id": {
          "type": "string",
          "description": "The parked step's rendered id."
        },
        "type": {
          "type": "string",
          "enum": ["human_gate", "agent_task", "adversarial_cycle", "phase_lint", "shell", "commit", "acceptance_gate", "retrospective"],
          "description": "The parked step's type. `phase_lint` and the remaining built-in types were APPENDED additively by recovery-redesign P5: a plan/lint defect now parks artifact_invalid on the responsible step (plan §5.1), and a drive-level plan-parse park lands on the step the stage walk stopped at, whatever its type."
        },
        "reason": {
          "type": "string",
          "enum": ["usage_limit", "usage_window", "artifact_invalid", "response", "gate", "provider_unavailable"],
          "description": "Normalized PRD park reason (harness-efficiency FR-7.2): `gate` for a human_gate; `response` for a builder UPSTREAM CONFLICT or a cycle escalation (agent_task vs adversarial_cycle recovered from `type`); `usage_limit` for a provider usage-limit park; `provider_unavailable` (APPENDED by recovery-redesign P5, plan §5.2) for a transport/dependency park after the bounded persisted retries — plain-resumable, never `--response`. Legacy on-disk values (upstream_conflict/cycle_escalation) and a pre-P3 null gate reason are mapped to this enum on read and never emitted verbatim."
        }
      }
    },
    "failure": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["step_id", "status", "evidence_path"],
      "description": "Present (object) iff state in {failed, halted, interrupted}, else null (enforced by the state-coupling allOf below).",
      "properties": {
        "step_id": {
          "type": "string",
          "description": "The failing step's rendered id."
        },
        "status": {
          "type": "string",
          "enum": ["failed", "halted", "interrupted"],
          "description": "The failing step's status."
        },
        "evidence_path": {
          "type": "string",
          "pattern": "^(?!/)(?!.*\\.\\.).+$",
          "description": "POSIX-relative path under run_root (no leading slash, no `..`): the failing step's dir."
        }
      }
    },
    "reconciliation": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["intent_step_id", "nonce_matches_lock", "recommended_command"],
      "description": "Non-null iff read-only `status` detects a surviving .recovery-intent.json for the selected run instance (FR-5.6). Report-only — `status` never reconciles; null when no intent survives (or it is malformed/unreadable, which is a human-footer anomaly note only).",
      "properties": {
        "intent_step_id": {
          "type": "string",
          "description": "The step_id recorded in the surviving intent."
        },
        "nonce_matches_lock": {
          "type": "boolean",
          "description": "true when the intent's lock_nonce matches the current lock OR the lock is absent (reconciliation would FINALIZE — the live branch); false when the lock is present with a different nonce (reconciliation would DISCARD it as stale)."
        },
        "recommended_command": {
          "type": "string",
          "description": "Human-display command that finalizes the intent (e.g. `gauntlet recover <slug>`); for display only, never executed."
        }
      }
    },
    "current_step_freshness": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["last_event_age_s"],
      "description": "Advisory freshness for a running, streamed step (live-run-observability FR-5): the age in seconds of the newest streamed event (now - mtime of the step's events.jsonl). The OBJECT is the nullable unit: null for a non-streamed/not-applicable step or the pre-first-event window (events.jsonl absent or empty); when present, last_event_age_s is ALWAYS a number, never null. There is no top-level last_event_age_s. Drives no gate and no automatic action (FR-5.2).",
      "properties": {
        "last_event_age_s": {
          "type": "number",
          "description": "Age in seconds of the newest streamed event (now - mtime of the current step's events.jsonl). Always a number when the object is present."
        }
      }
    },
    "suspension": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["classification", "last_heartbeat_age_s", "intervals"],
      "description": "Suspend/sleep view (harness-efficiency FR-5.3): driver heartbeat age, detected host-suspension intervals, and the fail-closed stall classification. null only when there is neither a heartbeat nor any recorded interval.",
      "properties": {
        "classification": {
          "type": ["string", "null"],
          "enum": ["host_suspended", "driver_orphaned", "agent_silent", null],
          "description": "The stalled-run classification: host_suspended (a detected sleep gap on a live driver), driver_orphaned (heartbeat stale, driver process dead), agent_silent (live driver, no agent output / a live-but-stale heartbeat with no clock evidence — fail closed to hung, never sleep), or null (healthy / not applicable)."
        },
        "last_heartbeat_age_s": {
          "type": ["number", "null"],
          "description": "Age in seconds of the newest driver heartbeat (now - its wallclock), or null when no heartbeat exists yet."
        },
        "intervals": {
          "type": "array",
          "description": "Detected host-suspension intervals (manifest.suspensions), in detection order.",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["start", "end", "gap_s"],
            "properties": {
              "start": {"type": "string", "description": "Wallclock of the heartbeat before the gap."},
              "end": {"type": "string", "description": "Wallclock of the heartbeat after the gap."},
              "gap_s": {"type": "integer", "description": "Wallclock width of the suspension in seconds."}
            }
          }
        }
      }
    },
    "gate": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["cycle_step_id", "convergence", "prior_responses", "escalated"],
      "description": "Gate context block (PRD §6 / harness-efficiency FR-8.1): always present, non-null only when parked at a human gate. Assembled from the manifest + the upstream adversarial_cycle's persisted artifacts (no transcript read): the cycle's convergence summary, prior human `--response`/rejection decisions for this gate, and the per-escalated-finding triage reasoning. Content-bearing fields pass through the redaction path (PRD §7). null for every non-gate state.",
      "properties": {
        "cycle_step_id": {
          "type": ["string", "null"],
          "description": "Id of the adversarial_cycle this gate ratifies (the last cycle before the gate), or null when the gate has no upstream cycle."
        },
        "convergence": {
          "type": ["object", "null"],
          "additionalProperties": false,
          "required": ["rounds", "findings_total", "accepted_total", "per_round"],
          "description": "Cycle convergence summary from the upstream cycle's `metrics` (aggregate) plus its per-round sub-step checkpoints (FR-4). null when there is no upstream cycle. rounds/findings_total/accepted_total are null when the cycle recorded no metrics.",
          "properties": {
            "rounds": {"type": ["integer", "null"], "description": "Rounds the cycle ran."},
            "findings_total": {"type": ["integer", "null"], "description": "Findings raised across all rounds."},
            "accepted_total": {"type": ["integer", "null"], "description": "Findings triaged fix_now across all rounds."},
            "per_round": {
              "type": "array",
              "description": "Per-round breakdown from the checkpointed round artifacts, in ascending round order. A count is null when the round's artifact is absent/unreadable.",
              "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["round", "raised", "fixed", "declined"],
                "properties": {
                  "round": {"type": "integer", "description": "1-based round number."},
                  "raised": {"type": ["integer", "null"], "description": "Findings raised this round (from the round's findings.json)."},
                  "fixed": {"type": ["integer", "null"], "description": "Findings triaged action=fix_now this round."},
                  "declined": {"type": ["integer", "null"], "description": "Findings triaged action in {defer, reject} this round."}
                }
              }
            }
          }
        },
        "prior_responses": {
          "type": "array",
          "description": "Prior human `--response`/rejection decisions bearing on this gate (from the upstream cycle's and the gate's human_responses), in record order, with timestamps. response_text is redacted.",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["response_id", "response_text", "timestamp", "user", "state"],
            "properties": {
              "response_id": {"type": "string", "description": "Stable response handle (`<step>-resp-<n>`)."},
              "response_text": {"type": "string", "description": "The decision text, redacted."},
              "timestamp": {"type": "string", "description": "When the decision was recorded."},
              "user": {"type": "string", "description": "Who recorded it."},
              "state": {"type": "string", "enum": ["pending", "consumed"], "description": "Idempotent-recovery state."}
            }
          }
        },
        "escalated": {
          "type": "array",
          "description": "Per-escalated-finding triage reasoning: latest-round verdicts flagged `escalated` or `low_confidence`, merged with their finding. Empty when no verdict was flagged. Content fields (claim/reasoning) redacted.",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["finding_id", "severity", "category", "location", "claim", "verdict", "action", "confidence", "reasoning"],
            "properties": {
              "finding_id": {"type": "string"},
              "severity": {"type": ["string", "null"]},
              "category": {"type": ["string", "null"]},
              "location": {"type": ["string", "null"]},
              "claim": {"type": ["string", "null"]},
              "verdict": {"type": ["string", "null"]},
              "action": {"type": ["string", "null"]},
              "confidence": {"type": ["string", "null"]},
              "reasoning": {"type": ["string", "null"]}
            }
          }
        }
      }
    },
    "worktree": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["mode", "path", "registered", "present", "locked_reason", "prunable", "head"],
      "description": "Which tree this run's agents edit (recovery-redesign P7c, spike \u00a76.2/\u00a713). Always present. `mode` is resolved from EVIDENCE and from what the run recorded at birth, never from the live `worktree.mode` config \u2014 flipping that config never moves an existing run (spike \u00a710: a pre-P7 run is never auto-migrated, and never wedged). A `same_tree` run reports mode `same_tree` with every other field null/false: it drives the operator's own checkout, which is the pre-P7 layout, the mode of every run started before P7c, and \u2014 since P7g made `dedicated` the default \u2014 an explicitly-selected fallback rather than the norm. A `dedicated` run reports the derived worktree path, `<main-worktree>/.gauntlet/worktrees/<slug>/<run-id>` (relocated out of the git directory by P7e). `registered` is whether git's worktree administration knows the tree; `present` is whether it is actually on disk \u2014 registered-but-absent is spike \u00a711 row 2 (the tree was swept or deleted while the branch ref and the journal survived) and is recreatable from refs plus journal state. `prunable` carries git's own reason string for that case. `locked_reason` is the `git worktree lock --reason` marker held for the life of a live run, which is the only thing that stops another run's prune from removing this run's admin entry. Observed facts only \u2014 never a recommendation; actions belong in next_actions.",
      "properties": {
        "mode": {
          "type": "string",
          "enum": ["same_tree", "dedicated"],
          "description": "The tree layout this run drives in."
        },
        "path": {
          "type": ["string", "null"],
          "description": "Absolute path of the run worktree; null for a `same_tree` run or a `dedicated` run with no registered tree."
        },
        "registered": {
          "type": "boolean",
          "description": "Whether `git worktree list` registers a worktree for this run's branch."
        },
        "present": {
          "type": "boolean",
          "description": "Whether the registered worktree is actually on disk."
        },
        "locked_reason": {
          "type": ["string", "null"],
          "description": "The `git worktree lock --reason` text, or null when unlocked."
        },
        "prunable": {
          "type": ["string", "null"],
          "description": "Git's own reason string when the admin entry is prunable (spike \u00a711 row 2), else null."
        },
        "head": {
          "type": ["string", "null"],
          "description": "The worktree's HEAD SHA as git reports it, or null."
        }
      }
    },
    "projection": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "required": ["health", "journal_seq", "rebuild_pending"],
      "description": "Journal-vs-projection agreement (recovery-redesign P6, plan §4.6/R8): the append-only run journal is the authoritative state and manifest.json is its regenerated projection. Always present; null when the on-disk projection matches the journal head (or the run predates the journal). Non-null names the divergence: `stale` (an older journaled state — a kill window or a branch reset; the next mutating command catches it up from the journal head), `unjournaled` (a state the journal has never recorded — an out-of-band write: a branch reset onto a pre-journal committed manifest, a stale driver, or a hand-edit; the next mutating command preserves those bytes as evidence and restores the journal head, never adopting them as authority), or `corrupt`/`missing` (the next mutating command rebuilds the projection from the journal head, preserving the malformed original as recovery evidence — plan §5.5). Advisory: drives no automatic action.",
      "properties": {
        "health": {
          "type": "string",
          "enum": ["stale", "unjournaled", "corrupt", "missing"],
          "description": "How the on-disk projection diverges from the journal head."
        },
        "journal_seq": {
          "type": ["integer", "null"],
          "description": "The journal head's monotonic sequence number (the authoritative state), or null when unavailable."
        },
        "rebuild_pending": {
          "type": "boolean",
          "description": "true when the next mutating command will rewrite manifest.json from the journal head (catch-up or full rebuild with evidence preservation)."
        }
      }
    },
    "steps": {
      "type": "array",
      "description": "Authoritative ordered step list.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "iteration", "status", "duration_s", "notes", "halt_reason", "parked_reason", "recovery_cause", "recovery_disposition", "judge_tool_calls"],
        "properties": {
          "id": {"type": "string", "description": "Step id."},
          "iteration": {
            "type": ["integer", "null"],
            "description": "Iteration index for a cycle/foreach step, else null."
          },
          "status": {
            "type": "string",
            "enum": [
              "pending",
              "running",
              "done",
              "failed",
              "interrupted",
              "parked",
              "halted",
              "skipped"
            ],
            "description": "Step status."
          },
          "duration_s": {
            "type": ["number", "null"],
            "description": "Wall-clock seconds the step ran (harness-efficiency FR-7.1): ended - started, or now - started while running. null when the step has no start timestamp or its timestamps are unparseable."
          },
          "notes": {
            "type": ["string", "null"],
            "description": "The step's engine/human notes verbatim (harness-efficiency FR-7.1), or null when none. Content-bearing; already redacted on write."
          },
          "halt_reason": {
            "type": ["string", "null"],
            "enum": ["timeout", "budget", "judge_deny", "signal_kill", "adapter_error", "precondition", "operator_recover", null],
            "description": "Terminal halt reason (harness-efficiency FR-7.2) on a HALTED/FAILED/INTERRUPTED step; null otherwise. DISJOINT from parked_reason — never both set. null on steps predating P3."
          },
          "parked_reason": {
            "type": ["string", "null"],
            "enum": ["usage_limit", "usage_window", "artifact_invalid", "response", "gate", "provider_unavailable", null],
            "description": "Normalized PRD park reason (harness-efficiency FR-7.2) on a PARKED step; null otherwise. DISJOINT from halt_reason. Legacy on-disk values are mapped to this enum on read, never emitted verbatim. `provider_unavailable` was APPENDED by recovery-redesign P5 (plan §5.2)."
          },
          "recovery_cause": {
            "type": ["string", "null"],
            "enum": ["none", "provider_unavailable", "quota_exhausted", "process_lost", "artifact_invalid", "precondition_unsatisfied", "worktree_partial", "branch_ahead", "branch_diverged", "state_inconsistent", "policy_denied", "internal_error", null],
            "description": "Orthogonal recovery cause (recovery-redesign plan §4.1, APPENDED by P5): WHY this step needs recovery, stamped by the engine on every terminal/parked outcome from the outcome's own evidence. null on non-terminal steps and on manifests predating P5."
          },
          "recovery_disposition": {
            "type": ["string", "null"],
            "enum": ["continue", "retry", "resume_session", "edit_then_retry", "restart_from_checkpoint", "adopt_commits", "snapshot_and_restart", "restore_snapshot", "continue_on_recovery_branch", "rebuild_projection", "human_decision", "abort_only", null],
            "description": "Orthogonal recovery disposition (recovery-redesign plan §4.1, APPENDED by P5): WHAT recovery strategy applies, stamped alongside recovery_cause. null on non-terminal steps and on manifests predating P5."
          },
          "judge_tool_calls": {
            "type": "object",
            "additionalProperties": false,
            "required": ["allowed", "denied"],
            "description": "Pre-execution judge authorization counts for the step's current agent invocation (issue #83). These are allow/deny decisions, not inferred post-execution success. Zeroes mean no judged tool calls were observed.",
            "properties": {
              "allowed": {"type": "integer", "minimum": 0},
              "denied": {"type": "integer", "minimum": 0}
            }
          }
        }
      }
    },
    "next_actions": {
      "type": "array",
      "description": "Structured next action(s); always present, possibly empty (e.g. a done run). Each entry per FR-4.2.",
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": [
          "label",
          "kind",
          "argv",
          "required_inputs",
          "executable",
          "command",
          "consequence"
        ],
        "properties": {
          "label": {"type": "string", "description": "Short action label."},
          "kind": {
            "type": "string",
            "enum": ["observe", "decide", "control", "recover"],
            "description": "Action class."
          },
          "argv": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
            "description": "Already-split, fully-resolved argument tokens — no shell quoting, no interpolation. Executed only when `executable` is true."
          },
          "required_inputs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Named operator-supplied inputs the action needs before it is runnable (empty when none)."
          },
          "executable": {
            "type": "boolean",
            "description": "true only when required_inputs is empty and argv is complete and safe to run as-is."
          },
          "command": {
            "type": "string",
            "description": "Rendered string for HUMAN DISPLAY ONLY, never for execution (may contain placeholder text)."
          },
          "consequence": {
            "type": ["string", "null"],
            "description": "One-line description of what this action does when taken (harness-efficiency FR-8.2): e.g. a gate approve says what proceeds, a gate reject names the adversarial_cycle it re-runs with the notes injected. null for actions with no distinct consequence to spell out."
          }
        }
      }
    }
  },
  "allOf": [
    {
      "description": "parked is an object iff the composite state is a parked class, else null.",
      "if": {
        "properties": {
          "state": {"enum": ["parked_gate", "parked_for_response", "parked_usage_limit", "parked_usage_window", "parked_artifact_invalid", "parked_provider_unavailable"]}
        }
      },
      "then": {"properties": {"parked": {"type": "object"}}},
      "else": {"properties": {"parked": {"type": "null"}}}
    },
    {
      "description": "failure is an object iff the composite state is a failure class, else null.",
      "if": {
        "properties": {
          "state": {"enum": ["failed", "halted", "interrupted"]}
        }
      },
      "then": {"properties": {"failure": {"type": "object"}}},
      "else": {"properties": {"failure": {"type": "null"}}}
    }
  ]
}'''

STATUS_SCHEMA: dict = json.loads(_STATUS_SCHEMA_JSON)


def _validate_status_payload(payload: dict) -> None:
    """Validate a completed payload against the embedded §6.1 schema (F-003).

    Fail-closed before emission: unconstrained persisted inputs (an out-of-enum
    ``StepRecord.status``, a non-string lock field, etc.) flow into the payload
    via the existing models, and the CLI would otherwise print them verbatim —
    schema-invalid JSON that breaks a strict consumer. A violation raises
    :class:`StatusContractError`, which the CLI turns into a non-zero exit with
    empty stdout, never a half-formed object."""
    import jsonschema

    try:
        jsonschema.validate(instance=payload, schema=STATUS_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise StatusContractError(
            f"status payload violates schemas/status.json: {exc.message}"
        ) from exc


def _iteration_for_json(rec: StepRecord) -> int | None:
    """Render a step's iteration as the §6.1 ``integer|null``.

    Goes through :func:`_canonical_iteration`, the *same* canonical
    representation :func:`render_step_id` uses, so ``steps[].iteration`` and the
    rendered ``current_step`` can never diverge — a non-canonical value fails
    closed in both places rather than rendering as ``step.01`` here but
    ``step.1`` there (F-001).
    """
    return _canonical_iteration(rec.iteration)


def _evidence_path(
    run_root: Path, run_instance_dir: Path, failure: FailureDescriptor
) -> str:
    """The §6.1 ``failure.evidence_path``: the failing step's dir, POSIX-relative
    under ``run_root`` (no leading ``/``, no ``..``).

    The dir is ``<run_instance_dir>/steps/<rendered-leaf>`` (mirrors
    :func:`step_dir_for`); the rendered leaf is exactly ``failure.step_id``. That
    leaf becomes a single path component, so it is validated as a single safe
    path segment first — a step id carrying a separator, ``.``/``..``, or NUL
    (``relative_to`` is lexical and would NOT strip it) is a corrupt manifest and
    fails closed rather than emitting a traversal/absolute path that violates
    ``schemas/status.json`` and §6.1 (F-002). ``run_instance_dir`` is always a
    descendant of ``run_root``; a root that is unrelated, or a result that is
    somehow not contained, is likewise a contract violation, not a silent
    fallback.
    """
    try:
        safe_run_segment(failure.step_id, kind="step id")
    except UnsafeRunSegment as exc:
        raise StatusContractError(str(exc)) from exc
    step_dir = run_instance_dir / "steps" / failure.step_id
    try:
        rel = step_dir.relative_to(run_root)
    except ValueError as exc:
        raise StatusContractError(
            f"evidence dir {step_dir} is not under run_root {run_root}"
        ) from exc
    posix = rel.as_posix()
    if posix.startswith("/") or ".." in rel.parts:
        raise StatusContractError(
            f"evidence_path {posix!r} is not a contained relative path"
        )
    return posix


# --- timing / usage rendering (harness-efficiency FR-7.1) --------------------
def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 manifest timestamp (``started``/``ended``); None on any
    failure, so a malformed/absent timestamp yields a null duration rather than
    an exception."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _elapsed_between(start: datetime | None, end: datetime | None) -> float | None:
    """``(end - start)`` in seconds, clamped to ≥0; None if either is missing or
    the subtraction is not well-defined (e.g. mixed naive/aware timestamps)."""
    if start is None or end is None:
        return None
    try:
        return max(0.0, (end - start).total_seconds())
    except TypeError:
        return None


def _step_duration_s(rec: StepRecord, now: datetime) -> float | None:
    """Wall-clock seconds a step ran (FR-7.1): ``ended - started`` for a finished
    step, ``now - started`` while running, else None (no start, or a started step
    with no end that is not currently running)."""
    start = _parse_iso(rec.started)
    if start is None:
        return None
    if rec.ended:
        end = _parse_iso(rec.ended)
    elif rec.status == M.RUNNING:
        end = now
    else:
        end = None
    return _elapsed_between(start, end)


def _run_elapsed_s(man: Manifest, now: datetime) -> float | None:
    """Wall-clock seconds from the earliest step start to now (a running run) or
    to the latest step end (a finished run) (FR-7.1); None if no step started."""
    starts = [t for t in (_parse_iso(s.started) for s in man.steps) if t is not None]
    if not starts:
        return None
    if man.status == M.RUN_RUNNING:
        end: datetime | None = now
    else:
        ends = [t for t in (_parse_iso(s.ended) for s in man.steps) if t is not None]
        end = max(ends) if ends else now
    return _elapsed_between(min(starts), end)


def _usage_totals_dict(u) -> dict:
    """A UsageTotals rendered as the §6.1 ``usage_totals`` object (FR-7.1)."""
    return {
        "input_tokens": u.input_tokens or 0,
        "output_tokens": u.output_tokens or 0,
        "cached_input_tokens": u.cached_input_tokens or 0,
        "cost_usd": u.cost_usd,
    }


# --- gate decision context (harness-efficiency FR-8.1) -----------------------
def _upstream_cycle_record(man: Manifest, gate_rec: StepRecord) -> StepRecord | None:
    """The ``adversarial_cycle`` a gate ratifies: the last cycle before it in
    manifest step order (``prd-cycle`` for ``prd-approve`` etc.).

    Pure over ``man.steps`` — the same relationship the orchestrator resolves from
    the pipeline (``_upstream_cycle_for_gate``), but read from the manifest so both
    the status contract and the web view can name the cycle without a pipeline
    load. Matches ``gate_rec`` by identity first (it is normally an element of
    ``man.steps``), falling back to ``(id, iteration)`` so a copy still resolves.
    Returns ``None`` when no cycle precedes the gate (fail-soft — the caller then
    renders a null convergence / a terminal-reject consequence)."""
    cyc: StepRecord | None = None
    for rec in man.steps:
        if rec is gate_rec or (
            rec.id == gate_rec.id and rec.iteration == gate_rec.iteration
        ):
            return cyc
        if rec.type == "adversarial_cycle":
            cyc = rec
    return cyc


def _resolve_upstream_cycle(
    man: Manifest, gate_rec: StepRecord, pipeline: "Pipeline | None"
) -> StepRecord | None:
    """The cycle record a gate ratifies, resolved for a content-bearing surface.

    With a ``pipeline`` snapshot, resolve the cycle *id* by the same rule the
    reject path re-drives (``upstream_cycle_id_for_gate`` — same non-``foreach``
    stage) and return that manifest record, so the gate context names exactly the
    cycle a reject re-runs (F-001). A foreach gate / a gate with no same-stage
    cycle resolves to ``None`` (terminal reject, no convergence to show). Without
    a snapshot, fall back to the pure manifest-order heuristic (fail-soft)."""
    if pipeline is None:
        return _upstream_cycle_record(man, gate_rec)
    cyc_id = upstream_cycle_id_for_gate(pipeline, gate_rec.id)
    if cyc_id is None:
        return None
    return next((r for r in man.steps if r.id == cyc_id), None)


def _read_json_under(base: Path, rel: str | None) -> object | None:
    """Read+parse a run-relative JSON artifact, fail-soft, with containment.

    ``rel`` is an engine-written run-dir-relative path (a checkpoint ``artifact``
    or ``artifacts/<name>``); it is still resolved and asserted to stay under
    ``base`` so a corrupt/hostile value can never read outside the run tree
    (FR-10.1 posture). Any absence/parse/containment failure returns ``None`` — a
    gate view must never crash on a missing round artifact."""
    if not rel:
        return None
    try:
        base_r = Path(base).resolve()
        target = (base_r / rel).resolve()
        target.relative_to(base_r)
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        return json.loads(target.read_text())
    except (OSError, ValueError):
        return None


def _as_int(v: object) -> int | None:
    """An int metric value, or None (bool excluded — it is an int subclass)."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _convergence_summary(
    cycle_rec: StepRecord | None, run_instance_dir: Path
) -> dict | None:
    """The FR-8.1 convergence block: aggregate ``metrics`` + per-round breakdown.

    Aggregate counts come from the cycle's ``metrics`` (the ``_CycleMetrics``
    dict); the per-round raised/fixed/declined breakdown is read from the round's
    checkpointed artifacts (FR-4: ``artifacts/r<N>/findings.json`` /
    ``triage.json``). No transcript, no re-execution — pure rendering over
    already-persisted data. ``None`` when the gate has no upstream cycle."""
    if cycle_rec is None:
        return None
    metrics = cycle_rec.metrics or {}
    per_round: list[dict] = []
    for rnd in sorted({c.round for c in cycle_rec.checkpoints}):
        cps = {c.sub_step: c for c in cycle_rec.checkpoints if c.round == rnd}
        raised = fixed = declined = None
        rev = cps.get("review")
        if rev is not None:
            data = _read_json_under(run_instance_dir, rev.artifact)
            if isinstance(data, dict):
                raised = len(data.get("findings") or [])
        tri = cps.get("triage")
        if tri is not None:
            data = _read_json_under(run_instance_dir, tri.artifact)
            if isinstance(data, dict):
                verdicts = [v for v in (data.get("verdicts") or []) if isinstance(v, dict)]
                fixed = sum(1 for v in verdicts if v.get("action") == "fix_now")
                declined = sum(
                    1 for v in verdicts if v.get("action") in ("defer", "reject")
                )
        per_round.append(
            {"round": rnd, "raised": raised, "fixed": fixed, "declined": declined}
        )
    return {
        "rounds": _as_int(metrics.get("rounds")),
        "findings_total": _as_int(metrics.get("findings_total")),
        "accepted_total": _as_int(metrics.get("accepted_total")),
        "per_round": per_round,
    }


def _prior_responses(
    cycle_rec: StepRecord | None, gate_rec: StepRecord, redact
) -> list[dict]:
    """Prior human ``--response``/rejection decisions bearing on this gate (FR-8.1).

    A gate rejection is appended to the *upstream cycle's* ``human_responses``
    (``orchestrator.reject_gate`` → ``_append_response``); a gate's own record may
    also carry responses. Both are surfaced in record order with timestamps;
    ``response_text`` is content-bearing and passed through ``redact`` (PRD §7)."""
    out: list[dict] = []
    for src in (cycle_rec, gate_rec):
        if src is None:
            continue
        for hr in src.human_responses:
            out.append(
                {
                    "response_id": hr.response_id,
                    "response_text": redact(hr.response_text),
                    "timestamp": hr.timestamp,
                    "user": hr.user,
                    "state": hr.state,
                }
            )
    return out


def _escalated_findings(run_instance_dir: Path, redact) -> list[dict]:
    """Latest-round triage verdicts flagged ``escalated``/``low_confidence`` merged
    with their finding (FR-8.1 per-escalated-finding reasoning).

    Reads the cycle's latest-round-wins ``artifacts/triage.json`` +
    ``findings.json`` (the same artifacts the gate ``show:`` lists), keeps only the
    engine-flagged verdicts, and joins each to its finding. Content fields
    (claim/reasoning) pass through ``redact`` (PRD §7). Empty when nothing is
    flagged or the artifacts are absent."""
    triage = _read_json_under(run_instance_dir, "artifacts/triage.json")
    findings = _read_json_under(run_instance_dir, "artifacts/findings.json")
    by_id: dict[str, dict] = {}
    if isinstance(findings, dict):
        for f in findings.get("findings") or []:
            if isinstance(f, dict) and f.get("id"):
                by_id[f["id"]] = f
    out: list[dict] = []
    verdicts = triage.get("verdicts") if isinstance(triage, dict) else None
    for v in verdicts or []:
        if not isinstance(v, dict):
            continue
        if not (v.get("escalated") or v.get("low_confidence")):
            continue
        fid = v.get("finding_id")
        if not fid:
            continue
        f = by_id.get(fid, {})
        out.append(
            {
                "finding_id": fid,
                "severity": f.get("severity"),
                "category": f.get("category"),
                "location": f.get("location"),
                "claim": redact(f.get("claim")),
                "verdict": v.get("verdict"),
                "action": v.get("action"),
                "confidence": v.get("confidence"),
                "reasoning": redact(v.get("reasoning")),
            }
        )
    return out


def compute_worktree_block(
    repo_root: Path, man: "M.Manifest", *, mode: str | None = None
) -> dict | None:
    """The §6.1 ``worktree`` object for one run (P7c). I/O-bearing.

    Kept out of :func:`status_payload` so that serializer stays pure, exactly
    like ``gate`` and ``current_step_freshness``.

    ``mode`` is the run's EFFECTIVE mode, resolved by
    ``RunManager._effective_worktree_mode`` — evidence plus what the run
    recorded at birth, never the live config. Passing it in rather than
    re-deriving it here is deliberate: one resolution rule, one place, so the
    status surface and the mutating verbs can never disagree about which tree a
    run drives (R4).

    Returns ``None`` only when git cannot be observed at all, which renders as
    ``worktree: null`` — "unknown", which is a different statement from
    ``mode: "same_tree"`` ("this run drives your own checkout").
    """
    from gauntlet.engine import worktree as WT

    if mode is None:
        mode = man.worktree_mode or WT.MODE_SAME_TREE
    try:
        state = WT.describe(repo_root, mode=mode, branch=man.branch)
    except Exception:  # pragma: no cover - status must never fail on observation
        return None
    return {
        "mode": state.mode,
        "path": str(state.path) if state.path is not None else None,
        "registered": state.registered,
        "present": state.present,
        "locked_reason": state.locked_reason,
        "prunable": state.prunable,
        "head": state.head,
    }


def compute_gate_context(
    man: Manifest,
    run_instance_dir: Path,
    gate_rec: StepRecord,
    *,
    pipeline: "Pipeline | None" = None,
    redaction: "RedactionSettings | None" = None,
) -> dict:
    """Assemble the FR-8.1 gate decision context for a parked ``human_gate``.

    A gate decision must be makeable from this block alone (PRD G6): the upstream
    cycle's convergence summary, the prior human responses/rejections for the
    gate, and the per-escalated-finding triage reasoning — all sourced from the
    manifest + the cycle's persisted artifacts, never a transcript. The I/O
    (round-artifact reads) lives here, not in the pure :func:`status_payload`; the
    caller threads the result in, mirroring ``current_step_freshness`` /
    ``suspension``. Content-bearing fields are redacted (PRD §7).

    ``pipeline`` is the run's pipeline snapshot (loaded by the I/O-bearing
    caller). When given, the upstream cycle is resolved by the *same* rule the
    reject path re-drives (``upstream_cycle_id_for_gate`` — same non-``foreach``
    stage), so ``cycle_step_id`` names the cycle a reject actually re-runs and
    never a cross-stage/foreach cycle it does not (F-001). Absent a snapshot it
    falls back to the pure manifest-order heuristic (fail-soft).

    ``redaction`` is the run's configured redaction list (``RunConfig.redaction``).
    It must be threaded through: with it omitted, a secret whose env-var name
    misses the default KEY/TOKEN heuristic but is configured under
    ``extra_env_vars`` would leak verbatim into this block via ``status --json`` /
    the web gate view (F-002). ``None`` falls back to default settings.

    Always returns a dict (never ``None``) so a gate park always emits a shaped
    block; a gate with no upstream cycle yields ``cycle_step_id``/``convergence``
    null and empty lists (fail-soft, still schema-valid)."""
    from gauntlet.logging.redact import build_redactor

    redactor = build_redactor(redaction)

    def redact(text: object) -> object:
        if not isinstance(text, str):
            return text
        return redactor.redact(text)[0]

    cycle_rec = _resolve_upstream_cycle(man, gate_rec, pipeline)
    return {
        "cycle_step_id": cycle_rec.id if cycle_rec is not None else None,
        "convergence": _convergence_summary(cycle_rec, run_instance_dir),
        "prior_responses": _prior_responses(cycle_rec, gate_rec, redact),
        "escalated": _escalated_findings(run_instance_dir, redact),
    }


def status_payload(
    man: Manifest,
    driver: DriverInfo,
    rstate: RunState,
    reconciliation: Reconciliation | None,
    *,
    run_root: Path,
    run_instance_dir: Path,
    current_step_freshness: float | None = None,
    suspension: dict | None = None,
    gate: dict | None = None,
    now: datetime | None = None,
    current_step_timeout_s: float | None = None,
    projection: dict | None = None,
    worktree: dict | None = None,
) -> dict:
    """The §6.1 ``status --json`` object — a *second rendering* of the P1 state.

    Pure: it serializes the already-computed ``driver`` / ``rstate`` /
    ``reconciliation`` plus manifest fields, doing no I/O and no recomputation,
    so the JSON contract and the human footer can never diverge (FR-4.1/§4.2).
    Nullable fields are always present and explicitly ``null`` when not
    applicable (§6.1). A malformed/unreadable surviving intent surfaces as a
    human-footer anomaly, never here — the caller passes ``reconciliation=None``
    in that case, so ``--json`` never fabricates an ``intent_step_id``.

    ``current_step_freshness`` is the advisory age (seconds) of the newest
    streamed event for a running, streamed step (live-run-observability FR-5),
    computed by the I/O-bearing :func:`compute_current_step_freshness` in the
    caller and threaded in here so this serializer stays pure. ``None`` (a
    non-streamed / not-applicable step, or the pre-first-event window) renders as
    ``current_step_freshness: null``; a number renders as the nested object
    ``{ "last_event_age_s": <number> }`` — the **object** is the nullable unit,
    never a top-level ``last_event_age_s`` (§6.1).

    ``worktree`` is the P7c tree-layout block, assembled by the I/O-bearing
    :func:`compute_worktree_block` in the caller and threaded in the same way.
    It is ALWAYS rendered (never omitted): a `same_tree` run renders the
    `same_tree` object rather than ``null``, because "this run drives your own
    checkout" is a fact the operator needs, not an absence. ``None`` here means
    the caller could not observe git at all, which renders as ``worktree:
    null`` — the honest "unknown", distinct from "same_tree".

    ``gate`` is the FR-8.1 gate decision context, assembled by the I/O-bearing
    :func:`compute_gate_context` in the caller and threaded in the same way (so
    this serializer stays pure). It is ``None`` for every non-gate state and the
    caller passes it only for a ``parked_gate`` — ``None`` renders as ``gate:
    null``.

    The completed object is validated against the committed §6.1 schema before it
    is returned (F-003): unconstrained persisted inputs (e.g. an out-of-enum
    ``StepRecord.status`` or a non-string lock field) can otherwise reach a
    consumer as schema-invalid JSON. A violation raises
    :class:`StatusContractError`, so emission fails closed rather than printing a
    contract-breaking object.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # FR-7.1 timing: current-step elapsed is the current step's own duration; the
    # timeout remaining is best-effort from a caller-resolved effective timeout
    # (null when there is no running current step or no resolvable timeout).
    by_rendered = {render_step_id(rec): rec for rec in man.steps}
    current_rec = (
        by_rendered.get(rstate.current_step) if rstate.current_step else None
    )
    current_elapsed = (
        _step_duration_s(current_rec, now) if current_rec is not None else None
    )
    timeout_remaining: float | None = None
    if (
        current_rec is not None
        and current_rec.status == M.RUNNING
        and current_step_timeout_s is not None
        and current_elapsed is not None
    ):
        timeout_remaining = max(0.0, current_step_timeout_s - current_elapsed)
    # FR-7.1/FR-10.3 quota: the reset time of a usage_limit park, OR the projected
    # replenishment time of a usage_window park (both stored on quota_reset_at);
    # else null.
    quota = None
    if (
        rstate.state in (
            STATE_PARKED_USAGE_LIMIT,
            STATE_PARKED_USAGE_WINDOW,
            STATE_PARKED_PROVIDER_UNAVAILABLE,
        )
        and rstate.parked is not None
    ):
        parked_rec = by_rendered.get(rstate.parked.step_id)
        quota = {"reset_at": parked_rec.quota_reset_at if parked_rec else None}
    payload = {
        "schema_version": SCHEMA_VERSION,
        "slug": man.slug,
        "run_id": man.run_id,
        "run_status": man.status,
        "state": rstate.state,
        "current_step": rstate.current_step,
        "current_step_elapsed_s": current_elapsed,
        "current_step_timeout_remaining_s": timeout_remaining,
        "run_elapsed_s": _run_elapsed_s(man, now),
        # FR-7.1 usage: run-level totals + per-profile split (empty {} when none).
        "totals": _usage_totals_dict(man.totals),
        "agent_usage": {
            name: _usage_totals_dict(u) for name, u in man.agent_usage.items()
        },
        # FR-10.3 advisory channel: non-fatal run anomalies surfaced rather than
        # swallowed — including advisory usage-window shortfalls (the
        # warn-don't-park default). Always present; an empty array when none.
        "warnings": list(man.warnings),
        "quota": quota,
        "worktree": worktree,
        "driver": {
            "state": driver.state,
            "pid": driver.pid,
            "since": driver.since,
            "host": driver.host,
        },
        "parked": (
            {
                "step_id": rstate.parked.step_id,
                "type": rstate.parked.type,
                "reason": rstate.parked.reason,
            }
            if rstate.parked is not None
            else None
        ),
        "failure": (
            {
                "step_id": rstate.failure.step_id,
                "status": rstate.failure.status,
                "evidence_path": _evidence_path(
                    run_root, run_instance_dir, rstate.failure
                ),
            }
            if rstate.failure is not None
            else None
        ),
        "reconciliation": (
            reconciliation.to_dict() if reconciliation is not None else None
        ),
        "current_step_freshness": (
            {"last_event_age_s": current_step_freshness}
            if current_step_freshness is not None
            else None
        ),
        # Suspend/sleep view (FR-5.3): heartbeat age, detected intervals, and the
        # stall classification. Always present; null only when there is neither a
        # heartbeat nor any recorded interval (nothing to report).
        "suspension": suspension,
        # Gate context block (PRD §6 / FR-8.1): always present, non-null only when
        # parked at a human gate. The body (convergence summary, prior responses,
        # per-escalated-finding triage reasoning) is assembled by the I/O-bearing
        # `compute_gate_context` in the caller and threaded in here (like
        # `suspension`), so this serializer stays pure. `None` for every non-gate
        # state — the caller passes it only for a `parked_gate`.
        "gate": gate,
        # P6 (plan §4.6): journal ↔ projection agreement. Always present; null
        # when the on-disk manifest matches the journal head (or the run
        # predates the journal). Non-null names the divergence and whether a
        # rebuild is pending — the caller threads `ProjectionView.payload_block()`.
        "projection": projection,
        "steps": [
            {
                "id": rec.id,
                "iteration": _iteration_for_json(rec),
                "status": rec.status,
                # FR-7.1/FR-7.2 per-step explainers: duration, notes, and the
                # disjoint reason fields. parked_reason is normalized to the PRD
                # enum (a legacy on-disk value / pre-P3 gate is never emitted
                # verbatim); halt_reason is engine-written PRD values (or null).
                "duration_s": _step_duration_s(rec, now),
                "notes": rec.notes,
                "halt_reason": rec.halt_reason,
                "parked_reason": M.normalize_parked_reason(
                    rec.parked_reason, rec.type, rec.status
                ),
                # P5 (plan §6): the orthogonal recovery taxonomy, additive and
                # always-present (null when unstamped / pre-P5).
                "recovery_cause": rec.recovery_cause,
                "recovery_disposition": rec.recovery_disposition,
                # Issue #83: make an all-denied agent visible in ordinary status
                # output without requiring transcript/audit archaeology.
                "judge_tool_calls": {
                    "allowed": rec.judge_tool_calls_allowed,
                    "denied": rec.judge_tool_calls_denied,
                },
            }
            for rec in man.steps
        ],
        "next_actions": [a.to_dict() for a in rstate.next_actions],
    }
    _validate_status_payload(payload)
    return payload


# --- read-only evidence access (`gauntlet logs`, FR-3) -----------------------
TRANSCRIPT_TAIL_LINES = 200  # FR-3.1b — normative default tail for v1
_TRANSCRIPT_NAME = "transcript.md"
_EVENTS_NAME = "events.jsonl"


# --- advisory freshness signal (live-run-observability FR-5) -----------------
def compute_current_step_freshness(
    man: Manifest,
    run_instance_dir: Path,
    *,
    streaming: bool,
    now: float | None = None,
) -> float | None:
    """Age (seconds) of the newest streamed event for a running, streamed step.

    The single I/O point behind the §6.1 ``current_step_freshness`` field
    (FR-5.1). It lives in the status-computation path (not in the pure
    :func:`status_payload`) and its result is threaded into that serializer. The
    value is ``now − mtime`` of the current step's ``events.jsonl`` — the live
    file's last-append time — requiring **no** event-body parse (matching the §6
    note that a freshness read must never block persistence).

    Returns ``None`` (→ ``current_step_freshness: null``) unless **all** hold:

    * the run is streaming (``streaming`` — the ``stream_step_output`` flag); a
      non-streamed run is always ``null``;
    * the manifest ``run_status`` is ``running`` (the only "running step" window;
      a parked/failed/done run has no streaming step);
    * a default (running) step resolves, and its rendered id is a single safe
      path segment that resolves to an ``events.jsonl`` **contained under the run
      instance** — a corrupt manifest id (separator / ``..`` / absolute / NUL) or
      a symlinked leaf that would escape the run tree raises
      :class:`StatusContractError` (fail closed, F-001), never a silent ``null``
      or an uncaught ``ValueError``;
    * a per-line stream is **open** for that step now — its live-stream sidecar
      marker (written by :meth:`StepLogger.open_stream`, removed by
      :meth:`StepStream.close`) is present. A non-empty ``events.jsonl`` from a
      *buffered* adapter (no stream opened) or a *prior/killed* attempt has no
      open marker and stays ``null`` — never misreported as current progress
      (F-002);
    * that step's ``events.jsonl`` **exists and is non-empty** — a single
      ``stat`` does both checks. The pre-first-event window (file absent, i.e.
      ``FileNotFoundError`` / any ``OSError``, or zero bytes) is ``null`` — never
      ``0``, never a surfaced stat error, never an age off the file's
      create/open time. Only once ≥1 line has been appended does a number land.

    A negative result from clock skew (mtime slightly ahead of ``now``) is
    clamped to ``0.0`` so the advisory value is never a nonsensical negative.
    Freshness drives no gate and no automatic action (FR-5.2)."""
    if not streaming or man.status != M.RUN_RUNNING:
        return None
    rec = select_default_step(man)
    if rec is None:
        return None
    # FR-10.1 containment (F-001): the step id flows straight into a filesystem
    # path (``steps/<leaf>/...``); a corrupt manifest whose id carries a
    # separator, ``.``/``..``, an absolute path, or a NUL must fail CLOSED before
    # any stat — never stat an out-of-tree ``events.jsonl``, and never raise an
    # uncaught ``ValueError`` (a NUL byte makes ``Path.stat`` raise ``ValueError``,
    # which the ``except OSError`` below would NOT catch). Mirrors the same guard
    # in :func:`_evidence_path`.
    try:
        safe_run_segment(render_step_id(rec), kind="step id")
    except UnsafeRunSegment as exc:
        raise StatusContractError(str(exc)) from exc
    # Resolve the active transcript leaf the same way `logs`/`--follow` and the
    # console tail do (metadata-driven, never mtime), so the freshness signal is
    # the age of exactly the file those surfaces stream.
    events_path = resolve_transcript_dir(run_instance_dir, rec) / _EVENTS_NAME
    # Defense in depth (F-001): the fully-resolved events path must stay
    # contained under the selected run instance. ``resolve_transcript_dir`` only
    # appends internally-derived leaves, but a symlinked leaf or any other escape
    # is a contract violation — fail closed rather than stat outside the tree.
    try:
        events_path.resolve().relative_to(run_instance_dir.resolve())
    except ValueError as exc:
        raise StatusContractError(
            f"freshness events path {events_path} escapes run instance "
            f"{run_instance_dir}"
        ) from exc
    # A per-line stream must be OPEN for this step right now (F-002):
    # ``StepLogger.open_stream`` writes this sidecar marker next to the live file
    # and ``close()`` removes it. Requiring it keeps a non-empty ``events.jsonl``
    # left by a *buffered* adapter (no stream opened this invocation) or by a
    # *prior/killed* attempt from being misreported as current streamed progress
    # — freshness stays ``null`` until the CURRENT stream has appended a line.
    marker_path = events_path.parent / (events_path.name + STREAM_MARKER_SUFFIX)
    if not marker_path.exists():
        return None  # no live stream open for this step → not current progress
    try:
        st = events_path.stat()
    except OSError:
        return None  # absent / unreadable → pre-first-event window, fail to null
    if st.st_size <= 0:
        return None  # established but empty → no event yet
    if now is None:
        now = time.time()
    return max(0.0, now - st.st_mtime)


# --- suspend/sleep view (harness-efficiency FR-5.3) --------------------------
def _agent_output_age_s(
    man: Manifest, run_instance_dir: Path, now: datetime
) -> float | None:
    """Age (s) since the current running step's adapter child last wrote output.

    Best-effort and advisory (the ``agent_silent`` signal for
    :func:`compute_suspension_view`): the ``now − mtime`` of the running step's
    ``events.jsonl``, regardless of streaming. Any resolution/stat failure (no
    running step, absent file, corrupt id) is ``None`` — never an exception, so
    the classification simply omits the agent-silence input rather than failing.
    """
    if man.status != M.RUN_RUNNING:
        return None
    rec = select_default_step(man)
    if rec is None:
        return None
    try:
        safe_run_segment(render_step_id(rec), kind="step id")
        events_path = resolve_transcript_dir(run_instance_dir, rec) / _EVENTS_NAME
        st = events_path.stat()
    except (UnsafeRunSegment, StatusContractError, OSError, ValueError):
        return None
    return max(0.0, now.timestamp() - st.st_mtime)


def compute_suspension_view(
    man: Manifest,
    run_instance_dir: Path,
    liveness: str,
    *,
    now: datetime | None = None,
    agent_silence_s: float = HB.DEFAULT_AGENT_SILENCE_S,
    interval_s: float = HB.DEFAULT_HEARTBEAT_INTERVAL_S,
    threshold_s: float = HB.SUSPEND_THRESHOLD_S,
) -> dict | None:
    """The §6 ``suspension`` block for ``status --json`` (FR-5.3).

    Surfaces the driver heartbeat age, the detected suspension intervals
    (``manifest.suspensions``), and the fail-closed stall classification
    (``host_suspended`` / ``driver_orphaned`` / ``agent_silent`` / null). All
    inputs are sampled from disk here (the I/O point) and fed to the pure
    :func:`heartbeat.classify_stall`, so the serializer stays pure. Returns
    ``None`` (→ ``suspension: null``) only when there is neither a heartbeat nor
    any recorded interval — nothing to say.

    Classification inputs:

    * ``pid_alive`` — the driver is proven alive only when liveness is ``alive``;
      an ``orphaned`` (proven-dead/reused) driver with a stale heartbeat is the
      ``driver_orphaned`` shape. ``indeterminate``/``none`` never assert
      ``host_suspended`` (fail closed) and never credit.
    * the *current* skew pair — the latest recorded interval counts as the pair
      straddling the current heartbeat gap only when its ``end`` equals the live
      heartbeat's wallclock (i.e. the driver detected the suspend on its most
      recent write). Once the driver writes a later, non-suspend heartbeat the
      match lapses and the run reads as working again, not perpetually suspended.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    hb = HB.HeartbeatSample.read(run_instance_dir / HB.HEARTBEAT_FILENAME)
    hb_age_s: float | None = None
    if hb is not None:
        hb_wall = HB.parse_wallclock(hb.wallclock_utc)
        if hb_wall is not None:
            hb_age_s = max(0.0, (now - hb_wall).total_seconds())
    # Union the manifest's drained intervals with the heartbeat writer's live,
    # still-un-drained ``suspensions.jsonl`` (FR-5.1/5.3): a run that just woke
    # but is still driving, and a crash before the drive drained, both surface.
    intervals = _merge_suspension_intervals(
        man.suspensions, HB.read_persisted_suspensions(run_instance_dir)
    )
    if hb is None and not intervals:
        return None

    # The current skew pair is the interval whose end equals the live heartbeat's
    # wallclock (the driver detected the suspend on its most recent write); once a
    # later non-suspend heartbeat lands the match lapses and the run reads working.
    current = None
    if hb is not None:
        current = next(
            (iv for iv in intervals if iv["end"] == hb.wallclock_utc), None
        )
    classification = HB.classify_stall(
        pid_alive=(liveness == LIVENESS_ALIVE),
        pair_gap_s=(current["gap_s"] if current is not None else None),
        clock_skew=current is not None,
        hb_age_s=hb_age_s,
        agent_output_age_s=_agent_output_age_s(man, run_instance_dir, now),
        interval_s=interval_s,
        threshold_s=threshold_s,
        agent_silence_s=agent_silence_s,
    )
    return {
        "classification": classification,
        "last_heartbeat_age_s": hb_age_s,
        "intervals": intervals,
    }


def _merge_suspension_intervals(manifest_intervals, persisted) -> list[dict]:
    """Union manifest + live-persisted intervals, deduped, manifest order first.

    A drained interval is in the manifest AND still in the append-only log, so
    dedup by (start, end, gap_s) keeps `status` from double-reporting one sleep.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for s in manifest_intervals:
        d = s.model_dump()
        key = (d["start"], d["end"], d["gap_s"])
        if key not in seen:
            seen.add(key)
            out.append(d)
    for s in persisted:
        key = (s.start, s.end, s.gap_s)
        if key not in seen:
            seen.add(key)
            out.append(s.to_dict())
    return out


class LogsError(RuntimeError):
    """`gauntlet logs` could not resolve a step, or a path escaped the run tree.

    A *step-id* / *containment* problem — exit 1. Distinct from an absent or
    unreadable transcript for a *known* step, which is a non-error notice + exit
    0 (FR-3.1c).
    """


@dataclass
class LogsResult:
    """The resolved, read-only evidence view for one step (FR-3).

    ``transcript_lines`` is ``None`` (with ``notice`` set) when the transcript is
    absent or unreadable — the FR-3.1c exit-0 case; otherwise it is the (possibly
    tail-truncated) lines and ``truncated`` says whether the tail was applied.
    """

    run_instance_dir: Path
    step_id: str  # rendered id of the selected top-level step
    step_status: str  # that step's manifest status (for the FR-3.1c notice)
    transcript_dir: Path  # the resolved transcript leaf dir
    transcript_path: Path
    events_path: Path
    transcript_lines: list[str] | None
    truncated: bool
    notice: str | None


def _resolve_under(component: Path, ancestor_real: Path, *, label: str) -> Path:
    """``realpath``-resolve ``component`` and assert it stays under ``ancestor_real``.

    Fail-closed (FR-3.3): a symlink escaping the run tree, or a path that cannot
    be resolved, is refused with a :class:`LogsError` *before* any read.
    """
    try:
        real = component.resolve()
    except (OSError, RuntimeError) as exc:
        raise LogsError(f"cannot resolve {label} {component}: {exc}") from exc
    if not _within(real, ancestor_real):
        raise LogsError(
            f"{label} {component} escapes the run tree; refusing to read it"
        )
    return real


def _contained(path: Path, ancestor_real: Path) -> bool:
    """True iff ``path`` is a non-symlink directory resolving under ``ancestor_real``.

    Fail-closed (FR-3.3): a symlink, an unresolvable path, or one escaping the
    run tree is treated as not contained — so the enumeration below never reads
    or lists directories out of the run tree.
    """
    if path.is_symlink():
        return False
    try:
        real = path.resolve()
    except (OSError, RuntimeError):
        return False
    return _within(real, ancestor_real)


def _addressable_leaves(
    man: Manifest, run_instance_dir: Path, ancestor_real: Path
) -> list[str]:
    """Every selectable ``--step`` leaf: top-level rendered ids + composite sub-leaves.

    For a composite step (cycle/retro) the role sub-dirs (and their immediate
    children, e.g. ``r1-triage/<finding-id>``) are addressable transcripts, so
    they are listed too — bounded to two levels, never following into a symlink
    loop. Each composite step dir is contained under the run tree *before* it is
    enumerated, and ``_subdirs`` never follows symlinks, so a symlinked composite
    step or role can never cause out-of-tree enumeration or leak names through
    the available-steps message (FR-3.3). Sorted for a deterministic error
    message (FR-3.2).
    """
    leaves: list[str] = []
    for rec in man.steps:
        rid = render_step_id(rec)
        leaves.append(rid)
        if rec.type in _COMPOSITE_STEP_TYPES:
            sd = step_dir_for(run_instance_dir, rec)
            if not _contained(sd, ancestor_real):
                continue  # escaping/symlinked composite: never enumerate it
            for role in sorted(_subdirs(sd)):
                leaves.append(f"{rid}/{role.name}")
                for child in sorted(_subdirs(role)):
                    leaves.append(f"{rid}/{role.name}/{child.name}")
    return leaves


def _select_logs_step(
    man: Manifest, run_instance_dir: Path, step: str | None, ancestor_real: Path
) -> tuple[StepRecord, Path]:
    """Resolve ``(top-level record, transcript-leaf dir)`` for `logs` (FR-3.1a/3.2).

    ``step=None`` → the FR-3.1a default step + its resolved transcript leaf. An
    explicit ``step`` is either a top-level rendered id (``<id>`` / ``<id>.<it>``)
    or a composite role sub-leaf path (``<leaf>/r2-fix``,
    ``<leaf>/r1-triage/<finding-id>``). Nested selectors are valid **only** under
    a composite step and bounded to the documented leaf grammar — a role
    (``<leaf>/<role>``) or a role plus one child (``<leaf>/<role>/<finding-id>``),
    i.e. two or three total segments. An unknown id, a nested selector under a
    non-composite step or beyond that depth, or a sub-leaf dir that does not
    exist raises :class:`LogsError` listing the real leaves. ``ancestor_real`` is
    the resolved run dir, used to contain the available-steps enumeration.
    """
    if step is None:
        rec = select_default_step(man)
        if rec is None:
            raise LogsError(f"no steps recorded in {run_instance_dir}")
        return rec, resolve_transcript_dir(run_instance_dir, rec)

    # Split the (possibly nested) selector and validate every segment against
    # traversal — `safe_run_segment` rejects empty / `.` / `..` / NUL.
    segments = step.split("/")
    try:
        for seg in segments:
            safe_run_segment(seg, kind="step")
    except UnsafeRunSegment as exc:
        raise LogsError(str(exc)) from exc

    def _unknown() -> LogsError:
        leaves = _addressable_leaves(man, run_instance_dir, ancestor_real)
        return LogsError(
            f"unknown step {step!r}; available steps: {leaves or '(none)'}"
        )

    head = segments[0]
    by_id = {render_step_id(r): r for r in man.steps}
    rec = by_id.get(head)
    if rec is None:
        raise _unknown()
    if len(segments) == 1:
        return rec, resolve_transcript_dir(run_instance_dir, rec)
    # A nested role sub-leaf is addressable only under a composite step and only
    # to the documented depth (role, or role + finding-id); anything else is an
    # unknown leaf, never an arbitrary nested directory walk (FR-3.2).
    if rec.type not in _COMPOSITE_STEP_TYPES or len(segments) > 3:
        raise _unknown()
    # A non-existent sub-dir is an unknown leaf (exit 1), distinct from an
    # existing dir with no transcript.
    sub = step_dir_for(run_instance_dir, rec).joinpath(*segments[1:])
    if not sub.is_dir():
        raise _unknown()
    return rec, sub


def _read_transcript_tail(
    path: Path, tail: int
) -> tuple[list[str] | None, bool]:
    """Read the last ``tail`` lines of ``path`` → ``(lines | None, truncated)``.

    ``None`` lines means the file is absent or unreadable (FR-3.1c); the full
    file is returned (``truncated=False``) when it has ``≤ tail`` lines.
    """
    try:
        text = path.read_text()
    except (OSError, ValueError):
        return None, False
    lines = text.splitlines()
    if len(lines) <= tail:
        return lines, False
    return lines[-tail:], True


def resolve_logs(
    run_root: Path,
    slug_dir: Path,
    slug: str,
    *,
    step: str | None = None,
    tail: int = TRANSCRIPT_TAIL_LINES,
) -> LogsResult:
    """Resolve read-only evidence for `gauntlet logs <slug>` (FR-3).

    Strictly read-only and contained (FR-3.3): two directional ``realpath``
    checks — the run dir (``slug_dir``) under ``run_root``, and the run-instance
    dir, step dir, transcript leaf, and ``events.jsonl`` each under the run dir —
    so a symlink escaping the run tree, or a traversal in the slug/``--step``, is
    refused before any read. Never writes.
    """
    try:
        safe_run_segment(slug, kind="slug")
    except UnsafeRunSegment as exc:
        raise LogsError(str(exc)) from exc

    # Containment check 1: the run dir is a descendant of (or equal to) run_root.
    run_root_real = run_root.resolve()
    slug_dir_real = _resolve_under(slug_dir, run_root_real, label="run dir")

    # Resolve the instance (validates active-run.txt), then contain it.
    run_instance_dir = resolve_run_instance(slug_dir)
    _resolve_under(run_instance_dir, slug_dir_real, label="run instance")

    # A missing, unreadable, non-JSON, or schema-invalid manifest is the
    # command's controlled error path, not an unhandled crash (FR-3.3, fail
    # closed). `read_text` raises OSError; `model_validate_json` raises pydantic
    # ValidationError (a ValueError) for both JSON-decode and schema failures.
    manifest_path = run_instance_dir / "manifest.json"
    try:
        man = Manifest.load(manifest_path)
    except (OSError, ValueError) as exc:
        raise LogsError(
            f"cannot load manifest {manifest_path}: {exc}"
        ) from exc
    rec, transcript_dir = _select_logs_step(
        man, run_instance_dir, step, slug_dir_real
    )

    # Containment check 2: every leaf path stays under the run dir.
    _resolve_under(step_dir_for(run_instance_dir, rec), slug_dir_real, label="step dir")
    _resolve_under(transcript_dir, slug_dir_real, label="transcript dir")
    transcript_path = transcript_dir / _TRANSCRIPT_NAME
    events_path = transcript_dir / _EVENTS_NAME
    _resolve_under(transcript_path, slug_dir_real, label="transcript")
    _resolve_under(events_path, slug_dir_real, label="events")

    lines, truncated = _read_transcript_tail(transcript_path, tail)
    notice = None
    if lines is None:
        notice = (
            f"transcript absent/unreadable (step status: {rec.status})"
        )
    return LogsResult(
        run_instance_dir=run_instance_dir,
        step_id=render_step_id(rec),
        step_status=rec.status,
        transcript_dir=transcript_dir,
        transcript_path=transcript_path,
        events_path=events_path,
        transcript_lines=lines,
        truncated=truncated,
        notice=notice,
    )


# --- `gauntlet logs --follow`: offset-tail the live step (FR-3) --------------
DEFAULT_FOLLOW_INTERVAL_S = 1.0  # poll cadence; mirrors the console SSE tail
# Cap a single read so one poll never pulls an unbounded log into memory; the
# poll loop drains repeatedly to EOF, so this is a window size, not a ceiling.
# Matches the console's ``store.DEFAULT_LOG_MAX_BYTES`` so CLI and console agree.
FOLLOW_MAX_BYTES = 256 * 1024


@dataclass
class LogChunkBytes:
    """A byte-offset slice of a log file — the shared offset-tail unit.

    ``text`` is the bytes in ``[start, end)`` decoded ``utf-8``/``replace``.
    ``end >= size`` means the read reached EOF. The console SSE tail
    (``store._read_chunk``) and ``gauntlet logs --follow`` both read through
    :func:`read_log_chunk`, so the two surfaces frame identically (plan P3).
    """

    text: str
    start: int
    end: int
    size: int


def read_log_chunk(path: Path, offset: int, max_bytes: int) -> LogChunkBytes:
    """Read the bytes of ``path`` after ``offset`` (up to ``max_bytes``).

    Bytes before ``offset`` are never returned, so a caller re-reading with
    ``offset=<prior end>`` sees only appended bytes. If ``offset`` is past EOF
    (rotation/truncation) ``start`` resets to ``0`` so the reader re-syncs rather
    than reading garbage — identical semantics to the console's
    ``store._read_chunk``. The file must exist; ``--follow`` guards the
    not-yet-created window itself before calling.

    Containment and read operate on a *single* opened object (F-001): the file
    is opened once with ``O_NOFOLLOW`` and then ``fstat``/``read`` go through that
    one descriptor. A caller validates the path's containment (e.g. ``--follow``
    via :func:`_resolve_under`) and then calls here; opening on the same path with
    a separate ``stat``/``open`` would leave a symlink-swap TOCTOU window — a leaf
    swapped to an escaping symlink between the check and the open could redirect
    the tail out of the run tree. ``O_NOFOLLOW`` refuses a symlink leaf at open
    (``OSError``/``ELOOP``, fail-closed; FR-3.3), and ``fstat`` reads the same
    inode the open returned, so there is no second path lookup to race.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        start = max(0, offset)
        if start > size:  # the file shrank under us → resync from the top
            start = 0
        fh.seek(start)
        raw = fh.read(max_bytes)
    end = start + len(raw)
    return LogChunkBytes(
        text=raw.decode("utf-8", errors="replace"), start=start, end=end, size=size
    )


@dataclass
class FollowResult:
    """Outcome of a ``gauntlet logs --follow`` session (read-only, FR-3)."""

    step_id: str  # rendered id of the followed step
    final_status: str  # the step's last-observed manifest status
    followed: bool  # True iff the live poll loop ran (step was `running`)
    interrupted: bool  # True iff stopped by SIGINT (KeyboardInterrupt)


def _reload_step_status(manifest_path: Path, step_id: str) -> str | None:
    """Re-read the manifest and return the rendered ``step_id``'s status.

    Raises :class:`LogsError` if the manifest is absent/unreadable/invalid. The
    manifest is published atomically (``os.replace``), so a reader never sees a
    torn write — a failed load is a genuine integrity problem, not a transient
    race. Mapping such a failure to ``running`` would let ``--follow`` poll a
    corrupt manifest forever and never observe the step end; instead we fail
    closed and surface the error (fail-closed principle; FR-3.1). Returns
    ``None`` only when the manifest loads cleanly but the step id is gone — a
    distinct case the caller may ride out as a transient miss.
    """
    try:
        man = Manifest.load(manifest_path)
    except (OSError, ValueError) as exc:
        raise LogsError(f"cannot reload manifest {manifest_path}: {exc}") from exc
    for rec in man.steps:
        if render_step_id(rec) == step_id:
            return rec.status
    return None


def follow_logs(
    run_root: Path,
    slug_dir: Path,
    slug: str,
    *,
    step: str | None = None,
    emit: Callable[[str], None],
    sleep: Callable[[float], None] = time.sleep,
    interval: float = DEFAULT_FOLLOW_INTERVAL_S,
    max_bytes: int = FOLLOW_MAX_BYTES,
    max_polls: int | None = None,
) -> FollowResult:
    """Tail the current step's ``events.jsonl`` until it ends (FR-3.1/3.2/3.3).

    Resolves the step through the same read-only, contained
    :func:`resolve_logs` path (so traversal in ``slug``/``--step`` is refused and
    only redacted on-disk bytes are ever read — never the raw pipe), then:

    - while the step's manifest status is ``running``, polls ``events.jsonl`` and
      ``emit``s appended bytes every ``interval`` seconds;
    - **reads the status before draining each tick and only stops *after* the
      drain** — so the iteration in which terminal status is first observed still
      drains to EOF, capturing bytes flushed in the window the status flipped
      (no dropped tail; the step's sink is closed before its status goes
      terminal, so every byte is on disk by then);
    - if the step is **not** ``running`` at entry (already finished, or not yet
      started), this degrades to a single one-shot dump + exit — no hang
      (FR-3.2);
    - on SIGINT (``KeyboardInterrupt``) it stops cleanly with
      ``interrupted=True``.

    ``sleep``/``max_polls`` are injectable so a test can drive the loop to a
    deterministic end. ``emit`` receives raw appended text (it already carries
    the per-event newlines); the caller writes it without adding any.
    """
    # Resolve once for containment + the events path + the step identity; the
    # transcript tail it also reads is unused here (cheap, one-time).
    resolved = resolve_logs(run_root, slug_dir, slug, step=step)
    events_path = resolved.events_path
    step_id = resolved.step_id
    manifest_path = resolved.run_instance_dir / "manifest.json"
    # The resolved run dir — the containment ancestor `events_path` must stay
    # under (matches the `label="events"` check in `resolve_logs`). Revalidated
    # before every read below, not just at resolve.
    run_dir_real = _resolve_under(slug_dir, run_root.resolve(), label="run dir")

    def _drain_to_eof(offset: int) -> int:
        """Emit every byte from ``offset`` to current EOF; return the new offset.

        Loops so a large backlog (or a one-shot dump of a finished step) is
        flushed in full, not one window per poll. An absent file (a `running`
        step that has not written its first line yet) is a no-op — the live tail
        simply has nothing to show until the producer creates it (§P4 baseline).

        Containment is re-checked immediately before each read: the live file
        can be created or replaced between polls, so a one-time resolve is a
        TOCTOU hole — a symlink swapped in after resolve would redirect the tail
        out of the run tree. `_resolve_under` follows the leaf symlink and fails
        closed (:class:`LogsError`) if the target escapes; a not-yet-created
        file resolves to its in-tree path and passes (FR-3.3). The remaining
        window between that check and the open is closed inside
        :func:`read_log_chunk`, which opens with `O_NOFOLLOW` and stats/reads the
        same descriptor — so a leaf swapped to an escaping symlink *after*
        `_resolve_under` passes but *before* the open is refused at open (its
        `OSError` is caught here as a no-op; the next poll's `_resolve_under`
        then surfaces it as a `LogsError`). Either way the out-of-tree target is
        never read (F-001).
        """
        while True:
            _resolve_under(events_path, run_dir_real, label="events")
            try:
                chunk = read_log_chunk(events_path, offset, max_bytes)
            except (FileNotFoundError, OSError):
                return offset
            if chunk.text:
                emit(chunk.text)
            offset = chunk.end
            if chunk.end >= chunk.size:
                return offset

    offset = 0
    followed = False
    interrupted = False
    status = _reload_step_status(manifest_path, step_id) or resolved.step_status
    polls = 0
    try:
        first = True
        while max_polls is None or polls < max_polls:
            polls += 1
            if not first:
                sleep(interval)
                status = _reload_step_status(manifest_path, step_id) or M.RUNNING
            first = False
            # Read status *before* draining, stop *after*: the terminal-status
            # iteration still drains to EOF (final drain — no dropped tail).
            offset = _drain_to_eof(offset)
            if status != M.RUNNING:
                break
            followed = True
    except KeyboardInterrupt:  # SIGINT → clean stop (FR-3.1)
        interrupted = True

    return FollowResult(
        step_id=step_id,
        final_status=status,
        followed=followed,
        interrupted=interrupted,
    )


# --- human footer rendering (FR-1.1/FR-1.2) ----------------------------------
def _state_meaning(rstate: RunState) -> str:
    """The footer's "what this state means" phrase for ``rstate``.

    For a halted run with a recorded ``halt_reason``, the meaning names the
    actual guard that fired (#64); everything else uses the per-state line.
    """
    if (
        rstate.state == STATE_HALTED
        and rstate.failure is not None
        and rstate.failure.halt_reason in _HALT_REASON_MEANING
    ):
        return _HALT_REASON_MEANING[rstate.failure.halt_reason]
    return _MEANING.get(rstate.state, "")


def render_footer(
    driver: DriverInfo,
    rstate: RunState,
    *,
    reconciliation: Reconciliation | None = None,
    anomaly: str | None = None,
    current_step_freshness: float | None = None,
    suspension: dict | None = None,
    run_elapsed_s: float | None = None,
    cost_usd: float | None = None,
    quota_reset_at: str | None = None,
) -> list[str]:
    """The status footer lines: driver-liveness line + next-action block.

    Each action renders as ``  $ <command>`` so the footer's commands are
    exactly the ``command`` fields of ``rstate.next_actions`` (FR-1.2 lockstep).

    When ``current_step_freshness`` is a number (a running, streamed step with
    ≥1 streamed event), a single advisory line reports the age of the newest
    event (live-run-observability FR-5). ``None`` (a non-streamed/not-applicable
    step) adds no line, so the footer is unchanged for every existing run.

    When ``suspension`` is present (the :func:`compute_suspension_view` block),
    the footer surfaces the stall classification, the heartbeat age, and each
    detected suspension interval — FR-5.3 requires the human ``status`` to show
    the same heartbeat age + intervals as ``--json``, not just the JSON path.
    ``None`` (no heartbeat and no interval) adds no line.
    """
    lines: list[str] = []
    if driver.state == LIVENESS_NONE:
        lines.append("driver: none (no active drive lock)")
    else:
        extra: list[str] = []
        if driver.pid is not None:
            extra.append(f"pid {driver.pid}")
        if driver.host:
            extra.append(f"host {driver.host}")
        if driver.since:
            extra.append(f"since {driver.since}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"driver: {driver.state}{suffix}")

    lines.append(f"state: {rstate.state} — {_state_meaning(rstate)}")

    # FR-7.3: elapsed + cost-so-far, so a parked/running state is legible without
    # opening a transcript. Each line is added only when its datum is available,
    # so an existing run with no timing/cost recorded renders unchanged.
    if run_elapsed_s is not None:
        lines.append(f"elapsed: {run_elapsed_s:.0f}s")
    if cost_usd is not None:
        lines.append(f"cost so far: ${cost_usd:.4f}")
    # When parked on a provider usage limit, name the reset time (the datum the
    # operator needs to know when a plain `resume` will get past the wall).
    if rstate.state == STATE_PARKED_USAGE_LIMIT:
        lines.append(
            f"quota reset: {quota_reset_at}" if quota_reset_at
            else "quota reset: unknown (no provider retry hint reported)"
        )
    # A usage_window park (FR-10.3) names the projected window-replenishment time,
    # so the operator knows when a plain `resume` will fit the next step.
    if rstate.state == STATE_PARKED_USAGE_WINDOW:
        lines.append(
            f"window replenishes: {quota_reset_at}" if quota_reset_at
            else "window replenishes: unknown"
        )
    # A provider_unavailable park (plan §5.2, P5) names the recorded backoff /
    # Retry-After deadline — the datum that makes the wait legitimate (F-006)
    # and tells the operator when a plain `resume` should retry.
    if rstate.state == STATE_PARKED_PROVIDER_UNAVAILABLE:
        lines.append(
            f"retry deadline: {quota_reset_at}" if quota_reset_at
            else "retry deadline: unrecorded (a repeated resume will refuse as no-progress)"
        )

    if current_step_freshness is not None:
        lines.append(
            f"freshness: last streamed event {current_step_freshness:.1f}s ago "
            "(advisory — drives no action)"
        )

    if suspension is not None:
        classification = suspension.get("classification")
        lines.append(
            f"suspension: {classification if classification else 'none'} "
            "(stall classification, FR-5.3)"
        )
        age = suspension.get("last_heartbeat_age_s")
        if age is not None:
            lines.append(f"heartbeat: last written {age:.1f}s ago")
        intervals = suspension.get("intervals") or []
        if intervals:
            lines.append(f"detected suspensions: {len(intervals)}")
            for iv in intervals:
                lines.append(
                    f"  - {iv['start']} → {iv['end']} ({iv['gap_s']}s)"
                )

    # A lingering lock under a terminal/parked run is harmless residue (§6.3 P2).
    if (
        rstate.state in (STATE_DONE, STATE_ABORTED, STATE_PARKED_GATE,
                         STATE_PARKED_FOR_RESPONSE)
        and driver.state != LIVENESS_NONE
    ):
        lines.append(
            "note: a driver lock is still present; it is residue and does not "
            "change the action"
        )

    if rstate.next_actions:
        lines.append("next actions:")
        for action in rstate.next_actions:
            lines.append(f"  $ {action.command}")
    else:
        lines.append("next actions: (none — the run is finished)")

    if reconciliation is not None:
        if reconciliation.nonce_matches_lock:
            disposition = "finalize"
            verb = "finalize it"
        else:
            # Mismatched nonce: the normative contract discards the intent as
            # stale, so the command reconciles it — it does NOT finalize it.
            disposition = "discard as stale"
            verb = "reconcile it"
        lines.append(
            f"reconciliation: a pending recovery intent for step "
            f"{reconciliation.intent_step_id} survives ({disposition}); run "
            f"`{reconciliation.recommended_command}` to {verb}"
        )
    if anomaly is not None:
        lines.append(f"reconciliation: {anomaly}")
    return lines

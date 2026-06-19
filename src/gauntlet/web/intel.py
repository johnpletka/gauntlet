"""resume_intel — the failure-diagnosis / recovery classifier (P5, FR-5).

A **pure** function over a :class:`~gauntlet.engine.manifest.Manifest`: given the
run's state it returns the recovery the operator should take and which controls
the UI may offer. It re-implements no engine logic — it only *reads* on-disk
state and maps it to advice (D6, FR-10.1).

Determinism is the whole point (CLAUDE.md §2): the classifier keys on the
**existing ``StepRecord.status`` enum** (``parked``/``interrupted``/``halted``/
``failed``/…, all defined in ``engine/manifest.py``) and reads ``notes`` text in
**exactly two** bounded places, both grounded in strings the engine actually
writes and both table-tested so a wording drift fails a test instead of silently
mis-classifying (FR-5.1, R3):

1. within a ``halted`` step — to tell a *timeout* halt from a *budget* halt
   (they share one status); and
2. within a parked ``adversarial_cycle`` step — to split the FR-4.6 escalation
   sub-kinds (FR-10.4 upstream invalidation / FR-10.5 open-blocker / review
   F-009 unresolved blocker).

Ambiguity fails **closed**: a ``halted`` note matching both markers, or neither,
collapses to a generic halt with "inspect the log/diff; raise the relevant guard
before resuming" — never a confident timeout-vs-budget guess (FR-5.2). A control
is never offered when the state makes it meaningless (FR-5.3).
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from gauntlet.engine.manifest import (
    FAILED,
    HALTED,
    INTERRUPTED,
    PARKED,
    RUN_ABORTED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PARKED,
    Manifest,
)
from gauntlet.web.store import _current_record

# --- recovery states (the `state` field) ------------------------------------
GATE = "gate"  # parked at a human_gate → Approve / Reject
ESCALATION = "escalation"  # parked in an adversarial_cycle → Reconcile-then-Resume
INTERRUPTED_STATE = "interrupted"  # mid-edit interrupt → Resume (work preserved)
HALT_TIMEOUT = "halted_timeout"  # halted, timeout note → resume re-triggers
HALT_BUDGET = "halted_budget"  # halted, budget note → raise budget first
HALT_GENERIC = "halted_generic"  # halted, ambiguous/unknown note → inspect first
FAILED_STATE = "failed"  # hard failure → resume will not help
REJECTED = "rejected"  # a rejected gate → terminal
RUNNING_STATE = "running"  # nothing to recover (still driving)
DONE_STATE = "done"
ABORTED_STATE = "aborted"
PARKED_UNKNOWN = "parked_unknown"  # parked, but step state unrecognized → fail closed

# --- available controls (the verbs the UI may surface) ----------------------
APPROVE = "approve"
REJECT = "reject"
RESUME = "resume"

# --- escalation sub-kinds (FR-4.6/FR-5.2, EXP-1) ----------------------------
SUB_UPSTREAM = "upstream"  # FR-10.4 upstream invalidation
SUB_OPEN_BLOCKER = "open_blocker"  # FR-10.5 max_rounds / not fixed this round
SUB_UNRESOLVED = "unresolved"  # review F-009 blocking/low-confidence verdict
SUB_UNKNOWN = "unknown"

# Engine-grounded note markers. These are the *only* note substrings the
# classifier interprets; each is emitted verbatim by the engine (cites below),
# so a change there is caught by the table tests rather than mis-classifying.
#   timeout halt  — orchestrator.py:244 "timeout halt (FR-3.3): …"
#                   steptypes.py:86     "shell timeout halt (FR-3.3): …"
#   budget halt   — orchestrator.py:324 "budget halt (FR-3.3): …"
_TIMEOUT_MARK = "timeout halt"
_BUDGET_MARK = "budget halt"
# escalation markers — engine/cycle.py (all on a parked adversarial_cycle step,
# the note always *begins* with the case-insensitive marker "escalation"):
_ESCALATION_MARK = "escalation"

# A finding id as the engine writes it (F-001, F-R1-MUTATION). The leading
# word-boundary + required hyphen-after-F means "FR-10.4" never matches.
_FINDING_ID_RE = re.compile(r"\bF-[A-Za-z0-9][A-Za-z0-9-]*")


class ResumeIntel(BaseModel):
    """The classifier's verdict for the UI's recovery banner (FR-5.1)."""

    state: str
    recommended_action: str
    rationale: str
    available_controls: list[str] = []
    # Escalation context (only set for the ESCALATION state) so the panel can
    # frame the reconciliation without re-parsing the note (FR-4.6/FR-5.2).
    sub_kinds: list[str] = []
    escalated_finding_ids: list[str] = []


def extract_finding_ids(notes: str | None) -> list[str]:
    """Finding ids referenced in a note (``F-001`` style), de-duped, in order."""
    if not notes:
        return []
    seen: dict[str, None] = {}
    for m in _FINDING_ID_RE.findall(notes):
        seen.setdefault(m, None)
    return list(seen)


def escalation_sub_kinds(notes: str) -> list[str]:
    """Split a cycle-escalation note into its FR-4.6 sub-kinds (may be several).

    A single escalation note can carry *both* an upstream-invalidation reason and
    an open-blocker reason (engine/cycle.py joins them with ``; ``), so this
    returns every sub-kind present. Grounded in the engine's exact wording; falls
    back to :data:`SUB_UNKNOWN` only if none of the known markers appear (which a
    table test guards against silent drift).
    """
    low = notes.lower()
    kinds: list[str] = []
    if "upstream invalidation" in low or "fr-10.4" in low:
        kinds.append(SUB_UPSTREAM)
    if "fr-10.5" in low or "max_rounds" in low or "not fixed this round" in low:
        kinds.append(SUB_OPEN_BLOCKER)
    if "review f-009" in low:
        kinds.append(SUB_UNRESOLVED)
    return kinds or [SUB_UNKNOWN]


def _gate_intel(notes: str) -> ResumeIntel:
    # An UPSTREAM CONFLICT signalled at a gate is a human reconciliation, not a
    # resume (FR-4.5) — but it is still resolved through the gate's Approve/Reject
    # verbs, so the controls are unchanged; the gate panel surfaces the conflict
    # text. resume is *not* the verb for a gate (FR-5.2).
    if "upstream conflict" in notes.lower():
        return ResumeIntel(
            state=GATE,
            recommended_action=(
                "the builder signalled an UPSTREAM CONFLICT — reconcile the "
                "approved artifact (human ratification) before deciding; then "
                "Approve to continue or Reject with notes"
            ),
            rationale=(
                "a parked human_gate whose notes carry an UPSTREAM CONFLICT is a "
                "reconciliation decision (FR-4.5); the engine still only offers "
                "approve/reject for a gate"
            ),
            available_controls=[APPROVE, REJECT],
        )
    return ResumeIntel(
        state=GATE,
        recommended_action="review the assembled evidence, then Approve or Reject",
        rationale=(
            "a parked human_gate is a human decision; resume is not the verb "
            "(FR-4.1/FR-5.2)"
        ),
        available_controls=[APPROVE, REJECT],
    )


def _escalation_intel(notes: str) -> ResumeIntel:
    kinds = escalation_sub_kinds(notes)
    ids = extract_finding_ids(notes)
    if SUB_UPSTREAM in kinds:
        action = (
            "reconcile the upstream artifact named in the triage "
            "`target_artifact` (amend the approved PRD/plan through human "
            "ratification), or apply the in-code fix, THEN Resume"
        )
    elif SUB_UNRESOLVED in kinds:
        action = (
            "a human must resolve the blocking / low-confidence finding(s) no "
            "escalation agent could (review F-009); reconcile, then Resume"
        )
    else:  # open-blocker / max_rounds, or unknown → still reconcile-then-resume
        action = (
            "open blocker(s) exhausted the cycle (FR-10.5); resolve the "
            "finding(s) — in code or by amending the upstream artifact — then "
            "Resume"
        )
    return ResumeIntel(
        state=ESCALATION,
        recommended_action=action,
        rationale=(
            "a bare Resume re-runs the adversarial_cycle from scratch and will "
            "re-park on the same escalation unless the conflict is reconciled "
            "first (FR-4.6); the engine offers no approve/reject for a cycle park"
        ),
        # Resume only — never approve/reject for a cycle park (FR-5.2).
        available_controls=[RESUME],
        sub_kinds=kinds,
        escalated_finding_ids=ids,
    )


def _halt_intel(notes: str) -> ResumeIntel:
    low = notes.lower()
    has_timeout = _TIMEOUT_MARK in low
    has_budget = _BUDGET_MARK in low
    if has_timeout and not has_budget:
        return ResumeIntel(
            state=HALT_TIMEOUT,
            recommended_action=(
                "Resume re-triggers the same timeout — raise the step/profile "
                "timeout first (config guidance; not auto-applied), then Resume"
            ),
            rationale=(
                "a deterministic timeout halt re-fires on a bare resume; editing "
                "the snapshot pipeline would break the resume hash guard, so the "
                "fix is a profile/config change (FR-5.2)"
            ),
            available_controls=[RESUME],
        )
    if has_budget and not has_timeout:
        return ResumeIntel(
            state=HALT_BUDGET,
            recommended_action=(
                "Resume re-triggers the same budget halt — raise `budget_usd` "
                "first (config guidance; not auto-applied), then Resume"
            ),
            rationale=(
                "a deterministic budget halt re-fires on a bare resume; the fix "
                "is a config change before resuming (FR-5.2)"
            ),
            available_controls=[RESUME],
        )
    # Both markers, or neither → fail closed to a generic halt: never a confident
    # single-cause guess (FR-5.2, R3).
    return ResumeIntel(
        state=HALT_GENERIC,
        recommended_action=(
            "inspect the captured log/diff and raise the relevant guard "
            "(timeout or budget) before resuming"
        ),
        rationale=(
            "the halt note did not unambiguously identify timeout vs budget; "
            "failing closed to generic guidance rather than guessing (FR-5.2)"
        ),
        available_controls=[RESUME],
    )


def resume_intel(manifest: Manifest) -> ResumeIntel:
    """Classify a run's state into a recovery recommendation (FR-5.1/5.2/5.3).

    Pure and side-effect free, so it is table-testable in isolation over fixture
    manifests. The primary discriminator is the run status plus the *current
    step's* ``StepRecord.status`` enum; ``notes`` is consulted only in the two
    bounded places documented in the module header.
    """
    rec = _current_record(manifest)
    run_status = manifest.status
    notes = (rec.notes or "") if rec else ""

    if run_status == RUN_PARKED:
        if rec is None:
            return ResumeIntel(
                state=PARKED_UNKNOWN,
                recommended_action="inspect the run; no current step is recorded",
                rationale="run is parked but current_step resolves to no record",
                available_controls=[RESUME],
            )
        if rec.type == "human_gate" and rec.status == PARKED:
            return _gate_intel(notes)
        if (
            rec.type == "adversarial_cycle"
            and rec.status == PARKED
            and notes.lower().startswith(_ESCALATION_MARK)
        ):
            return _escalation_intel(notes)
        if rec.status == INTERRUPTED:
            return ResumeIntel(
                state=INTERRUPTED_STATE,
                recommended_action="Resume — the engine re-enters the step cleanly",
                rationale=(
                    "a mid-edit interrupt parks the run; partial work is "
                    "preserved and resume applies the interrupted-step policy "
                    "(FR-5.2)"
                ),
                available_controls=[RESUME],
            )
        if rec.status == HALTED:
            return _halt_intel(notes)
        # Parked but not a recognized sub-state → fail closed: still resumable,
        # but advise inspection (FR-5.3 — never invent a richer control).
        return ResumeIntel(
            state=PARKED_UNKNOWN,
            recommended_action="inspect the current step before resuming",
            rationale=(
                f"run is parked at a {rec.type} step in state {rec.status!r} "
                "that the classifier does not specially recognize"
            ),
            available_controls=[RESUME],
        )

    if run_status == RUN_FAILED:
        if rec is not None and notes.lower().startswith("rejected:"):
            return ResumeIntel(
                state=REJECTED,
                recommended_action=(
                    "terminal — the gate was rejected; start a fresh run after "
                    "addressing the feedback (abort/clean the branch as needed)"
                ),
                rationale="a rejected gate fails the run; resume is meaningless",
                available_controls=[],
            )
        return ResumeIntel(
            state=FAILED_STATE,
            recommended_action=(
                "Resume will not help — read the step log and failing diff; the "
                "fix happens outside the console, then start/resume as appropriate"
            ),
            rationale=(
                "a hard failure (test failure / agent crash / invalid commit / "
                "missing completion signal) re-fires on a bare resume (FR-5.2)"
            ),
            available_controls=[],
        )

    if run_status == RUN_DONE:
        return ResumeIntel(
            state=DONE_STATE,
            recommended_action="run completed; nothing to recover",
            rationale="run status is done",
            available_controls=[],
        )
    if run_status == RUN_ABORTED:
        return ResumeIntel(
            state=ABORTED_STATE,
            recommended_action="run was aborted; start a fresh run if needed",
            rationale="run status is aborted",
            available_controls=[],
        )
    # Still running — no recovery action; the abort control is governed by
    # ownership in the detail view, not by resume_intel.
    return ResumeIntel(
        state=RUNNING_STATE,
        recommended_action="run is in progress",
        rationale="run status is running",
        available_controls=[],
    )


__all__ = [
    "ResumeIntel",
    "resume_intel",
    "extract_finding_ids",
    "escalation_sub_kinds",
    "GATE",
    "ESCALATION",
    "INTERRUPTED_STATE",
    "HALT_TIMEOUT",
    "HALT_BUDGET",
    "HALT_GENERIC",
    "FAILED_STATE",
    "REJECTED",
    "SUB_UPSTREAM",
    "SUB_OPEN_BLOCKER",
    "SUB_UNRESOLVED",
    "SUB_UNKNOWN",
    "APPROVE",
    "REJECT",
    "RESUME",
]

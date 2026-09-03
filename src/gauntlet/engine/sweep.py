"""Unattended resume sweep — idempotent, judgment-free (issue #134, rec. 1b).

A dead driver cannot self-resume: the in-process auto-resume loop (FR-3.4)
dies with the process that armed it, and a stale drive lock is only ever
reclaimed by the next driving verb someone types. Unattended recovery
therefore needs a *resident* process — the console's timer, or a cron/launchd
job — running a sweep that takes ONLY the two actions the operator playbook
already classes as no-decision:

* **orphan reclaim** — a run whose manifest says ``running`` while its drive
  lock names a driver that :func:`locking.record_is_live` PROVES dead or
  PID-reused. ``gauntlet resume`` is the documented action; the sweep merely
  types it.
* **firing a due schedule** — a parked step carrying an armed
  ``scheduled_resume`` whose ``attempt_at`` has passed and whose attempts are
  under the ceiling, under the config knob that armed it. The driver that
  armed it would have fired it had it survived.

Everything else is skipped with a one-line reason: gates and response parks
(a human decision), ``indeterminate`` liveness and malformed locks (fail
closed, FR-10.5), live drivers (theirs to drive), terminal runs, and every
failure state (``failed``/``halted``/``interrupted`` each need the playbook's
judgment). The decision is a pure function of the composite state
(:func:`recovery_exec.classify_composite` — the SAME classifier ``status`` and
the mutating verbs use, R4), the lock proof, the schedule and the config, so
it is table-testable and cannot drift from what ``status`` renders.

The sweep never reimplements a resume: an action goes through
:meth:`RunManager.resume` (lock reclaim + projection reconcile + drive), either
in the foreground or as a detached ``gauntlet resume <slug>`` child supplied
by the caller as a ``launcher``. Mutual exclusion between a console sweep, a
cron sweep and a human's own ``resume`` rides on the drive lock: the audit
stamp is written under it, and a resume that loses the race fails closed
inside the engine and is reported as ``refused`` — never silently retried.

Reason-agnostic by design: "due" is a property of ``scheduled_resume`` alone
(``attempt_at <= now`` and ``attempts < max_attempts``), whichever park reason
armed it; the park reason only selects the config knob that must be ``auto``.
A knob that does not exist on the loaded config is a skip, not a guess.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.engine import locking
from gauntlet.engine import manifest as M
from gauntlet.engine import operator
from gauntlet.engine import recovery_exec as RX
from gauntlet.engine.config import RESUME_ON_QUOTA_AUTO
from gauntlet.engine.manifest import Manifest
from gauntlet.engine.recovery import NoProgressError
from gauntlet.engine.run import (
    AUTO_RESUME_EXHAUST,
    AUTO_RESUME_RESUME,
    AUTO_RESUME_WAIT,
    UnsafeRunSegment,
    next_auto_resume_action,
    safe_run_segment,
)

log = logging.getLogger(__name__)

# What the sweep did for one run.
ACTION_RESUMED = "resumed"  # a resume was launched (foreground done / child started)
ACTION_SKIPPED = "skipped"  # no-decision rule did not apply; nothing touched
ACTION_REFUSED = "refused"  # the engine's resume failed closed (lock race, guard)

# Why the sweep acted — stamped into the manifest audit warning.
REASON_ORPHAN = "orphan_reclaim"
REASON_SCHEDULE = "scheduled_resume"

# The lock proof the sweep computes for a run (the reclaim precondition).
LOCK_DEAD = "dead"  # a record for THIS slug whose holder is proven dead/reused
LOCK_LIVE = "live"  # some holder in scope is live (or unverifiable → live)
LOCK_MALFORMED = "malformed"  # exists but unreadable/unparseable → fail closed
LOCK_ABSENT = "absent"  # no lock anywhere in scope

# Composite states that mean "a human decides" — never swept.
_HUMAN_STATES = frozenset({
    RX.STATE_PARKED_GATE,
    RX.STATE_PARKED_FOR_RESPONSE,
    RX.STATE_PARKED_ARTIFACT_INVALID,
})
_FAILURE_STATES = frozenset({
    RX.STATE_FAILED, RX.STATE_HALTED, RX.STATE_INTERRUPTED,
})
_TERMINAL_STATES = frozenset({RX.STATE_DONE, RX.STATE_ABORTED})

# Engine refusals a launched resume may raise. These are the fail-closed
# outcomes the sweep REPORTS (exit 0 — nothing is wrong with the sweep): a lock
# lost to a concurrent driver, a guard, a "cannot proceed" validation, an
# unchanged-fingerprint NoProgressError. Every engine error class derives from
# RuntimeError/ValueError; a genuine bug (TypeError, AttributeError, …) is not
# in this tuple and propagates so the sweep exits non-zero.
_RESUME_REFUSALS: tuple[type[BaseException], ...] = (
    NoProgressError, RuntimeError, ValueError, OSError,
)


@dataclass(frozen=True)
class SweepDecision:
    """The pure verdict for one run: act (and why) or skip (and why)."""

    act: bool
    reason: str  # REASON_* when acting; a one-line skip reason otherwise
    step_id: str | None = None  # the parked step a schedule action targets
    park_reason: str | None = None  # normalized park reason for the audit line

    @property
    def audit_reason(self) -> str:
        if self.reason == REASON_SCHEDULE and self.park_reason:
            return f"{REASON_SCHEDULE}/{self.park_reason}"
        return self.reason


@dataclass
class SweepOutcome:
    """One line of sweep output: slug, state, action, reason."""

    slug: str
    run_id: str | None
    state: str
    action: str
    reason: str
    detail: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self) -> str:
        line = f"{self.slug}\t{self.state}\t{self.action}: {self.reason}"
        if self.detail:
            line += f" — {self.detail}"
        return line


Launcher = Callable[[object, str, Path, str | None], str]


# --- knob selection ----------------------------------------------------------
def auto_resume_knob(config: object, park_reason: str | None) -> tuple[str | None, bool]:
    """``(knob_name, is_auto)`` for the config knob governing ``park_reason``.

    ``usage_limit`` → ``resume_on_quota``; ``provider_unavailable`` →
    ``resume_on_provider_unavailable`` (the sibling change that arms schedules
    on dependency parks). A reason with no knob, or a knob absent from the
    loaded config, answers ``(None, False)`` — the sweep skips rather than
    inferring a policy the operator never set.
    """
    knob = {
        M.PARKED_REASON_USAGE_LIMIT: "resume_on_quota",
        M.PARKED_REASON_PROVIDER_UNAVAILABLE: "resume_on_provider_unavailable",
    }.get(park_reason or "")
    if knob is None:
        return (None, False)
    value = getattr(config, knob, None)
    if value is None:
        return (knob, False)
    return (knob, str(value).strip().lower() == RESUME_ON_QUOTA_AUTO)


# --- the pure decision --------------------------------------------------------
def decide(
    man: Manifest,
    liveness: str,
    *,
    lock: str,
    config: object,
    now: datetime,
) -> SweepDecision:
    """The state → action table (spec item 1). Pure; no I/O.

    ``liveness`` is :func:`operator.driver_liveness`'s answer, ``lock`` the
    :func:`lock_proof` for the run. Both are threaded in so the table is
    exercised row by row in tests with no lockfiles or processes.
    """
    state, parked_rec, _failure = RX.classify_composite(man, liveness)

    if state in _TERMINAL_STATES:
        return SweepDecision(False, f"{state}: terminal run")
    if state == RX.STATE_IN_PROGRESS:
        return SweepDecision(False, "in_progress: a live driver owns this run")
    if state == RX.STATE_INDETERMINATE:
        return SweepDecision(
            False, "indeterminate: driver liveness cannot be proven — fail closed"
        )
    if state == RX.STATE_UNKNOWN:
        return SweepDecision(
            False, "unknown: unclassifiable state — read-only inspection only"
        )
    if state in _HUMAN_STATES:
        return SweepDecision(False, f"{state}: awaiting a human decision")
    if state in _FAILURE_STATES:
        return SweepDecision(
            False, f"{state}: a failure state — recovery needs the playbook's judgment"
        )

    # (a) orphan reclaim: running + dead-or-absent driver. Act ONLY on a lock
    # that PROVES the driver dead or PID-reused; a run that says `running`
    # with no lock at all has no proof to offer, and an unreadable lock may
    # belong to a live holder (FR-10.5).
    if state == RX.STATE_ORPHANED:
        if lock == LOCK_DEAD:
            return SweepDecision(True, REASON_ORPHAN)
        if lock == LOCK_ABSENT:
            return SweepDecision(
                False,
                "orphaned: no drive lock to prove the driver dead (liveness "
                "none) — inspect with `gauntlet status`, then `gauntlet resume`",
            )
        if lock == LOCK_MALFORMED:
            return SweepDecision(
                False, "orphaned: drive lock is malformed — refusing to reclaim "
                "a lock whose holder cannot be identified (fail closed)",
            )
        return SweepDecision(
            False, "orphaned: a live lock holder is in scope — not reclaimable"
        )

    # (b) a parked step with an armed, due schedule under its knob.
    if parked_rec is None:
        return SweepDecision(False, f"{state}: no parked step to act on")
    reason = M.normalize_parked_reason(
        parked_rec.parked_reason, parked_rec.type, parked_rec.status
    )
    sched = parked_rec.scheduled_resume
    if sched is None:
        return SweepDecision(
            False, f"{state}: no scheduled_resume armed — a human resumes"
        )
    knob, is_auto = auto_resume_knob(config, reason)
    if knob is None:
        return SweepDecision(
            False, f"{state}: no auto-resume knob governs {reason!r} parks"
        )
    if not is_auto:
        return SweepDecision(
            False, f"{state}: scheduled_resume armed but {knob} is not `auto`"
        )
    # The driver that armed the schedule may still be alive, waiting it out in
    # process (FR-3.4). It fires the schedule itself; never race it.
    if liveness == operator.LIVENESS_ALIVE:
        return SweepDecision(
            False, f"{state}: a live driver is waiting on this schedule"
        )
    if liveness == operator.LIVENESS_INDETERMINATE or lock in (
        LOCK_LIVE, LOCK_MALFORMED
    ):
        return SweepDecision(
            False, f"{state}: driver/lock state cannot be proven — fail closed"
        )
    action, wait_s = next_auto_resume_action(sched, now)
    if action == AUTO_RESUME_EXHAUST:
        return SweepDecision(
            False,
            f"{state}: schedule exhausted ({sched.attempts}/{sched.max_attempts} "
            "attempts) — a human resumes",
        )
    if action == AUTO_RESUME_WAIT:
        return SweepDecision(
            False, f"{state}: not due until {sched.attempt_at} ({int(wait_s)}s)"
        )
    assert action == AUTO_RESUME_RESUME
    return SweepDecision(
        True, REASON_SCHEDULE, step_id=parked_rec.id, park_reason=reason
    )


# --- I/O: lock proof, enumeration, execution ----------------------------------
def lock_paths(run_root: Path, run_dir: Path) -> list[Path]:
    """Every path this run's drive lock can occupy: per-run, per-slug, tree.

    Mirrors ``RunManager._lock_paths_for`` (P7b/#86): the per-run lock inside
    the instance dir, the per-slug minting lock, and the retained worktree
    guard. A live holder at ANY of them blocks (the engine's own acquire
    reads all three and fails closed on a live one).
    """
    name = locking.DRIVING_LOCK_NAME
    return [run_dir / name, run_dir.parent / name, run_root / name]


def lock_proof(run_root: Path, run_dir: Path, slug: str) -> str:
    """Classify the lock evidence for ``slug`` in FR-10.5 terms.

    * ``malformed`` — any lock in scope exists but cannot be read/parsed:
      fail closed, whatever else is present (mirrors ``_acquire_one``).
    * ``live`` — any holder in scope that :func:`locking.record_is_live`
      cannot prove gone (alive, or alive-but-unverifiable).
    * ``dead`` — a record naming THIS slug whose holder is proven dead or
      PID-reused, with nothing live in scope. The only reclaim proof.
    * ``absent`` — no evidence anywhere.
    """
    proof = LOCK_ABSENT
    for path in lock_paths(run_root, run_dir):
        kind, rec = locking.read_lock_state(path)
        if kind == locking.LOCK_MALFORMED:
            return LOCK_MALFORMED
        if kind != locking.LOCK_PRESENT:
            continue
        assert rec is not None
        if locking.record_is_live(rec):
            return LOCK_LIVE
        if rec.slug == slug:
            proof = LOCK_DEAD
        # A dead FOREIGN record (another slug's stale tree guard) is not
        # evidence about this run either way; the engine reclaims it on its
        # own terms when this run's resume takes the guard.
    return proof


def enumerate_slugs(run_root: Path) -> list[str]:
    """Every slug dir under ``run_root`` holding at least one run instance."""
    if not run_root.exists():
        return []
    out: list[str] = []
    for d in sorted(run_root.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if operator.list_run_instances(d):
            out.append(d.name)
    return out


def _resolve_run_dir(mgr, slug: str) -> Path:
    """The slug's run instance, containment-checked (the CLI's two-link chain)."""
    safe_run_segment(slug, kind="slug")
    layout = mgr.layout(slug)
    run_dir = operator.resolve_run_instance(layout.slug_dir)
    run_root = (mgr.repo_root / mgr.config.run_root).resolve()
    slug_dir = layout.slug_dir.resolve()
    slug_dir.relative_to(run_root)
    run_dir.resolve().relative_to(slug_dir)
    return run_dir


def audit_note(reason: str, now: datetime) -> str:
    """The manifest warning every sweep action stamps (spec item 5)."""
    return f"unattended sweep resumed ({reason}) at {now.isoformat()}"


def _stamp_under_lock(
    mgr, slug: str, run_dir: Path, run_id: str | None, decision: SweepDecision,
    now: datetime,
) -> str | None:
    """Re-verify and stamp the audit warning under the drive lock.

    Returns ``None`` when the run may proceed to its resume, else a skip
    reason. Taking the lock here is the double-resume guard: a concurrent
    sweep or human resume that already holds it makes this raise
    ``WorktreeLockError`` (caller reports ``refused``); and the reload +
    re-check means a state that moved between the read-only decision and
    this write is never acted on from stale evidence. For a schedule action
    the attempt is counted write-ahead (the same discipline as the in-process
    loop's ``_arm_next_attempt``) so a crash after launch never re-tries for
    free and a persistent limit cannot become a hot loop across sweeps.
    """
    handle = mgr._acquire_worktree_lock(slug, run_id, run_dir=run_dir)
    try:
        man = Manifest.load(run_dir / "manifest.json")
        if decision.reason == REASON_ORPHAN:
            if man.status != M.RUN_RUNNING:
                return f"state changed under the sweep (run is {man.status})"
        else:
            step = next(
                (s for s in man.steps
                 if s.id == decision.step_id and s.status == M.PARKED), None,
            )
            if step is None or step.scheduled_resume is None:
                return "state changed under the sweep (schedule gone)"
            sched = step.scheduled_resume
            if sched.attempts >= sched.max_attempts:
                return "state changed under the sweep (schedule exhausted)"
            sched.attempts += 1
        note = audit_note(decision.audit_reason, now)
        if note not in man.warnings:
            man.warnings.append(note)
        man.write_atomic(run_dir / "manifest.json")
        return None
    finally:
        mgr._release_worktree_lock(handle)


def foreground_launcher(mgr, slug: str, run_dir: Path, run_id: str | None) -> str:
    """Default launcher: drive the resume in this process (blocks until the
    next park). The single-slug CLI default."""
    status = mgr.resume(slug)
    return f"run status: {status}"


SWEEP_LOG_NAME = "sweep-resume.log"


def detached_launcher(mgr, slug: str, run_dir: Path, run_id: str | None) -> str:
    """Launch ``gauntlet resume <slug>`` as a detached child (its own session,
    stdin closed, output appended to ``<run_dir>/sweep-resume.log``) and return
    at once. The ``--all`` default: one stuck run can never block the sweep of
    the others, and the child holds the drive lock on its own terms — a
    concurrent human resume fails closed inside the engine, never here.
    ``python -m gauntlet`` so the child never depends on a console entry point."""
    import subprocess
    import sys

    log_path = Path(run_dir) / SWEEP_LOG_NAME
    with log_path.open("ab") as log_fh:
        log_fh.write(
            f"--- unattended sweep launched `gauntlet resume {slug}` at "
            f"{datetime.now(timezone.utc).isoformat()}\n".encode()
        )
        proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [sys.executable, "-m", "gauntlet", "resume", slug],
            cwd=str(mgr.repo_root),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return f"detached `gauntlet resume {slug}` (pid {proc.pid}; log {log_path})"


def sweep_run(
    mgr, slug: str, *, now: datetime | None = None, launcher: Launcher | None = None,
) -> SweepOutcome:
    """Sweep one run: classify read-only, act only on a no-decision rule."""
    now = now or datetime.now(timezone.utc)
    launch = launcher or foreground_launcher
    run_root = mgr.repo_root / mgr.config.run_root
    try:
        run_dir = _resolve_run_dir(mgr, slug)
    except (UnsafeRunSegment, operator.RunResolutionError, ValueError, OSError) as exc:
        return SweepOutcome(slug, None, RX.STATE_UNKNOWN, ACTION_SKIPPED,
                            f"unresolvable run instance: {exc}")
    # The authoritative state, read-only (P6): the journal head when the
    # projection is stale/corrupt — the same view `status` classifies from.
    try:
        view = operator.load_projection_view(mgr.repo_root, run_dir, slug=slug)
        man = view.manifest
    except (OSError, ValueError) as exc:
        return SweepOutcome(slug, run_dir.name, RX.STATE_UNKNOWN, ACTION_SKIPPED,
                            f"manifest unreadable: {exc}")
    if man is None:
        return SweepOutcome(slug, run_dir.name, RX.STATE_UNKNOWN, ACTION_SKIPPED,
                            "manifest unreadable and no journal state to classify from")
    liveness = operator.driver_liveness(run_root, slug, run_instance_dir=run_dir)
    lock = lock_proof(run_root, run_dir, slug)
    state = RX.classify_composite(man, liveness)[0]
    decision = decide(man, liveness, lock=lock, config=mgr.config, now=now)
    if not decision.act:
        return SweepOutcome(slug, man.run_id, state, ACTION_SKIPPED, decision.reason)
    try:
        moved = _stamp_under_lock(mgr, slug, run_dir, man.run_id, decision, now)
    except _RESUME_REFUSALS as exc:
        return SweepOutcome(slug, man.run_id, state, ACTION_REFUSED,
                            decision.audit_reason, f"{type(exc).__name__}: {exc}")
    if moved is not None:
        return SweepOutcome(slug, man.run_id, state, ACTION_SKIPPED, moved)
    try:
        detail = launch(mgr, slug, run_dir, man.run_id)
    except _RESUME_REFUSALS as exc:
        return SweepOutcome(slug, man.run_id, state, ACTION_REFUSED,
                            decision.audit_reason, f"{type(exc).__name__}: {exc}")
    return SweepOutcome(slug, man.run_id, state, ACTION_RESUMED,
                        decision.audit_reason, detail)


def sweep_slugs(
    mgr, slugs: list[str], *, now: datetime | None = None,
    launcher: Launcher | None = None,
) -> list[SweepOutcome]:
    """Sweep each slug in order; one line per run, never short-circuits."""
    now = now or datetime.now(timezone.utc)
    return [sweep_run(mgr, s, now=now, launcher=launcher) for s in slugs]


__all__ = [
    "ACTION_REFUSED",
    "ACTION_RESUMED",
    "ACTION_SKIPPED",
    "LOCK_ABSENT",
    "LOCK_DEAD",
    "LOCK_LIVE",
    "LOCK_MALFORMED",
    "REASON_ORPHAN",
    "REASON_SCHEDULE",
    "SweepDecision",
    "SweepOutcome",
    "audit_note",
    "auto_resume_knob",
    "decide",
    "detached_launcher",
    "enumerate_slugs",
    "foreground_launcher",
    "lock_paths",
    "lock_proof",
    "sweep_run",
    "sweep_slugs",
]

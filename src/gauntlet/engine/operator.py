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
from dataclasses import dataclass, field
from pathlib import Path

from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import Manifest, StepRecord
from gauntlet.engine.run import (
    DRIVING_LOCK_NAME,
    UnsafeRunSegment,
    _LockRecord,
    safe_run_segment,
)
from gauntlet.procident import ProcessIdentity, read_process_identity

# --- liveness values (FR-2.4) ------------------------------------------------
LIVENESS_ALIVE = "alive"
LIVENESS_ORPHANED = "orphaned"
LIVENESS_INDETERMINATE = "indeterminate"
LIVENESS_NONE = "none"

# --- composite run-state classes (§6.3, the eleven-class total set) ----------
STATE_IN_PROGRESS = "in_progress"
STATE_ORPHANED = "orphaned"
STATE_INDETERMINATE = "indeterminate"
STATE_PARKED_GATE = "parked_gate"
STATE_PARKED_FOR_RESPONSE = "parked_for_response"
STATE_FAILED = "failed"
STATE_HALTED = "halted"
STATE_INTERRUPTED = "interrupted"
STATE_DONE = "done"
STATE_ABORTED = "aborted"
STATE_UNKNOWN = "unknown"

# Step statuses that mean "a terminal failure of this step" — the failure
# descriptor selection space (§6.3a). Their values double as the failed-class
# composite states (failed→failed, halted→halted, interrupted→interrupted).
_FAILURE_STATUSES = (M.FAILED, M.HALTED, M.INTERRUPTED)

# Park reasons that classify a parked step as `parked_for_response` regardless
# of the parked step's `type` (§6.3a — the *reason* defines the response).
_RESPONSE_REASONS = (
    M.PARKED_REASON_UPSTREAM_CONFLICT,
    M.PARKED_REASON_CYCLE_ESCALATION,
)

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
    STATE_FAILED: "a step failed",
    STATE_HALTED: "the budget/timeout guard tripped",
    STATE_INTERRUPTED: "the run was killed mid-step",
    STATE_DONE: "run complete",
    STATE_ABORTED: "run aborted by an operator",
    STATE_UNKNOWN: "unrecognized or contradictory run state — inspect read-only only",
}


class RunResolutionError(RuntimeError):
    """Run-instance/step selection could not resolve deterministically (FR-3.1a)."""


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

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "kind": self.kind,
            "argv": list(self.argv),
            "required_inputs": list(self.required_inputs),
            "executable": self.executable,
            "command": self.command,
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
def _lock_state(run_root: Path) -> tuple[str, _LockRecord | None]:
    """Read ``<run_root>/.driving.lock`` once → ``(kind, record)``.

    ``kind`` is ``absent`` (no file), ``malformed`` (unreadable or unparseable
    / missing required field — FR-2.4 row g, fail closed), or ``present``.
    """
    path = Path(run_root) / DRIVING_LOCK_NAME
    try:
        text = path.read_text()
    except FileNotFoundError:
        return ("absent", None)
    except OSError:
        return ("malformed", None)
    rec = _LockRecord.from_json(text)
    if rec is None:
        return ("malformed", None)
    return ("present", rec)


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


def driver_info(run_root: Path, slug: str) -> DriverInfo:
    """The full driver-liveness view for ``slug`` (FR-2.4 total table).

    ``pid``/``host``/``since`` are populated from the lock only when a parsed
    record for this slug yields a non-``none`` liveness; they are ``None``
    (the §6.1 nullable contract) for the no-lock, foreign, and malformed cases.
    """
    kind, rec = _lock_state(run_root)
    if kind == "absent":
        return DriverInfo(LIVENESS_NONE, None, None, None)  # row a
    if kind == "malformed":
        return DriverInfo(LIVENESS_INDETERMINATE, None, None, None)  # row g
    assert rec is not None
    if rec.slug != slug:
        return DriverInfo(LIVENESS_NONE, None, None, None)  # row b — foreign lock
    state = _liveness_for_record(rec)
    return DriverInfo(state, rec.pid, rec.host or None, rec.started_at or None)


def driver_liveness(run_root: Path, slug: str) -> str:
    """Just the FR-2.4 liveness value (``alive``/``orphaned``/``indeterminate``/``none``)."""
    return driver_info(run_root, slug).state


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


def _decide_approve(slug: str) -> Action:
    return Action("approve", "decide", ["gauntlet", "approve", slug], [], True,
                  f"gauntlet approve {slug}")


def _decide_reject(slug: str) -> Action:
    # `--notes` is a flag with no value here; the operator supplies the reason,
    # so the action is non-executable and `command` carries a placeholder.
    return Action("reject", "decide", ["gauntlet", "reject", slug, "--notes"],
                  ["notes"], False,
                  f'gauntlet reject {slug} --notes "<your reason>"')


def _decide_resume_response(slug: str) -> Action:
    return Action("resume --response", "decide",
                  ["gauntlet", "resume", slug, "--response"], ["response"], False,
                  f'gauntlet resume {slug} --response "<your decision>"')


def _actions_for(state: str, slug: str) -> list[Action]:
    """The §6.3 next-action column for a composite ``state`` (total)."""
    if state == STATE_IN_PROGRESS:
        return [_observe_logs(slug), _observe_status_json(slug)]
    if state == STATE_ORPHANED:
        return [_control_resume(slug)]
    if state == STATE_PARKED_GATE:
        return [_decide_approve(slug), _decide_reject(slug)]
    if state == STATE_PARKED_FOR_RESPONSE:
        return [_decide_resume_response(slug)]
    if state in (STATE_FAILED, STATE_HALTED, STATE_INTERRUPTED):
        return [_observe_logs(slug), _control_resume(slug)]
    if state in (STATE_DONE, STATE_ABORTED):
        return []
    # indeterminate and unknown: read-only inspection only, never a mutating verb.
    return [_observe_logs(slug), _observe_status_json(slug)]


# --- step / run-instance resolution (FR-3.1a) --------------------------------
def render_step_id(rec: StepRecord) -> str:
    """The rendered step id used everywhere a leaf is named (``id`` / ``id.it``)."""
    return rec.id if rec.iteration is None else f"{rec.id}.{rec.iteration}"


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
            return findings[-1]  # lexicographically-greatest finding-id
    review = step_dir / f"r{rnd}-review"
    if review.is_dir():
        return review
    return step_dir


# --- composite run-state classification (§6.3 + §6.3a, total) ----------------
def _classify(man: Manifest, liveness: str) -> tuple[str, ParkedDescriptor | None, FailureDescriptor | None]:
    """The total ``(run_status, liveness, descriptor) -> state`` function.

    Any unrecognized ``run_status``, or an internally contradictory manifest
    (zero/multiple parked steps, an invalid ``(type, reason)``, a failed run
    with no failure step, or a descriptor present under a ``—`` status), maps
    to ``unknown`` → read-only inspection only (the §6.3 P4 clause).
    """
    status = man.status
    parked_steps = [s for s in man.steps if s.status == M.PARKED]
    failure_steps = [s for s in man.steps if s.status in _FAILURE_STATUSES]

    # P3: only `running` is untrustworthy from the manifest, so liveness governs.
    if status == M.RUN_RUNNING:
        if parked_steps or failure_steps:
            return STATE_UNKNOWN, None, None  # descriptor under a `—` status
        if liveness == LIVENESS_ALIVE:
            return STATE_IN_PROGRESS, None, None
        if liveness in (LIVENESS_ORPHANED, LIVENESS_NONE):
            return STATE_ORPHANED, None, None
        return STATE_INDETERMINATE, None, None  # indeterminate → read-only

    # P2: done/aborted are engine-written and authoritative; a parked/failure
    # descriptor under them is contradictory.
    if status in (M.RUN_DONE, M.RUN_ABORTED):
        if parked_steps or failure_steps:
            return STATE_UNKNOWN, None, None
        return (STATE_DONE if status == M.RUN_DONE else STATE_ABORTED), None, None

    # P2: parked — a genuine human/response park OR a budget/timeout halt / a
    # mid-step interruption. The engine records the latter by parking the *run*
    # (RUN_PARKED) while the *step* keeps its HALTED/INTERRUPTED status
    # (orchestrator._set_run_status, FR-3.3), so a real halted/interrupted run is
    # RUN_PARKED with a single halt/interrupt step and no PARKED step. Classify
    # by which the unique non-terminal step is; a mix, or zero/multiple of
    # either, is a contradiction → unknown.
    if status == M.RUN_PARKED:
        halt_steps = [s for s in man.steps if s.status in (M.HALTED, M.INTERRUPTED)]
        if len(halt_steps) == 1 and not parked_steps:
            hs = halt_steps[0]
            return hs.status, None, FailureDescriptor(render_step_id(hs), hs.status)
        if len(parked_steps) != 1 or halt_steps:
            return STATE_UNKNOWN, None, None  # zero/multiple/mixed → contradiction
        ps = parked_steps[0]
        if ps.parked_reason in _RESPONSE_REASONS:
            return (
                STATE_PARKED_FOR_RESPONSE,
                ParkedDescriptor(render_step_id(ps), ps.type, ps.parked_reason),
                None,
            )
        if ps.parked_reason is None and ps.type == "human_gate":
            return (
                STATE_PARKED_GATE,
                ParkedDescriptor(render_step_id(ps), ps.type, None),
                None,
            )
        # A non-gate step parked with no reason, or an unknown reason value, has
        # no defined operator response → contradiction.
        return STATE_UNKNOWN, None, None

    # P2: failed — the last failure step in manifest order is authoritative (§6.3a).
    if status == M.RUN_FAILED:
        if not failure_steps:
            return STATE_UNKNOWN, None, None  # failed run with no failure step
        fs = failure_steps[-1]
        return fs.status, None, FailureDescriptor(render_step_id(fs), fs.status)

    # P4: any unrecognized run_status.
    return STATE_UNKNOWN, None, None


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


def compute_run_state(man: Manifest, liveness: str) -> RunState:
    """The single computed composite state both the footer and ``--json`` render."""
    state, parked, failure = _classify(man, liveness)
    return RunState(
        state=state,
        slug=man.slug,
        current_step=_current_step(man, state, parked, failure),
        parked=parked,
        failure=failure,
        next_actions=_actions_for(state, man.slug),
    )


def next_actions(man: Manifest, liveness: str) -> list[Action]:
    """The structured next action(s) for a manifest + liveness (FR-1.2/FR-4)."""
    return compute_run_state(man, liveness).next_actions


def composite_state(man: Manifest, liveness: str) -> str:
    """Just the composite ``state`` class for a manifest + liveness (§6.3)."""
    return compute_run_state(man, liveness).state


# --- read-only recovery-intent parser (FR-5.6 report half) -------------------
_RECOVERY_INTENT_NAME = ".recovery-intent.json"


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
    kind, rec = _lock_state(run_root)
    if kind == "absent":
        nonce_matches = True  # finalize branch — verified target already gone
    elif kind == "present" and rec is not None:
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


# --- read-only evidence access (`gauntlet logs`, FR-3) -----------------------
TRANSCRIPT_TAIL_LINES = 200  # FR-3.1b — normative default tail for v1
_TRANSCRIPT_NAME = "transcript.md"
_EVENTS_NAME = "events.jsonl"


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


# --- human footer rendering (FR-1.1/FR-1.2) ----------------------------------
def render_footer(
    driver: DriverInfo,
    rstate: RunState,
    *,
    reconciliation: Reconciliation | None = None,
    anomaly: str | None = None,
) -> list[str]:
    """The status footer lines: driver-liveness line + next-action block.

    Each action renders as ``  $ <command>`` so the footer's commands are
    exactly the ``command`` fields of ``rstate.next_actions`` (FR-1.2 lockstep).
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

    lines.append(f"state: {rstate.state} — {_MEANING.get(rstate.state, '')}")

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

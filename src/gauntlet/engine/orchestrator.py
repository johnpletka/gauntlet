"""The pipeline orchestrator: a resumable state machine (FR-8, FR-10, F-003).

Determinism over cleverness (plan §2): execution is an explicit walk over
stages → steps with a write-ahead manifest. Every step is bracketed by a
manifest write *before* (status ``running`` + base SHA for worktree steps) and
*after* (terminal status + usage). A ``kill -9`` at any instant therefore
leaves a consistent manifest, and :meth:`resume` re-enters at the first
non-``done`` step. Worktree-mutating agent steps killed mid-edit are detected
via the base-SHA transaction boundary and parked (or reset), never re-run over.

Control flow lives here, not in handlers: ``when`` skipping, ``foreach``
fan-out, ``on_fail`` routing with bounded retries, ``human_gate`` parking,
and per-step budget halts (FR-3.3).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from gauntlet.adapters.base import AgentFailedError, AgentTimeoutError
from gauntlet.engine import gitops, ledger as L, manifest as M
from gauntlet.engine.config import RESUME_ON_QUOTA_AUTO, RunConfig
from gauntlet.engine.execution import (
    DONE,
    FAILED,
    HALTED,
    INTERRUPTED,
    PARKED,
    SKIPPED,
    StepContext,
    StepResult,
    engine_bookkeeping_candidates,
    get_spec,
    human_owned_excludes,
    run_bookkeeping_excludes,
    run_bookkeeping_paths,
)
from gauntlet.engine.expr import eval_when, resolve_list
from gauntlet.engine.manifest import Manifest, StepRecord
from gauntlet.engine.planphases import PlanPhasesError
from gauntlet.engine.pipeline import (
    Pipeline,
    Stage,
    Step,
    upstream_cycle_for_gate,
)
from gauntlet.logging.redact import RedactingWriter
from gauntlet.logging.transcript import write_run_index


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Author/committer for orchestrator-owned manifest-checkpoint commits (FR-2.2).
# A response's *operator* identity (FR-9) is recorded IN the manifest entry's
# ``user`` field — it is deliberately NOT the commit author, which is the fixed
# engine identity so bookkeeping commits are attributable to the engine, never
# mislabelled as a human's work. Canonically defined in gitops (the cycle's
# fix-rerun rewind labels its bookkeeping commit with the same identity);
# re-exported here for existing importers.
ENGINE_IDENTITY = gitops.ENGINE_IDENTITY

# A numeric phase prefix (P1, P2…). Only these carry intra-phase `P<N> wip:`
# checkpoints, so checkpoint discovery/recovery scopes to a matching prefix and
# stays unscoped for non-numeric stage labels (PRD/PLAN/REVIEW).
_NUMERIC_PHASE_RE = re.compile(r"P\d+")


@dataclass
class ResponseAction:
    """A planned ``gauntlet resume --response`` transition (FR-1/FR-2/FR-7.1).

    Built by :class:`~gauntlet.engine.run.RunManager` after it has run every
    guard and resolved operator identity, so the orchestrator only *applies* an
    already-validated decision:

    - ``append``  — a new response: append a ``pending`` entry + commit, then the
      stage walk re-executes the parked step.
    - ``recover`` — a prior invocation crashed with a still-``pending`` entry:
      reuse it (its ``pending`` checkpoint is flushed before re-launch); never
      re-append.
    - ``none``    — a plain resume with no response handling.
    """

    kind: str  # "append" | "recover" | "none"
    step_id: str | None = None
    iteration: str | None = None
    text: str | None = None
    user: str | None = None


class _LazyPlanPhases:
    """Defer parsing plan.md's phase list until ``plan.phases`` is read.

    ``foreach: plan.phases`` is the only expression that reads the phase list,
    and only the phases stage — which runs *after* the plan gate — does. Parsing
    eagerly in every ``_context()`` (built once per step) meant a malformed
    ``gauntlet-phases`` block crashed the context for *unrelated* steps: it
    pre-empted the deterministic ``plan-lint`` gate and broke the FR-10.2
    contract that the foreach "only resolves after the plan gate". Deferring the
    parse to attribute access restores both — the lint handler is the one place
    that reports a structural defect, and the foreach is the fail-closed backstop
    (its raise is caught in ``drive()``).
    """

    __slots__ = ("_plan_path",)

    def __init__(self, plan_path: Path) -> None:
        self._plan_path = plan_path

    @property
    def phases(self) -> list[Any]:
        from gauntlet.engine.planphases import load_plan_phases

        phases = load_plan_phases(self._plan_path)
        if phases is None:
            # No plan / no phase block yet: surface as a missing attribute so
            # `foreach` resolution fails closed with its own "does not resolve"
            # error rather than fanning out over a guess. A *malformed* block
            # raises PlanPhasesError here instead, which propagates to drive().
            raise AttributeError("phases")
        return phases


class Orchestrator:
    def __init__(
        self,
        *,
        repo_root: Path,
        run_dir: Path,
        artifact_root: Path,
        config: RunConfig,
        pipeline: Pipeline,
        manifest: Manifest,
        writer: RedactingWriter | None = None,
        judge_env: dict[str, str] | None = None,
        adapter_factory: Callable[[str], Any] | None = None,
        extra_context: dict[str, Any] | None = None,
        clock: Callable[[], str] = _utcnow,
        response_action: "ResponseAction | None" = None,
        ledger_path: Path | None = None,
        interrupted_override: str | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.artifact_root = artifact_root
        self.config = config
        self.pipeline = pipeline
        self.manifest = manifest
        self.writer = writer or RedactingWriter()
        self.judge_env = judge_env or {}
        self.adapter_factory = adapter_factory
        self.extra_context = extra_context or {}
        self.clock = clock
        self.response_action = response_action
        # One-shot `resume --reset-interrupted` override (#72): forces the
        # reset_to_base disposition for THIS drive only, giving the
        # interrupted-park state a sanctioned, non-destructive operator verb
        # (backup ref + checkpoint-preserving rewind) instead of git surgery.
        # Never persisted — the configured `interrupted_step` policy resumes
        # effect on the next drive.
        self.interrupted_override = interrupted_override
        # Machine-global usage ledger (FR-10.1): resolved once (honors
        # GAUNTLET_LEDGER_PATH) so the append/admission paths agree on one file.
        self.ledger_path = ledger_path or L.default_ledger_path()
        self._repo_hash = L.repo_root_hash(repo_root)
        self.manifest_path = run_dir / "manifest.json"
        self.artifacts: dict[str, Path] = {}
        # Narrow exclusion: only the engine's own bookkeeping is hidden from
        # dirty checks / commits — real run artifacts stay visible (review F-001).
        self.excludes = run_bookkeeping_excludes(repo_root, run_dir, artifact_root)
        self._ignore_run_dir()
        self._seed_artifacts()

    def _ignore_run_dir(self) -> None:
        """Keep the engine's own live run-instance dir out of the worktree state.

        The manifest/transcripts are written *into* the repo (FR-4.1) and would
        otherwise dirty the worktree continuously — destroying the clean-handoff
        invariant (CLAUDE.md §1) and the base-SHA transaction boundary (F-003).
        A self-ignoring ``.gitignore`` (``*``) makes the live run dir invisible
        to ``git status`` / ``git add -A``; finalized, tracked artifacts are the
        P4 logger's concern (FR-4.5). prd.md/plan.md live in the parent slug dir
        and stay tracked.
        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.run_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")

    # ---- public entry points -------------------------------------------------
    def drive(self) -> str:
        """Run pending steps until completion, a gate, a halt, or a failure.

        Idempotent and resumable: steps already ``done``/``skipped`` in the
        manifest are not re-run. Returns the resulting run status.
        """
        self.manifest.status = M.RUN_RUNNING
        self._persist()
        # Apply any planned `--response` transition BEFORE the stage walk and
        # OUTSIDE the PlanPhasesError guard below: it touches only manifest
        # bookkeeping / checkpoint commits (never plan.md parsing), so it cannot
        # raise PlanPhasesError, and the parked step it re-arms must be in place
        # before the walk re-executes it.
        self._apply_response_action()
        try:
            for stage in self.pipeline.stages:
                status = self._run_stage(stage)
                if status != DONE:
                    return self._set_run_status(status)
            return self._set_run_status(DONE)
        except PlanPhasesError as exc:
            # A malformed `gauntlet-phases` block in plan.md is a builder-authored
            # artifact defect, not an orchestrator fault — and `_plan_context`
            # parses it while building the context for *any* step (a gate, a
            # foreach), so an uncaught raise here kills `drive()` mid-walk and
            # leaves the write-ahead RUN_RUNNING status persisted: `gauntlet
            # status` then reads as a live run that is actually dead. Fail closed
            # (CLAUDE.md §2): record the precise parse error and park for a human
            # via HALTED -> RUN_PARKED, the same terminal-park path a budget/
            # timeout halt takes. The human fixes plan.md and `gauntlet resume`
            # re-drives — reaching the plan gate and parking normally.
            self.manifest.warnings.append(f"plan.md gauntlet-phases unparseable: {exc}")
            return self._set_run_status(HALTED)

    def approve_gate(self, step_id: str, notes: str | None = None) -> str:
        rec = self._find_parked_gate(step_id)
        rec.status = M.DONE
        rec.ended = self.clock()
        # Current-state invariant (FR-2.1, F-001): a direct terminal transition
        # clears parked_reason so a finished step never carries a stale conflict
        # discriminator. (A human_gate park never sets it, but be explicit.)
        rec.parked_reason = None
        rec.notes = f"approved: {notes}" if notes else "approved"
        self._persist()
        return self.drive()

    def reject_gate(self, step_id: str, notes: str, user: str = "operator") -> str:
        """Reject a parked gate (FR-8.1).

        A rejection is actionable feedback, not a dead end: when the gate sits
        downstream of an ``adversarial_cycle`` (the PRD/plan review loops), inject
        the rejection note into that cycle as a new round and re-drive — the same
        way ``resume --response`` re-drives a parked cycle escalation. This matches
        the operator playbook ("reject with a reason the builder can act on") and
        removes the trap where a rejected gate terminally failed the run while
        ``status`` still recommended a `resume` that no-op'd.

        Falls back to a terminal reject (the prior behavior) only when there is no
        upstream cycle to iterate — a gate not preceded by an ``adversarial_cycle``
        in its stage, or one inside a ``foreach`` fan-out (iteration re-arming is
        out of scope) — so the verb is never silently a no-op.
        """
        rec = self._find_parked_gate(step_id)
        cycle_step, stage = self._upstream_cycle_for_gate(step_id)
        cyc_rec = (
            self.manifest.record(cycle_step.id, None)
            if cycle_step is not None else None
        )
        # Iterate only when the upstream cycle actually ran to DONE. A missing
        # record (defensive) or a non-DONE one — e.g. a `when:`-skipped cycle, the
        # only path that leaves a SKIPPED record before the gate — has nothing to
        # re-arm: re-driving would just re-skip it and orphan the injected note.
        # Fall back to a terminal reject with a clear reason (never a crash, never
        # a silent no-op).
        if cyc_rec is None or cyc_rec.status != M.DONE:
            rec.status = M.FAILED
            rec.ended = self.clock()
            rec.parked_reason = None  # current-state invariant (FR-2.1, F-001)
            # FR-7.2: a terminal reject has no re-runnable path (no upstream cycle
            # to iterate) — the precondition to proceed past the gate is unmet.
            rec.halt_reason = M.HALT_REASON_PRECONDITION
            why = (
                "no upstream adversarial_cycle to iterate"
                if cycle_step is None
                else f"upstream cycle {cycle_step.id!r} is not in a re-runnable "
                f"state ({cyc_rec.status if cyc_rec else 'no record'})"
            )
            rec.notes = f"rejected ({why}): {notes}"
            self._persist()
            return self._set_run_status(FAILED)
        # Iterate: append the rejection note to the upstream cycle as a pending
        # `--response` (audited + checkpoint-committed like any decision), then
        # reset the cycle and everything after it in the stage — including this
        # gate — so the stage walk re-runs the cycle (consuming the note as
        # authoritative round guidance) and re-parks the gate for a fresh decision.
        self._append_response(cyc_rec, notes, user)
        self._reset_for_retry(stage, cycle_step.id, None)
        return self.drive()

    def _upstream_cycle_for_gate(self, gate_id: str):
        """The ``adversarial_cycle`` step a gate ratifies, and its stage.

        Delegates to the shared
        :func:`~gauntlet.engine.pipeline.upstream_cycle_for_gate` so the cycle the
        reject path re-drives is resolved by the *same* rule the status/web
        surfaces name in the reject consequence and gate context (F-001) — one
        definition, no drift.
        """
        return upstream_cycle_for_gate(self.pipeline, gate_id)

    # ---- stage / step walk ---------------------------------------------------
    def _run_stage(self, stage: Stage) -> str:
        if not eval_when(stage.when, self._context()):
            for step in stage.steps:
                self._mark_skipped(step.id, None)
            self._persist()
            return DONE
        if stage.foreach is None:
            return self._run_steps(stage, iteration=None, item=None)
        items = resolve_list(stage.foreach, self._context())
        for idx, item in enumerate(items):
            status = self._run_steps(stage, iteration=str(idx), item=item)
            if status != DONE:
                return status
        return DONE

    def _run_steps(self, stage: Stage, *, iteration: str | None, item: Any) -> str:
        index = {step.id: i for i, step in enumerate(stage.steps)}
        ptr = 0
        while ptr < len(stage.steps):
            step = stage.steps[ptr]
            rec = self.manifest.record(step.id, iteration)
            if rec is not None and rec.status in (M.DONE, M.SKIPPED):
                ptr += 1
                continue
            # A terminal FAILURE is reconciled bookkeeping-only, never re-executed
            # (FR-7.1/FR-9.3, review F-002): re-running it would re-invoke the
            # adapter, double-count the failure, and — for a step whose terminal
            # `failed` carries an `ended` timestamp — rewrite that state back to
            # `running` on every later resume, breaking the clean-committed-worktree
            # handoff invariant. Surface the failure instead so control passes to a
            # human. The companion predicate excludes failures that a pending
            # --response or an on_fail retry budget legitimately re-arms.
            if self._is_terminal_failure(step, rec):
                return FAILED
            if not eval_when(step.when, self._context(item, iteration)):
                self._mark_skipped(step.id, iteration)
                self._persist()
                ptr += 1
                continue
            if step.foreach is not None and iteration is None:
                status = self._run_step_foreach(step)
            else:
                result = self._execute(step, iteration, item)
                status = result.status
            if status == DONE:
                ptr += 1
                continue
            if status == FAILED and step.on_fail is not None:
                # FR-6 / review F-003: route on the PERSISTED failure counter
                # (`StepRecord.attempts`, advanced in `_finalize` on this failure),
                # NOT an invocation-local tally. That makes the retry budget
                # survive crashes and span separate resume invocations — a budget
                # that reset each process restart could retry unboundedly. Exhaust
                # when attempts exceeds max_retries (the N+1-th genuine failure).
                rec = self.manifest.record(step.id, iteration)
                if rec is not None and rec.attempts <= step.on_fail.max_retries:
                    self._reset_for_retry(stage, step.on_fail.route_to, iteration)
                    ptr = index[step.on_fail.route_to]
                    continue
            return status  # FAILED, PARKED, HALTED, or INTERRUPTED
        return DONE

    @staticmethod
    def _is_terminal_failure(step: Step, rec: StepRecord | None) -> bool:
        """True for a FAILED record that must surface, never re-execute on resume.

        A FAILED step is re-runnable on a response-less resume ONLY when something
        legitimately re-arms it; absent that, the failure is terminal and a resume
        must reconcile bookkeeping only — re-executing it would re-invoke the
        adapter, double-count the failure (FR-6), and rewrite the terminal
        ``failed`` state (with its stale ``ended`` timestamp) back to ``running``,
        violating the clean-committed-worktree handoff (FR-9.3, review F-002).

        Re-arming cases (NOT terminal):

        - The failure carries a re-runnable ``failure_kind`` (a PRECONDITION
          guard such as FR-9.3's round-1 clean-handoff): nothing ran, so once the
          operator fixes the precondition a plain resume re-runs the guard.
        - The latest ``--response`` is still ``pending``: a human injected a
          decision the resume re-runs the step to consume (FR-7.1).
        - The step carries an ``on_fail`` retry policy: its budget governs
          re-entry, so a response-less resume re-runs it for the retry/reroute
          (asserted by ``test_retry_budget_does_not_reset_across_resume``).

        Terminal cases (surface FAILED):

        - The latest ``--response`` is already ``consumed``: a prior invocation
          launched the human's decision, it failed, and finalize counted the one
          failure (the original FR-7.1 / F-002 guard).
        - No ``--response`` history AND no ``on_fail`` policy: the step has no
          re-entry path (e.g. an ``adversarial_cycle`` whose fixer made no
          changes), so re-running only repeats the same failure.
        """
        if rec is None or rec.status != M.FAILED:
            return False
        # A re-runnable PRECONDITION failure (FR-9.3 round-1 clean-handoff guard)
        # is NOT terminal: it fired before any adapter call (no cost), so once the
        # operator fixes the precondition a plain `resume` re-runs the guard. The
        # rationale that bars re-executing terminal failures (re-invoking the
        # adapter, double-counting the failure) does not apply — nothing ran.
        if rec.failure_kind in M.RERUNNABLE_FAILURE_KINDS:
            return False
        if rec.human_responses:
            # Pending → a human decision re-arms it; consumed → terminal.
            return rec.human_responses[-1].state != M.RESPONSE_PENDING
        # No human decision: an on_fail step is re-entered under its retry budget;
        # a step without one has nowhere to go but a repeated failure.
        return step.on_fail is None

    def _run_step_foreach(self, step: Step) -> str:
        items = resolve_list(step.foreach, self._context())
        for idx, item in enumerate(items):
            rec = self.manifest.record(step.id, str(idx))
            if rec is not None and rec.status in (M.DONE, M.SKIPPED):
                continue  # resume: don't re-run a completed iteration (F-004)
            result = self._execute(step, str(idx), item)
            if result.status != DONE:
                return result.status
        return DONE

    def _reset_for_retry(
        self, stage: Stage, route_to: str, iteration: str | None
    ) -> None:
        """Mark the route target and everything after it pending so they re-run.

        The route target is typically already ``done`` (e.g. routing tests back
        to a completed implement); it must be reset too, otherwise the loop skips
        it and only the failing step re-runs forever.
        """
        ids = [s.id for s in stage.steps]
        start = ids.index(route_to)
        for sid in ids[start:]:
            rec = self.manifest.record(sid, iteration)
            if rec is not None:
                rec.status = M.PENDING
                rec.ended = None
                # The transaction boundary belongs to a step ATTEMPT, not to the
                # record's first run ever (#65): the retried attempt must stamp
                # base_sha at its own entry HEAD. Keeping the stale value makes
                # a later interrupt's dirty check diff against — and a
                # reset_to_base rewind past — everything the run landed since.
                rec.base_sha = None

    # ---- single-step execution ----------------------------------------------
    def _execute(self, step: Step, iteration: str | None, item: Any) -> StepResult:
        spec = get_spec(step.type)
        rec = self.manifest.record(step.id, iteration)
        resuming = rec is not None and rec.status in (M.RUNNING, M.INTERRUPTED)
        if rec is None:
            rec = StepRecord(
                id=step.id, type=step.type, agent=step.agent, iteration=iteration
            )
            self.manifest.upsert(rec)

        if resuming:
            short = self._resume_disposition(step, spec, rec, item)
            if short is not None:
                self._finalize(rec, short)
                return short

        # NOTE: `rec.attempts` is NO LONGER incremented here. FR-6 redefines it
        # as the failure-retry counter: it advances exactly once, in `_finalize`,
        # only on a FAILED outcome — never on success, conflict park, halt,
        # interruption, or a `--response` continuation. Relocating it there is
        # what keeps conflict/response resumes from consuming the retry budget.
        # Re-arm a re-runnable PRECONDITION failure (FR-9.3 clean-handoff): the
        # operator fixed the precondition (e.g. committed the dirty tree) before
        # this resume, so HEAD has advanced. The prior FAILED attempt recorded a
        # base_sha pointing BEFORE that cleanup commit; reusing it would make a
        # later interrupt's transaction-boundary check (`is_dirty_vs(base_sha)` /
        # rewind) diff against — or rewind past — the operator's cleanup. Clear it
        # so this fresh attempt re-stamps the boundary at the current HEAD below,
        # and drop the now-superseded failure_kind (re-set by `_finalize`).
        if rec.status == M.FAILED and rec.failure_kind in M.RERUNNABLE_FAILURE_KINDS:
            rec.base_sha = None
            rec.failure_kind = None
        rec.status = M.RUNNING
        rec.started = rec.started or self.clock()
        self.manifest.current_step = step.id
        if spec.step_touches_worktree(step) and rec.base_sha is None:
            rec.base_sha = self._head_sha()
        # Per-step agent attribution: EVERY in-run child agent must see
        # GAUNTLET_STEP_ID (FR-5.5) — it is both the per-step marker the judge's
        # `pipeline_step_only` rules key on AND the operator-only boundary that
        # makes an in-pipeline `gauntlet recover` refuse fail-closed. It is
        # therefore exported for every step INDEPENDENT of whether a judge is
        # configured: under `--no-judge` (judge_env empty) the guard must still
        # fire, so that path is not exempt (review F-001). Captured and restored
        # around the step so the marker never leaks past it into the parent
        # session — the judge's env snapshot then sees it already restored.
        prior_step_id = os.environ.get("GAUNTLET_STEP_ID")
        os.environ["GAUNTLET_STEP_ID"] = step.id
        self._persist()  # WRITE-AHEAD: before any side effect

        try:
            ctx = self._make_context(step, rec, iteration, item)
            # FR-10.3: window admission runs BEFORE the handler, so an enforce
            # park lands at a clean boundary with zero work in flight (in
            # contrast to FR-3's reactive mid-step usage_limit park). An advisory
            # short-fall records a manifest warning and returns None (launch as
            # usual); a `sufficient`/unconstrained step returns None too.
            admission = self._window_admission(step, rec)
            if admission is not None:
                result = admission
            else:
                # Evidence-tiered gate (FR-4.1, P8): a `human_gate` configured
                # `policy: auto_when_clean` auto-approves in place of parking when
                # the strict §4.2 clean-signal predicate holds. A miss returns None
                # and falls through to the handler, which parks exactly as today —
                # fail closed. Every non-gate step, and an `always`-policy gate,
                # skip this entirely.
                auto = self._maybe_auto_approve(step, iteration, item)
                try:
                    result = auto if auto is not None else spec.handler(step, ctx)
                except AgentTimeoutError as exc:
                    result = StepResult(
                        status=HALTED,
                        halt_reason=M.HALT_REASON_TIMEOUT,
                        usage=exc.partial.usage if exc.partial else None,
                        session_id=exc.partial.session_id if exc.partial else None,
                        notes=f"timeout halt (FR-3.3/FR-5.2): {exc}",
                    )
                except AgentFailedError as exc:
                    # FR-3.2: a TRANSIENT failure (usage limit / overload) is not
                    # a step failure — park (not FAILED) with
                    # parked_reason=usage_limit, preserving the worktree (no reset
                    # on this park kind) and the CLI session so a plain `gauntlet
                    # resume` continues it (FR-3.3). A TERMINAL or unclassified
                    # failure fails closed → FAILED (a human decides), exactly as
                    # an unclassified handler fault would.
                    result = self._agent_failure_result(exc)
                except Exception as exc:  # fail closed: a handler fault halts it
                    result = StepResult(
                        status=FAILED,
                        halt_reason=M.HALT_REASON_ADAPTER_ERROR,
                        notes=f"handler error: {exc}",
                    )

            result = self._apply_budget_guard(step, rec, result)
            # Clean-handoff invariant (CLAUDE.md §1, review F-001): a conflict park —
            # whether signalled by the textual UPSTREAM CONFLICT marker (first
            # conflict) or a re-park disposition (amendment_required/new_conflict) —
            # hands control to a human, so the worktree MUST be clean. The builder
            # runs with repo-write access; if it wrote implementation edits and THEN
            # signalled a conflict, those edits are still uncommitted. Restore the
            # clean tree (backed up, lossless) BEFORE finalizing the park, so a later
            # `--response` resume never re-runs over — and commits — stale edits.
            result = self._restore_clean_after_conflict_park(step, spec, rec, result)
            consumed = self._finalize(rec, result)
            self._persist()  # WRITE-AHEAD: after the side effect, terminal state
            # The consume flip, the FAILED attempt-increment, and the status all
            # landed in the single `_persist` above — one atomic on-disk transaction
            # (FR-2.2/F-003 dedup boundary). Only AFTER it is durable do we commit
            # the `consumed` checkpoint, so a crash here leaves the entry already
            # `consumed` on disk and recovery merely flushes this commit, never
            # re-executing or double-counting.
            if consumed is not None:
                self._commit_manifest_checkpoint(
                    f"gauntlet: response {consumed.response_id} consumed"
                )
            return result
        finally:
            # Scoped restoration (FR-5.5): never leak this step's id into the next
            # step or the parent session.
            if prior_step_id is None:
                os.environ.pop("GAUNTLET_STEP_ID", None)
            else:
                os.environ["GAUNTLET_STEP_ID"] = prior_step_id

    def _resume_disposition(
        self, step: Step, spec, rec: StepRecord, item: Any = None
    ) -> StepResult | None:
        """Decide how to re-enter a step that was interrupted (review F-003).

        Returns a terminal StepResult to short-circuit (park), or ``None`` to
        proceed with a normal (re-)run. Only repo-writing agent steps that left
        a dirty worktree are parked/reset; idempotent steps (shell) and the
        commit step (which reconciles from git log) simply re-run.
        """
        if rec.base_sha is None or not spec.step_touches_worktree(step):
            return None
        # F-005: an artifact_invalid park interrupted mid-revalidation must
        # re-enter the validator-only path, NOT the generic dirty-mid-edit
        # recovery. A plain resume of such a park sets the record RUNNING and
        # write-ahead-persists BEFORE `_revalidate_on_resume` runs; a crash in
        # that gap leaves a RUNNING record that still carries
        # `parked_reason=artifact_invalid`. The on-disk artifact is INTENTIONALLY
        # dirty vs base_sha (the sanctioned hand-edit awaiting revalidation), so
        # the dirty-base check below would wrongly park it INTERRUPTED / rewind
        # the hand-edit. `handle_agent_task` re-runs only the validator against
        # the current bytes (no adapter call), which is idempotent and safe to
        # re-enter — proceed.
        if rec.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID:
            return None
        # F-002: an adversarial_cycle that recorded sub-step checkpoints owns its
        # OWN checkpoint-aware recovery (cycle.py `_Resume` + the SHA/worktree
        # guards). A kill after the fix sub-step commits+checkpoints but before
        # finalization leaves HEAD ahead of base_sha with the commit still absent
        # from the manifest; the generic dirty-base recovery below would then park
        # INTERRUPTED (or, under reset_to_base, rewind past — and orphan — that fix
        # commit) instead of letting the cycle adopt its fix checkpoint. Defer to
        # the handler: it reuses the completed prefix, re-records the fix commit,
        # and resets a genuinely dirty mid-fixer-edit tree from the round handoff.
        if step.type == "adversarial_cycle" and rec.checkpoints:
            return None
        # agent_task killed mid-edit AND a checkpoint-less adversarial_cycle killed
        # mid-round are both non-idempotent worktree writers: park (or reset) on a
        # dirty base rather than re-running over partial fixer edits / unmanifested
        # fix-round commits. shell and commit re-enter safely on their own.
        is_agent_write = (
            step.type == "agent_task" and spec.step_requires_repo_write(step)
        ) or step.type == "adversarial_cycle"
        if not is_agent_write:
            return None
        # Detect partial work against the narrow bookkeeping exclusion, so a
        # partial *artifact* under the run root (not just a repo-root file) is
        # still seen as a mid-edit interruption (review F-001).
        if not gitops.is_dirty_vs(
            self.repo_root, rec.base_sha, exclude=self.excludes,
            bookkeeping=engine_bookkeeping_candidates(self.repo_root, self.run_dir),
        ):
            # Clean re-entry: the agent left no partial edits, and any HEAD
            # advance past base_sha is engine bookkeeping only (is_dirty_vs
            # tolerates exactly that). Re-arm the transaction boundary the same
            # way the FAILED+rerunnable path does: clear base_sha so `_execute`
            # re-stamps it at the current HEAD — the boundary tracks THIS
            # attempt, never a prior one (#65). A stale boundary makes a later
            # `interrupted_step=reset_to_base` rewind past work that predates
            # this attempt (e.g. a plan-cycle base stamped before plan.md
            # existed) — destructive by construction.
            rec.base_sha = None
            return None  # agent never progressed; safe to re-run
        if (self.interrupted_override or self.config.interrupted_step) == "reset_to_base":
            ts = self.clock().replace(":", "-")
            backup = f"refs/gauntlet/backup/{self.manifest.run_id}/{rec.id}-{ts}"
            # Human-owned excluded files (PR.md) are hidden from the dirty
            # check by policy, but the reset below is not policy-scoped. Carry
            # their worktree state across the rewind and include it in the
            # backup ref so the preservation is durable (PR #77 review).
            human_patterns = human_owned_excludes(self.excludes)
            overlay = gitops.worktree_overlay(
                self.repo_root, human_patterns
            )
            # Snapshot the partial work (tracked + untracked) before discarding.
            gitops.backup_dirty_worktree(
                self.repo_root, backup, f"interrupted {rec.id} partial work",
                exclude=self.excludes,
                include=human_patterns if overlay else None,
            )
            # Flush the authoritative in-memory manifest to disk BEFORE the
            # rewind, so the bookkeeping the rewind preserves carries the latest
            # response state (e.g. a still-`pending` entry the re-run consumes).
            self._persist()
            # FR-11.2: rewind to the latest intra-phase checkpoint commit that is
            # a descendant of base_sha (a `P<N> wip:` milestone) rather than all
            # the way to base_sha, so completed milestones survive the rewind and
            # the worst-case repeated work is one milestone, not one phase. Falls
            # back to base_sha (today's behavior) when the phase landed none. The
            # subject is recorded so the re-run prompt can name the checkpoint it
            # resumes from.
            target, checkpoint_subject = self._checkpoint_rewind_target(
                rec, self._expected_phase(step, item)
            )
            rec.resumed_from_checkpoint = checkpoint_subject
            # F-001: the rewind target predates the engine bookkeeping commits
            # stacked on top of it — notably the pending-response checkpoint
            # (FR-2.2/FR-7.1). A plain `reset --hard target` would delete the
            # force-committed manifest from disk AND orphan that checkpoint, so a
            # kill in the gap before it was re-persisted would lose the human
            # response. Instead, rewind the implementation to the target in a
            # single reset whose target commit still carries the manifest — the
            # response is never, even for an instant, absent from both disk and
            # reachable history. Label that commit with the canonical
            # response-checkpoint subject so it stands in as the pending
            # checkpoint itself (the post-rewind reconcile below is then a no-op
            # rather than stacking a duplicate).
            paths = self._bookkeeping_paths()
            if paths and gitops.head_sha(self.repo_root) != target:
                message = (
                    self._response_checkpoint_message()
                    or f"gauntlet: rewind implementation to {target[:10]} "
                    f"for re-run ({rec.id})"
                )
                gitops.rewind_impl_preserving_bookkeeping(
                    self.repo_root, target, paths, message,
                    identity=ENGINE_IDENTITY,
                )
            else:
                # Nothing to preserve above the target (HEAD == target, or no
                # bookkeeping on disk): the plain rewind is already crash-safe.
                gitops.reset_hard(self.repo_root, target)
            # `clean` is broader than the dirty check on purpose: it spares the
            # whole run root so the reset never wipes the run pointer, manifests,
            # the authored prd.md, or prior declared artifacts — the re-run
            # regenerates its own outputs over them.
            gitops.clean_untracked(self.repo_root, exclude=[self.config.run_root])
            gitops.restore_overlay(self.repo_root, overlay)
            # The rewind restored the on-disk manifest to base_sha's tree + the
            # overlaid bookkeeping; re-persist the authoritative in-memory state
            # over it and idempotently flush the checkpoint (still `pending` here
            # — the re-run consumes it and lands the `consumed` checkpoint).
            self._persist()
            self._reconcile_response_checkpoint()
            return None  # tree restored to base; checkpoints preserved; re-run cleanly
        return StepResult(
            status=INTERRUPTED,
            # FR-7.2: a dirty worktree vs base means the prior attempt was killed
            # mid-edit by a signal / crash — attribute the interruption to that.
            halt_reason=M.HALT_REASON_SIGNAL_KILL,
            notes=(
                "interrupted mid-edit: worktree dirty vs base SHA "
                f"{rec.base_sha[:10]}; parked for a human (F-003, "
                "interrupted_step=park). To discard the interrupted attempt "
                "and re-run cleanly (partial work is backed up to "
                "refs/gauntlet/backup/ first, and committed checkpoints are "
                f"preserved): `gauntlet resume {self.manifest.slug} "
                "--reset-interrupted`."
                + self._dirty_verdict_detail(rec.base_sha)
            ),
        )

    def _dirty_verdict_detail(self, base_sha: str) -> str:
        """The evidence behind a dirty-base park (#65): WHAT read as dirty.

        A zero-work insta-park that says only "parked" sends the operator in
        circles; name the uncommitted paths and/or the commits sitting between
        the recorded base and HEAD so the verdict is inspectable from the park
        message alone. Best-effort — never let evidence-gathering mask the park.
        """
        parts: list[str] = []
        try:
            porcelain = gitops.status_porcelain(self.repo_root, exclude=self.excludes)
            if porcelain:
                parts.append(f"uncommitted changes:\n{porcelain}")
            head = gitops.head_sha(self.repo_root)
            if head != base_sha:
                rng = gitops.log_range(self.repo_root, base_sha, head)
                parts.append(
                    f"commits in {base_sha[:10]}..{head[:10]}:\n"
                    + (rng or "(none — HEAD is behind or forked from the base)")
                )
        except gitops.GitError as exc:
            parts.append(f"(dirty-verdict detail unavailable: {exc})")
        if not parts:
            return ""
        return " Dirty verdict — " + "\n".join(parts)

    def _restore_clean_after_conflict_park(
        self, step: Step, spec, rec: StepRecord, result: StepResult
    ) -> StepResult:
        """Restore the clean-worktree invariant on a conflict park (review F-001).

        A builder conflict park (an ``agent_task`` parked ``response``) returns
        control to a human, so the worktree must be clean (CLAUDE.md §1). But the builder
        ran with repo-write access and may have written implementation edits
        before deciding to signal the conflict; those edits are uncommitted (the
        checkpoint commits stage ONLY bookkeeping, never the implementation
        diff). Left in place they would be re-run over — and silently committed —
        by the next ``--response`` resume, which re-enters the PARKED step with
        ``resuming=False`` and so bypasses the dirty-base recovery in
        ``_resume_disposition``.

        Unlike a mid-edit *kill* (whose salvageability is ambiguous and governed
        by ``interrupted_step``), a deliberate conflict park is an unambiguous "I
        am not implementing" signal — partial edits are definitionally unwanted,
        so we always restore clean regardless of policy. Nothing is lost: the
        dirty tree is snapshotted to a backup ref first. We reset to HEAD (NOT
        ``base_sha``): HEAD already carries the engine bookkeeping checkpoints
        (e.g. the pending-response commit), and its implementation tree equals
        ``base_sha``'s — no phase/impl commit lands on a park — so resetting to it
        discards only the builder's uncommitted edits while preserving every
        checkpoint (the rewind-past-checkpoint hazard ``_resume_disposition``
        guards against does not arise here).
        """
        # A `response` park on an ``agent_task`` is the builder UPSTREAM CONFLICT;
        # the SAME `response` value on an ``adversarial_cycle`` is a cycle
        # escalation whose commits the cycle already manages — the step type
        # disambiguates the two now that both collapse to ``response`` (FR-7.2).
        # Restore clean ONLY for the builder-conflict case.
        if (
            result.status != PARKED
            or result.parked_reason != M.PARKED_REASON_RESPONSE
            or step.type != "agent_task"
            or rec.base_sha is None
            or not spec.step_touches_worktree(step)
        ):
            return result
        # Scope the dirty check (and the discard) to OUTSIDE the run root: the
        # builder's implementation edits land in the source tree, while prd.md,
        # plan.md, declared outputs, and engine bookkeeping all live under
        # `run_root`. Excluding the whole run root therefore isolates exactly the
        # implementation leakage F-001 is about, and avoids touching real run
        # artifacts (the same exclusion the `clean` below and `_resume_disposition`
        # use). A clean result here means the builder honored the
        # classify-don't-implement contract — nothing to restore.
        run_root = [self.config.run_root]
        if gitops.is_clean(self.repo_root, exclude=run_root):
            return result
        ts = self.clock().replace(":", "-")
        backup = f"refs/gauntlet/backup/{self.manifest.run_id}/{rec.id}-conflict-{ts}"
        # PR #77 review: carry human-owned PR.md state across the reset and
        # include it in the backup ref so preservation survives a process kill.
        human_patterns = human_owned_excludes(self.excludes)
        overlay = gitops.worktree_overlay(
            self.repo_root, human_patterns
        )
        gitops.backup_dirty_worktree(
            self.repo_root,
            backup,
            f"conflict-park partial work for {rec.id} (F-001)",
            exclude=run_root,
            include=human_patterns if overlay else None,
        )
        gitops.reset_hard(self.repo_root, self._head_sha())
        # `reset --hard` leaves untracked files; clear the builder's untracked
        # edits too, sparing the whole run root so the manifest/transcripts/
        # authored artifacts under it survive.
        gitops.clean_untracked(self.repo_root, exclude=run_root)
        gitops.restore_overlay(self.repo_root, overlay)
        note = (
            f"conflict park left an uncommitted worktree; backed up to {backup} "
            "and restored the clean tree before handoff (F-001)"
        )
        result.notes = f"{result.notes}\n{note}" if result.notes else note
        return result

    def _now_dt(self) -> datetime:
        """The orchestrator clock as an aware UTC datetime (for window math).

        Parses the injectable ISO clock so admission is deterministic under a
        stubbed clock in tests; an unparseable clock falls back to wall-clock UTC.
        """
        try:
            dt = datetime.fromisoformat(self.clock())
        except (ValueError, TypeError):
            return datetime.now(timezone.utc)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    # The cycle roles that each launch agent calls billing their own provider
    # (FR-10.2). A standard ``adversarial_cycle`` carries these instead of a
    # single ``agent:``, so admission must check each declared role — not bail
    # because ``step.agent`` is unset (F-002). ``confirmer`` defaults to the
    # reviewer profile (already covered by ``reviewer``); listing it still
    # correctly checks a distinct confirmer profile when one is configured.
    _CYCLE_ROLES = ("reviewer", "triager", "fixer", "confirmer", "escalation_agent")

    def _admission_profiles(self, step: Step) -> dict[str, int]:
        """The agent profiles whose provider window this step must clear, mapped to
        the admission SCALE FACTOR for each (FR-10.2; pipeline-effectiveness FR-1.1).

        An ``agent_task`` bills one profile (``step.agent``) once; an
        ``adversarial_cycle`` bills one PER declared role — each can drive a
        constrained provider into a reactive usage-limit park, so each is
        admission-checked before the cycle launches (F-002). The reviewer role
        expands to the review panel (``reviewers:``): a member profile used by N
        lenses is counted N times, so window admission scales its estimate by panel
        size rather than a single-reviewer one (plan-cycle-resp-2a). Other roles
        contribute their profile once, de-duplicated with the panel (first-seen),
        so a single-reviewer cycle is exactly today's one-per-profile check.
        """
        from gauntlet.engine.cycle import _panel

        counts: dict[str, int] = {}
        if step.type == "agent_task":
            if step.agent:
                counts[step.agent] = 1
            return counts
        if step.type == "adversarial_cycle":
            for member in _panel(step):  # reviewer / panel members, N per profile
                if member.profile:
                    counts[member.profile] = counts.get(member.profile, 0) + 1
            for role in ("triager", "fixer", "confirmer", "escalation_agent"):
                profile = step.get(role)
                if profile and profile not in counts:  # dedup vs the panel
                    counts[profile] = 1
            return counts
        return {}

    def _window_admission(self, step: Step, rec: StepRecord) -> StepResult | None:
        """Pre-step provider-window admission (FR-10.2/10.3).

        For each agent profile the step bills (one for an ``agent_task``; one per
        declared role for an ``adversarial_cycle``, F-002), estimates the step's
        provider usage and compares it to remaining window headroom. Returns a
        PARKED ``usage_window`` StepResult when a window-constrained,
        ``enforce: true`` profile lacks headroom — a clean-boundary park before any
        adapter call. In advisory mode (default) the same short-fall records a
        manifest warning — also surfaced through ``gauntlet status`` and a
        notification (FR-10.3) — and admission keeps scanning the other profiles
        (launch as usual). Returns ``None`` for a step with no window-constrained
        profile, sufficient headroom everywhere, or any ledger error (advisory — a
        ledger fault never blocks a run, PRD §4.2).
        """
        profiles = self._admission_profiles(step)
        if not profiles:
            return None
        try:
            rows = L.load_rows(self.ledger_path)
        except Exception:
            return None  # advisory: never block a launch on a ledger error
        now = self._now_dt()
        for profile, count in profiles.items():
            provider = L.profile_provider(self.config, profile)
            if provider is None:
                continue
            window = self.config.providers.get(provider)
            if window is None:
                continue
            try:
                decision = L.admit_step(
                    rows, window, provider=provider, step_type=step.type,
                    profile=profile, now=now, count=count,
                )
            except Exception:
                continue  # advisory: an estimate fault never blocks a launch
            if decision.sufficient:
                continue
            note = f"usage-window admission (FR-10.3): {decision.summary()}"
            if window.enforce:
                return StepResult(
                    status=PARKED,
                    parked_reason=M.PARKED_REASON_USAGE_WINDOW,
                    # Reuse the quota reset field for the projected replenishment
                    # time (the datum status surfaces for a usage_window park,
                    # mirroring a usage_limit park's reset time).
                    quota_reset_at=decision.replenish_at,
                    notes=note,
                )
            # Advisory: record the warning; keep scanning so every constrained
            # profile's short-fall lands, not just the first.
            self._record_window_warning(step, note)
        return None

    def _record_window_warning(self, step: Step, note: str) -> None:
        """Stamp a de-duplicated advisory usage-window warning into the manifest.

        ``manifest.warnings`` is the FR-10.3 advisory channel: rendered by
        ``gauntlet status`` (its ``warnings`` block) and pushed as a
        ``usage-window-warning`` notification by the web notifier, so a live
        short-fall is visible without opening the manifest.
        """
        warning = f"[{step.id}] {note}"
        if warning not in self.manifest.warnings:
            self.manifest.warnings.append(warning)

    def _append_ledger_row(self, rec: StepRecord) -> None:
        """Append this step's content-free usage to the machine-global ledger.

        Best-effort and idempotent (FR-10.1). A single-agent step yields one row;
        a compound step (adversarial_cycle) yields one row PER ROLE from
        ``rec.agent_usage``, each attributed to that role's own provider — so a
        cycle's spend (the bulk of a run's usage) is recorded, not dropped. The
        ``run_id::step_id[::profile]`` de-dup key means a re-finalized (resumed)
        step never double-counts — the first execution's spend wins. Any error is
        swallowed: the ledger is advisory, never a correctness dependency (§4.2).
        """
        try:
            rows = L.rows_from_step(
                rec, run_id=self.manifest.run_id, repo_hash=self._repo_hash,
                config=self.config,
            )
            if rows:
                L.append_unique(rows, path=self.ledger_path)
        except Exception:
            pass

    def _agent_failure_result(self, exc: AgentFailedError) -> StepResult:
        """Turn a classified adapter failure into a park (transient) or FAILED.

        FR-3.2: a ``transient_*`` classification parks the step with
        ``parked_reason=usage_limit``, the failing call's ``session_id`` preserved
        (so a plain ``gauntlet resume`` continues it, FR-3.3) and its usage
        accounted (the failed call still cost tokens). The worktree is left
        untouched — this park kind bypasses the reset/conflict-restore paths (both
        key on other reasons). An absent/terminal ``failure_info`` fails closed to
        FAILED (never auto-continued past an unknown error, §7).
        """
        info = exc.failure_info
        partial = exc.partial
        if info is not None and info.is_transient:
            after = (
                f"; provider retry hint ~{info.retry_after_s}s"
                if info.retry_after_s else ""
            )
            return StepResult(
                status=PARKED,
                parked_reason=M.PARKED_REASON_USAGE_LIMIT,
                session_id=partial.session_id if partial else None,
                usage=partial.usage if partial else None,
                retry_after_s=info.retry_after_s,
                notes=(
                    f"usage-limit park (FR-3.2): {info.kind} [{info.marker}]; "
                    "worktree and CLI session preserved — `gauntlet resume` "
                    "continues the session" + after
                ),
            )
        return StepResult(
            status=FAILED,
            halt_reason=M.HALT_REASON_ADAPTER_ERROR,
            usage=partial.usage if partial else None,
            session_id=partial.session_id if partial else None,
            notes=f"agent failed (terminal, FR-3.1): {exc}",
        )

    def _quota_reset_at(self, retry_after_s: int | None) -> str | None:
        """Absolute UTC reset time = now + ``retry_after_s`` (FR-3.2 "when reported").

        Derived from the orchestrator clock so tests are deterministic; ``None``
        when no structured retry hint was reported, or when the clock string is
        not ISO-parseable (fail-safe — an un-parseable reset is simply omitted).
        ``retry_after_s=0`` is a real hint (RFC 7231: retry immediately), not an
        absent one — it must yield "now", or auto-resume never fires.
        """
        if retry_after_s is None:
            return None
        try:
            base = datetime.fromisoformat(self.clock())
        except ValueError:
            return None
        return (base + timedelta(seconds=retry_after_s)).isoformat()

    def _apply_budget_guard(
        self, step: Step, rec: StepRecord, result: StepResult
    ) -> StepResult:
        if result.status != DONE or result.usage is None:
            return result
        budget = step.budget_usd
        if budget is None and step.agent and step.agent in self.config.agents:
            budget = self.config.profile(step.agent).budget_usd
        if budget is None or result.usage.cost_usd is None:
            return result
        projected = (rec.usage.cost_usd or 0.0) + result.usage.cost_usd
        if projected > budget:
            # The handler may have ALREADY produced side effects before the
            # projection tripped — a commit / adversarial_cycle can have created
            # a commit and per-agent usage at this checkpoint. Discarding those
            # (the prior fresh-StepResult conversion) would record the step as
            # halted with no commit, artifact, or per-agent usage, breaking
            # FR-3.3 checkpointing and FR-9 branch/manifest consistency (F-001).
            # Preserve every field; only the status and notes change.
            halt_note = (
                f"budget halt (FR-3.3): step cost ${projected:.4f} exceeds "
                f"budget ${budget:.4f}; halting at checkpoint"
            )
            result.status = HALTED
            result.halt_reason = M.HALT_REASON_BUDGET  # FR-7.2
            result.notes = (
                f"{result.notes}\n{halt_note}" if result.notes else halt_note
            )
            return result
        return result

    def _finalize(self, rec: StepRecord, result: StepResult) -> "M.HumanResponse | None":
        rec.status = {
            DONE: M.DONE,
            FAILED: M.FAILED,
            PARKED: M.PARKED,
            HALTED: M.HALTED,
            SKIPPED: M.SKIPPED,
            INTERRUPTED: M.INTERRUPTED,
        }[result.status]
        rec.ended = self.clock()
        # Reason discriminators are CURRENT-STATE, not a latch: copy the just-
        # finished execution's values so a step later resumed to a different
        # outcome never carries a stale reason.
        rec.parked_reason = result.parked_reason
        rec.halt_reason = result.halt_reason
        # Failure kind is CURRENT-STATE too (FR-9.3 recovery): copy the just-
        # finished execution's value so a precondition failure is re-runnable on
        # resume, and any later non-precondition finalization clears a stale value.
        rec.failure_kind = result.failure_kind
        # FR-7.2 disjoint-reason invariant, enforced in one place for every
        # terminal/parked outcome: a PARKED step carries exactly a `parked_reason`
        # (halt_reason null); a HALTED/FAILED/INTERRUPTED step carries exactly a
        # `halt_reason` (parked_reason null); a DONE/SKIPPED step carries neither.
        # The applicable field is MANDATORY, so a handler that returned a terminal
        # status without one is defaulted here rather than persisting an
        # unexplainable state (`reason_fields_disjoint` holds afterward).
        if rec.status == M.PARKED:
            rec.halt_reason = None
            # Every PARKED step must carry a PRD reason (FR-7.2 / P3 park
            # invariant): usage_limit / artifact_invalid parks stamp theirs at the
            # handler, a halt_on park stamps `response` (see `_completion_signal`),
            # and a human_gate park stamps `gate`. The human_gate backfill here is
            # belt-and-suspenders (the handler already set it); no park is written
            # with a null parked_reason, which would classify as `unknown`.
            if rec.parked_reason is None and rec.type == "human_gate":
                rec.parked_reason = M.PARKED_REASON_GATE
        elif rec.status in (M.HALTED, M.FAILED, M.INTERRUPTED):
            rec.parked_reason = None
            if rec.halt_reason is None:
                # A precondition guard fired before any adapter call (carries a
                # failure_kind); everything else is a terminal adapter/execution
                # failure.
                rec.halt_reason = (
                    M.HALT_REASON_PRECONDITION if rec.failure_kind
                    else M.HALT_REASON_ADAPTER_ERROR
                )
        else:  # DONE / SKIPPED carry neither reason
            rec.parked_reason = None
            rec.halt_reason = None
        # Usage-limit park stamps are CURRENT-STATE as well (FR-3.2): set on a
        # usage_limit park, cleared (back to None) on any other finalization —
        # so a step later resumed to DONE never carries a stale reset time. The
        # absolute reset time is derived here (from the retry hint + the engine
        # clock) so both the agent_task and cycle park paths get it uniformly;
        # an explicit ``quota_reset_at`` on the result (rare) still wins.
        rec.retry_after_s = result.retry_after_s
        rec.quota_reset_at = result.quota_reset_at or self._quota_reset_at(
            result.retry_after_s
        )
        # The parked sub-step is current-state alongside the usage-limit stamps
        # (FR-3.3): set on a usage-limit cycle park so resume continues the
        # preserved session in the matching sub-step, cleared on any other
        # finalization so a step resumed to DONE never carries a stale value.
        rec.parked_substep = result.parked_substep
        # The revalidation content-hash pair is current-state too (FR-2.2/§6): set
        # on an artifact_invalid park (hash_at_park) and on the resume that
        # revalidates the hand-edited artifact (the full pair + passed_on_resume),
        # cleared on any other finalization so a step later resumed to a clean DONE
        # never carries a stale hand-edit record.
        rec.revalidation = result.revalidation
        # Auto-resume schedule (FR-3.4) is current-state and armed at park time so
        # a usage-limit park under `resume_on_quota: auto` carries its schedule the
        # instant `_drive` returns (before any wait), and a process death loses
        # nothing. Preserve the prior attempts count across re-parks (the
        # RunManager increments it around each in-process resume). Cleared on any
        # non-usage-limit-park finalization so a step resumed to DONE never carries
        # a stale schedule. `notify` mode never arms one.
        if (
            result.status == PARKED
            and result.parked_reason == M.PARKED_REASON_USAGE_LIMIT
            and self.config.resume_on_quota == RESUME_ON_QUOTA_AUTO
            # Arm ONLY with a structured reset time (FR-3.4): a park with no
            # reported reset has ``quota_reset_at is None``; falling back to
            # ``self.clock()`` would make the schedule due immediately, and the
            # auto-resume loop would burn every attempt in an unspaced hot loop.
            # With no reset time, leave a plain usage-limit park for a human.
            and rec.quota_reset_at is not None
        ):
            prior_attempts = (
                rec.scheduled_resume.attempts if rec.scheduled_resume else 0
            )
            rec.scheduled_resume = M.ScheduledResume(
                attempt_at=rec.quota_reset_at,
                attempts=prior_attempts,
                max_attempts=self.config.max_auto_resume_attempts,
            )
        else:
            rec.scheduled_resume = None
        if result.session_id:
            rec.session_id = result.session_id
        if result.usage is not None:
            rec.usage.add(result.usage)
            self.manifest.totals.add(result.usage)
        # Per-agent-profile accumulation (FR-3.2): a step reports which profile
        # each slice of usage belongs to so `gauntlet report` can answer the
        # FR-3 cost-attribution acceptance. Kept separate from `totals`, never
        # double-counted (totals already took the step's grand usage above).
        for agent_name, agent_usage in (result.usage_by_agent or {}).items():
            self.manifest.agent_usage.setdefault(
                agent_name, M.UsageTotals()
            ).add(agent_usage)
            # Also record the per-role split ON THE STEP (FR-3.2 / FR-10.1): the
            # step-level `agent_usage` is what the ledger reads to attribute a
            # compound step's spend (an adversarial_cycle's reviewer/triager/fixer)
            # to each role's OWN provider — a cycle's roles can bill different
            # providers, so a single lumped row would misattribute. Accumulated
            # (like `rec.usage`) so a re-finalized/resumed step stays consistent.
            rec.agent_usage.setdefault(agent_name, M.UsageTotals()).add(agent_usage)
        if result.notes:
            rec.notes = result.notes
        if result.metrics:
            rec.metrics = dict(result.metrics)  # trend outcome counts (FR-6.6)
        if result.commit_sha:
            self.manifest.commits.append(
                M.CommitRecord(
                    step_id=rec.id,
                    phase=result.commit_phase or "",
                    sha=result.commit_sha,
                )
            )
        for phase, sha in result.commits:  # multi-commit steps (adversarial_cycle)
            self.manifest.commits.append(
                M.CommitRecord(step_id=rec.id, phase=phase, sha=sha)
            )
        if result.status == DONE:
            for name, path in result.artifact_writes.items():
                self.artifacts[name] = path
        # FR-10.1: append this step's content-free usage to the machine-global
        # ledger the instant its usage is recorded, so later runs (and window
        # admission on the next step) see it. Best-effort + idempotent inside.
        self._append_ledger_row(rec)
        # Failure-only attempt increment (FR-6): the audit retry counter advances
        # ONLY when a run ends in failure — relocated here from `_execute`'s old
        # unconditional top-of-run bump. DONE / PARKED / HALTED / INTERRUPTED do
        # not advance it, so arbitrarily many conflict or `--response` cycles
        # never exhaust `max_retries`; a genuine response failure still counts
        # once, like any other failed run.
        if result.status == FAILED:
            rec.attempts += 1
        # Consume a pending `--response` on a terminal agent outcome (FR-2 /
        # FR-7.1): proceed (DONE), re-park (PARKED), or genuine failure (FAILED).
        # An INTERRUPTED mid-edit park or a HALTED budget checkpoint is NOT a
        # terminal agent outcome, so the response stays `pending` for the next
        # resume to reconcile. The flip rides the same `_persist` as the status
        # and the FAILED increment above (atomic; F-003), and is returned so the
        # caller can commit the matching `consumed` checkpoint only once durable.
        consumed: "M.HumanResponse | None" = None
        if rec.human_responses:
            latest = rec.human_responses[-1]
            if latest.state == M.RESPONSE_PENDING and result.status in (
                DONE, FAILED, PARKED
            ):
                latest.state = M.RESPONSE_CONSUMED
                consumed = latest
        return consumed

    # ---- helpers -------------------------------------------------------------
    def _make_context(
        self, step: Step, rec: StepRecord, iteration: str | None, item: Any
    ) -> StepContext:
        return StepContext(
            repo_root=self.repo_root,
            run_dir=self.run_dir,
            artifact_root=self.artifact_root,
            config=self.config,
            pipeline=self.pipeline,
            manifest=self.manifest,
            record=rec,
            writer=self.writer,
            judge_env=self.judge_env,
            artifacts=dict(self.artifacts),
            excludes=self.excludes,
            iteration_item=item,
            iteration_index=int(iteration) if iteration is not None else None,
            adapter_factory=self.adapter_factory,
            persist=self._persist,  # FR-4.1: cycle sub-step write-ahead flush
        )

    def _context(self, item: Any = None, iteration: str | None = None) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "config": self.config,
            "artifacts": {name: True for name in self._existing_artifacts()},
            "vars": self.extra_context,
            # `foreach: plan.phases` (FR-5.1): the structured phase list the
            # plan-author emits in plan.md. Resolved lazily from the artifact so
            # the phases stage fans out over exactly what the (approved) plan
            # declares — and stays empty/missing until plan.md exists, so the
            # foreach only resolves after the plan gate (FR-10.2).
            "plan": self._plan_context(),
        }
        ctx.update(self.extra_context)
        if item is not None:
            ctx["item"] = item
        return ctx

    def _plan_context(self) -> _LazyPlanPhases:
        # Lazy: parsing is deferred to `.phases` access (see _LazyPlanPhases), so
        # a malformed plan.md only surfaces where the phase list is actually used
        # — the phases foreach — not while building context for every step.
        return _LazyPlanPhases(self.artifact_root / "plan.md")

    def _seed_artifacts(self) -> None:
        for name in ("prd.md", "plan.md"):
            path = self.artifact_root / name
            if path.exists():
                self.artifacts[name] = path

    def _existing_artifacts(self) -> set[str]:
        names = set(self.artifacts)
        for name in ("prd.md", "plan.md"):
            if (self.artifact_root / name).exists():
                names.add(name)
        return names

    def _mark_skipped(self, step_id: str, iteration: str | None) -> None:
        rec = self.manifest.record(step_id, iteration)
        if rec is None:
            rec = StepRecord(id=step_id, type="", iteration=iteration)
            self.manifest.upsert(rec)
        rec.status = M.SKIPPED
        rec.ended = self.clock()
        rec.parked_reason = None  # never carry a stale conflict reason (FR-2.1, F-001)

    def _find_parked_gate(self, step_id: str) -> StepRecord:
        # Scan across iterations so a gate parked inside a foreach (record
        # `gate` with iteration `1`) is reachable by approve/reject (F-004).
        # Only a parked *human_gate* is an approve/reject target (F-001): an
        # agent_task halted on an UPSTREAM CONFLICT also carries status PARKED
        # (with parked_reason set), but approving it would drive the run to
        # `done` while leaving that conflict discriminator live — a false
        # current state (FR-2.1). A conflict park is resolved upstream, not
        # rubber-stamped through the gate path.
        for rec in self.manifest.steps:
            if (
                rec.id == step_id
                and rec.status == M.PARKED
                and rec.type == "human_gate"
            ):
                return rec
        existing = self.manifest.record(step_id)
        raise ValueError(
            f"step {step_id!r} is not parked at a human gate "
            f"(status: {existing.status if existing else 'absent'}, "
            f"type: {existing.type if existing else 'absent'})"
        )

    def _head_sha(self) -> str:
        return gitops.head_sha(self.repo_root)

    # ---- evidence-tiered gates (FR-4, P8) -----------------------------------
    def _maybe_auto_approve(
        self, step: Step, iteration: str | None, item: Any
    ) -> StepResult | None:
        """Auto-approve a clean-signal code gate, else fall through to a park.

        Returns a DONE :class:`StepResult` (and appends the ``auto_approval``
        record + sends a notification) iff ``step`` is a ``human_gate`` configured
        ``policy: auto_when_clean`` AND the strict §4.2 clean-signal predicate
        holds. Returns ``None`` for every other step, an ``always``-policy gate,
        or any predicate miss — so the handler runs and parks for a human exactly
        as today (fail closed, FR-4.1).
        """
        if step.type != "human_gate":
            return None
        if step.get("policy") != M.GATE_POLICY_AUTO_WHEN_CLEAN:
            return None
        from gauntlet.engine import gates

        phase = self._expected_phase(step, item)
        decision = gates.evaluate_clean_gate(
            self.manifest, self.pipeline, step, iteration,
            load_findings=self._load_round_findings,
            phase=phase,
        )
        if not decision.clean:
            # Fall through to the human_gate handler (parks); leave a note so the
            # operator sees WHY it did not auto-approve (data over inference).
            self._record_gate_miss(step, decision, phase)
            return None
        record = M.AutoApproval(
            gate_id=step.id,
            iteration=iteration,
            phase=phase,
            policy=M.GATE_POLICY_AUTO_WHEN_CLEAN,
            evidence=decision.evidence,
            at=self.clock(),
        )
        self.manifest.auto_approvals.append(record)
        self._notify_auto_approval(record)
        return StepResult(
            status=DONE,
            notes=(
                f"auto-approved (FR-4.1): {decision.summary()}; evidence snapshot "
                f"recorded for PR-level ratification (FR-4.2): {decision.evidence}"
            ),
        )

    def _load_round_findings(
        self,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """This phase's round findings + triage verdicts + confirm verdicts, from
        the cycle artifacts.

        Reads ``run_dir/artifacts/{findings,triage,confirm}.json`` — the "latest
        round wins" bookkeeping the just-run cycle wrote (the same files ``PR.md``
        reads). A missing/unparseable findings or triage file raises, which the
        predicate treats as a fail-closed miss (cannot prove the findings clean).

        ``confirm.json`` is treated as OPTIONAL-BUT-NOT-STALE (FR-4.1 v0.5). It is
        legitimately absent on the zero-findings convergence path — nothing was
        fixed, so nothing was confirmed — and cycle.py invalidates it there rather
        than leaving the previous phase's file behind (absent > stale), so an
        absent file means "this phase confirmed nothing", never "read the last
        phase's verdicts". Absent therefore yields an empty verdict list, which is
        only ever reached when there is also no legitimate blocking/major finding
        to confirm; if there IS one, its missing verdict fails the conjunct closed
        in :func:`gates.evaluate_clean_gate` rather than passing unproven.
        """
        import json

        art = self.run_dir / "artifacts"
        findings = json.loads((art / "findings.json").read_text()).get("findings") or []
        verdicts = json.loads((art / "triage.json").read_text()).get("verdicts") or []
        confirm_path = art / "confirm.json"
        confirms: list[dict[str, Any]] = []
        if confirm_path.exists():
            confirms = json.loads(confirm_path.read_text()).get("verdicts") or []
        return findings, verdicts, confirms

    def _notify_auto_approval(self, record: "M.AutoApproval") -> None:
        """Send the FR-4.1 auto-approval notification via the advisory channel.

        Stamped into ``manifest.warnings`` (the engine's durable advisory push
        channel the web notifier surfaces) so an auto-approval is visible without
        opening the manifest, and de-duplicated by text like every other advisory.
        """
        note = (
            f"[{record.gate_id}] auto-approval (FR-4.1): code gate "
            f"{record.phase or record.gate_id} cleared without a human on clean "
            f"evidence (verifier={record.evidence.get('verifier')}, "
            f"rounds={record.evidence.get('rounds')}); enumerated in PR.md for "
            "ratification (FR-4.2)"
        )
        if note not in self.manifest.warnings:
            self.manifest.warnings.append(note)

    def _record_gate_miss(self, step: Step, decision, phase: str | None) -> None:
        """Note why an ``auto_when_clean`` gate parked instead of auto-approving.

        The note names the phase, not just the (foreach-stable) step id — the
        warnings list de-duplicates by text, so a phase-less note for ``P2``
        would silently collapse into ``P1``'s identical one (review F-9).
        """
        note = (
            f"[{step.id}{f' {phase}' if phase else ''}] auto_when_clean predicate "
            f"miss (FR-4.1): parked for a human — {decision.summary()}"
        )
        if note not in self.manifest.warnings:
            self.manifest.warnings.append(note)

    def _expected_phase(self, step: Step, item: Any) -> str | None:
        """The numeric phase prefix (P1, P2…) a step belongs to, or ``None``.

        An explicit ``phase:`` wins; otherwise the ``foreach: plan.phases`` item
        id supplies it. Non-numeric stage labels (PRD/PLAN/REVIEW) never carry
        intra-phase checkpoints, so they scope to ``None`` (unscoped discovery).
        """
        candidate = step.get("phase")
        if not candidate and isinstance(item, dict):
            candidate = item.get("id")
        candidate = str(candidate or "")
        return candidate if _NUMERIC_PHASE_RE.fullmatch(candidate) else None

    def _checkpoint_rewind_target(
        self, rec: StepRecord, phase: str | None = None
    ) -> tuple[str, str | None]:
        """Rewind target + checkpoint subject for a dirty re-run (FR-11.2).

        Returns ``(target_sha, subject)``: the newest ``P<N> wip:`` checkpoint
        commit reachable from HEAD and descended from ``rec.base_sha`` (so
        completed milestones survive the rewind), with its subject for the re-run
        prompt / audit trail. Discovery is SCOPED to ``phase`` when known (review
        F-001), so a wrong-phase checkpoint is never chosen as the rewind target.
        Falls back to ``(base_sha, None)`` when the phase landed no checkpoint —
        today's reset-to-base behavior. Only reached with a non-null ``base_sha``
        (the caller guards it).
        """
        wips = gitops.wip_checkpoints(self.repo_root, base=rec.base_sha, phase=phase)
        if wips:
            sha, subject = wips[0]  # newest first
            return sha, subject
        return rec.base_sha, None

    def _set_run_status(self, step_status: str) -> str:
        self.manifest.status = {
            DONE: M.RUN_DONE,
            PARKED: M.RUN_PARKED,
            HALTED: M.RUN_PARKED,  # a halt parks the run for a human (FR-3.3)
            INTERRUPTED: M.RUN_PARKED,  # mid-edit interruption parks (F-003)
            FAILED: M.RUN_FAILED,
        }.get(step_status, M.RUN_RUNNING)
        if self.manifest.status in (M.RUN_DONE,):
            self.manifest.current_step = None
        self._persist()
        return self.manifest.status

    # ---- response handling (FR-2, FR-2.2, FR-7.1) ---------------------------
    def _apply_response_action(self) -> None:
        """Apply a planned `--response` transition at the start of a resume.

        Order matters for crash recovery (FR-7.1): FIRST flush the latest
        response step's CURRENT state to git, so a crash between an atomic
        manifest write and its checkpoint commit can never leave that state
        unreachable in history — and, for a recovered `pending` entry, so a
        distinct `pending` commit always precedes the later `consumed` one
        (F-002). THEN, only for a brand-new response, append the `pending` entry
        and commit it before the stage walk re-executes the parked step.
        """
        self._reconcile_response_checkpoint()
        action = self.response_action
        if action is None or action.kind in ("none", "recover"):
            return
        if action.kind == "append":
            rec = self.manifest.record(action.step_id, action.iteration)
            if rec is None:  # defensive: validated in RunManager before we drive
                return
            self._append_response(rec, action.text, action.user)

    def _reconcile_response_checkpoint(self) -> None:
        """Idempotently flush the latest response step's current-state commit."""
        message = self._response_checkpoint_message()
        if message is None:
            return
        self._commit_manifest_checkpoint(message)

    def _response_checkpoint_message(self) -> str | None:
        """Canonical checkpoint subject for the latest response entry, or None.

        The single source of the ``gauntlet: response <id> <state>`` wording, so
        the reconcile flush and the F-001 dirty-base rewind label the checkpoint
        identically — the rewind commit can then stand in as that very checkpoint.
        """
        rec = self._latest_response_step()
        if rec is None or not rec.human_responses:
            return None
        entry = rec.human_responses[-1]
        return f"gauntlet: response {entry.response_id} {entry.state}"

    def _latest_response_step(self) -> StepRecord | None:
        """The most recently active step carrying `--response` history.

        Steps are appended in execution order, so the last record with a
        non-empty `human_responses` list is the one whose checkpoint commit may
        still be un-flushed after a crash. At most one is ever in flight.
        """
        target: StepRecord | None = None
        for rec in self.manifest.steps:
            if rec.human_responses:
                target = rec
        return target

    def _append_response(self, rec: StepRecord, text: str, user: str) -> M.HumanResponse:
        """Append one `pending` response entry, persist, and commit it (FR-2).

        `response_id` (`<step_id>-resp-<ordinal>`) and `response_attempt` are
        both the 1-based position in the array, assigned once and never changed —
        the stable handle the builder references (FR-5/FR-10) and recovery
        deduplicates on (FR-7.1). The `pending` write is atomic and committed
        BEFORE the agent launches.
        """
        ordinal = len(rec.human_responses) + 1
        entry = M.HumanResponse(
            response_id=f"{rec.id}-resp-{ordinal}",
            response_text=text,
            timestamp=self.clock(),
            user=user,
            response_attempt=ordinal,
            state=M.RESPONSE_PENDING,
        )
        rec.human_responses.append(entry)
        self._persist()  # atomic write-ahead of the pending entry (FR-2.2)
        self._commit_manifest_checkpoint(
            f"gauntlet: response {entry.response_id} pending"
        )
        return entry

    def _commit_manifest_checkpoint(self, message: str) -> str | None:
        """Commit run bookkeeping (manifest.json + RUN.md) as an engine commit.

        The orchestrator is the sole committer of manifest state (FR-2.2); the
        builder gets no direct-write path. Stages ONLY the two bookkeeping paths
        (never the implementation diff) under the fixed engine identity, forcing
        past the run-dir gitignore. Idempotent — a no-op when nothing changed —
        so recovery can call it to flush a not-yet-landed state safely.
        """
        # Keep RUN.md consistent with the manifest we are about to commit (the
        # manifest is authoritative; RUN.md is its derived index).
        write_run_index(self.run_dir, self.manifest, self.writer)
        paths = self._bookkeeping_paths()
        if not paths:
            return None
        return gitops.commit_run_bookkeeping(
            self.repo_root, message, paths, identity=ENGINE_IDENTITY
        )

    def _bookkeeping_paths(self) -> list[str]:
        """Repo-relative paths of the two on-disk run-bookkeeping files.

        Delegates to the shared :func:`run_bookkeeping_paths` — the single
        definition of what every engine bookkeeping commit (and the F-001 /
        fix-rerun rewinds) force-stages.
        """
        return run_bookkeeping_paths(self.repo_root, self.run_dir)

    def _persist(self) -> None:
        self.manifest.write_atomic(self.manifest_path)
        # RUN.md (FR-4.3) is regenerated on every checkpoint so the index never
        # lags the state machine; it is derived data, cheap to rewrite.
        write_run_index(self.run_dir, self.manifest, self.writer)

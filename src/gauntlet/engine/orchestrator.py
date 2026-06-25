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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from gauntlet.adapters.base import AgentTimeoutError
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.execution import (
    DONE,
    FAILED,
    HALTED,
    INTERRUPTED,
    PARKED,
    SKIPPED,
    StepContext,
    StepResult,
    get_spec,
    run_bookkeeping_excludes,
)
from gauntlet.engine.expr import eval_when, resolve_list
from gauntlet.engine.manifest import Manifest, StepRecord
from gauntlet.engine.planphases import PlanPhasesError
from gauntlet.engine.pipeline import Pipeline, Stage, Step
from gauntlet.logging.redact import RedactingWriter
from gauntlet.logging.transcript import write_run_index


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Author/committer for orchestrator-owned manifest-checkpoint commits (FR-2.2).
# A response's *operator* identity (FR-9) is recorded IN the manifest entry's
# ``user`` field — it is deliberately NOT the commit author, which is the fixed
# engine identity so bookkeeping commits are attributable to the engine, never
# mislabelled as a human's work.
ENGINE_IDENTITY = gitops.Identity(name="Gauntlet Engine", email="engine@gauntlet.local")


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

    def reject_gate(self, step_id: str, notes: str) -> str:
        rec = self._find_parked_gate(step_id)
        rec.status = M.FAILED
        rec.ended = self.clock()
        rec.parked_reason = None  # current-state invariant (FR-2.1, F-001)
        rec.notes = f"rejected: {notes}"
        self._persist()
        return self._set_run_status(FAILED)

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
            # A consumed-response terminal FAILURE is reconciled bookkeeping-only,
            # never re-executed (FR-7.1, review F-002): the human's --response was
            # processed and the step genuinely failed, persisted as FAILED with the
            # response already CONSUMED (and its checkpoint flushed at drive start).
            # Re-running it would re-invoke the adapter and double-count the failure
            # on every later resume; surface the failure instead.
            if self._is_consumed_terminal_failure(rec):
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
    def _is_consumed_terminal_failure(rec: StepRecord | None) -> bool:
        """True for a FAILED record whose latest `--response` is already consumed.

        Such a record is a terminal, already-reconciled outcome (FR-7.1): a prior
        invocation launched the response, it failed, and the finalize flipped the
        entry to ``consumed`` and counted the one failure. It must never re-run.
        """
        return (
            rec is not None
            and rec.status == M.FAILED
            and bool(rec.human_responses)
            and rec.human_responses[-1].state == M.RESPONSE_CONSUMED
        )

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
            short = self._resume_disposition(step, spec, rec)
            if short is not None:
                self._finalize(rec, short)
                return short

        # NOTE: `rec.attempts` is NO LONGER incremented here. FR-6 redefines it
        # as the failure-retry counter: it advances exactly once, in `_finalize`,
        # only on a FAILED outcome — never on success, conflict park, halt,
        # interruption, or a `--response` continuation. Relocating it there is
        # what keeps conflict/response resumes from consuming the retry budget.
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
            try:
                result = spec.handler(step, ctx)
            except AgentTimeoutError as exc:
                result = StepResult(
                    status=HALTED,
                    usage=exc.partial.usage if exc.partial else None,
                    session_id=exc.partial.session_id if exc.partial else None,
                    notes=f"timeout halt (FR-3.3): {exc}",
                )
            except Exception as exc:  # fail closed: a handler fault halts the step
                result = StepResult(status=FAILED, notes=f"handler error: {exc}")

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
        self, step: Step, spec, rec: StepRecord
    ) -> StepResult | None:
        """Decide how to re-enter a step that was interrupted (review F-003).

        Returns a terminal StepResult to short-circuit (park), or ``None`` to
        proceed with a normal (re-)run. Only repo-writing agent steps that left
        a dirty worktree are parked/reset; idempotent steps (shell) and the
        commit step (which reconciles from git log) simply re-run.
        """
        if rec.base_sha is None or not spec.step_touches_worktree(step):
            return None
        # agent_task killed mid-edit AND adversarial_cycle killed mid-round are
        # both non-idempotent worktree writers: park (or reset) on a dirty base
        # rather than re-running over partial fixer edits / unmanifested
        # fix-round commits. shell and commit re-enter safely on their own.
        is_agent_write = (
            step.type == "agent_task" and spec.step_requires_repo_write(step)
        ) or step.type == "adversarial_cycle"
        if not is_agent_write:
            return None
        # Detect partial work against the narrow bookkeeping exclusion, so a
        # partial *artifact* under the run root (not just a repo-root file) is
        # still seen as a mid-edit interruption (review F-001).
        if not gitops.is_dirty_vs(self.repo_root, rec.base_sha, exclude=self.excludes):
            return None  # clean re-entry: agent never progressed; safe to re-run
        if self.config.interrupted_step == "reset_to_base":
            ts = self.clock().replace(":", "-")
            backup = f"refs/gauntlet/backup/{self.manifest.run_id}/{rec.id}-{ts}"
            # Snapshot the partial work (tracked + untracked) before discarding.
            gitops.backup_dirty_worktree(
                self.repo_root, backup, f"interrupted {rec.id} partial work",
                exclude=self.excludes,
            )
            # Flush the authoritative in-memory manifest to disk BEFORE the
            # rewind, so the bookkeeping the rewind preserves carries the latest
            # response state (e.g. a still-`pending` entry the re-run consumes).
            self._persist()
            # F-001: base_sha predates the engine bookkeeping commits stacked on
            # top of it — notably the pending-response checkpoint (FR-2.2/FR-7.1).
            # A plain `reset --hard base_sha` would delete the force-committed
            # manifest from disk AND orphan that checkpoint, so a kill in the gap
            # before it was re-persisted would lose the human response. Instead,
            # rewind the implementation to base_sha in a single reset whose target
            # commit still carries the manifest — the response is never, even for
            # an instant, absent from both disk and reachable history. Label that
            # commit with the canonical response-checkpoint subject so it stands in
            # as the pending checkpoint itself (the post-rewind reconcile below is
            # then a no-op rather than stacking a duplicate).
            paths = self._bookkeeping_paths()
            if paths and gitops.head_sha(self.repo_root) != rec.base_sha:
                message = (
                    self._response_checkpoint_message()
                    or f"gauntlet: rewind implementation to base for re-run ({rec.id})"
                )
                gitops.rewind_impl_preserving_bookkeeping(
                    self.repo_root, rec.base_sha, paths, message,
                    identity=ENGINE_IDENTITY,
                )
            else:
                # No checkpoint sits above base_sha (HEAD == base_sha, or no
                # bookkeeping on disk): the plain rewind is already crash-safe.
                gitops.reset_hard(self.repo_root, rec.base_sha)
            # `clean` is broader than the dirty check on purpose: it spares the
            # whole run root so the reset never wipes the run pointer, manifests,
            # the authored prd.md, or prior declared artifacts — the re-run
            # regenerates its own outputs over them.
            gitops.clean_untracked(self.repo_root, exclude=[self.config.run_root])
            # The rewind restored the on-disk manifest to base_sha's tree + the
            # overlaid bookkeeping; re-persist the authoritative in-memory state
            # over it and idempotently flush the checkpoint (still `pending` here
            # — the re-run consumes it and lands the `consumed` checkpoint).
            self._persist()
            self._reconcile_response_checkpoint()
            return None  # tree restored to base; checkpoints preserved; re-run cleanly
        return StepResult(
            status=INTERRUPTED,
            notes=(
                "interrupted mid-edit: worktree dirty vs base SHA "
                f"{rec.base_sha[:10]}; parked for a human (F-003, "
                "interrupted_step=park)"
            ),
        )

    def _restore_clean_after_conflict_park(
        self, step: Step, spec, rec: StepRecord, result: StepResult
    ) -> StepResult:
        """Restore the clean-worktree invariant on a conflict park (review F-001).

        A conflict park (``parked_reason == upstream_conflict``) returns control
        to a human, so the worktree must be clean (CLAUDE.md §1). But the builder
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
        if (
            result.status != PARKED
            or result.parked_reason != M.PARKED_REASON_UPSTREAM_CONFLICT
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
        gitops.backup_dirty_worktree(
            self.repo_root,
            backup,
            f"conflict-park partial work for {rec.id} (F-001)",
            exclude=run_root,
        )
        gitops.reset_hard(self.repo_root, self._head_sha())
        # `reset --hard` leaves untracked files; clear the builder's untracked
        # edits too, sparing the whole run root so the manifest/transcripts/
        # authored artifacts under it survive.
        gitops.clean_untracked(self.repo_root, exclude=run_root)
        note = (
            f"conflict park left an uncommitted worktree; backed up to {backup} "
            "and restored the clean tree before handoff (F-001)"
        )
        result.notes = f"{result.notes}\n{note}" if result.notes else note
        return result

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
        # Conflict-park discriminator is CURRENT-STATE, not a latch (FR-2.1):
        # copy the just-finished execution's parked_reason onto the record. It is
        # PARKED_REASON_UPSTREAM_CONFLICT only when this run halted on an UPSTREAM
        # CONFLICT, and None for every other outcome — so a conflict park later
        # resumed to done/failed/non-conflict-park clears the stale value here.
        rec.parked_reason = result.parked_reason
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

        The manifest is authoritative; RUN.md is its derived index. Returns only
        the files that currently exist, repo-root-relative and POSIX-formatted —
        the exact set every engine bookkeeping commit (and the F-001 rewind)
        force-stages.
        """
        root = self.repo_root.resolve()
        paths: list[str] = []
        for name in ("manifest.json", "RUN.md"):
            p = self.run_dir / name
            if not p.exists():
                continue
            try:
                paths.append(p.resolve().relative_to(root).as_posix())
            except ValueError:
                pass
        return paths

    def _persist(self) -> None:
        self.manifest.write_atomic(self.manifest_path)
        # RUN.md (FR-4.3) is regenerated on every checkpoint so the index never
        # lags the state machine; it is derived data, cheap to rewrite.
        write_run_index(self.run_dir, self.manifest, self.writer)

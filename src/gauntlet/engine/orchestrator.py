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
from gauntlet.engine.pipeline import Pipeline, Stage, Step
from gauntlet.logging.redact import RedactingWriter
from gauntlet.logging.transcript import write_run_index


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        for stage in self.pipeline.stages:
            status = self._run_stage(stage)
            if status != DONE:
                return self._set_run_status(status)
        return self._set_run_status(DONE)

    def approve_gate(self, step_id: str, notes: str | None = None) -> str:
        rec = self._find_parked_gate(step_id)
        rec.status = M.DONE
        rec.ended = self.clock()
        rec.notes = f"approved: {notes}" if notes else "approved"
        self._persist()
        return self.drive()

    def reject_gate(self, step_id: str, notes: str) -> str:
        rec = self._find_parked_gate(step_id)
        rec.status = M.FAILED
        rec.ended = self.clock()
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
        retries: dict[str, int] = {}
        ptr = 0
        while ptr < len(stage.steps):
            step = stage.steps[ptr]
            rec = self.manifest.record(step.id, iteration)
            if rec is not None and rec.status in (M.DONE, M.SKIPPED):
                ptr += 1
                continue
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
                if retries.get(step.id, 0) < step.on_fail.max_retries:
                    retries[step.id] = retries.get(step.id, 0) + 1
                    self._reset_for_retry(stage, step.on_fail.route_to, iteration)
                    ptr = index[step.on_fail.route_to]
                    continue
            return status  # FAILED, PARKED, HALTED, or INTERRUPTED
        return DONE

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

        rec.attempts += 1
        rec.status = M.RUNNING
        rec.started = rec.started or self.clock()
        self.manifest.current_step = step.id
        if spec.step_touches_worktree(step) and rec.base_sha is None:
            rec.base_sha = self._head_sha()
        # Per-step judge attribution: child agent hooks read GAUNTLET_STEP_ID
        # (only under an active judge run; unit tests leave judge_env empty).
        if self.judge_env:
            os.environ["GAUNTLET_STEP_ID"] = step.id
        self._persist()  # WRITE-AHEAD: before any side effect

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
        self._finalize(rec, result)
        self._persist()  # WRITE-AHEAD: after the side effect, terminal state
        return result

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
            gitops.reset_hard(self.repo_root, rec.base_sha)
            # `clean` is broader than the dirty check on purpose: it spares the
            # whole run root so the reset never wipes the run pointer, manifests,
            # the authored prd.md, or prior declared artifacts — the re-run
            # regenerates its own outputs over them.
            gitops.clean_untracked(self.repo_root, exclude=[self.config.run_root])
            return None  # tree restored to base; re-run cleanly
        return StepResult(
            status=INTERRUPTED,
            notes=(
                "interrupted mid-edit: worktree dirty vs base SHA "
                f"{rec.base_sha[:10]}; parked for a human (F-003, "
                "interrupted_step=park)"
            ),
        )

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
            return StepResult(
                status=HALTED,
                usage=result.usage,
                session_id=result.session_id,
                notes=(
                    f"budget halt (FR-3.3): step cost ${projected:.4f} exceeds "
                    f"budget ${budget:.4f}; halting at checkpoint"
                ),
            )
        return result

    def _finalize(self, rec: StepRecord, result: StepResult) -> None:
        rec.status = {
            DONE: M.DONE,
            FAILED: M.FAILED,
            PARKED: M.PARKED,
            HALTED: M.HALTED,
            SKIPPED: M.SKIPPED,
            INTERRUPTED: M.INTERRUPTED,
        }[result.status]
        rec.ended = self.clock()
        if result.session_id:
            rec.session_id = result.session_id
        if result.usage is not None:
            rec.usage.add(result.usage)
            self.manifest.totals.add(result.usage)
        if result.notes:
            rec.notes = result.notes
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
        }
        ctx.update(self.extra_context)
        if item is not None:
            ctx["item"] = item
        return ctx

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

    def _find_parked_gate(self, step_id: str) -> StepRecord:
        # Scan across iterations so a gate parked inside a foreach (record
        # `gate` with iteration `1`) is reachable by approve/reject (F-004).
        for rec in self.manifest.steps:
            if rec.id == step_id and rec.status == M.PARKED:
                return rec
        existing = self.manifest.record(step_id)
        raise ValueError(
            f"step {step_id!r} is not parked at a gate "
            f"(status: {existing.status if existing else 'absent'})"
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

    def _persist(self) -> None:
        self.manifest.write_atomic(self.manifest_path)
        # RUN.md (FR-4.3) is regenerated on every checkpoint so the index never
        # lags the state machine; it is derived data, cheap to rewrite.
        write_run_index(self.run_dir, self.manifest, self.writer)

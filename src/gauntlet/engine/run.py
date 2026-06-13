"""Run lifecycle: new / run / status / approve / reject / resume / abort / rollback.

Glue between the CLI and the :class:`Orchestrator`. Owns the on-disk layout
(FR-4.1), the entry contract (FR-10.1), branch management (FR-9.1), the
engine-managed judge lifecycle (FR-7.1), and guarded rollback (FR-9.9 /
review F-010).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.execution import run_bookkeeping_excludes
from gauntlet.engine.judgeproc import ManagedJudge
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.validate import validate_pipeline
from gauntlet.logging.redact import RedactingWriter, build_redactor

# Marker written into a scaffolded PRD; the entry contract refuses to run while
# it is still present (FR-10.1 / review OQ-1: existence + non-stub-ness).
PRD_STUB_MARKER = "<!-- GAUNTLET-PRD-STUB: replace this file with a real PRD -->"

_PRD_STUB = f"""{PRD_STUB_MARKER}
# PRD: <title>

> Gauntlet does not author PRDs (FR-10.1). Replace this stub with a real,
> human-authored PRD, then run `gauntlet run <slug>`. The run refuses to start
> while this marker is present.

## Problem statement

## Requirements
"""


class EntryContractError(RuntimeError):
    """The entry contract (FR-10.1) is not satisfied."""


class RollbackGuardError(RuntimeError):
    """A rollback guard (review F-010) refused the operation."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _strip_marker(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if PRD_STUB_MARKER not in line)


def _normalize(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


@dataclass
class RunLayout:
    repo_root: Path
    config: RunConfig
    slug: str

    @property
    def slug_dir(self) -> Path:
        return self.repo_root / self.config.run_root / self.slug

    @property
    def prd_path(self) -> Path:
        return self.slug_dir / "prd.md"

    @property
    def active_pointer(self) -> Path:
        return self.slug_dir / "active-run.txt"

    def run_dir(self, name: str) -> Path:
        return self.slug_dir / name

    def active_run_dir(self) -> Path:
        if not self.active_pointer.exists():
            raise FileNotFoundError(
                f"no active run for {self.slug!r}; has `gauntlet run` been started?"
            )
        return self.slug_dir / self.active_pointer.read_text().strip()


class RunManager:
    def __init__(self, repo_root: Path, config: RunConfig | None = None) -> None:
        self.repo_root = repo_root
        self.config = config or RunConfig.load(repo_root / ".gauntlet/config.yaml")
        # The configured redaction list (FR-4.4) governs every byte the run
        # writes; default-on even with an empty `redaction:` section.
        self.writer = RedactingWriter(build_redactor(self.config.redaction))

    def layout(self, slug: str) -> RunLayout:
        return RunLayout(self.repo_root, self.config, slug)

    @staticmethod
    def _ensure_slug_gitignore(layout: "RunLayout") -> None:
        """Ignore the slug-level live bookkeeping (BOOTSTRAP-NOTES #33).

        Idempotent; engine-owned so the guarantee never depends on the repo's
        own .gitignore. Two bookkeeping entries: the active-run pointer, and the
        slug ``.gitignore`` itself — it is engine-regenerated each run, never a
        commit payload, and leaving it untracked would dirty the worktree at the
        very first review handoff of a `standard` run (prd-cycle is step 1, with
        no commit step before it to sweep it in — unlike the bootstrap pipeline,
        whose first step is a phase commit). Self-ignoring mirrors the run-dir's
        own ``*`` self-ignore. prd.md/plan.md and manual records stay tracked."""
        layout.slug_dir.mkdir(parents=True, exist_ok=True)
        gi = layout.slug_dir / ".gitignore"
        existing = gi.read_text().split() if gi.exists() else []
        wanted = [".gitignore", "active-run.txt"]
        if any(w not in existing for w in wanted):
            lines = list(dict.fromkeys(existing + wanted))  # dedup, stable order
            gi.write_text("\n".join(lines) + "\n")

    # ---- new (FR-8.1 scaffold) ----------------------------------------------
    def new(self, slug: str) -> Path:
        layout = self.layout(slug)
        layout.slug_dir.mkdir(parents=True, exist_ok=True)
        if not layout.prd_path.exists():
            layout.prd_path.write_text(_PRD_STUB)
        return layout.prd_path

    # ---- entry contract (FR-10.1) -------------------------------------------
    def check_entry_contract(self, slug: str) -> None:
        layout = self.layout(slug)
        if not layout.prd_path.exists():
            raise EntryContractError(
                f"{layout.prd_path} does not exist; `gauntlet new {slug}` scaffolds "
                "a stub for a human to author (FR-10.1)"
            )
        content = layout.prd_path.read_text()
        if PRD_STUB_MARKER in content:
            raise EntryContractError(
                f"{layout.prd_path} is still the scaffolded stub; a human must "
                "author the PRD before a run can start (FR-10.1)"
            )
        # Deleting only the marker line leaves the rest of the scaffold intact —
        # still not a human-authored PRD. Compare the whole body, marker-stripped
        # and whitespace-normalized, against the stub (review F-007).
        if _normalize(content) == _normalize(_strip_marker(_PRD_STUB)):
            raise EntryContractError(
                f"{layout.prd_path} is the scaffolded stub with only the marker "
                "removed; a human must author a real PRD before a run (FR-10.1)"
            )

    # ---- run (FR-8.1) -------------------------------------------------------
    def start(
        self,
        slug: str,
        pipeline_path: Path,
        *,
        use_judge: bool = True,
        adapter_factory=None,
        extra_context: dict | None = None,
        clock=None,
    ) -> str:
        self.check_entry_contract(slug)
        layout = self.layout(slug)
        pipeline, phash = load_pipeline(pipeline_path)
        validate_pipeline(pipeline, self.config)

        branch = f"{self.config.branch_prefix}{slug}"
        gitops.checkout_or_create_branch(self.repo_root, branch, self.config.base_branch)

        run_id = f"run-{_utc_stamp()}"
        run_dir = layout.run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        # The active-run pointer is live bookkeeping, never commit payload
        # (BOOTSTRAP-NOTES #33). An engine-written slug-level .gitignore keeps
        # it ignored in EVERY repo — including throwaway fixture repos that
        # lack the init-provided `runs/*/active-run.txt` rule — so it never
        # dirties the worktree and `git add` never collides with it.
        self._ensure_slug_gitignore(layout)
        # Snapshot the exact pipeline source into the run dir so resume reloads
        # precisely what started the run (FR-5.6 reproducibility).
        (run_dir / "pipeline.yaml").write_text(pipeline_path.read_text())
        layout.active_pointer.write_text(run_id)

        man = Manifest(
            run_id=run_id,
            slug=slug,
            branch=branch,
            base_branch=self.config.base_branch,
            pipeline=PipelineRef(name=pipeline.name, version=pipeline.version, hash=phash),
            prompt_hashes=self._prompt_hashes(pipeline),
        )
        return self._drive(
            layout, run_dir, pipeline, man,
            use_judge=use_judge, adapter_factory=adapter_factory,
            extra_context=extra_context, clock=clock,
        )

    # ---- resume (FR-8.2) ----------------------------------------------------
    def resume(self, slug: str, *, use_judge: bool = True, adapter_factory=None,
               extra_context: dict | None = None, clock=None) -> str:
        layout = self.layout(slug)
        self._ensure_slug_gitignore(layout)  # idempotent (#33; old runs too)
        run_dir = layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")
        pipeline, phash = load_pipeline(run_dir / "pipeline.yaml")
        if phash != man.pipeline.hash:
            raise RuntimeError(
                "pipeline content hash changed since the run started "
                f"({man.pipeline.hash} -> {phash}); resume refuses to run a "
                "different pipeline against an existing manifest (FR-5.6)"
            )
        gitops.checkout_or_create_branch(self.repo_root, man.branch, man.base_branch)
        return self._drive(
            layout, run_dir, pipeline, man,
            use_judge=use_judge, adapter_factory=adapter_factory,
            extra_context=extra_context, clock=clock,
        )

    # ---- gates --------------------------------------------------------------
    def approve(self, slug: str, gate: str | None = None, notes: str | None = None,
                *, use_judge: bool = True, adapter_factory=None) -> str:
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")
        gate = gate or man.current_step
        if gate is None:
            raise ValueError("no gate to approve; run is not parked")
        pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
        # Approving a gate drives the rest of the run, so honor use_judge for it.
        if use_judge:
            return self._with_judge(man, run_dir, lambda env: self._approve_drive(
                layout, run_dir, pipeline, man, gate, notes, env, adapter_factory))
        orch = self._orchestrator(layout, run_dir, pipeline, man,
                                  judge_env={}, adapter_factory=adapter_factory)
        status = orch.approve_gate(gate, notes)
        self._maybe_draft_pr(layout, run_dir, man, status)
        return status

    def reject(self, slug: str, notes: str, gate: str | None = None) -> str:
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")
        gate = gate or man.current_step
        if gate is None:
            raise ValueError("no gate to reject; run is not parked")
        pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
        orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env={})
        return orch.reject_gate(gate, notes)

    # ---- abort --------------------------------------------------------------
    def abort(self, slug: str) -> str:
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")
        man.status = M.RUN_ABORTED
        man.write_atomic(run_dir / "manifest.json")
        return man.status

    # ---- status -------------------------------------------------------------
    def status(self, slug: str) -> Manifest:
        layout = self.layout(slug)
        return Manifest.load(layout.active_run_dir() / "manifest.json")

    # ---- feedback (FR-6.1) --------------------------------------------------
    def save_feedback(self, slug: str, data, *, run_dir: Path | None = None) -> Path:
        """Capture human feedback into the run's ``retro/feedback.md`` (+ json)."""
        from gauntlet.engine.feedback import write_feedback

        layout = self.layout(slug)
        run_dir = run_dir or layout.active_run_dir()
        if not data.run_id and (run_dir / "manifest.json").exists():
            data.run_id = Manifest.load(run_dir / "manifest.json").run_id
        return write_feedback(run_dir, data, self.writer)

    # ---- proposals (FR-6.3/6.4) ---------------------------------------------
    def _all_slugs(self) -> list[str]:
        root = self.repo_root / self.config.run_root
        if not root.exists():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir())

    def _iter_run_dirs(self, slug: str | None = None):
        slugs = [slug] if slug else self._all_slugs()
        for s in slugs:
            sdir = self.layout(s).slug_dir
            if not sdir.exists():
                continue
            for run_dir in sorted(sdir.glob("run-*")):
                if (run_dir / "manifest.json").exists():
                    yield run_dir

    def list_proposals(self, slug: str | None = None) -> list[tuple[Path, object]]:
        """Every proposal across runs (optionally one slug), as (run_dir, Proposal)."""
        from gauntlet.engine.proposals import list_proposals

        out: list[tuple[Path, object]] = []
        for run_dir in self._iter_run_dirs(slug):
            for p in list_proposals(run_dir / "retro" / "proposals"):
                out.append((run_dir, p))
        return out

    def review_proposals(self, slug: str | None = None, *, decide, timestamp=None) -> list[dict]:
        """Present pending proposals to ``decide`` and apply/reject each (FR-6.4).

        ``decide(proposal) -> (action, notes)`` where action is ``approve`` or
        ``reject``; the CLI wires it to interactive prompts, tests pass a
        callback. Approved diffs are applied on a clean tree and committed — no
        proposal self-applies (this is an engine action gated on human approval).
        Per-proposal failures are recorded, never aborting the whole review.
        """
        from gauntlet.engine import proposals as P
        from gauntlet.engine.execution import run_bookkeeping_excludes

        timestamp = timestamp or _utc_stamp()
        changelog = self.repo_root / "prompts" / "CHANGELOG.md"
        identity = self.config.identity("retro")
        results: list[dict] = []
        for run_dir, proposal in self.list_proposals(slug):
            if proposal.status != P.PENDING or not proposal.valid:
                continue
            action, notes = decide(proposal)
            if action != "approve":
                P.reject_proposal(proposal, notes or "")
                results.append({"proposal": proposal.name, "action": "rejected"})
                continue
            excludes = run_bookkeeping_excludes(self.repo_root, run_dir, run_dir.parent)
            if not gitops.is_clean(self.repo_root, exclude=excludes):
                raise P.ProposalError(
                    "refusing to apply a proposal: worktree is dirty; commit or "
                    "discard changes first (governed apply needs a clean tree)"
                )
            try:
                sha = P.apply_proposal(
                    self.repo_root, proposal, identity=identity,
                    changelog_path=changelog, timestamp=timestamp,
                )
                results.append({"proposal": proposal.name, "action": "applied", "sha": sha})
            except P.ProposalError as exc:
                results.append({"proposal": proposal.name, "action": "error", "reason": str(exc)})
        return results

    # ---- trend metrics (FR-6.6) ---------------------------------------------
    def trend(self, slug: str | None = None) -> list:
        from gauntlet.engine.trend import build_run_trend

        rows = []
        for run_dir in self._iter_run_dirs(slug):
            man = Manifest.load(run_dir / "manifest.json")
            rows.append(build_run_trend(man, judge_audit_path=run_dir / "judge-audit.jsonl"))
        rows.sort(key=lambda r: r.run_id)
        return rows

    # ---- rollback (FR-9.9 / review F-010) -----------------------------------
    def rollback(self, slug: str, phase: int) -> str:
        layout = self.layout(slug)
        run_dir = layout.active_run_dir()
        man = Manifest.load(run_dir / "manifest.json")

        # Guard 1: clean work tree — only the engine's own bookkeeping is
        # excluded (review F-001), so an uncommitted real artifact still blocks.
        excludes = run_bookkeeping_excludes(self.repo_root, run_dir, layout.slug_dir)
        if not gitops.is_clean(self.repo_root, exclude=excludes):
            raise RollbackGuardError(
                "refusing rollback: worktree is dirty; commit or discard first"
            )
        # Guard 2: branch tip MUST equal the manifest's last recorded commit.
        # A branch ahead of the manifest (extra unmanifested commits) is a
        # divergence — reset would silently discard those commits (review F-003).
        if not man.commits:
            raise RollbackGuardError("no recorded commits to roll back to")
        last_recorded = man.commits[-1].sha
        head = gitops.head_sha(self.repo_root)
        if head != last_recorded:
            raise RollbackGuardError(
                "refusing rollback: branch has diverged from the manifest "
                f"(HEAD {head[:10]} != last recorded {last_recorded[:10]}); the "
                "branch and manifest must agree before a rewind (FR-9.9)"
            )
        # Resolve the target: the last commit whose phase prefix is P<phase>.
        target = self._phase_boundary_sha(man, phase)
        if target is None:
            raise RollbackGuardError(
                f"no recorded phase-{phase} commit boundary to roll back to"
            )

        # Backup ref + manifest snapshot before any rewind (F-010).
        ts = _utc_stamp()
        gitops.create_ref(
            self.repo_root, f"refs/gauntlet/backup/{man.run_id}/{ts}", head
        )
        shutil.copy2(run_dir / "manifest.json", run_dir / f"manifest.snapshot-{ts}.json")

        gitops.reset_hard(self.repo_root, target)
        self._rewind_manifest(man, run_dir, target)
        man.write_atomic(run_dir / "manifest.json")
        return target

    def _rewind_manifest(self, man: Manifest, run_dir: Path, target: str) -> None:
        """Rewind the manifest to match the reset branch (review F-002).

        Drop commits after the target, and reset to `pending` EVERY step record
        (any type, any iteration) that executes after the target phase boundary
        in pipeline order — not just the steps that produced dropped commits.
        Otherwise a later resume skips work `git reset --hard` removed and the
        branch and manifest disagree (FR-9.9).
        """
        keep: list = []
        for commit in man.commits:
            keep.append(commit)
            if commit.sha == target:
                break
        man.commits = keep
        target_step = keep[-1].step_id

        pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
        order = [s.id for s in pipeline.all_steps()]
        try:
            cutoff = order.index(target_step)
        except ValueError:  # pragma: no cover - defensive
            cutoff = len(order) - 1
        keep_ids = set(order[: cutoff + 1])
        for rec in man.steps:
            if rec.id not in keep_ids:
                rec.status = M.PENDING
                rec.base_sha = None
                rec.session_id = None
                rec.ended = None
        man.status = M.RUN_PARKED
        man.current_step = None

    # ---- internals ----------------------------------------------------------
    def _phase_boundary_sha(self, man: Manifest, phase: int) -> str | None:
        prefix = f"P{phase}"
        match = None
        for commit in man.commits:
            head = commit.phase.split(".")[0]  # P3.1 -> P3
            if head == prefix:
                match = commit.sha
        return match

    def _drive(self, layout, run_dir, pipeline, man, *, use_judge, adapter_factory,
               extra_context, clock) -> str:
        if not use_judge:
            orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env={},
                                      adapter_factory=adapter_factory,
                                      extra_context=extra_context, clock=clock)
            status = orch.drive()
        else:
            status = self._with_judge(man, run_dir, lambda env: self._orchestrator(
                layout, run_dir, pipeline, man, judge_env=env,
                adapter_factory=adapter_factory, extra_context=extra_context,
                clock=clock).drive())
        self._maybe_draft_pr(layout, run_dir, man, status)
        return status

    def _maybe_draft_pr(self, layout, run_dir, man, status: str) -> None:
        """Draft runs/<slug>/PR.md at final-gate pass (FR-9.8); never opens it.

        Owned by the RunManager (not the orchestrator) because PR.md is a
        slug-dir deliverable a human edits and commits — opening and pushing
        stay human actions (PRD §2.2).

        PR.md is a REQUIRED final-gate artifact (FR-9.8), so a failure to render
        it is not swallowed (review F-005): the error is recorded as a manifest
        warning, persisted, and re-raised. Fail closed and data over inference —
        a completed run never silently returns RUN_DONE with the deliverable
        missing and no trace of why.
        """
        if status != M.RUN_DONE:
            return
        from gauntlet.engine.pr import write_pr_draft

        try:
            write_pr_draft(layout.slug_dir, run_dir, man, self.writer)
        except Exception as exc:
            man.warnings.append(
                f"FR-9.8 PR.md draft failed at final-gate pass: {exc!r}"
            )
            man.write_atomic(run_dir / "manifest.json")
            raise

    def _with_judge(self, man, run_dir, fn):
        judge_model = None
        if "judge_llm" in self.config.agents:
            judge_model = self.config.agents["judge_llm"].model
        judge = ManagedJudge(
            policy_path=self.repo_root / "policy.yaml",
            audit_path=run_dir / "judge-audit.jsonl",
            run_id=man.run_id,
            judge_model=judge_model,
            repo_root=self.repo_root,  # the fixed path boundary (notes #29)
        )
        env = judge.start()
        try:
            return fn(env)
        finally:
            judge.stop()
            # The judge stopped, so its audit log is fully flushed — fold any
            # LLM-classifier spend it recorded into the manifest (review F-003).
            self._merge_judge_usage(man, run_dir)

    def _merge_judge_usage(self, man: Manifest, run_dir: Path) -> None:
        """Fold judge LLM-classifier spend into the manifest (review F-003).

        The judge runs as a separate process and records each LLM-rung
        decision's usage in ``judge-audit.jsonl``. Without this merge that spend
        never reaches ``manifest.totals``/``agent_usage``, so it is excluded from
        both total run cost and the per-profile table — and the FR-3 acceptance
        check ("judge/triage/retro each < 5% of total") cannot be measured.

        Idempotent: the ``judge_llm`` total is recomputed from the FULL audit on
        every call and only the delta is applied to ``totals``. A run that parks
        and resumes (or steps through several gates) appends to the same audit
        and re-runs this merge, so judge spend is never double counted.
        """
        from gauntlet.adapters.base import Usage

        audit_path = run_dir / "judge-audit.jsonl"
        if not audit_path.exists():
            return
        agg = M.UsageTotals()
        saw_usage = False
        for line in audit_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:  # a torn final line is not fatal here
                continue
            recorded = entry.get("usage")
            if not recorded:
                continue
            saw_usage = True
            agg.add(Usage(**recorded))
        if not saw_usage:
            return
        prior = man.agent_usage.get("judge_llm") or M.UsageTotals()
        delta = Usage(
            input_tokens=agg.input_tokens - prior.input_tokens,
            output_tokens=agg.output_tokens - prior.output_tokens,
            cached_input_tokens=agg.cached_input_tokens - prior.cached_input_tokens,
            cost_usd=(None if agg.cost_usd is None
                      else agg.cost_usd - (prior.cost_usd or 0.0)),
        )
        man.totals.add(delta)
        man.agent_usage["judge_llm"] = agg
        man.write_atomic(run_dir / "manifest.json")

    def _approve_drive(self, layout, run_dir, pipeline, man, gate, notes, env,
                       adapter_factory):
        orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env=env,
                                  adapter_factory=adapter_factory)
        status = orch.approve_gate(gate, notes)
        self._maybe_draft_pr(layout, run_dir, man, status)
        return status

    def _orchestrator(self, layout, run_dir, pipeline, man, *, judge_env,
                      adapter_factory=None, extra_context=None, clock=None) -> Orchestrator:
        kwargs = dict(
            repo_root=self.repo_root,
            run_dir=run_dir,
            artifact_root=layout.slug_dir,
            config=self.config,
            pipeline=pipeline,
            manifest=man,
            writer=self.writer,
            judge_env=judge_env,
            adapter_factory=adapter_factory,
            extra_context=extra_context or {},
        )
        if clock is not None:
            kwargs["clock"] = clock
        return Orchestrator(**kwargs)

    # Every prompt-template reference a step can carry, so the manifest records
    # the exact version of the whole prompt set a run used (FR-5.6 / the P5
    # "versioned prompt set" deliverable) — not just the `prompt:` author/commit
    # templates, but the adversarial_cycle's review/triage/fix/confirm overrides.
    _PROMPT_REF_KEYS = (
        "prompt", "review_prompt", "rereview_prompt", "triage_prompt",
        "fix_prompt", "confirm_prompt",
        # retrospective + proposal-synthesis templates (FR-6.2/6.3): versioned
        # like every other prompt, so a retro proposal that edits them shows up
        # in the next run's manifest hashes (FR-6 acceptance).
        "retro_prompt", "synthesis_prompt",
    )

    def _prompt_hashes(self, pipeline) -> dict[str, str]:
        from gauntlet.engine.cycle import CYCLE_PROMPT_DEFAULTS
        from gauntlet.engine.pipeline import content_hash

        hashes: dict[str, str] = {}

        def record(ref: str | None) -> None:
            if ref and ref not in hashes:
                path = self.repo_root / ref
                if path.exists():
                    hashes[ref] = content_hash(path.read_text())

        # Judge policy is a versioned, retro-tunable asset (FR-6.3): record its
        # content hash so an approved policy proposal provably changes the next
        # run's manifest, exactly as an approved prompt proposal does (FR-6
        # acceptance — "the next run uses the new version, visible in the
        # manifest's prompt/policy hashes").
        record("policy.yaml")

        for step in pipeline.all_steps():
            for key in self._PROMPT_REF_KEYS:
                record(step.get(key))
            # An adversarial_cycle loads default templates for every role the
            # pipeline leaves unspecified (rereview/triage/fix/confirm), and those
            # files steer behavior — so hash the EFFECTIVE path for each role,
            # override or default, not just the refs spelled out in the YAML
            # (review F-002; FR-5.6 reproducibility).
            if step.type == "adversarial_cycle":
                for key, default_ref in CYCLE_PROMPT_DEFAULTS.items():
                    record(step.get(key) or default_ref)
        return hashes

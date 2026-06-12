"""Run lifecycle: new / run / status / approve / reject / resume / abort / rollback.

Glue between the CLI and the :class:`Orchestrator`. Owns the on-disk layout
(FR-4.1), the entry contract (FR-10.1), branch management (FR-9.1), the
engine-managed judge lifecycle (FR-7.1), and guarded rollback (FR-9.9 /
review F-010).
"""

from __future__ import annotations

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
        return orch.approve_gate(gate, notes)

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
            return orch.drive()
        return self._with_judge(man, run_dir, lambda env: self._orchestrator(
            layout, run_dir, pipeline, man, judge_env=env,
            adapter_factory=adapter_factory, extra_context=extra_context,
            clock=clock).drive())

    def _with_judge(self, man, run_dir, fn):
        judge_model = None
        if "judge_llm" in self.config.agents:
            judge_model = self.config.agents["judge_llm"].model
        judge = ManagedJudge(
            policy_path=self.repo_root / "policy.yaml",
            audit_path=run_dir / "judge-audit.jsonl",
            run_id=man.run_id,
            judge_model=judge_model,
        )
        env = judge.start()
        try:
            return fn(env)
        finally:
            judge.stop()

    def _approve_drive(self, layout, run_dir, pipeline, man, gate, notes, env,
                       adapter_factory):
        orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env=env,
                                  adapter_factory=adapter_factory)
        return orch.approve_gate(gate, notes)

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

    def _prompt_hashes(self, pipeline) -> dict[str, str]:
        from gauntlet.engine.pipeline import content_hash

        hashes: dict[str, str] = {}
        for step in pipeline.all_steps():
            ref = step.get("prompt")
            if ref:
                path = self.repo_root / ref
                if path.exists():
                    hashes[ref] = content_hash(path.read_text())
        return hashes

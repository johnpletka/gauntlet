"""JobSupervisor — own and supervise console-launched runs (P3, FR-6/FR-7 seed).

The supervisor turns "launch a run from the console" into the *same*
``gauntlet run`` a human would type (D1/FR-10.1), with three jobs:

1. **Run-dir allocation handshake (FR-6.1a).** Pre-allocate the child's
   ``run_id`` so we know ``run_dir`` *before* launch, pass it as
   ``--run-id``, and place the captured log + ``job.json`` under
   ``run_dir/.serve/`` from the first byte (so even a crash-before-manifest is
   diagnosable and never a phantom owned run).
2. **Lifecycle.** Launch / reap (process-group kill) owned children
   (:class:`RunProcess`); resolve which on-disk runs are *owned* vs *observed*
   for the FR-1.4 badge, with PID-reuse-safe liveness (FR-7.2).
3. **Worktree-lock surfacing (FR-10.5).** Read the engine's ``.driving.lock``
   (read-only — the *enforcement* is the engine lock, never a UI heuristic) so
   the console can show a holder as **running (external)** and disable
   Launch/Resume/Approve repo-wide while it is held.

The supervisor never imports the orchestrator and holds no authoritative run
state — disk does (D2). Its in-memory registry only tracks children it launched
*this session* (a convenience for reaping); everything else is re-derivable
from ``.serve/job.json`` on restart (P4).
"""

from __future__ import annotations

import json
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.engine.config import RunConfig
from gauntlet.engine.run import DRIVING_LOCK_NAME
from gauntlet.procident import ProcessIdentity, process_is_alive
from gauntlet.web.jobproc import JOB_FILENAME, SERVE_DIRNAME, JobRecord, RunProcess


def _utc_stamp() -> str:
    # Matches the engine's run-id stamp format (engine/run.py `_utc_stamp`) so a
    # pre-allocated id is indistinguishable from an engine-minted one.
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


@dataclass
class Job:
    """A discovered owned run (one ``.serve/job.json`` on disk)."""

    slug: str
    run_id: str
    run_dir: Path
    record: JobRecord

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def has_manifest(self) -> bool:
        return self.manifest_path.exists()

    def is_live(self) -> bool:
        return self.record.is_live()

    def classify(self) -> str:
        """Coarse P3 state of an owned run; P4 refines re-attach/interrupted.

        - ``failed_launch`` — no manifest and the child is dead: it died before
          the engine wrote any run state (FR-6.1a). The captured log under
          ``.serve/`` is still readable, so the failure is diagnosable, and the
          run never appears in the manifest-keyed run list (no phantom owned run).
        - ``starting`` — no manifest yet but the child is still alive.
        - ``live`` — manifest present and the child is the original live process.
        - ``exited`` — manifest present, child gone (parked/done/interrupted —
          the manifest is authoritative; P4 adds re-attach classification).
        """
        if not self.has_manifest:
            return "starting" if self.is_live() else "failed_launch"
        return "live" if self.is_live() else "exited"


@dataclass
class LockInfo:
    """A read-only view of the worktree ``.driving.lock`` (FR-10.5 surface)."""

    slug: str
    run_id: str | None
    pid: int
    live: bool


class JobSupervisor:
    """Owns console-launched runs; read-only over their on-disk identity."""

    def __init__(
        self,
        repo_root: Path,
        config: RunConfig | None = None,
        *,
        python: str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.config = config or self._load_config(self.repo_root)
        self.python = python
        # Children launched THIS session, keyed by slug (one active run/slug).
        # A convenience for reaping; not authoritative — disk is (D2).
        self._procs: dict[str, RunProcess] = {}

    @staticmethod
    def _load_config(repo_root: Path) -> RunConfig:
        # An absent config degrades to defaults (a bare repo must still serve);
        # a present-but-malformed one fails closed (mirrors RunStore.from_repo).
        try:
            return RunConfig.load(repo_root / ".gauntlet/config.yaml")
        except FileNotFoundError:
            return RunConfig()

    # ---- layout --------------------------------------------------------------
    @property
    def run_root(self) -> Path:
        return self.repo_root / self.config.run_root

    def _slug_dir(self, slug: str) -> Path:
        return self.run_root / slug

    def _run_dir(self, slug: str, run_id: str) -> Path:
        return self._slug_dir(slug) / run_id

    def _active_run_id(self, slug: str) -> str:
        pointer = self._slug_dir(slug) / "active-run.txt"
        if not pointer.exists():
            raise FileNotFoundError(
                f"no active run for slug {slug!r} (no active-run.txt)"
            )
        rid = pointer.read_text().strip()
        if not rid:
            raise FileNotFoundError(f"empty active-run pointer for slug {slug!r}")
        return rid

    # ---- launch (FR-6.1/6.1a) ------------------------------------------------
    def _allocate_run_id(self, slug: str) -> str:
        """Pre-allocate a fresh, single-use run id (FR-6.1a)."""
        stamp = _utc_stamp()
        run_id = f"run-{stamp}"
        suffix = 1
        while self._run_dir(slug, run_id).exists():
            run_id = f"run-{stamp}-{suffix}"
            suffix += 1
        return run_id

    def launch_run(
        self, slug: str, *, pipeline: str | None = None, no_judge: bool = False
    ) -> RunProcess:
        """Launch ``gauntlet run <slug> --run-id <pre-allocated> [...]`` (FR-6.1)."""
        run_id = self._allocate_run_id(slug)
        run_dir = self._run_dir(slug, run_id)
        flags = ["--run-id", run_id]
        if pipeline:
            flags += ["--pipeline", pipeline]
        if no_judge:
            # Exposed only as the existing unsafe-testing flag, defaulted off;
            # the UI warns when used (FR-6.5). The engine still owns the judge.
            flags.append("--no-judge")
        rp = RunProcess(
            verb="run",
            slug=slug,
            run_id=run_id,
            run_dir=run_dir,
            repo_root=self.repo_root,
            flags=flags,
            python=self.python,
        )
        rp.start()
        self._procs[slug] = rp
        return rp

    # ---- abort (FR-6.2/6.4) --------------------------------------------------
    def abort(self, slug: str) -> RunProcess:
        """Stop a live owned driver, then mark the run aborted via the CLI verb.

        ``gauntlet abort`` is not a driving verb (it does not take the worktree
        lock), so it can record the aborted status even if a killed driver left
        a stale lock behind — that lock is reclaimed by the next driving verb.
        """
        run_id = self._active_run_id(slug)
        run_dir = self._run_dir(slug, run_id)
        self._stop_live(slug, run_dir)
        # Control = sanctioned CLI verb (FR-10.1). record_job=False so this quick
        # control child does not clobber the run's owned-run job.json.
        rp = RunProcess(
            verb="abort",
            slug=slug,
            run_id=run_id,
            run_dir=run_dir,
            repo_root=self.repo_root,
            python=self.python,
            record_job=False,
        )
        rp.start()
        rp.wait(timeout=30)
        return rp

    def _stop_live(self, slug: str, run_dir: Path) -> None:
        """Reap a live owned driver: in-memory handle if we have one, else the
        recorded process group from ``job.json`` (restart-safe)."""
        rp = self._procs.get(slug)
        if rp is not None and rp.poll() is None:
            rp.stop()
            return
        # No in-memory handle (e.g. after a server restart): use job.json's pgid,
        # but only if it is the original live process (PID-reuse-safe, FR-7.2).
        rec = self._read_job(run_dir)
        if rec is not None and rec.is_live():
            RunProcess._signal_group(rec.pgid, signal.SIGTERM)
            RunProcess._signal_group(rec.pgid, signal.SIGKILL)

    # ---- owned/observed discovery (FR-1.4/FR-7.1) ----------------------------
    @staticmethod
    def _read_job(run_dir: Path) -> JobRecord | None:
        jp = run_dir / SERVE_DIRNAME / JOB_FILENAME
        try:
            return JobRecord.from_json(jp.read_text())
        except (OSError, FileNotFoundError):
            return None

    def jobs(self) -> list[Job]:
        """Every owned run discovered from ``.serve/job.json`` (FR-7.1 seed)."""
        out: list[Job] = []
        root = self.run_root
        if not root.exists():
            return out
        for slug_dir in sorted(root.iterdir()):
            if not slug_dir.is_dir() or slug_dir.name.startswith("."):
                continue
            for run_dir in sorted(slug_dir.glob("run-*")):
                rec = self._read_job(run_dir)
                if rec is not None:
                    out.append(Job(slug_dir.name, run_dir.name, run_dir, rec))
        return out

    def is_owned(self, slug: str, run_id: str) -> bool:
        return (self._run_dir(slug, run_id) / SERVE_DIRNAME / JOB_FILENAME).exists()

    def is_attached(self, slug: str, run_id: str) -> bool:
        """Owned **and** the recorded process is the original live one (FR-7.2)."""
        rec = self._read_job(self._run_dir(slug, run_id))
        return rec is not None and rec.is_live()

    # ---- worktree lock surface (FR-10.5) -------------------------------------
    def driving_lock(self) -> LockInfo | None:
        """The current worktree-lock holder, if any (read-only surface).

        Returns ``None`` when no lockfile exists or it is unparseable. ``live``
        is the PID-reuse-safe liveness of the holder; the console disables
        repo-wide controls only while a holder is **live** (a stale lock is the
        engine's to reclaim, not the UI's).
        """
        path = self.run_root / DRIVING_LOCK_NAME
        try:
            data = json.loads(path.read_text())
        except (OSError, FileNotFoundError, ValueError):
            return None
        try:
            pid = int(data["pid"])
        except (KeyError, TypeError, ValueError):
            return None
        identity = ProcessIdentity.from_dict(data.get("proc_identity"))
        return LockInfo(
            slug=data.get("slug", "?"),
            run_id=data.get("run_id"),
            pid=pid,
            live=process_is_alive(pid, identity),
        )


__all__ = ["JobSupervisor", "Job", "LockInfo"]

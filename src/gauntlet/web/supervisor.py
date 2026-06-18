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
import logging
import secrets
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import (
    RUN_ABORTED,
    RUN_DONE,
    RUN_FAILED,
    Manifest,
)
from gauntlet.engine.run import DRIVING_LOCK_NAME, safe_run_segment
from gauntlet.procident import ProcessIdentity, process_is_alive
from gauntlet.web.jobproc import JOB_FILENAME, SERVE_DIRNAME, JobRecord, RunProcess

log = logging.getLogger(__name__)

# Terminal run states are read-only to the abort control path (review F-002),
# mirroring web.store._TERMINAL_RUN_STATES / engine _TERMINAL_RUN_STATES.
_TERMINAL_RUN_STATES = frozenset({RUN_DONE, RUN_ABORTED, RUN_FAILED})

# Re-attach dispositions (P4, FR-7.1/7.2). A fresh server (no in-memory handles)
# re-discovers every owned run from its `.serve/job.json` and classifies it into
# exactly one of these — the server holds no authoritative state of its own (D2):
#   - REATTACHED    — the recorded process is still the original live one
#                     (PID-reuse-safe match, FR-7.2); re-adopt it (its captured
#                     log is on disk for tailing, control rides job.json's pgid).
#   - INTERRUPTED   — owned but dead/unverifiable AND a non-terminal manifest:
#                     an orphan whose recovery is `resume`, the *same* path as a
#                     `kill -9`'d run (FR-7.3). The stale sidecar is removed so it
#                     falls back to that observed/resume path.
#   - COMPLETED     — owned, dead, and the manifest is terminal (done/aborted/
#                     failed): nothing to recover; the sidecar is kept so the run
#                     keeps its owned badge for history (FR-1.4).
#   - FAILED_LAUNCH — owned, dead, and NO manifest: the child died before the
#                     engine wrote any run state (FR-6.1a). The captured log stays
#                     diagnosable; it is never a phantom owned run in the list.
REATTACHED = "reattached"
INTERRUPTED = "interrupted"
COMPLETED = "completed"
FAILED_LAUNCH = "failed_launch"


class ControlRefused(RuntimeError):
    """A control verb refused the target run (fail closed, FR-5.3/review F-002).

    Carries the HTTP status the control surface should return: 404 when there
    is no run to act on, 409 when the run's state makes the verb meaningless
    (observed-not-owned for abort, already-terminal, no live driver, etc.).
    """

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class ControlFailed(RuntimeError):
    """A sanctioned ``gauntlet <verb>`` control child failed (review F-003).

    A non-zero exit or a timeout from a quick control verb (abort/reject) must
    never be reported to the operator as success (project fail-closed rule for
    unexpected external command exits).
    """


class AbortRefused(ControlRefused):
    """The P3 abort path refused the target run (review F-002).

    The P3 abort verb only stops a live, console-owned, non-terminal run; an
    observed/missing/terminal/driverless run fails closed."""


class AbortFailed(ControlFailed):
    """The sanctioned ``gauntlet abort`` child failed (review F-003)."""


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

    def recovery_disposition(self, manifest: Manifest | None, *, live: bool) -> str:
        """The P4 re-attach disposition of this owned run (FR-7.1/7.2).

        A **pure** classifier — no side effects, so it is table-testable in
        isolation. ``live`` is the caller's PID-reuse-safe liveness verdict
        (computed once, since on macOS it shells out to ``ps``); ``manifest`` is
        the run's manifest or ``None`` when absent/unreadable. The four outcomes
        are documented on the module-level disposition constants. Fail-closed by
        construction: an unverifiable/dead process with a non-terminal manifest
        is always an :data:`INTERRUPTED` orphan, never a spurious re-attach.
        """
        if live:
            return REATTACHED
        if manifest is None:
            return FAILED_LAUNCH
        if manifest.status in _TERMINAL_RUN_STATES:
            return COMPLETED
        return INTERRUPTED


@dataclass
class RecoveryOutcome:
    """One owned run's re-attach result at server startup (P4, FR-7.1)."""

    slug: str
    run_id: str
    run_dir: Path
    disposition: str

    @property
    def resume_available(self) -> bool:
        """True for an interrupted orphan — recovery is ``resume`` (FR-7.3)."""
        return self.disposition == INTERRUPTED


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
        # Containment first (FR-10.1 / review F-001): the slug flows straight
        # into the run-root path below, so refuse a traversal/separator segment
        # before any sidecar or log is written.
        safe_run_segment(slug, kind="slug")
        run_id = self._allocate_run_id(slug)
        run_dir = self._run_dir(slug, run_id)
        # Single-use reservation token (FR-6.1a / review F-005): written under
        # `run_dir/.serve/` and passed as `--reservation-token` so the child
        # engine may adopt the pre-created run dir but no leftover dir can be
        # reused without it.
        token = secrets.token_hex(16)
        flags = ["--run-id", run_id, "--reservation-token", token]
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
            reservation_token=token,
        )
        rp.start()
        self._procs[slug] = rp
        return rp

    # ---- abort (FR-6.2/6.4) --------------------------------------------------
    def abort(self, slug: str) -> RunProcess:
        """Stop a live owned driver, then mark the run aborted via the CLI verb.

        Fails closed (review F-002): the P3 abort path only stops a run this
        console **owns**, that is currently **live/attached**, and whose
        manifest is **non-terminal**. Observed, missing, or already-terminal
        runs raise :class:`AbortRefused` (the caller maps it to 404/409) so the
        verb can never mutate run history it did not launch, nor an outcome that
        is already recorded.

        ``gauntlet abort`` is not a driving verb (it does not take the worktree
        lock), so it can record the aborted status even if a killed driver left
        a stale lock behind — that lock is reclaimed by the next driving verb.
        """
        safe_run_segment(slug, kind="slug")  # FR-10.1 / review F-001
        try:
            run_id = self._active_run_id(slug)
        except FileNotFoundError as exc:
            raise AbortRefused(str(exc), status_code=404) from exc
        run_dir = self._run_dir(slug, run_id)
        # Ownership: only runs the console launched (have a `.serve/job.json`)
        # are abortable here; an observed CLI-started run is read-only (F-002).
        if not self.is_owned(slug, run_id):
            raise AbortRefused(
                f"run {slug}/{run_id} is observed, not console-owned; the P3 "
                "abort path only stops runs this console launched",
                status_code=409,
            )
        # Non-terminal: terminal history is read-only (F-002).
        man = self._load_manifest(run_dir)
        if man is not None and man.status in _TERMINAL_RUN_STATES:
            raise AbortRefused(
                f"run {slug}/{run_id} is already {man.status}; terminal history "
                "is read-only",
                status_code=409,
            )
        # Liveness: the P3 abort path requires a live attached driver to stop.
        if not self.is_attached(slug, run_id):
            raise AbortRefused(
                f"run {slug}/{run_id} has no live attached driver to abort",
                status_code=409,
            )
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
        # Fail closed on the sanctioned child (review F-003): a non-zero exit or
        # a timeout must not be reported to the operator as success.
        try:
            code = rp.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            rp.stop()
            raise AbortFailed(
                f"`gauntlet abort` for {slug}/{run_id} timed out after 30s; "
                f"see {rp.log_path}"
            ) from exc
        if code != 0:
            raise AbortFailed(
                f"`gauntlet abort` for {slug}/{run_id} exited {code}; "
                f"see {rp.log_path}"
            )
        return rp

    # ---- approve / resume / reject (FR-6.2, the human-value verbs) ----------
    def _control_run_dir(self, slug: str) -> tuple[str, Path]:
        """Resolve the slug's active run dir for a control verb (FR-6.1a).

        For approve/resume/reject the run dir already exists, so we resolve it
        from the slug's ``active-run.txt`` (no pre-allocation). A missing pointer
        fails closed → :class:`ControlRefused` (404)."""
        safe_run_segment(slug, kind="slug")  # FR-10.1 / review F-001
        try:
            run_id = self._active_run_id(slug)
        except FileNotFoundError as exc:
            raise ControlRefused(str(exc), status_code=404) from exc
        return run_id, self._run_dir(slug, run_id)

    def approve(
        self, slug: str, *, gate: str | None = None, notes: str | None = None
    ) -> RunProcess:
        """Launch ``gauntlet approve`` as a long-lived owned driver (FR-6.2/R6).

        Approve *drives the rest of the run*, so it is handled with the full
        :class:`RunProcess` lifecycle (it records ``job.json`` and becomes the
        console-owned driver), not a quick RPC. Control = the sanctioned CLI verb
        a human would type (FR-10.1); the engine takes the worktree lock and
        gates every tool call inside the child."""
        run_id, run_dir = self._control_run_dir(slug)
        flags: list[str] = []
        if gate:
            flags += ["--gate", gate]
        if notes:
            flags += ["--notes", notes]
        return self._launch_driver("approve", slug, run_id, run_dir, flags)

    def resume(self, slug: str) -> RunProcess:
        """Launch ``gauntlet resume`` as a long-lived owned driver (FR-6.2/R6)."""
        run_id, run_dir = self._control_run_dir(slug)
        return self._launch_driver("resume", slug, run_id, run_dir, [])

    def _launch_driver(
        self, verb: str, slug: str, run_id: str, run_dir: Path, flags: list[str]
    ) -> RunProcess:
        rp = RunProcess(
            verb=verb,
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

    def reject(
        self, slug: str, notes: str, *, gate: str | None = None
    ) -> RunProcess:
        """Run ``gauntlet reject`` as a quick, fail-closed control child.

        Reject fails a parked gate and is **not** a driving verb (it takes no
        worktree lock — it only marks the gate failed), so unlike approve/resume
        it is a short-lived RPC we wait on. A non-zero exit or timeout raises
        :class:`ControlFailed` so a failed reject never reads as success
        (review F-003). ``notes`` is required by the CLI verb (FR-4.4)."""
        if not notes or not notes.strip():
            raise ControlRefused("reject requires notes (FR-4.4)", status_code=400)
        run_id, run_dir = self._control_run_dir(slug)
        flags = ["--notes", notes]
        if gate:
            flags += ["--gate", gate]
        rp = RunProcess(
            verb="reject",
            slug=slug,
            run_id=run_id,
            run_dir=run_dir,
            repo_root=self.repo_root,
            flags=flags,
            python=self.python,
            record_job=False,
        )
        rp.start()
        try:
            code = rp.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            rp.stop()
            raise ControlFailed(
                f"`gauntlet reject` for {slug}/{run_id} timed out after 30s; "
                f"see {rp.log_path}"
            ) from exc
        if code != 0:
            raise ControlFailed(
                f"`gauntlet reject` for {slug}/{run_id} exited {code}; "
                f"see {rp.log_path}"
            )
        return rp

    @staticmethod
    def _load_manifest(run_dir: Path) -> Manifest | None:
        """The run's manifest, or ``None`` when absent/unreadable (pre-manifest
        starting runs have none yet, which is not itself terminal)."""
        try:
            return Manifest.load(run_dir / "manifest.json")
        except (OSError, FileNotFoundError, ValueError):
            return None

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

    # ---- re-attach / crash survival (P4, FR-7.1/7.2/7.3) ---------------------
    def reattach(self) -> list[RecoveryOutcome]:
        """Re-discover owned runs from disk and reconcile orphans (FR-7.1).

        Called on server startup (the console keeps **no** authoritative run
        state — disk does, D2): a fresh process re-scans every ``.serve/job.json``
        and classifies each run with the PID-reuse-safe liveness check (FR-7.2).
        Side effects are confined to the orphan case: an :data:`INTERRUPTED` run
        (dead/unverifiable driver + non-terminal manifest) has its **stale
        sidecar removed** so it falls back to the exact same recovery path a
        ``kill -9``'d run already has — ``resume`` (FR-7.3). The captured
        ``.serve/…log`` is deliberately left in place so the orphan stays
        diagnosable. Live runs are reported :data:`REATTACHED` and need no action
        (their captured log is on disk for tailing and control rides
        ``job.json``'s recorded pgid, both already restart-safe from P3).

        Returns one :class:`RecoveryOutcome` per discovered owned run, in slug /
        run-id order, so a caller (or the startup log) can report what happened.
        """
        outcomes: list[RecoveryOutcome] = []
        for job in self.jobs():
            # Narrow per-job best-effort (review F-001): one unreconcilable run
            # must not abort the whole re-discovery pass, but its failure is
            # surfaced (logged), never silently swallowed. A *scan*-level failure
            # (`self.jobs()` above) is deliberately NOT caught here — it
            # propagates so the server's startup reattach pass fails closed
            # rather than coming up with stale, unreconciled ownership state.
            try:
                # Liveness is computed exactly once per job (it may shell out to
                # `ps` on macOS) and threaded into the pure classifier.
                live = job.is_live()
                manifest = None if live else self._load_manifest(job.run_dir)
                disposition = job.recovery_disposition(manifest, live=live)
                if disposition == INTERRUPTED:
                    self._remove_stale_sidecar(job)
            except Exception as exc:  # narrow per-job best-effort (review F-001)
                log.warning(
                    "reattach skipped %s/%s: %s", job.slug, job.run_id, exc
                )
                continue
            outcomes.append(
                RecoveryOutcome(
                    slug=job.slug,
                    run_id=job.run_id,
                    run_dir=job.run_dir,
                    disposition=disposition,
                )
            )
            log.info(
                "reattach %s/%s -> %s", job.slug, job.run_id, disposition
            )
        return outcomes

    def _remove_stale_sidecar(self, job: Job) -> None:
        """Delete an orphan's dead ``job.json`` so recovery = ``resume`` (FR-7.3).

        Fail-soft: a removal error is logged and swallowed (a re-discovery scan
        must never crash the server). Only the sidecar is removed — the captured
        log stays for diagnosis. Any stale in-memory handle for the same run is
        dropped too (defensive — there are none right after a restart).
        """
        sidecar = job.run_dir / SERVE_DIRNAME / JOB_FILENAME
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - defensive (perms/races)
            log.warning("could not remove stale sidecar %s: %s", sidecar, exc)
        rp = self._procs.get(job.slug)
        if rp is not None and getattr(rp, "run_id", None) == job.run_id:
            del self._procs[job.slug]

    def reap(self) -> None:
        """Reap any console children that have exited this session (review F-004).

        Called on the list/refresh path before liveness is computed: an
        unwaited child becomes a zombie that still passes the PID-liveness
        check, so a finished owned run would otherwise keep showing as
        live/attached. Reaping collects exit status and closes the captured log.
        """
        for rp in list(self._procs.values()):
            rp.reap()

    def is_attached(self, slug: str, run_id: str) -> bool:
        """Owned **and** the recorded process is the original live one (FR-7.2).

        A child we launched this session is authoritative via its in-memory
        handle: once it has exited we report detached (and reap it) even if a
        not-yet-reaped zombie would still fool the PID-liveness check on
        ``.serve/job.json`` (review F-004).
        """
        rp = self._procs.get(slug)
        if rp is not None and rp.run_id == run_id:
            if rp.poll() is None:
                return True
            rp.reap()
            return False
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


__all__ = [
    "JobSupervisor",
    "Job",
    "LockInfo",
    "RecoveryOutcome",
    "ControlRefused",
    "ControlFailed",
    "AbortRefused",
    "AbortFailed",
    "REATTACHED",
    "INTERRUPTED",
    "COMPLETED",
    "FAILED_LAUNCH",
]

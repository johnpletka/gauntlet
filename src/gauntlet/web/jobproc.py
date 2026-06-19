"""RunProcess — a console-owned `gauntlet` CLI child (P3, FR-6.1/6.3/6.4).

A near-clone of :class:`gauntlet.engine.judgeproc.ManagedJudge`, but for a whole
run: the console launches the *same `gauntlet <verb>` a human would type* as a
``subprocess.Popen`` in its **own session/process group** (``start_new_session``),
captures its combined stdout/stderr to a log under the run dir, and reaps the
**whole group** on ``stop()`` (a run spawns agent CLIs + a judge as
grandchildren). This is design decision D1: own runs as subprocess children of
the CLI, never in-process, so every CLI guarantee (judge gating, read-only
reviewer, no-push-to-main) rides along and a crash is survivable.

Identity lives on disk, not in server memory (D2): a sidecar ``.serve/job.json``
records ``{pid, pgid, verb, slug, run_id, started_at, log_path, proc_identity}``
where ``proc_identity`` is the FR-7.2 PID-reuse-safe process-creation identity,
so a restarted server (P4) can re-discover and safely re-attach.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.engine.run import RESERVATION_FILENAME, SERVE_DIRNAME
from gauntlet.procident import ProcessIdentity, process_is_alive, read_process_identity

JOB_FILENAME = "job.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_atomic(path: Path, text: str) -> None:
    """Temp-file + ``os.replace`` so a reader never sees a torn sidecar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".job-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


@dataclass
class JobRecord:
    """The on-disk ``.serve/job.json`` (FR-6.4)."""

    pid: int
    pgid: int
    verb: str
    slug: str
    run_id: str
    started_at: str
    log_path: str
    proc_identity: dict | None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "JobRecord | None":
        try:
            data = json.loads(text)
            return cls(
                pid=int(data["pid"]),
                pgid=int(data.get("pgid", data["pid"])),
                verb=data["verb"],
                slug=data["slug"],
                run_id=data["run_id"],
                started_at=data.get("started_at", ""),
                log_path=data.get("log_path", ""),
                proc_identity=data.get("proc_identity"),
            )
        except (ValueError, KeyError, TypeError):
            return None

    def identity(self) -> ProcessIdentity | None:
        return ProcessIdentity.from_dict(self.proc_identity)

    def is_live(self) -> bool:
        """PID-reuse-safe liveness of the recorded process (FR-7.2)."""
        return process_is_alive(self.pid, self.identity())


class RunProcess:
    """One console-launched `gauntlet <verb> <slug>` child process.

    ``run_dir`` is **pre-allocated** by the supervisor (FR-6.1a) so the captured
    log and ``job.json`` land under ``run_dir/.serve/`` from the first byte —
    even if the child crashes before the engine writes its manifest.
    """

    def __init__(
        self,
        *,
        verb: str,
        slug: str,
        run_id: str,
        run_dir: Path,
        repo_root: Path,
        flags: list[str] | None = None,
        python: str | None = None,
        record_job: bool = True,
        reservation_token: str | None = None,
    ) -> None:
        self.verb = verb
        self.slug = slug
        self.run_id = run_id
        self.run_dir = Path(run_dir)
        self.repo_root = Path(repo_root)
        self.flags = list(flags or [])
        self.python = python or sys.executable
        self.record_job = record_job
        self.reservation_token = reservation_token
        self.serve_dir = self.run_dir / SERVE_DIRNAME
        self.log_path = self.serve_dir / f"{verb}.log"
        self.job_path = self.serve_dir / JOB_FILENAME
        self.reservation_path = self.serve_dir / RESERVATION_FILENAME
        self._proc: subprocess.Popen | None = None
        self._log_fh = None
        self._pgid: int | None = None

    def argv(self) -> list[str]:
        # The exact command a human would type — control = sanctioned CLI verb
        # (FR-10.1). `python -m gauntlet` so it never depends on the console
        # script being on PATH (mirrors ManagedJudge).
        return [self.python, "-m", "gauntlet", self.verb, self.slug, *self.flags]

    def start(self) -> "RunProcess":
        self.serve_dir.mkdir(parents=True, exist_ok=True)
        # The run-id reservation handshake (FR-6.1a / review F-005): write the
        # single-use token *before* launch so the child engine can verify,
        # race-free, that this pre-created run dir is its own fresh reservation
        # (it is also passed as `--reservation-token`). Without it the engine
        # refuses to reuse any pre-existing run dir.
        if self.reservation_token is not None:
            _write_atomic(self.reservation_path, self.reservation_token)
        # Self-ignoring run-dir .gitignore so the captured log never dirties the
        # worktree (FR-6.3) — written before launch in case the engine's own
        # orchestrator (which also writes `*`) hasn't run yet. Idempotent.
        gitignore = self.run_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("*\n")
        # Unbuffered binary append so a live tail (FR-3.3) sees bytes promptly.
        self._log_fh = open(self.log_path, "ab", buffering=0)
        self._proc = subprocess.Popen(
            self.argv(),
            cwd=str(self.repo_root),
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            # Own session/process group so stop() can reap the whole tree and so
            # the child outlives transient parent state (D1).
            start_new_session=True,
        )
        try:
            self._pgid = os.getpgid(self._proc.pid)
        except OSError:  # pragma: no cover - platform without process groups
            self._pgid = self._proc.pid
        if self.record_job:
            self._write_job()
        return self

    def _write_job(self) -> None:
        pid = self._proc.pid
        identity = read_process_identity(pid)
        record = JobRecord(
            pid=pid,
            pgid=self._pgid if self._pgid is not None else pid,
            verb=self.verb,
            slug=self.slug,
            run_id=self.run_id,
            started_at=_utc_now_iso(),
            log_path=str(self.log_path),
            proc_identity=identity.to_dict() if identity is not None else None,
        )
        _write_atomic(self.job_path, record.to_json())

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def pgid(self) -> int | None:
        return self._pgid

    def poll(self) -> int | None:
        return self._proc.poll() if self._proc else None

    def reap(self) -> bool:
        """If the child has exited, ``wait()`` it and close the captured log.

        A console child left unwaited becomes a zombie that still passes the
        FR-7.2 PID-liveness check (``os.kill(pid, 0)`` + start identity), so a
        completed owned run would keep reporting as live/attached (review
        F-004). Reaping on the list/refresh path collects the exit status so
        liveness reflects reality. Returns ``True`` when the child is gone (or
        was never started); a still-running child is left untouched.
        """
        if self._proc is None:
            self._close_log()
            return True
        if self._proc.poll() is None:
            return False
        try:
            self._proc.wait(timeout=0)
        except subprocess.TimeoutExpired:  # pragma: no cover - just polled dead
            pass
        self._close_log()
        return True

    def wait(self, timeout: float | None = None) -> int:
        if self._proc is None:
            raise RuntimeError("RunProcess.wait() before start()")
        return self._proc.wait(timeout)

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    def stop(self, *, term_timeout: float = 5.0) -> None:
        """Reap the whole process group: terminate → wait → kill (FR-6.4)."""
        if self._proc is None:
            self._close_log()
            return
        pgid = self._pgid if self._pgid is not None else self._proc.pid
        self._signal_group(pgid, signal.SIGTERM)
        try:
            self._proc.wait(term_timeout)
        except subprocess.TimeoutExpired:
            self._signal_group(pgid, signal.SIGKILL)
            try:
                self._proc.wait(5.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                pass
        self._close_log()

    @staticmethod
    def _signal_group(pgid: int, sig: int) -> None:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already gone / not ours — fail-soft

    def _close_log(self) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:  # pragma: no cover - defensive
                pass
            self._log_fh = None


__all__ = [
    "RunProcess",
    "JobRecord",
    "SERVE_DIRNAME",
    "JOB_FILENAME",
    "RESERVATION_FILENAME",
]

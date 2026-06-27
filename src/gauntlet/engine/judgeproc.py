"""Engine-managed judge lifecycle (FR-7.1, plan P3; supersedes BOOTSTRAP-NOTES #12).

``gauntlet run`` starts the localhost judge as a subprocess, injects the per-run
``GAUNTLET_JUDGE_*`` env so the agent CLIs' PreToolUse hooks gate against it
(live session gating, the dogfood deferred from P2), and stops it on exit. The
judge is launched via ``python -m gauntlet judge serve`` so it does not depend
on the console script being on PATH.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.judge.hook_client import (
    MODE_ENV_VAR,
    REPO_ROOT_ENV_VAR,
    RUN_ID_ENV_VAR,
    STEP_ID_ENV_VAR,
    URL_ENV_VAR,
)
from gauntlet.judge.service import TOKEN_ENV_VAR
from gauntlet.procident import read_process_identity

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# The run-dir sidecar recording the per-run judge's endpoint + reap identity
# (FR-5, §6.2). Gitignored (the run dir self-ignores `*`), mode 0600.
JUDGE_RECORD_NAME = "judge.json"

# Every GAUNTLET_* var the run touches — snapshotted at start, restored at stop
# so nothing (incl. the per-step GAUNTLET_STEP_ID set by the orchestrator) leaks
# into the parent session (review F-009).
_MANAGED_ENV_VARS = (
    TOKEN_ENV_VAR,
    URL_ENV_VAR,
    MODE_ENV_VAR,
    RUN_ID_ENV_VAR,
    STEP_ID_ENV_VAR,
    REPO_ROOT_ENV_VAR,
)


def classifier_disabled_warning() -> str:
    """The stderr warning emitted when the engine starts a judge with no
    classifier model (FR-7.2). Mirrors the standalone ``gauntlet judge serve``
    warning, but its remedy is config-shaped (add a ``judge_llm`` profile), since
    the engine derives the model from config — not a CLI flag. Surfaced loudly so
    a fail-closed judge is never a silent surprise (data over inference)."""
    return (
        "gauntlet: WARNING — engine-managed judge has no judge_llm model; the "
        "LLM classifier is DISABLED, so any command the policy.yaml fast-path "
        "does not match will FAIL CLOSED. Add a `judge_llm` agent profile to "
        ".gauntlet/config.yaml to enable classification."
    )


@dataclass(frozen=True)
class JudgeRecord:
    """The on-disk content of ``<run_dir>/judge.json`` (§6.2).

    Two distinct concerns share the record:

    - ``pid``/``pgid``/``proc_identity``/``host`` are the FR-6 reap-identity
      datums — PID-reuse-safe, so cleanup verbs can prove a judge is *ours on
      this host* before signalling it (P2). ``proc_identity`` is ``None`` on an
      unsupported platform → never reaped (procident's fail-closed contract).
    - ``port``/``url``/``token``/``run_id`` are what the monitor (§6.3) needs to
      wire itself to *this run's* judge. ``token`` is the **per-run judge token**
      (the value the judge accepts on ``GAUNTLET_JUDGE_TOKEN``) — never the
      console ``serve`` token; the two credentials are distinct (§6.2).
    """

    pid: int
    pgid: int
    proc_identity: dict | None
    host: str
    port: int
    url: str
    token: str
    run_id: str
    started_at: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "pgid": self.pgid,
                "proc_identity": self.proc_identity,
                "host": self.host,
                "port": self.port,
                "url": self.url,
                "token": self.token,
                "run_id": self.run_id,
                "started_at": self.started_at,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "JudgeRecord | None":
        """Rehydrate a record, or ``None`` if missing/malformed (fail-closed).

        A corrupt or partial sidecar round-trips to ``None`` so every reader
        (reap gate, monitor wiring) treats it as *absent* rather than acting on
        half a record (§6.4 fail-closed; data over inference)."""
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            pid = int(data["pid"])
            pgid = int(data["pgid"])
            port = int(data["port"])
            url = data["url"]
            token = data["token"]
            run_id = data["run_id"]
            host = data["host"]
            started_at = data["started_at"]
        except (KeyError, TypeError, ValueError):
            return None
        # The string-typed fields must be strings; `bool` is an int subclass but
        # not a str, so it is rejected here (mirrors _LockRecord.from_json).
        if not all(isinstance(v, str) for v in (url, token, run_id, host, started_at)):
            return None
        proc_identity = data.get("proc_identity")
        if proc_identity is not None and not isinstance(proc_identity, dict):
            return None
        return cls(
            pid=pid,
            pgid=pgid,
            proc_identity=proc_identity,
            host=host,
            port=port,
            url=url,
            token=token,
            run_id=run_id,
            started_at=started_at,
        )


def read_judge_record(run_dir: Path) -> JudgeRecord | None:
    """Read ``<run_dir>/judge.json``, or ``None`` if absent/unreadable/malformed.

    The single reader the monitor launchers (P3/P4) and the reap gate (P2)
    consume. A missing or corrupt file is ``None`` — treated as *no live judge*,
    which fails closed everywhere it is used (a normal prompted session, or no
    reap signal)."""
    try:
        text = (run_dir / JUDGE_RECORD_NAME).read_text()
    except (OSError, FileNotFoundError):
        return None
    return JudgeRecord.from_json(text)


def operator_session_env(record: JudgeRecord) -> dict[str, str]:
    """The operator-session env for a monitor wired to a live judge (§6.3).

    Sets ``GAUNTLET_RUN_ID`` + the judge ``URL``/``TOKEN`` and
    ``GAUNTLET_JUDGE_MODE=interactive``, and **deliberately omits**
    ``GAUNTLET_STEP_ID`` — its *absence* is what marks the operator's own session
    (validating §1.3): the judge classifies a ``step_id``-absent caller as the
    operator (broad auto-allow), a ``step_id``-present caller as an in-run agent
    (push/PR denied), purely on ``step_id`` presence (FR-10). Consumers that hit
    the degraded (no-live-judge) path set **none** of these — never partial.

    Mode is ``interactive`` because the monitor IS a human at the keyboard. The
    run's judge is ephemeral — it dies the instant the run ends (cleanly or, as
    seen live, on an early-step failure that reaps the judge ~seconds in). Under
    the default ``unattended`` mode the hook would then fail closed on the
    now-unreachable judge and deny EVERY operator tool call — even read-only
    diagnostics like ``gauntlet status`` — bricking the supervisor session with a
    misleading "judge unreachable" error. ``interactive`` degrades that liveness
    failure to an ``ask`` prompt instead (review F-004): the operator becomes the
    backstop and can still investigate why the run died. A judge *deny* (live
    judge, refused action) still denies in both modes, so this never loosens
    policy on a reachable judge."""
    return {
        RUN_ID_ENV_VAR: record.run_id,
        URL_ENV_VAR: record.url,
        TOKEN_ENV_VAR: record.token,
        MODE_ENV_VAR: "interactive",
    }


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _write_judge_record_best_effort(path: Path, record: JudgeRecord) -> None:
    """Write ``judge.json`` atomically with mode ``0600``; never raise (FR-5.2).

    Best-effort: a write failure is logged to stderr and the run proceeds — the
    judge is up in-process regardless, so its absence on disk degrades the
    monitor/reap helpers (which fail closed) but never blocks the run."""
    try:
        tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(record.to_json())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
    except OSError as exc:
        print(
            f"gauntlet: WARNING — could not write {path} ({exc}); the judge is "
            "up in-process but its endpoint/reap record is unavailable on disk.",
            file=sys.stderr,
        )


class ManagedJudge:
    def __init__(
        self,
        *,
        policy_path: Path,
        audit_path: Path,
        run_id: str,
        judge_model: str | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        mode: str = "unattended",
        startup_timeout_s: float = 15.0,
        repo_root: Path | None = None,
        run_dir: Path | None = None,
    ) -> None:
        self.policy_path = policy_path
        self.audit_path = audit_path
        self.run_id = run_id
        self.judge_model = judge_model
        self.host = host
        self.port = port
        self.mode = mode
        self.startup_timeout_s = startup_timeout_s
        self.repo_root = repo_root
        self.run_dir = run_dir
        self.token = secrets.token_urlsafe(32)
        self._proc: subprocess.Popen | None = None
        self._env_snapshot: dict[str, str | None] = {}

    @property
    def url(self) -> str:
        # Follows host:port; the port may have moved off the default if it was
        # already taken when we spawned (see start()).
        return f"http://{self.host}:{self.port}"

    def env(self) -> dict[str, str]:
        """The per-run judge env to inject for agent subprocesses (FR-7.3)."""
        env = {
            TOKEN_ENV_VAR: self.token,
            URL_ENV_VAR: self.url,
            MODE_ENV_VAR: self.mode,
            RUN_ID_ENV_VAR: self.run_id,
        }
        if self.repo_root is not None:
            # The run's fixed repo boundary for the hooks' path checks — the
            # agent's floating cwd must never define "the repository tree"
            # (P5 deny-loop, notes #29).
            env[REPO_ROOT_ENV_VAR] = str(self.repo_root)
        return env

    def start(self) -> dict[str, str]:
        # A `gauntlet run` always starts its OWN run-scoped judge so the run's
        # policy.yaml, audit file (run_dir/judge-audit.jsonl), repo_root, and
        # model are authoritative and the decisions are reconstructable from the
        # run dir (FR-4/FR-7; PR #16 review). It never attaches to an arbitrary
        # standalone judge, which would enforce a different policy and write its
        # audit elsewhere. The operator's own session reuses their judge via the
        # PreToolUse hook + GAUNTLET_JUDGE_* env — a path that does not go
        # through ManagedJudge at all, so it is unaffected.
        if not self.judge_model:
            print(classifier_disabled_warning(), file=sys.stderr)
        # If the default port is already taken (a stale judge from a killed run,
        # the operator's own standalone judge, or an unrelated listener), move to
        # a free ephemeral port rather than colliding and failing startup. The
        # hooks learn the URL from the injected env, so the port need not be fixed.
        port_free = self._port_is_free(self.host, self.port)
        if not port_free:
            taken = self.port
            self.port = self._free_port(self.host)
            print(
                f"gauntlet: judge port {taken} is in use; starting the "
                f"engine-managed judge on free port {self.port} instead.",
                file=sys.stderr,
            )
        elif os.environ.get(TOKEN_ENV_VAR):
            # No port conflict: adopt the operator's global GAUNTLET_JUDGE_TOKEN
            # (e.g. exported in ~/.zshenv) instead of minting a fresh per-run
            # token, so a manually-started judge and external tooling that share
            # that token agree with the run's judge. We still start our OWN
            # run-scoped judge (PR #16) — only the token VALUE is reused. On a
            # port CONFLICT we keep the minted token: the default-port listener
            # is someone else's judge, and reusing the global token for our
            # moved-port judge would be misleading. (Trade-off: a long-lived
            # global token rotates less often than a per-run one.)
            self.token = os.environ[TOKEN_ENV_VAR]
        child_env = {**os.environ, TOKEN_ENV_VAR: self.token}
        argv = [
            sys.executable,
            "-m",
            "gauntlet",
            "judge",
            "serve",
            "--policy",
            str(self.policy_path),
            "--audit",
            str(self.audit_path),
            "--host",
            self.host,
            "--port",
            str(self.port),
            # Bind the judge to THIS run (FR-10.2): /decide rejects any request
            # whose run_id does not match, so a valid-token caller for a different
            # (or absent) run is never classified+allowed as if it were ours.
            "--run-id",
            self.run_id,
        ]
        if self.judge_model:
            argv += ["--judge-model", self.judge_model]
        if self.repo_root is not None:
            # Authoritative path boundary in the SERVICE itself (#31): the
            # judge never depends on GAUNTLET_REPO_ROOT reaching the agent's
            # hook subprocess (which it didn't, on claude — #29). The env var
            # stays as belt-and-suspenders for the hook fallback.
            argv += ["--repo-root", str(self.repo_root)]
        # Isolate the judge in its own session/process group (F-001): without
        # this the judge inherits the driver/console process group, so the
        # recorded ``pgid`` (``os.getpgid(pid)``) would name the driver's group
        # and the FR-6 reaper's group-wide SIGTERM/SIGKILL could kill unrelated
        # sibling processes. A new session makes ``pgid == pid``, so cleanup only
        # ever signals the judge's own group (FR-6.3).
        self._proc = subprocess.Popen(argv, env=child_env, start_new_session=True)
        self._await_healthy()
        # The judge answered healthz: record its endpoint + reap identity for the
        # monitor (§6.3) and the cleanup verbs (FR-6). Best-effort (FR-5.2): a
        # write failure never blocks the run — the judge is up in-process.
        self._write_record()
        # Snapshot prior values of every managed var so stop() restores exactly.
        self._env_snapshot = {v: os.environ.get(v) for v in _MANAGED_ENV_VARS}
        env = self.env()
        os.environ.update(env)  # the bootstrap session + child agents see it
        return env

    def _record_path(self) -> Path | None:
        return None if self.run_dir is None else self.run_dir / JUDGE_RECORD_NAME

    def _write_record(self) -> None:
        """Persist ``judge.json`` (§6.2) for the judge subprocess (FR-5.1)."""
        path = self._record_path()
        if path is None or self._proc is None:
            return  # no run dir (e.g. a standalone judge) → no sidecar
        pid = self._proc.pid
        try:
            pgid = os.getpgid(pid)
        except OSError:
            pgid = pid  # fall back to the pid; a non-positive pgid is fail-closed in P2
        identity = read_process_identity(pid)
        record = JudgeRecord(
            pid=pid,
            pgid=pgid,
            proc_identity=identity.to_dict() if identity is not None else None,
            host=socket.gethostname(),
            port=self.port,
            url=self.url,
            token=self.token,
            run_id=self.run_id,
            started_at=_utc_stamp(),
        )
        _write_judge_record_best_effort(path, record)

    def _remove_record(self) -> None:
        """Remove ``judge.json`` on a clean stop (FR-5.1). Best-effort."""
        path = self._record_path()
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(
                f"gauntlet: WARNING — could not remove {path} ({exc}); a stale "
                "judge record may remain for an already-stopped judge.",
                file=sys.stderr,
            )

    @staticmethod
    def _port_is_free(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return True
            except OSError:
                return False

    @staticmethod
    def _free_port(host: str) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, 0))
            return s.getsockname()[1]

    def _await_healthy(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_s
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    f"judge exited during startup (code {self._proc.returncode})"
                )
            try:
                with urllib.request.urlopen(f"{self.url}/healthz", timeout=1.0) as r:
                    if r.status == 200:
                        return
            except (urllib.error.URLError, OSError) as exc:
                last = exc
                time.sleep(0.1)
        self.stop()
        raise RuntimeError(f"judge did not become healthy in time: {last}")

    def stop(self) -> None:
        if self._proc is None:
            return
        # Restore every managed GAUNTLET_* var to its pre-run value (incl. the
        # per-step GAUNTLET_STEP_ID set by the orchestrator) — no env leak into
        # the parent session on success or failure (review F-009).
        for var, prior in (self._env_snapshot or {v: None for v in _MANAGED_ENV_VARS}).items():
            if prior is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = prior
        self._env_snapshot = {}
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=5.0)
        self._proc = None
        # Clean stop: the judge is down, so its sidecar must not outlive it (FR-5.1).
        # An orphaned judge (a killed/crashed driver that never reaches stop())
        # deliberately leaves the record behind for FR-6 reaping.
        self._remove_record()

    def __enter__(self) -> ManagedJudge:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

"""Engine-managed judge lifecycle (FR-7.1, plan P3; supersedes BOOTSTRAP-NOTES #12).

``gauntlet run`` starts the localhost judge as a subprocess, injects the per-run
``GAUNTLET_JUDGE_*`` env so the agent CLIs' PreToolUse hooks gate against it
(live session gating, the dogfood deferred from P2), and stops it on exit. The
judge is launched via ``python -m gauntlet judge serve`` so it does not depend
on the console script being on PATH.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from gauntlet.judge.hook_client import (
    MODE_ENV_VAR,
    REPO_ROOT_ENV_VAR,
    RUN_ID_ENV_VAR,
    STEP_ID_ENV_VAR,
    URL_ENV_VAR,
)
from gauntlet.judge.service import TOKEN_ENV_VAR

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

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
        self.token = secrets.token_urlsafe(32)
        self._proc: subprocess.Popen | None = None
        self._env_snapshot: dict[str, str | None] = {}

    @property
    def url(self) -> str:
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
        ]
        if self.judge_model:
            argv += ["--judge-model", self.judge_model]
        self._proc = subprocess.Popen(argv, env=child_env)
        self._await_healthy()
        # Snapshot prior values of every managed var so stop() restores exactly.
        self._env_snapshot = {v: os.environ.get(v) for v in _MANAGED_ENV_VARS}
        env = self.env()
        os.environ.update(env)  # the bootstrap session + child agents see it
        return env

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

    def __enter__(self) -> ManagedJudge:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

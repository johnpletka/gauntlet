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
import socket
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
        # Set when we attach to an externally-managed judge instead of spawning
        # one (see start()). We did not start it, so stop() must not kill it.
        self._external_url: str | None = None

    @property
    def url(self) -> str:
        # A reused external judge keeps its own URL; ours follows host:port (the
        # port may have moved off the default if it was already taken).
        return self._external_url or f"http://{self.host}:{self.port}"

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
        # Reuse an already-running judge if the environment advertises one
        # (GAUNTLET_JUDGE_URL + GAUNTLET_JUDGE_TOKEN) and it answers /healthz.
        # This is the "attach to a judge that's already up" path: an operator's
        # standalone `gauntlet judge serve`, or a judge a parent process started.
        # We adopt its url+token (so the per-run hooks gate against it), inject
        # only the per-run vars, and never stop it — we did not start it. The
        # operator opted in by exporting the token, so we trust that endpoint;
        # if it is the wrong judge the hook calls 401 and the run fails closed
        # (loud and recoverable), never silently ungated.
        if self._reuse_external():
            return self._inject_env()
        if not self.judge_model:
            print(classifier_disabled_warning(), file=sys.stderr)
        # Nothing to reuse → spawn our own. If the default port is already taken
        # (a stale judge from a killed run, or an unrelated listener), move to a
        # free ephemeral port rather than colliding and failing startup. The
        # hooks learn the URL from the injected env, so the port need not be fixed.
        if not self._port_is_free(self.host, self.port):
            taken = self.port
            self.port = self._free_port(self.host)
            print(
                f"gauntlet: judge port {taken} is in use; starting the "
                f"engine-managed judge on free port {self.port} instead.",
                file=sys.stderr,
            )
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
        if self.repo_root is not None:
            # Authoritative path boundary in the SERVICE itself (#31): the
            # judge never depends on GAUNTLET_REPO_ROOT reaching the agent's
            # hook subprocess (which it didn't, on claude — #29). The env var
            # stays as belt-and-suspenders for the hook fallback.
            argv += ["--repo-root", str(self.repo_root)]
        self._proc = subprocess.Popen(argv, env=child_env)
        self._await_healthy()
        return self._inject_env()

    def _inject_env(self) -> dict[str, str]:
        # Snapshot prior values of every managed var so stop() restores exactly.
        self._env_snapshot = {v: os.environ.get(v) for v in _MANAGED_ENV_VARS}
        env = self.env()
        os.environ.update(env)  # the bootstrap session + child agents see it
        return env

    def _reuse_external(self) -> bool:
        """Attach to an env-advertised, healthy judge instead of spawning.

        Returns True (and adopts its url+token) when both GAUNTLET_JUDGE_URL and
        GAUNTLET_JUDGE_TOKEN are set and the URL answers /healthz; False
        otherwise (so the caller spawns its own). /healthz is unauthenticated,
        matching the spawn path's own readiness probe.
        """
        url = os.environ.get(URL_ENV_VAR)
        token = os.environ.get(TOKEN_ENV_VAR)
        if not url or not token:
            return False
        if not self._healthz_ok(url):
            return False
        self._external_url = url
        self.token = token
        print(
            f"gauntlet: reusing the externally-managed judge at {url} "
            "(GAUNTLET_JUDGE_URL/TOKEN are set); not starting a new one.",
            file=sys.stderr,
        )
        return True

    @staticmethod
    def _healthz_ok(url: str) -> bool:
        """True iff ``url`` answers /healthz with 200. Its own seam so the reuse
        path is unit-testable without driving real HTTP."""
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=2.0) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

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
        # Restore every managed GAUNTLET_* var to its pre-run value (incl. the
        # per-step GAUNTLET_STEP_ID set by the orchestrator) — no env leak into
        # the parent session on success or failure (review F-009). This must run
        # for a reused external judge too (no _proc of our own), or the per-run
        # mode/run_id/repo_root we injected would leak past the run.
        if self._env_snapshot:
            for var, prior in self._env_snapshot.items():
                if prior is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = prior
            self._env_snapshot = {}
        # We only kill a judge we started. A reused external judge (and the bare
        # never-started case) leaves _proc None and is left running.
        if self._proc is None:
            return
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

"""Console registry — discover/reuse one console per worktree (P7, FR-12.4).

``gauntlet serve`` and ``gauntlet run --watch`` coordinate through a single
on-disk registry at ``<run_root>/.console.json`` so a second ``--watch`` (or a
second ``serve``) **reuses** a live console instead of duplicating it. The file
records the console's identity (``pid``/``pgid``/``proc_identity``), where it
listens (``host``/``port``/``url``), a non-reversible ``token_fingerprint`` (the
token itself is never persisted), and its ``log_path``.

Discovery reuses the recorded console **iff** it is PID-reuse-safe live (FR-7.2)
*and* its ``/healthz`` answers on the recorded host/port; anything else is
**stale** and reclaimed by the booting process. A console booted by ``--watch``
is **detached** (``start_new_session=True``) so it outlives the foreground run,
with stdout/stderr going to ``<run_root>/.console.log``. There is no
auto-shutdown in v1 (the console persists for history review, FR-12.3); a clean
exit removes the registry entry, a crash leaves a stale one the next discovery
reclaims.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from gauntlet.procident import (
    ProcessIdentity,
    process_is_alive,
    read_process_identity,
)
from gauntlet.web.auth import token_fingerprint

CONSOLE_REGISTRY_NAME = ".console.json"
CONSOLE_LOG_NAME = ".console.log"


class ConsoleBootError(RuntimeError):
    """The console could not be booted/reused (port conflict, no healthz)."""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConsoleRecord:
    """The on-disk ``<run_root>/.console.json`` (FR-12.4)."""

    pid: int
    pgid: int
    proc_identity: dict | None
    host: str
    port: int
    url: str
    token_fingerprint: str
    started_at: str
    log_path: str

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ConsoleRecord | None":
        try:
            data = json.loads(text)
            return cls(
                pid=int(data["pid"]),
                pgid=int(data.get("pgid", data["pid"])),
                proc_identity=data.get("proc_identity"),
                host=data["host"],
                port=int(data["port"]),
                url=data.get("url", ""),
                token_fingerprint=data.get("token_fingerprint", ""),
                started_at=data.get("started_at", ""),
                log_path=data.get("log_path", ""),
            )
        except (ValueError, KeyError, TypeError):
            return None

    def identity(self) -> ProcessIdentity | None:
        return ProcessIdentity.from_dict(self.proc_identity)

    def is_live(self) -> bool:
        """PID-reuse-safe liveness of the recorded console process (FR-7.2)."""
        return process_is_alive(self.pid, self.identity())


@dataclass
class ConsoleHandle:
    """The result of :func:`ensure_console` — a reused or freshly-booted console."""

    host: str
    port: int
    url: str
    reused: bool
    token: str | None = None  # the serve token, only when we booted it
    pid: int | None = None
    # True when reusing a live console whose token_fingerprint disagrees with the
    # caller's supplied GAUNTLET_WEB_TOKEN (FR-12.4): the running console is
    # authoritative and is *not* restarted; the caller just notes the mismatch.
    token_mismatch: bool = False

    @property
    def login_url(self) -> str:
        return f"{self.url}/login"


def registry_path(run_root: Path) -> Path:
    return run_root / CONSOLE_REGISTRY_NAME


def console_log_path(run_root: Path) -> Path:
    return run_root / CONSOLE_LOG_NAME


def read_registry(run_root: Path) -> ConsoleRecord | None:
    try:
        text = registry_path(run_root).read_text()
    except (OSError, FileNotFoundError):
        return None
    return ConsoleRecord.from_json(text)


def _ensure_registry_gitignore(run_root: Path) -> None:
    """Keep the console's run-root artifacts out of ``git status`` (clean-worktree).

    The registry (``.console.json``) and the detached-console log
    (``.console.log``) live at the run root, a sibling of the slug dirs —
    untracked, they would dirty the worktree at the next review handoff and break
    the central clean-worktree invariant. The console owns these files, so it
    owns ignoring them (mirroring :class:`RunProcess`, which writes its own run-dir
    ``.gitignore``). Additive + idempotent: it only *appends* missing entries, so
    the engine's own ``_ensure_run_root_gitignore`` (which preserves existing
    lines) and these coexist regardless of write order.
    """
    gi = run_root / ".gitignore"
    existing = gi.read_text().split() if gi.exists() else []
    wanted = [".gitignore", CONSOLE_REGISTRY_NAME, CONSOLE_LOG_NAME]
    if any(w not in existing for w in wanted):
        lines = list(dict.fromkeys(existing + wanted))  # dedup, stable order
        gi.write_text("\n".join(lines) + "\n")


def write_registry(run_root: Path, record: ConsoleRecord) -> None:
    """Atomically publish the registry (``os.replace``) so a reader never tears."""
    run_root.mkdir(parents=True, exist_ok=True)
    _ensure_registry_gitignore(run_root)
    path = registry_path(run_root)
    fd, tmp = tempfile.mkstemp(dir=str(run_root), prefix=".console-", suffix=".tmp")
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


def remove_registry(run_root: Path) -> None:
    """Best-effort removal of our registry entry on clean console exit (FR-12.4)."""
    try:
        registry_path(run_root).unlink()
    except FileNotFoundError:
        pass
    except OSError:  # pragma: no cover - defensive (perms/races)
        pass


def build_record(
    *, host: str, port: int, token: str, log_path: Path
) -> ConsoleRecord:
    """Build the registry record for *this* process (the console writes its own).

    ``proc_identity`` and ``pid``/``pgid`` are of the current process, so the
    PID-reuse-safe liveness check (FR-7.2) compares like-for-like on discovery.
    """
    pid = os.getpid()
    try:
        pgid = os.getpgid(pid)
    except OSError:  # pragma: no cover - platform without process groups
        pgid = pid
    identity = read_process_identity(pid)
    return ConsoleRecord(
        pid=pid,
        pgid=pgid,
        proc_identity=identity.to_dict() if identity is not None else None,
        host=host,
        port=port,
        url=f"http://{host}:{port}",
        token_fingerprint=token_fingerprint(token),
        started_at=_utc_now_iso(),
        log_path=str(log_path),
    )


def healthz_ok(host: str, port: int, *, timeout: float = 1.0) -> bool:
    """True iff the recorded console answers its unauthenticated ``/healthz``."""
    try:
        resp = httpx.get(f"http://{host}:{port}/healthz", timeout=timeout)
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    try:
        return resp.json().get("status") == "ok"
    except ValueError:
        return False


def port_is_free(host: str, port: int) -> bool:
    """True iff ``(host, port)`` can be bound right now (no live listener)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def is_reusable(record: ConsoleRecord | None) -> bool:
    """Reuse a recorded console iff it is live (FR-7.2) **and** healthz answers."""
    return (
        record is not None
        and record.is_live()
        and healthz_ok(record.host, record.port)
    )


def wait_for_healthz(host: str, port: int, *, timeout: float = 15.0) -> bool:
    """Poll ``/healthz`` until the freshly-booted console answers or we time out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if healthz_ok(host, port, timeout=0.5):
            return True
        time.sleep(0.1)
    return False


def ensure_console(
    repo_root: Path,
    run_root: Path,
    *,
    host: str,
    port: int,
    token: str | None = None,
    python: str | None = None,
    boot_timeout: float = 15.0,
) -> ConsoleHandle:
    """Reuse the live console, else boot a detached one (FR-12.1/12.4).

    Returns a :class:`ConsoleHandle`. On reuse, ``token`` is ``None`` (the
    running console keeps its own token, which we do not know — only its
    fingerprint) and we surface that console's existing login URL. On boot, we
    mint/inherit the serve token, launch ``gauntlet serve`` **detached** so it
    outlives the foreground run (FR-12.2), wait for ``/healthz``, and return its
    URL + token. A port already held by an **unrelated** process (the registry is
    not reusable yet the port is bound) fails closed (FR-12.4) — we never
    silently pick a different port the printed URL would then mismatch.
    """
    supplied = token or os.environ.get("GAUNTLET_WEB_TOKEN")
    existing = read_registry(run_root)
    if is_reusable(existing):
        assert existing is not None  # narrowed by is_reusable
        # Token compatibility (FR-12.4): a reused console keeps its OWN token; if
        # the caller supplied a different one we never restart the running
        # console — we only flag the mismatch so the caller can note it.
        mismatch = bool(
            supplied
            and existing.token_fingerprint
            and token_fingerprint(supplied) != existing.token_fingerprint
        )
        return ConsoleHandle(
            host=existing.host,
            port=existing.port,
            url=existing.url,
            reused=True,
            token_mismatch=mismatch,
        )

    # Not reusable: the recorded console (if any) is stale and will be
    # overwritten by the booting child's own registry write. Before launching,
    # make sure the port is ours to take.
    if not port_is_free(host, port):
        raise ConsoleBootError(
            f"cannot start console: {host}:{port} is in use by an unrelated "
            "process and no live gauntlet console is registered there; free the "
            "port or pass a different --port (FR-12.4, fail-closed)"
        )

    # Mint a token when neither an explicit token nor GAUNTLET_WEB_TOKEN exists,
    # so the detached child uses *our* token (not one it generates privately) and
    # the handle can surface it — otherwise the default `run --watch` flow prints
    # /login but never the token needed to sign in (FR-12.1).
    resolved_token = supplied or secrets.token_urlsafe(32)
    env = dict(os.environ)
    env["GAUNTLET_WEB_TOKEN"] = resolved_token

    log_path = console_log_path(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    # Ignore the console log before we open it (the child also ensures this on its
    # registry write, but the log file appears first) so it never dirties git.
    _ensure_registry_gitignore(run_root)
    log_fh = open(log_path, "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [
                python or sys.executable,
                "-m",
                "gauntlet",
                "serve",
                "--host",
                host,
                "--port",
                str(port),
            ],
            cwd=str(repo_root),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            # Detached so the console outlives the foreground `run` (FR-12.2).
            start_new_session=True,
            env=env,
        )
    finally:
        log_fh.close()

    if not wait_for_healthz(host, port, timeout=boot_timeout):
        raise ConsoleBootError(
            f"console booted (pid {proc.pid}) but /healthz did not answer within "
            f"{boot_timeout:g}s; see {log_path}"
        )

    # The child wrote its own registry on startup; read it back for the exact
    # token (we passed it via env, so we know it) and URL.
    booted = read_registry(run_root)
    url = booted.url if booted is not None else f"http://{host}:{port}"
    return ConsoleHandle(
        host=host,
        port=port,
        url=url,
        reused=False,
        token=resolved_token,
        pid=proc.pid,
    )


__all__ = [
    "ConsoleRecord",
    "ConsoleHandle",
    "ConsoleBootError",
    "CONSOLE_REGISTRY_NAME",
    "CONSOLE_LOG_NAME",
    "registry_path",
    "console_log_path",
    "read_registry",
    "write_registry",
    "remove_registry",
    "build_record",
    "healthz_ok",
    "port_is_free",
    "is_reusable",
    "wait_for_healthz",
    "ensure_console",
]

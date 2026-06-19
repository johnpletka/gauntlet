"""Host the console app over loopback (P1, FR-10.4/FR-11.1; P7 registry).

The `gauntlet serve` command resolves config, validates it is inside a git repo
(fail-closed), builds the :class:`RunStore` read model, mints/uses a per-serve
token, and runs uvicorn bound to loopback only — the same posture as
``judge/runner.py``.

P7 adds the **console registry** (FR-12.4): the serving process records itself at
``<run_root>/.console.json`` so a later ``gauntlet run --watch`` (or a second
``serve``) discovers and **reuses** it instead of duplicating a console. The
entry is removed on clean exit; a crash leaves a stale entry the next discovery
reclaims. The browser authenticates via the ``/login`` cookie exchange — the
token is printed at startup but never placed in the URL (FR-10.4).
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from gauntlet.engine import gitops
from gauntlet.web.registry import (
    build_record,
    console_log_path,
    is_reusable,
    read_registry,
    remove_registry,
    write_registry,
)
from gauntlet.web.service import TOKEN_ENV_VAR, create_app
from gauntlet.web.store import RunStore
from gauntlet.web.supervisor import JobSupervisor

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class NonLoopbackHostError(ValueError):
    """The console refuses to bind a non-loopback host (FR-10.4)."""


def assert_loopback(host: str) -> None:
    """Fail closed unless ``host`` is a loopback address (FR-10.4, §2.2)."""
    if host not in LOOPBACK_HOSTS:
        raise NonLoopbackHostError(
            f"console refuses to bind non-loopback host {host!r} "
            "(FR-10.4: localhost only, like the judge)"
        )


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def login_url(host: str, port: int) -> str:
    """The startup/`--watch` login URL — token-free (FR-10.4).

    Points at ``/login`` where the operator pastes the serve token into a POST
    form; the token is **never** placed in a URL, history, or the SSE handshake.
    """
    return f"http://{host}:{port}/login"


def serve(
    repo_root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
    enable_handoff: bool | None = None,
) -> None:  # pragma: no cover - exercised via the live contract suite
    import uvicorn

    assert_loopback(host)
    repo_root = repo_root.resolve()
    if not gitops.is_git_repo(repo_root):
        raise SystemExit(
            f"gauntlet serve must run inside a git repository; {repo_root} is not "
            "one (FR-11.1, fail-closed)"
        )
    resolved_token = token or os.environ.get(TOKEN_ENV_VAR) or generate_token()
    os.environ[TOKEN_ENV_VAR] = resolved_token
    supervisor = JobSupervisor(repo_root)
    store = RunStore.from_repo(repo_root, supervisor=supervisor)
    run_root = store.run_root_dir
    # FR-4.7 hand-off is opt-in: a `serve --enable-handoff` flag overrides the
    # `web.handoff` config key (default off). The console only assembles a
    # read-only prompt; it spawns nothing (D8).
    from gauntlet.web.config import web_config_from

    handoff_enabled = (
        enable_handoff
        if enable_handoff is not None
        else web_config_from(store.config).handoff
    )
    # Enable notifications (FR-9) for a real serve, with a deep-link base so the
    # desktop/Slack messages carry an absolute URL to /runs/<slug>. The configured
    # channels (web.notify) are wired off the watcher's event bus, fail-soft.
    app = create_app(
        store,
        token=resolved_token,
        supervisor=supervisor,
        handoff_enabled=handoff_enabled,
        notifications=True,
        base_url=f"http://{host}:{port}",
    )

    # Console registry (FR-12.4): a second `serve` (or one racing a `run --watch`
    # console) must REUSE a live console rather than try to bind the port it
    # already holds and crash. If a healthy reusable console is already
    # registered, report its existing URL/login URL and exit cleanly — we never
    # start a duplicate uvicorn on a port another console owns.
    existing = read_registry(run_root)
    if is_reusable(existing):
        assert existing is not None  # narrowed by is_reusable
        print(f"gauntlet console already listening on {existing.url}")
        print("reusing it; this serve will not start a second console.")
        # The login URL goes to stderr (token-free, FR-10.4); no token is printed
        # because a reused console keeps its own — we only hold its fingerprint.
        print(f"open {existing.url}/login", file=sys.stderr)
        return

    # No reusable console: own the registry entry and bind the port ourselves. A
    # port held by an *unrelated* process (not a live gauntlet console) makes
    # uvicorn.run fail closed below rather than silently picking another port.
    write_registry(
        run_root,
        build_record(
            host=host,
            port=port,
            token=resolved_token,
            log_path=console_log_path(run_root),
        ),
    )

    print(f"gauntlet console listening on http://{host}:{port}")
    print(f"{TOKEN_ENV_VAR}={resolved_token}")
    # The login URL goes to stderr so it never contaminates the token line on
    # stdout that tooling/operators scrape. It is token-free (FR-10.4).
    print(f"open {login_url(host, port)}", file=sys.stderr)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        # Remove the registry entry only if it is still ours (defensive against a
        # racing console that re-registered on the same path), so a clean exit
        # leaves no stale entry (FR-12.4).
        rec = read_registry(run_root)
        if rec is not None and rec.pid == os.getpid():
            remove_registry(run_root)


__all__ = [
    "assert_loopback",
    "generate_token",
    "login_url",
    "serve",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "NonLoopbackHostError",
]

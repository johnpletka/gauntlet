"""Host the console app over loopback (P1, FR-10.4/FR-11.1).

The `gauntlet serve` command resolves config, validates it is inside a git repo
(fail-closed), builds the :class:`RunStore` read model, mints/uses a per-serve
token, and runs uvicorn bound to loopback only — the same posture as
``judge/runner.py``.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

from gauntlet.engine import gitops
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


def login_url(host: str, port: int, token: str) -> str:
    """Convenience URL printed at startup (P1 bootstrap: token in the query).

    P7 replaces this with a ``/login`` POST-prefilled form so the token never
    rides in a URL; in P1 it is the judge-parity ``?token=`` bootstrap.
    """
    return f"http://{host}:{port}/?token={token}"


def serve(
    repo_root: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    token: str | None = None,
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
    app = create_app(store, token=resolved_token, supervisor=supervisor)
    print(f"gauntlet console listening on http://{host}:{port}")
    print(f"{TOKEN_ENV_VAR}={resolved_token}")
    # The convenience URL goes to stderr so it never contaminates the token line
    # on stdout that tooling/operators scrape.
    print(f"open {login_url(host, port, resolved_token)}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")


__all__ = [
    "assert_loopback",
    "generate_token",
    "login_url",
    "serve",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "NonLoopbackHostError",
]

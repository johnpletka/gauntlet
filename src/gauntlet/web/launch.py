"""Authenticated browser launch for ``run --watch`` / ``serve --resume`` (FR-1).

The console's whole ergonomic promise (PRD goal G1) is *zero manual paste*: from
a TTY, ``--watch`` should land the operator on an **already-authenticated**
console with no token to copy. This module builds the loopback ``?p=<token>`` URL
(FR-2) and opens the default browser to it on a TTY, always fail-soft: a browser
that cannot open, or a console reuse with no recoverable token, degrades to
surfacing the ``/login`` URL, never aborting the run (FR-1.2).
"""

from __future__ import annotations

import sys
import webbrowser
from collections.abc import Callable
from urllib.parse import urlencode

from gauntlet.web.auth import QUERY_TOKEN_PARAM
from gauntlet.web.registry import ConsoleHandle


def authenticated_url(handle: ConsoleHandle) -> str:
    """The loopback ``?p=`` URL when the serve token is known, else ``/login``.

    A known token (a console we booted, or a reused one whose registry persisted
    its token) yields an authenticated landing URL. A reuse with **no** token — a
    legacy pre-P5 ``.console.json`` that never persisted one — has nothing to
    authenticate with, so we surface the plain ``/login`` URL (FR-1.2 migration).
    """
    if handle.token:
        return f"{handle.url}/?{urlencode({QUERY_TOKEN_PARAM: handle.token})}"
    return handle.login_url


def open_authenticated(
    handle: ConsoleHandle,
    *,
    no_browser: bool = False,
    echo: Callable[[str], None] | None = None,
    isatty: Callable[[], bool] | None = None,
) -> str:
    """Print, and on a TTY open, the console URL — fail-soft (FR-1.1/FR-1.2).

    Opens the operator's default browser to the authenticated ``?p=`` URL **iff**
    ``no_browser`` is unset, stdout is a TTY, and a token is known. The URL is
    printed in all cases (FR-1.1). A ``webbrowser.open`` failure is swallowed and
    we fall back to surfacing the ``/login`` URL instead of an actionable ``?p=``
    one (FR-1.2). Returns the URL surfaced.
    """
    echo = echo or (lambda message: print(message))
    isatty = isatty or sys.stdout.isatty
    url = authenticated_url(handle)
    opened = False
    if not no_browser and isatty():
        try:
            webbrowser.open(url)
            opened = True
        except Exception as exc:  # fail-soft: never abort the run on a browser error
            # Don't leave the token sitting in a printed ?p= URL the operator must
            # hand-paste — surface /login so they sign in via the form (FR-1.2).
            url = handle.login_url
            echo(f"warning: could not open a browser ({exc})")
    if opened:
        echo(f"opened the console in your browser: {url}")
    else:
        echo(f"console: {url}")
    return url


__all__ = ["authenticated_url", "open_authenticated"]

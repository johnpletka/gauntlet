"""Authenticated browser launch (FR-1) — ``open_authenticated`` / ``authenticated_url``.

On a TTY (browser enabled, token known) the operator's browser opens to the
loopback ``?p=`` URL so there is nothing to paste (goal G1). With ``--no-browser``
or off a TTY it is not opened but the URL is still printed. Browser-open is
fail-soft: a ``webbrowser.open`` failure, or a console reuse with no recoverable
token, degrades to surfacing ``/login`` and never aborts the run (FR-1.2).
"""

from __future__ import annotations

import gauntlet.web.launch as launch
from gauntlet.web.launch import authenticated_url, open_authenticated
from gauntlet.web.registry import ConsoleHandle

URL = "http://127.0.0.1:8765"


def _handle(*, token: str | None, reused: bool = False) -> ConsoleHandle:
    return ConsoleHandle(host="127.0.0.1", port=8765, url=URL, reused=reused, token=token)


def _recorder():
    msgs: list[str] = []
    return msgs, msgs.append


# --- authenticated_url -------------------------------------------------------


def test_authenticated_url_carries_token_when_known():
    assert authenticated_url(_handle(token="tok-123")) == f"{URL}/?p=tok-123"


def test_authenticated_url_falls_back_to_login_without_token():
    assert authenticated_url(_handle(token=None)) == f"{URL}/login"


# --- open_authenticated ------------------------------------------------------


def test_opens_browser_on_tty_with_token(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launch.webbrowser, "open", lambda u: opened.append(u))
    msgs, echo = _recorder()
    url = open_authenticated(
        _handle(token="tok-123"), no_browser=False, echo=echo, isatty=lambda: True
    )
    assert opened == [f"{URL}/?p=tok-123"]  # called exactly once with the ?p= URL
    assert url == f"{URL}/?p=tok-123"


def test_no_browser_flag_does_not_open_but_prints(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launch.webbrowser, "open", lambda u: opened.append(u))
    msgs, echo = _recorder()
    open_authenticated(
        _handle(token="tok-123"), no_browser=True, echo=echo, isatty=lambda: True
    )
    assert opened == []  # not opened
    assert any("tok-123" in m for m in msgs)  # URL still printed


def test_non_tty_does_not_open_but_prints(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launch.webbrowser, "open", lambda u: opened.append(u))
    msgs, echo = _recorder()
    open_authenticated(
        _handle(token="tok-123"), no_browser=False, echo=echo, isatty=lambda: False
    )
    assert opened == []
    assert any("tok-123" in m for m in msgs)


def test_browser_failure_is_fail_soft_and_prints_login(monkeypatch):
    def boom(_u):
        raise RuntimeError("no display")

    monkeypatch.setattr(launch.webbrowser, "open", boom)
    msgs, echo = _recorder()
    # Does not raise; falls back to surfacing /login (not the actionable ?p= URL).
    url = open_authenticated(
        _handle(token="tok-123"), no_browser=False, echo=echo, isatty=lambda: True
    )
    assert url == f"{URL}/login"
    assert any("/login" in m for m in msgs)


def test_legacy_tokenless_reuse_surfaces_login(monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(launch.webbrowser, "open", lambda u: opened.append(u))
    msgs, echo = _recorder()
    # A reused console with no persisted token → /login, never an authenticated
    # ?p= URL (FR-1.2 migration). On a TTY it still opens, but to /login.
    url = open_authenticated(
        _handle(token=None, reused=True), no_browser=False, echo=echo,
        isatty=lambda: True,
    )
    assert opened == [f"{URL}/login"]
    assert url == f"{URL}/login"

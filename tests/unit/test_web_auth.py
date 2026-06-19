"""P7 durable auth — /login cookie exchange + session-bound CSRF (FR-10.4/10.6).

Covers the production auth posture that replaces the P1–P6 header/``?token=``
bootstrap: the browser exchanges the serve token for an ``HttpOnly`` session
cookie at ``POST /login`` (the token never appears in a URL), cookie-authenticated
state-changing POSTs require the session CSRF token and a same-origin Origin, and
``X-Gauntlet-Token`` API callers keep header parity (CSRF-exempt).
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.config import RunConfig
from gauntlet.web.auth import (
    COOKIE_NAME,
    CSRF_HEADER,
    SessionStore,
    is_same_origin,
    safe_next,
    token_fingerprint,
)
from gauntlet.web.service import TOKEN_HEADER, create_app
from gauntlet.web.store import RunStore

TOKEN = "p7-web-token-secret"


@pytest.fixture
def store(tmp_path) -> RunStore:
    (tmp_path / "runs").mkdir()
    return RunStore(tmp_path, RunConfig())


@pytest.fixture
def client(store: RunStore) -> TestClient:
    return TestClient(create_app(store, token=TOKEN))


def _login(client: TestClient, token: str = TOKEN, next_: str = "/"):
    return client.post(
        "/login", data={"token": token, "next": next_}, follow_redirects=False
    )


def _csrf_from(html: str) -> str:
    m = re.search(r'name="csrf-token" content="([^"]*)"', html)
    assert m, "no csrf meta in page"
    return m.group(1)


# --- /login exchange ---------------------------------------------------------


def test_login_form_is_unauthenticated(client: TestClient):
    assert client.get("/login").status_code == 200
    # /healthz too (the only other unauthenticated route).
    assert client.get("/healthz").status_code == 200


def test_login_bad_token_rejected(client: TestClient):
    resp = _login(client, token="wrong")
    assert resp.status_code == 401
    assert COOKIE_NAME not in resp.cookies


def test_login_sets_httponly_samesite_cookie(client: TestClient):
    resp = _login(client)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    sc = resp.headers["set-cookie"].lower()
    assert COOKIE_NAME in sc
    assert "httponly" in sc
    assert "samesite=strict" in sc
    # FR-10.4: the token is never placed in the redirect URL.
    assert TOKEN not in resp.headers["location"]


def test_login_next_is_sanitized_against_open_redirect(client: TestClient):
    # A protocol-relative / absolute `next` falls back to "/" (no open redirect).
    for evil in ("//evil.com", "https://evil.com", "javascript:alert(1)"):
        resp = _login(client, next_=evil)
        assert resp.headers["location"] == "/"
    # A legitimate local path is honoured.
    resp = _login(client, next_="/runs/demo")
    assert resp.headers["location"] == "/runs/demo"


def test_cookie_authenticates_pages_and_api(client: TestClient):
    _login(client)  # TestClient stores the cookie on the client
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/api/runs").status_code == 200


def test_token_never_appears_in_any_page_url(client: TestClient):
    _login(client)
    html = client.get("/").text
    # No ?token= / &token= anywhere, and the secret itself never embedded.
    assert "token=" not in html
    assert TOKEN not in html


# --- CSRF on cookie POSTs (FR-10.6) -----------------------------------------


def test_cookie_post_without_csrf_is_rejected(client: TestClient):
    _login(client)
    # No CSRF header → 403 (fails closed before reaching the handler).
    assert client.post("/api/runs", json={"slug": "demo"}).status_code == 403


def test_cookie_post_with_session_csrf_passes_auth(client: TestClient):
    _login(client)
    csrf = _csrf_from(client.get("/").text)
    # With the session CSRF header AND a same-origin Origin the request passes the
    # auth/CSRF gate; with no supervisor configured the handler returns 503.
    resp = client.post(
        "/api/runs",
        json={"slug": "demo"},
        headers={CSRF_HEADER: csrf, "Origin": "http://testserver"},
    )
    assert resp.status_code == 503


def test_cookie_post_without_origin_is_rejected(client: TestClient):
    # FR-10.6 fail-closed: a cookie POST that carries a valid CSRF token but no
    # Origin/Referer (the same-origin second factor) is rejected, not accepted.
    _login(client)
    csrf = _csrf_from(client.get("/").text)
    resp = client.post(
        "/api/runs", json={"slug": "demo"}, headers={CSRF_HEADER: csrf}
    )
    assert resp.status_code == 403


def test_cross_origin_cookie_post_rejected(client: TestClient):
    _login(client)
    csrf = _csrf_from(client.get("/").text)
    resp = client.post(
        "/api/runs",
        json={"slug": "demo"},
        headers={CSRF_HEADER: csrf, "Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_foreign_session_csrf_is_rejected(client: TestClient, store: RunStore):
    # A CSRF token minted for a *different* session must not validate this one.
    _login(client)
    other = SessionStore(TOKEN)
    _sid, foreign_csrf = other.create_session()
    resp = client.post(
        "/api/runs", json={"slug": "demo"}, headers={CSRF_HEADER: foreign_csrf}
    )
    assert resp.status_code == 403


def test_header_api_post_is_csrf_exempt(client: TestClient):
    # The X-Gauntlet-Token API path is not ambient and cannot be CSRF'd, so it
    # needs no CSRF token (FR-10.6) — it reaches the handler (503: no supervisor).
    resp = client.post(
        "/api/runs", json={"slug": "demo"}, headers={TOKEN_HEADER: TOKEN}
    )
    assert resp.status_code == 503


def test_logout_drops_session(client: TestClient):
    _login(client)
    csrf = _csrf_from(client.get("/").text)
    resp = client.post(
        "/logout",
        headers={CSRF_HEADER: csrf, "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # After logout the cookie is cleared and pages bounce back to /login.
    assert client.get("/", follow_redirects=False).status_code == 303


# --- pure helpers ------------------------------------------------------------


def test_csrf_rotates_per_login():
    sessions = SessionStore(TOKEN)
    _s1, c1 = sessions.create_session()
    _s2, c2 = sessions.create_session()
    assert c1 != c2


def test_safe_next_helper():
    assert safe_next("/runs/x") == "/runs/x"
    assert safe_next("//evil") == "/"
    assert safe_next("https://evil") == "/"
    assert safe_next(None) == "/"
    assert safe_next("") == "/"


def test_is_same_origin_helper():
    expected = "http://127.0.0.1:8765/"  # the console's own request origin
    # Absent/unparseable origin now fails closed (required 2nd factor, FR-10.6).
    assert is_same_origin(None, expected) is False
    assert is_same_origin("", expected) is False
    # Exact same-origin (scheme, host, port) passes — Origin or full Referer.
    assert is_same_origin("http://127.0.0.1:8765", expected) is True
    assert is_same_origin("http://127.0.0.1:8765/runs/demo", expected) is True
    # A different port, host, or scheme is rejected (not just hostname-loopback).
    assert is_same_origin("http://127.0.0.1:9999", expected) is False
    assert is_same_origin("http://localhost:8765", expected) is False
    assert is_same_origin("https://127.0.0.1:8765", expected) is False
    assert is_same_origin("http://evil.example", expected) is False
    # A console with no valid expected origin also fails closed.
    assert is_same_origin("http://127.0.0.1:8765", None) is False


def test_token_fingerprint_is_stable_and_non_reversible():
    fp = token_fingerprint(TOKEN)
    assert fp == token_fingerprint(TOKEN)
    assert TOKEN not in fp
    assert len(fp) == 64  # sha256 hex

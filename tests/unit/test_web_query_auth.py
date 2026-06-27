"""Loopback ``?p=`` query authentication (FR-2 — supersedes the "token never in
a URL" clause narrowly, for a loopback-only console).

A ``?p=<token>`` whose value matches the serve token authenticates (a third
source after header + cookie), bootstraps the HttpOnly session cookie, and — for
a page navigation — redirects to the same path with ``p`` stripped so the token
never lingers in the address bar/history. A pre-existing cookie short-circuits the
query check (no per-request session churn), and query auth is CSRF-exempt like
header auth (not ambient).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.config import RunConfig
from gauntlet.web.auth import COOKIE_NAME, CSRF_HEADER
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


def _session_count(client: TestClient) -> int:
    return len(client.app.state.sessions._sessions)


# --- FR-2.1: ?p= is a third auth source ------------------------------------


def test_valid_query_token_authenticates_api(client: TestClient):
    assert client.get(f"/api/runs?p={TOKEN}").status_code == 200


def test_invalid_query_token_api_is_401(client: TestClient):
    assert client.get("/api/runs?p=wrong").status_code == 401


def test_invalid_query_token_page_redirects_to_login(client: TestClient):
    resp = client.get("/?p=wrong", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


# --- FR-2.2: query auth bootstraps the cookie ------------------------------


def test_query_auth_sets_session_cookie_and_followup_uses_it(client: TestClient):
    # An API call by query token sets the HttpOnly session cookie...
    resp = client.get(f"/api/runs?p={TOKEN}")
    assert resp.status_code == 200
    sc = resp.headers["set-cookie"].lower()
    assert COOKIE_NAME in sc and "httponly" in sc and "samesite=strict" in sc
    # ...and a follow-up carrying only that cookie (no ?p=) authenticates.
    assert client.get("/api/runs").status_code == 200


# --- FR-2.3: an existing cookie short-circuits (no session churn) ----------


def test_existing_cookie_short_circuits_query(client: TestClient):
    # Establish a cookie session via ?p= bootstrap (one session minted).
    client.get(f"/api/runs?p={TOKEN}")
    assert _session_count(client) == 1
    # Two more requests carrying BOTH the cookie and ?p= must not mint sessions.
    client.get(f"/api/runs?p={TOKEN}")
    client.get(f"/api/runs?p={TOKEN}")
    assert _session_count(client) == 1


# --- FR-2.4: query auth is CSRF-exempt (not ambient) -----------------------


def test_query_authenticated_post_is_csrf_exempt(client: TestClient):
    # A state-changing POST authenticated by ?p= carries no CSRF token yet still
    # reaches the handler (503: no supervisor wired) — it is not ambient, so it is
    # exempt like a header call, not rejected with 403.
    resp = client.post(f"/api/runs?p={TOKEN}", json={"slug": "demo"})
    assert resp.status_code == 503


# --- FR-2.5: page GET strips `p` via a 303 redirect ------------------------


def test_page_get_strips_p_and_sets_cookie_on_redirect(client: TestClient):
    resp = client.get(f"/?p={TOKEN}", follow_redirects=False)
    assert resp.status_code == 303
    # Same path, `p` removed.
    assert resp.headers["location"] == "/"
    sc = resp.headers["set-cookie"].lower()
    assert COOKIE_NAME in sc and "httponly" in sc
    # The cookie-only follow-up renders the page.
    assert client.get("/", follow_redirects=False).status_code == 200


def test_page_get_redirect_preserves_other_query_params(client: TestClient):
    resp = client.get(f"/?status=running&p={TOKEN}", follow_redirects=False)
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert "status=running" in loc
    assert "p=" not in loc and TOKEN not in loc


def test_rendered_page_after_query_auth_has_no_token(client: TestClient):
    # Following the redirect lands on the token-free page; no p/token in its HTML.
    html = client.get(f"/?p={TOKEN}").text
    assert TOKEN not in html
    assert "?p=" not in html and "&p=" not in html


# --- FR-2.6: Referrer-Policy: no-referrer ----------------------------------


def test_referrer_policy_on_query_page_response(client: TestClient):
    resp = client.get(f"/?p={TOKEN}", follow_redirects=False)
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_referrer_policy_on_ordinary_response(client: TestClient):
    assert client.get("/healthz").headers["referrer-policy"] == "no-referrer"


# --- FR-2.4 regression: header/cookie paths unchanged ----------------------


def test_header_path_still_works_without_query(client: TestClient):
    assert client.get("/api/runs", headers={TOKEN_HEADER: TOKEN}).status_code == 200

"""Durable console auth — login cookie + session-bound CSRF (P7, FR-10.4/10.6).

P1–P6 ran the judge's simple bootstrap delivery: the serve token in the
``X-Gauntlet-Token`` header *or* a ``?token=`` query param. P7 retires the query
path and adds the production posture the PRD specifies:

- **Browser session via an httpOnly cookie.** The browser exchanges the serve
  token for an opaque session id once at ``POST /login`` (the token is **never**
  placed in a URL, history, or the SSE handshake — FR-10.4); the session id rides
  in a ``HttpOnly; SameSite=Strict`` cookie thereafter, so SSE (which cannot set
  headers) and form/fetch POSTs all authenticate by cookie.
- **API clients keep header parity.** A non-browser caller may still send the
  serve token in ``X-Gauntlet-Token`` (judge parity); header auth is not ambient
  and cannot be CSRF'd, so header-authenticated POSTs are CSRF-exempt.
- **Session-bound CSRF on cookie POSTs.** Because the cookie is ambient, every
  cookie-authenticated state-changing request must additionally carry a
  per-session CSRF token (constant-time compared) and originate same-origin
  (loopback) — FR-10.6. The token is minted per session and rotated on every
  login, so a token from another session is rejected.

The session registry is in-memory and per-serve (the console holds no durable
auth state — a restart simply invalidates outstanding cookies and the operator
logs in again, the same fail-closed posture as a fresh token). Session ids and
CSRF tokens are high-entropy ``secrets`` values, so a plain dict lookup on the
session id is not a secret comparison; the CSRF value *is* compared
constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from urllib.parse import urlsplit

COOKIE_NAME = "gauntlet_web"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "_csrf"
TOKEN_HEADER = "X-Gauntlet-Token"
# The loopback `?p=<token>` query password (FR-2). This narrowly supersedes the
# gauntlet-ui "token must never ride in a URL" clause for a loopback-only console
# (§7): it is the one query param that authenticates, exists only long enough to
# bootstrap a cookie, and is stripped from a page navigation's URL (FR-2.5).
QUERY_TOKEN_PARAM = "p"

# Default ports by scheme, so an Origin lacking an explicit port compares equal to
# the same scheme on its default port (FR-10.6 same-origin tuple).
_DEFAULT_PORTS = {"http": 80, "https": 443}


class Unauthenticated(Exception):
    """No valid cookie session or header token was presented (→ 401/redirect)."""


class CsrfError(Exception):
    """A cookie-authenticated POST lacked a valid same-origin CSRF token (→403)."""


class LoginRequired(Exception):
    """A browser GET needs the login form — carries the path to return to."""

    def __init__(self, next_path: str) -> None:
        super().__init__("login required")
        self.next_path = next_path


# Auth source of a request, so the CSRF gate can exempt non-ambient callers
# (header + loopback query token) and so the query path can bootstrap a cookie.
AUTH_COOKIE = "cookie"
AUTH_HEADER = "header"
AUTH_QUERY = "query"


class SessionStore:
    """In-memory per-serve session + CSRF registry (FR-10.4/10.6).

    Maps an opaque session id (the cookie value) to its per-session CSRF token.
    The serve token itself is held here for the constant-time login compare and
    the ``X-Gauntlet-Token`` API path; it is never persisted to disk and never
    placed in the cookie (the cookie carries only the random session id).
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self._sessions: dict[str, str] = {}  # session_id -> csrf_token

    # ---- token (login / API header) -----------------------------------------
    def verify_token(self, supplied: str | None) -> bool:
        """Constant-time compare ``supplied`` to the serve token (FR-10.4)."""
        return bool(supplied) and hmac.compare_digest(supplied, self._token)

    # ---- sessions ------------------------------------------------------------
    def create_session(self) -> tuple[str, str]:
        """Mint a fresh session id + bound CSRF token (rotated per login)."""
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self._sessions[sid] = csrf
        return sid, csrf

    def is_valid_session(self, sid: str | None) -> bool:
        return bool(sid) and sid in self._sessions

    def session_csrf(self, sid: str | None) -> str | None:
        if not sid:
            return None
        return self._sessions.get(sid)

    def drop_session(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)


def _origin_tuple(url: str | None) -> tuple[str, str | None, int | None] | None:
    """Normalize a URL to its ``(scheme, hostname, port)`` origin tuple, or None.

    An absent/unparseable URL, or one missing scheme or host, yields ``None`` (so
    the caller fails closed). A port absent from the URL is filled in from the
    scheme default, so ``http://h`` and ``http://h:80`` compare equal.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is None:
        port = _DEFAULT_PORTS.get(parts.scheme)
    return (parts.scheme, parts.hostname, port)


def is_same_origin(origin: str | None, expected: str | None) -> bool:
    """True iff ``origin`` is same-origin with ``expected`` (FR-10.6).

    Compares the full origin tuple — scheme, host, **and** port — of the browser's
    Origin/Referer against the console's own request origin. A *missing* or
    unparseable Origin/Referer fails closed: for an ambient cookie-authenticated
    state-changing request the same-origin evidence is a required second factor
    alongside the CSRF token, not optional defence-in-depth. So
    ``http://127.0.0.1:9999`` no longer passes for a console on
    ``http://127.0.0.1:8765``.
    """
    got = _origin_tuple(origin)
    want = _origin_tuple(expected)
    return got is not None and want is not None and got == want


def authenticate(request, sessions: SessionStore) -> str:
    """Classify a request's auth, or raise :class:`Unauthenticated` (FR-10.4/FR-2).

    Returns, in precedence order: :data:`AUTH_HEADER` for a valid
    ``X-Gauntlet-Token`` (API parity, CSRF-exempt); :data:`AUTH_COOKIE` for a
    valid login-session cookie; :data:`AUTH_QUERY` for a valid loopback
    ``?p=<token>`` query password (FR-2.1). A valid cookie **short-circuits** the
    query check (FR-2.3) so reloading a ``?p=`` URL never mints a fresh session.
    The general-purpose ``?token=`` query path of P1–P6 stays gone; only the
    narrow, single-use ``?p=`` loopback password authenticates (§7).
    """
    header = request.headers.get(TOKEN_HEADER)
    if sessions.verify_token(header):
        return AUTH_HEADER
    sid = request.cookies.get(COOKIE_NAME)
    if sessions.is_valid_session(sid):
        return AUTH_COOKIE
    query_token = request.query_params.get(QUERY_TOKEN_PARAM)
    if sessions.verify_token(query_token):
        return AUTH_QUERY
    raise Unauthenticated()


def enforce_csrf(request, sessions: SessionStore) -> None:
    """Validate the session-bound CSRF token on a cookie POST (FR-10.6).

    The submitted ``X-CSRF-Token`` header (the live-update fetch shim's carrier,
    equivalent to HTMX's) must match the session's CSRF token constant-time, and
    the Origin/Referer, when present, must be loopback. A missing/mismatched
    token fails closed. Header-authenticated (``X-Gauntlet-Token``) callers never
    reach here — they are exempt (not ambient, cannot be forged cross-site).
    """
    sid = request.cookies.get(COOKIE_NAME)
    expected = sessions.session_csrf(sid)
    supplied = request.headers.get(CSRF_HEADER)
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise CsrfError("missing or invalid CSRF token (FR-10.6)")
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not is_same_origin(origin, str(request.base_url)):
        raise CsrfError("cross-origin POST rejected (FR-10.6 same-origin)")


def set_session_cookie(response, sid: str) -> None:
    """Set the HttpOnly login-session cookie on ``response`` (FR-10.4).

    The one place the session cookie is minted onto a response, shared by the
    ``POST /login`` exchange and the ``?p=`` query-token bootstrap (FR-2.2) so
    both set identical attributes: HttpOnly (no JS access) + SameSite=Strict
    (defence-in-depth atop CSRF) + host-only ``Path=/``; Secure is omitted for
    loopback http. The cookie carries only the opaque session id, never the token.
    """
    response.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="strict", path="/")


def safe_next(path: str | None) -> str:
    """Constrain a login ``next`` redirect to a local path (no open redirect).

    Only a path beginning with a single ``/`` (and not ``//`` — a
    protocol-relative URL) is honoured; anything else falls back to ``/``.
    """
    if not path or not path.startswith("/") or path.startswith("//"):
        return "/"
    return path


def token_fingerprint(token: str) -> str:
    """Non-reversible fingerprint of the serve token for the console registry.

    The registry (FR-12.4) records a fingerprint so a ``--watch`` caller can
    detect a token mismatch *without* the token itself ever being persisted.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "SessionStore",
    "Unauthenticated",
    "CsrfError",
    "LoginRequired",
    "authenticate",
    "enforce_csrf",
    "is_same_origin",
    "safe_next",
    "set_session_cookie",
    "token_fingerprint",
    "COOKIE_NAME",
    "CSRF_HEADER",
    "CSRF_FIELD",
    "TOKEN_HEADER",
    "QUERY_TOKEN_PARAM",
    "AUTH_COOKIE",
    "AUTH_HEADER",
    "AUTH_QUERY",
]

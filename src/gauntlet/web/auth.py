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

# Hosts a same-origin loopback request may legitimately originate from (FR-10.6
# same-origin check). The console binds loopback only, so a real same-origin
# POST always carries one of these in Origin/Referer; anything else is rejected.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class Unauthenticated(Exception):
    """No valid cookie session or header token was presented (→ 401/redirect)."""


class CsrfError(Exception):
    """A cookie-authenticated POST lacked a valid same-origin CSRF token (→403)."""


class LoginRequired(Exception):
    """A browser GET needs the login form — carries the path to return to."""

    def __init__(self, next_path: str) -> None:
        super().__init__("login required")
        self.next_path = next_path


# Auth source of a request, so the CSRF gate can exempt header callers (FR-10.6).
AUTH_COOKIE = "cookie"
AUTH_HEADER = "header"


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


def is_same_origin(origin: str | None) -> bool:
    """True iff ``origin`` (an Origin/Referer header) is a loopback origin.

    A *missing* Origin/Referer returns ``True`` — the CSRF token match is the
    primary defence and many same-origin fetches omit Referer; the same-origin
    check is defence-in-depth that only ever *rejects* a present, cross-site
    origin (FR-10.6). A present but unparseable/non-loopback origin is rejected.
    """
    if not origin:
        return True
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return False
    return host in _LOOPBACK_HOSTS


def authenticate(request, sessions: SessionStore) -> str:
    """Classify a request's auth, or raise :class:`Unauthenticated` (FR-10.4).

    Returns :data:`AUTH_HEADER` for a valid ``X-Gauntlet-Token`` (API parity,
    CSRF-exempt) or :data:`AUTH_COOKIE` for a valid login-session cookie. The
    ``?token=`` query path of P1–P6 is **gone** — the token must never ride in a
    URL (FR-10.4).
    """
    header = request.headers.get(TOKEN_HEADER)
    if sessions.verify_token(header):
        return AUTH_HEADER
    sid = request.cookies.get(COOKIE_NAME)
    if sessions.is_valid_session(sid):
        return AUTH_COOKIE
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
    if not is_same_origin(origin):
        raise CsrfError("cross-origin POST rejected (FR-10.6 same-origin)")


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
    "token_fingerprint",
    "COOKIE_NAME",
    "CSRF_HEADER",
    "CSRF_FIELD",
    "TOKEN_HEADER",
    "AUTH_COOKIE",
    "AUTH_HEADER",
]

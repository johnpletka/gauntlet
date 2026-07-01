"""Linear issue-tracker provider (FR-6.3), GraphQL against ``api.linear.app``.

Resolves a human key (``ENG-1234``) or a ``linear.app/.../issue/<KEY>`` URL to a
normalized :class:`Issue`. Auth is a personal API key read **by env-var name**
from ``config.issue_tracker.api_key_env`` (default ``LINEAR_API_KEY``) — never
the token in repo config (FR-1.4 base spec, §7).

Every call is bounded by a per-call timeout (FR-6.4) and every failure maps to
the :mod:`gauntlet.trackers.base` taxonomy so a review fails closed rather than
degrading to a diff-only pass.
"""

from __future__ import annotations

import re

import httpx

from gauntlet.trackers.base import (
    Issue,
    IssueNotFound,
    IssueRef,
    IssueTrackerAuthError,
    IssueTrackerUnavailable,
)

PROVIDER = "linear"
LINEAR_API_URL = "https://api.linear.app/graphql"
DEFAULT_API_KEY_ENV = "LINEAR_API_KEY"

# A Linear human identifier: a team key (letters/digits, leading letter) + number.
_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*-[0-9]+")
# The same token, anchored, for parse_ref's bare-key form.
_KEY_ANCHORED_RE = re.compile(rf"^{_KEY_RE.pattern}$")
# A linear.app issue URL: capture the <KEY> segment after /issue/.
_URL_RE = re.compile(
    r"linear\.app/[^/\s]+/issue/([A-Za-z][A-Za-z0-9]*-[0-9]+)",
    re.IGNORECASE,
)
# The same URL, but consuming any trailing slug/query/fragment so extract_refs
# treats the whole URL run as one span. Without this the bare-key scan would
# harvest false keys from the slug (e.g. ENG-2 out of
# ``.../issue/ENG-1/fix-eng-2-regression``) — F-002.
_URL_SPAN_RE = re.compile(
    r"linear\.app/[^/\s]+/issue/([A-Za-z][A-Za-z0-9]*-[0-9]+)(?:[/?#]\S*)?",
    re.IGNORECASE,
)
# A ref token embedded in free text (word-bounded), for extract_refs.
_EMBEDDED_RE = re.compile(rf"(?<![A-Za-z0-9])({_KEY_RE.pattern})(?![A-Za-z0-9])")

_ISSUE_QUERY = (
    "query($id: String!) { issue(id: $id) { "
    "identifier title description url state { name } } }"
)
_VIEWER_QUERY = "query { viewer { id } }"

# GraphQL error codes / substrings that denote an unknown or inaccessible entity
# (→ IssueNotFound) versus an auth failure (→ IssueTrackerAuthError).
_NOT_FOUND_CODES = frozenset({"NOT_FOUND", "ENTITY_NOT_FOUND", "INVALID_INPUT"})
_AUTH_CODES = frozenset(
    {"AUTHENTICATION_ERROR", "FORBIDDEN", "UNAUTHENTICATED", "AUTHENTICATION_FAILED"}
)


class LinearIssueTracker:
    """``IssueTracker`` implementation over the Linear GraphQL API (FR-6.3).

    ``transport`` is injectable so the unit suite drives the provider with a
    mocked GraphQL transport and never touches the live network.
    """

    name = PROVIDER

    def __init__(
        self,
        *,
        api_key: str | None,
        api_key_env: str = DEFAULT_API_KEY_ENV,
        timeout_s: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.timeout_s = timeout_s
        self._transport = transport

    # ---- reference parsing (no network) -------------------------------------

    def parse_ref(self, raw: str) -> IssueRef:
        """Normalize a bare key or a Linear issue URL to an :class:`IssueRef`.

        A bare ``TEAM-NNNN`` is taken verbatim (upper-cased, stripped); a
        ``linear.app/<workspace>/issue/<KEY>/<slug>`` URL is reduced to ``<KEY>``
        (slug/query/fragment discarded). Anything else is a usage-class
        ``ValueError`` — not :class:`IssueNotFound` (no network happened).
        """
        candidate = (raw or "").strip()
        url_match = _URL_RE.search(candidate)
        if url_match:
            key = url_match.group(1).upper()
        elif _KEY_ANCHORED_RE.match(candidate):
            key = candidate.upper()
        else:
            raise ValueError(
                f"not a Linear issue reference: {raw!r}; expected a key like "
                "'ENG-1234' or a linear.app/.../issue/<KEY> URL"
            )
        return IssueRef(provider=PROVIDER, raw=raw, key=key)

    def extract_refs(self, text: str) -> list[IssueRef]:
        """Scan ``text`` for Linear refs in textual order, de-duplicated by key
        preserving first occurrence (FR-4.3).

        Linear issue URLs are matched first and contribute *only* their
        ``/issue/<KEY>`` key; the bare-key scan runs only on the text outside
        those URL spans. So a URL like ``.../issue/ENG-1/fix-eng-2-regression``
        yields ``ENG-1`` alone and never a spurious ``ENG-2`` harvested from the
        slug (F-002)."""
        text = text or ""
        # Full URL spans (incl. slug/query/fragment); bare keys inside them are
        # slug false positives — the URL already contributed its own key.
        url_spans: list[tuple[int, int]] = []
        # (position, raw_text, key) so refs can be emitted in textual order.
        hits: list[tuple[int, str, str]] = []
        for match in _URL_SPAN_RE.finditer(text):
            url_spans.append((match.start(), match.end()))
            hits.append((match.start(), match.group(1), match.group(1).upper()))
        for match in _EMBEDDED_RE.finditer(text):
            if any(start <= match.start() < end for start, end in url_spans):
                continue
            hits.append((match.start(), match.group(1), match.group(1).upper()))
        hits.sort(key=lambda h: h[0])
        seen: set[str] = set()
        refs: list[IssueRef] = []
        for _pos, raw_text, key in hits:
            if key in seen:
                continue
            seen.add(key)
            refs.append(IssueRef(provider=PROVIDER, raw=raw_text, key=key))
        return refs

    # ---- network calls ------------------------------------------------------

    def fetch(self, ref: IssueRef) -> Issue:
        """Resolve ``ref`` to a normalized :class:`Issue` (FR-6.3/FR-6.4)."""
        data = self._graphql(_ISSUE_QUERY, {"id": ref.key})
        issue = data.get("issue")
        if issue is None:
            raise IssueNotFound(
                f"Linear issue {ref.key!r} not found or not accessible with the "
                f"configured token ({self.api_key_env})"
            )
        state = issue.get("state") or {}
        return Issue(
            identifier=issue.get("identifier") or ref.key,
            title=issue.get("title") or "",
            description=issue.get("description") or "",
            url=issue.get("url") or "",
            state=state.get("name") if isinstance(state, dict) else None,
        )

    def verify_auth(self) -> None:
        """Cheap ``viewer { id }`` probe for doctor (FR-10.1); raises on failure."""
        self._graphql(_VIEWER_QUERY, {})

    # ---- GraphQL transport --------------------------------------------------

    def _graphql(self, query: str, variables: dict) -> dict:
        """Execute one GraphQL request, mapping every failure to the taxonomy.

        The per-call timeout (FR-6.4) is enforced by httpx; a
        :class:`httpx.TimeoutException` maps to :class:`IssueTrackerUnavailable`.
        """
        if not self.api_key:
            raise IssueTrackerAuthError(
                f"no Linear API key: env var {self.api_key_env} is not set. "
                "Export a Linear personal API key under that name."
            )
        payload = {"query": query, "variables": variables}
        # Linear personal API keys go in the Authorization header raw (no Bearer).
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(
                transport=self._transport, timeout=self.timeout_s
            ) as client:
                resp = client.post(LINEAR_API_URL, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise IssueTrackerUnavailable(
                f"Linear API timed out after {self.timeout_s}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise IssueTrackerUnavailable(
                f"Linear API is unreachable: {type(exc).__name__}"
            ) from exc

        return self._parse_response(resp)

    def _parse_response(self, resp: httpx.Response) -> dict:
        status = resp.status_code
        if status in (401, 403):
            raise IssueTrackerAuthError(
                f"Linear API rejected the token ({self.api_key_env}): HTTP {status}"
            )
        if status >= 500:
            raise IssueTrackerUnavailable(f"Linear API server error: HTTP {status}")

        try:
            body = resp.json()
        except ValueError as exc:
            # A non-JSON 2xx/4xx body is an unexpected/unavailable condition.
            raise IssueTrackerUnavailable(
                f"Linear API returned a non-JSON response (HTTP {status})"
            ) from exc

        errors = body.get("errors")
        if errors:
            self._raise_for_graphql_errors(errors, status)
        # A 4xx with no structured errors we can classify: fail closed.
        if status >= 400:
            raise IssueTrackerUnavailable(
                f"Linear API returned HTTP {status} with no classifiable error"
            )

        data = body.get("data")
        if not isinstance(data, dict):
            raise IssueTrackerUnavailable(
                "Linear API response carried no data payload"
            )
        return data

    def _raise_for_graphql_errors(self, errors: list, status: int) -> None:
        """Map a GraphQL ``errors`` array to the taxonomy (FR-6.4)."""
        codes: set[str] = set()
        messages: list[str] = []
        for err in errors:
            if not isinstance(err, dict):
                continue
            messages.append(str(err.get("message", "")))
            ext = err.get("extensions") or {}
            for field in ("code", "type"):
                val = ext.get(field)
                if isinstance(val, str):
                    codes.add(val.upper())
        blob = " ".join(messages).lower()
        if codes & _AUTH_CODES or "authentication" in blob or "unauthorized" in blob:
            raise IssueTrackerAuthError(
                f"Linear API authentication failed ({self.api_key_env}): "
                f"{'; '.join(m for m in messages if m) or 'auth error'}"
            )
        if (
            codes & _NOT_FOUND_CODES
            or "could not find" in blob
            or "not found" in blob
            or "entity not found" in blob
        ):
            raise IssueNotFound(
                f"Linear issue not found: "
                f"{'; '.join(m for m in messages if m) or 'not found'}"
            )
        raise IssueTrackerUnavailable(
            f"Linear API error (HTTP {status}): "
            f"{'; '.join(m for m in messages if m) or 'unknown GraphQL error'}"
        )

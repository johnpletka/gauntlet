"""Issue-tracker contract: protocol, normalized payloads, error taxonomy.

The tracker abstraction mirrors ``adapters/`` (FR-2.4, FR-6.1): a provider is a
plugin registered under the ``gauntlet.issue_trackers`` entry-point group, not a
fork. v1 ships exactly one provider (``linear``); GitHub Issues / Jira are a
registry seam, not built code.

Every failure fails closed through the typed taxonomy below (FR-6.4) so a review
run never proceeds with a missing or partial problem statement — that would
silently degrade a solution-correctness review to a diff-only pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class IssueRef:
    """A parsed, provider-native reference to one issue (§6).

    ``key`` is the single provider-native lookup token — for Linear the human
    identifier (``ENG-1234``), upper-cased and whitespace-stripped, never a UUID
    or URL. ``parse_ref`` normalizes every accepted form to it with no network
    round-trip; ``fetch`` passes it straight to the provider's lookup.
    """

    provider: str  # "linear"
    raw: str        # "ENG-1234" or a pasted URL, verbatim as supplied
    key: str        # normalized human key, "ENG-1234"


@dataclass(frozen=True)
class Issue:
    """A tracker-agnostic, normalized issue payload (§6).

    v1 exposes only a single description body — no discrete repro/expected
    fields — so ``render_intent`` places the full body under ``## Problem`` and
    omits the reserved sections (see ``intent.py`` / §6). ``state`` lets a later
    review flag an already-closed ticket (Open Question 11.6; captured, not yet
    acted on).
    """

    identifier: str        # "ENG-1234"
    title: str
    description: str       # ticket body (markdown)
    url: str               # canonical link, recorded in the run log / intent.md
    state: str | None      # "In Progress" / "Done", or None if unavailable
    # labels/assignee/comments are reserved for later providers; not modeled in v1.


@runtime_checkable
class IssueTracker(Protocol):
    """Common interface over issue trackers (FR-6.1).

    A provider normalizes references and payloads to :class:`IssueRef` /
    :class:`Issue` and maps every failure to the taxonomy below. Providers
    register via the ``gauntlet.issue_trackers`` entry point and are selected by
    ``config.issue_tracker.provider``.
    """

    name: str

    def parse_ref(self, raw: str) -> IssueRef:
        """Normalize a human key or issue URL to an :class:`IssueRef`.

        Raises a usage-class error (``ValueError``) — *not* :class:`IssueNotFound`
        — when ``raw`` matches no accepted shape. No network round-trip.
        """
        ...

    def fetch(self, ref: IssueRef) -> Issue:
        """Resolve a reference to a normalized :class:`Issue`.

        Fails closed via the taxonomy: auth → :class:`IssueTrackerAuthError`,
        unresolved ref → :class:`IssueNotFound`, network/5xx/timeout →
        :class:`IssueTrackerUnavailable` (FR-6.4).
        """
        ...

    def extract_refs(self, text: str) -> list[IssueRef]:
        """Scan free text (e.g. a PR body) for this provider's refs, in textual
        order, de-duplicated by key preserving first occurrence (FR-4.3)."""
        ...

    def verify_auth(self) -> None:
        """Cheap auth probe for ``gauntlet doctor`` (FR-10.1).

        Returns on success; raises the taxonomy error on failure. Never fetches a
        ticket body — it makes the cheapest authenticated call the provider
        supports (Linear: ``viewer { id }``).
        """
        ...


class IssueTrackerError(Exception):
    """Base issue-tracker failure. Every tracker error subclasses this so a
    caller can fail the run closed on the whole family (FR-6.4)."""


class IssueTrackerAuthError(IssueTrackerError):
    """Missing or invalid tracker token (unset env var, 401/403)."""


class IssueNotFound(IssueTrackerError):
    """The reference resolved to no accessible issue (data.issue is null, or an
    unknown/inaccessible-entity error code)."""


class IssueTrackerUnavailable(IssueTrackerError):
    """The tracker could not be reached or answered in time (transport error,
    5xx, or a per-call timeout exceeded — FR-6.4)."""

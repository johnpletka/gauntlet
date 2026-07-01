"""Issue-tracker providers and the entry-point registry (FR-6.1).

Providers register under the ``gauntlet.issue_trackers`` entry-point group, so a
future GitHub-Issues or Jira provider is a plugin, not a fork (mirrors
``gauntlet.adapters``, FR-2.4). v1 registers exactly one provider, ``linear``.

``get_tracker`` builds the configured provider instance from an
:class:`IssueTrackerConfig`, resolving the token **by env-var name** (never from
repo config). A missing token is not an error at construction: the tracker holds
``api_key=None`` and each network call raises :class:`IssueTrackerAuthError`, so
``doctor`` can construct the tracker and surface the typed error from its probe.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.metadata import entry_points

from gauntlet.trackers.base import (
    Issue,
    IssueNotFound,
    IssueRef,
    IssueTracker,
    IssueTrackerAuthError,
    IssueTrackerError,
    IssueTrackerUnavailable,
)
from gauntlet.trackers.intent import render_intent

ENTRY_POINT_GROUP = "gauntlet.issue_trackers"


def available_trackers() -> dict[str, type]:
    """All tracker provider classes registered under the entry-point group."""
    return {
        ep.name: ep.load() for ep in entry_points(group=ENTRY_POINT_GROUP)
    }


def get_tracker_class(name: str) -> type:
    """Resolve one tracker provider class by registered name (FR-6.2)."""
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == name:
            return ep.load()
    known = sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))
    raise KeyError(
        f"no issue tracker registered as {name!r} in group "
        f"{ENTRY_POINT_GROUP!r}; known: {known}"
    )


def get_tracker(
    config,
    *,
    env: Mapping[str, str] | None = None,
    transport=None,
) -> IssueTracker:
    """Build the configured provider instance from an ``IssueTrackerConfig``.

    ``config`` is the ``issue_tracker`` block (see
    :class:`gauntlet.engine.config.IssueTrackerConfig`). ``env`` and
    ``transport`` are injectable for offline tests. Raises :class:`KeyError` for
    an unknown provider (a config-load ``ValueError`` normally catches this
    first, FR-6.2).
    """
    if env is None:
        import os

        env = os.environ
    cls = get_tracker_class(config.provider)
    api_key = env.get(config.api_key_env)
    return cls(
        api_key=api_key or None,
        api_key_env=config.api_key_env,
        timeout_s=config.timeout_s,
        transport=transport,
    )


__all__ = [
    "ENTRY_POINT_GROUP",
    "Issue",
    "IssueNotFound",
    "IssueRef",
    "IssueTracker",
    "IssueTrackerAuthError",
    "IssueTrackerError",
    "IssueTrackerUnavailable",
    "available_trackers",
    "get_tracker",
    "get_tracker_class",
    "render_intent",
]

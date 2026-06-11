"""Agent adapters and the entry-point registry (FR-2.4).

Adapters register under the ``gauntlet.adapters`` entry-point group, so a
future Gemini-CLI or internal-agent adapter is a plugin, not a fork. The
built-in three (claude-code, codex, api) register the same way.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from gauntlet.adapters.base import (
    AdapterCapabilities,
    AdapterError,
    AgentAdapter,
    AgentFailedError,
    AgentResult,
    AgentTimeoutError,
    MalformedOutputError,
    UnsupportedFeatureError,
    Usage,
)

ENTRY_POINT_GROUP = "gauntlet.adapters"


def available_adapters() -> dict[str, type]:
    """All adapter classes registered under the entry-point group."""
    return {
        ep.name: ep.load() for ep in entry_points(group=ENTRY_POINT_GROUP)
    }


def get_adapter_class(name: str) -> type:
    """Resolve one adapter class by registered name."""
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == name:
            return ep.load()
    known = sorted(ep.name for ep in entry_points(group=ENTRY_POINT_GROUP))
    raise KeyError(
        f"no adapter registered as {name!r} in group {ENTRY_POINT_GROUP!r}; "
        f"known: {known}"
    )


__all__ = [
    "AdapterCapabilities",
    "AdapterError",
    "AgentAdapter",
    "AgentFailedError",
    "AgentResult",
    "AgentTimeoutError",
    "MalformedOutputError",
    "UnsupportedFeatureError",
    "Usage",
    "available_adapters",
    "get_adapter_class",
    "ENTRY_POINT_GROUP",
]

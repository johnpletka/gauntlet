"""Server-Sent-Events generators for the console (P2, FR-8.2/FR-3.2).

Two async generators, factored out of ``service.py`` so they are testable
without spinning a live server: :func:`event_stream` (run-list-level
transitions) and :func:`log_tail_stream` (a step's appended log bytes). Both
take an awaitable ``is_disconnected`` (the request's, in production) and an
optional bound (``max_events`` / ``max_iters``) so a test can drive them to a
deterministic end instead of looping forever.

SSE wire format (per the spec): each message is ``event: <name>\\n`` then one or
more ``data: <line>\\n`` then a blank line. A bare ``: <text>\\n\\n`` is a comment
keepalive that resets the browser's reconnection timer without delivering an
event. Clients (``static/live.js``) listen for the named events.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

from gauntlet.web.notify import Notification
from gauntlet.web.store import RunStore
from gauntlet.web.watcher import WatchEvent, Watcher

# Headers that keep proxies/browsers from buffering an SSE stream.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}

DEFAULT_KEEPALIVE_S = 15.0
DEFAULT_TAIL_INTERVAL_S = 1.0

IsDisconnected = Callable[[], Awaitable[bool]]


def pack(event: str, data: dict) -> str:
    """Format one named SSE message. ``data`` is JSON on a single ``data:`` line."""
    payload = json.dumps(data, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


async def event_stream(
    store: RunStore,
    watcher: Watcher,
    *,
    is_disconnected: IsDisconnected,
    keepalive: float = DEFAULT_KEEPALIVE_S,
    max_events: int | None = None,
) -> AsyncIterator[str]:
    """Stream run-list transitions to one browser (`GET /events`, FR-8.2).

    On connect it first emits a ``snapshot`` of current run-list rows, so a
    fresh or auto-reconnected client always re-reads authoritative state and can
    never miss a transition that fired while it was disconnected. Thereafter the
    shared subscriber queue carries two object types, dispatched by type: a
    :class:`~gauntlet.web.watcher.WatchEvent` is forwarded as a ``transition``
    (drives the live partial re-fetch, P2), and a
    :class:`~gauntlet.web.notify.Notification` (the P6 in-tab channel) as a
    ``notify`` (drives the browser ``Notification``). A quiet stream emits comment
    keepalives every ``keepalive`` seconds.
    """
    q = watcher.subscribe()
    sent = 0
    try:
        yield pack("snapshot", {"rows": [r.model_dump() for r in store.list_rows()]})
        while max_events is None or sent < max_events:
            if await is_disconnected():
                break
            try:
                item = await asyncio.wait_for(q.get(), timeout=keepalive)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if isinstance(item, Notification):
                yield pack("notify", item.model_dump())
            elif isinstance(item, WatchEvent):
                yield pack("transition", item.model_dump())
            else:  # pragma: no cover - defensive: unknown queue item
                continue
            sent += 1
    finally:
        watcher.unsubscribe(q)


async def log_tail_stream(
    store: RunStore,
    slug: str,
    step: str,
    *,
    run_id: str | None = None,
    name: str | None = None,
    start: int = 0,
    is_disconnected: IsDisconnected,
    interval: float = DEFAULT_TAIL_INTERVAL_S,
    max_iters: int | None = None,
) -> AsyncIterator[str]:
    """Tail a step's log, emitting only appended bytes (`/log/stream`, FR-3.2).

    Scoped to an *open* viewer (R5): the loop only runs while the SSE connection
    is held. The run id is pinned once up front so a long tail never drifts onto
    a newer run. Each tick reads from the current offset and, if anything was
    appended, emits an ``append`` event carrying the new ``text`` and the offsets
    the client should adopt as its next cursor; otherwise a keepalive.
    """
    # Pin the concrete run so the tail stays on one run dir for its lifetime.
    rid = store.resolve_run_id(slug, run_id)
    offset = start
    iters = 0
    try:
        while max_iters is None or iters < max_iters:
            iters += 1
            if await is_disconnected():
                break
            chunk = store.step_log(slug, step, run_id=rid, name=name, offset=offset)
            if chunk.text:
                yield pack(
                    "append",
                    {
                        "name": chunk.name,
                        "start": chunk.start,
                        "end": chunk.end,
                        "size": chunk.size,
                        "text": chunk.text,
                    },
                )
                offset = chunk.end
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(interval)
    finally:
        pass


__all__ = [
    "pack",
    "event_stream",
    "log_tail_stream",
    "SSE_HEADERS",
    "DEFAULT_KEEPALIVE_S",
    "DEFAULT_TAIL_INTERVAL_S",
]

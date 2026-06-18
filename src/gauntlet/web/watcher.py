"""Watcher — poll manifests, emit edge-triggered transitions (P2, FR-8).

A single async task stats each known ``manifest.json`` ~once per second and
publishes a transition to an in-process async event bus, which feeds the SSE
streams (P2) and, later, the notifier (P6). The watcher owns **no** run state —
it only observes on-disk manifests — so a watcher error can never affect a run.

**Two separate concerns (review F-002):**

- *File-change detection* uses the manifest's ``st_mtime_ns`` purely as a cheap
  gate: a changed mtime means "re-parse this file", an unchanged mtime means
  "skip the read". mtime is **not** part of an emitted event's identity.
- *Semantic-transition identity* is the tuple
  ``(run_id, current_step, current_step_status, run_status)`` (FR-8.1). An event
  is emitted only when that tuple changes after a re-parse.

Consequence: an atomic rewrite that preserves semantic state (``os.replace`` of
a byte-changed-but-same-state manifest — new mtime, same tuple) triggers a
re-read but emits **nothing**, so semantic no-op rewrites never produce phantom
transitions or duplicate downstream notifications (P6). The coarser
``(run_id, run_status)`` keying the PRD rejects would collapse a run parking at
successive gates into one event; keying on ``current_step`` keeps each distinct
gate transition observable exactly once (G3/G4).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from pydantic import BaseModel

from gauntlet.engine.manifest import Manifest
from gauntlet.web.store import RunStore, _current_record, _mtime_iso

DEFAULT_INTERVAL_S = 1.0

# The 4-field semantic identity of a transition (FR-8.1). `None` stands in for a
# manifest we failed to parse, so a broken→valid recovery still reads as a
# transition rather than being silently swallowed.
Identity = tuple[str, str | None, str | None, str]


class WatchEvent(BaseModel):
    """One observed run transition (the SSE/notify payload, FR-8.2/FR-9.2)."""

    slug: str
    run_id: str
    run_status: str
    current_step: str | None = None
    current_step_status: str | None = None
    current_step_notes: str | None = None
    updated: str | None = None  # manifest mtime (display only, not identity)

    @property
    def identity(self) -> Identity:
        """The semantic identity tuple the watcher de-dups on (FR-8.1)."""
        return (
            self.run_id,
            self.current_step,
            self.current_step_status,
            self.run_status,
        )


class Watcher:
    """Polls every known manifest and fans transitions out to subscribers.

    ``poll_once`` is the synchronous core — it does the stat/re-parse/diff and
    returns the events it emitted that tick (so it is directly testable and so
    P6's notifier can hang off the same call). ``run`` wraps it in the ~1s loop.
    """

    def __init__(self, store: RunStore, *, interval: float = DEFAULT_INTERVAL_S) -> None:
        self.store = store
        self.interval = interval
        # manifest path → (last mtime_ns, last semantic identity-or-None)
        self._seen: dict[Path, tuple[int, Identity | None]] = {}
        self._subscribers: set[asyncio.Queue[WatchEvent]] = set()
        self._task: asyncio.Task | None = None

    # ---- event bus -----------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[WatchEvent]:
        """Register a subscriber queue (one per open SSE stream)."""
        q: asyncio.Queue[WatchEvent] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[WatchEvent]) -> None:
        self._subscribers.discard(q)

    def _publish(self, event: WatchEvent) -> None:
        # Unbounded queues never raise QueueFull; a disconnecting subscriber is
        # dropped by unsubscribe in the stream's `finally`.
        for q in list(self._subscribers):
            q.put_nowait(event)

    # ---- polling core --------------------------------------------------------
    def _event_for(self, slug: str, man: Manifest, manifest_path: Path) -> WatchEvent:
        cur = _current_record(man)
        return WatchEvent(
            slug=slug,
            run_id=man.run_id,
            run_status=man.status,
            current_step=man.current_step,
            current_step_status=cur.status if cur else None,
            current_step_notes=cur.notes if cur else None,
            updated=_mtime_iso(manifest_path),
        )

    def poll_once(self) -> list[WatchEvent]:
        """Stat/re-parse every manifest; emit + return each new transition.

        Emits on first observation of a run (it became visible — a transition
        the list view wants live) and on every later semantic-identity change.
        A re-parse that yields the same identity (semantic no-op rewrite) emits
        nothing; an unchanged mtime is skipped without even re-parsing.
        """
        events: list[WatchEvent] = []
        live: set[Path] = set()
        for slug, _rid, manifest_path in self.store.iter_manifests():
            live.add(manifest_path)
            try:
                mtime_ns = manifest_path.stat().st_mtime_ns
            except OSError:
                continue
            prev = self._seen.get(manifest_path)
            if prev is not None and prev[0] == mtime_ns:
                continue  # cheap gate: file untouched since last tick
            try:
                man = Manifest.load(manifest_path)
            except (OSError, ValueError):
                # Fail closed: record the mtime so we don't spin re-parsing a
                # torn/broken file, but keep the prior identity so a later valid
                # rewrite still reads as a transition.
                self._seen[manifest_path] = (mtime_ns, prev[1] if prev else None)
                continue
            event = self._event_for(slug, man, manifest_path)
            identity = event.identity
            if prev is None or prev[1] != identity:
                events.append(event)
                self._publish(event)
            self._seen[manifest_path] = (mtime_ns, identity)
        # Forget runs whose dir vanished (rare), so memory tracks live runs only.
        for gone in set(self._seen) - live:
            del self._seen[gone]
        return events

    # ---- async lifecycle -----------------------------------------------------
    async def run(self) -> None:
        while True:
            self.poll_once()
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


__all__ = ["Watcher", "WatchEvent", "DEFAULT_INTERVAL_S"]

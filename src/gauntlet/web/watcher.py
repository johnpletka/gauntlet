"""Watcher — poll manifests, emit edge-triggered transitions (P2, FR-8).

A single async task stats each known ``manifest.json`` ~once per second and
publishes a transition to an in-process async event bus, which feeds the SSE
streams (P2) and, later, the notifier (P6). The watcher owns **no** run state —
it only observes on-disk manifests — so a watcher error can never affect a run.

**Two uses of the manifest mtime (review F-001/F-002):**

- *File-change detection* uses the manifest's ``st_mtime_ns`` as a cheap gate:
  a changed mtime means "re-parse this file", an unchanged mtime means "skip the
  read" without even parsing.
- *Event identity* is the FR-8.1 tuple ``(run_id, current_step,
  current_step_status, run_status, manifest_revision)``, where
  ``manifest_revision`` is that same ``st_mtime_ns`` — the PRD's v1 revision
  marker (``prd.md`` FR-8.1, "``mtime`` suffices"). Including the revision means
  any manifest write is a new identity even when the four semantic fields are
  unchanged, so a run that re-enters the *same* semantic state (e.g. parks at
  gate A, leaves, parks at gate A again) is still observed rather than collapsed.

De-duplicating actual *notifications* across revision-only changes is FR-9.1's
separate concern (its own ``(run_id, kind, current_step)`` key in P6), not the
watcher's. The coarser ``(run_id, run_status)`` keying the PRD rejects would
collapse a run parking at successive gates into one event; the finer identity
keeps each distinct transition observable (G3/G4).

NOTE (FR-8.1 vs plan deviation): the P2 plan text described a 4-field identity
that excludes mtime; including ``manifest_revision`` here follows the
higher-priority PRD (review F-001).
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from pydantic import BaseModel

from gauntlet.engine.manifest import Manifest
from gauntlet.web.store import RunStore, _current_record, _mtime_iso

DEFAULT_INTERVAL_S = 1.0

# The FR-8.1 event identity: four semantic fields plus the manifest revision
# (`st_mtime_ns`). The `None` semantic fields stand in for a manifest we failed
# to parse, so a broken→valid recovery still reads as a transition rather than
# being silently swallowed.
Identity = tuple[str, str | None, str | None, str, int | None]


class WatchEvent(BaseModel):
    """One observed run transition (the SSE/notify payload, FR-8.2/FR-9.2)."""

    slug: str
    run_id: str
    run_status: str
    current_step: str | None = None
    current_step_status: str | None = None
    current_step_notes: str | None = None
    updated: str | None = None  # manifest mtime as ISO (display)
    revision: int | None = None  # manifest mtime in ns: the FR-8.1 manifest_revision

    @property
    def identity(self) -> Identity:
        """The FR-8.1 identity tuple the watcher de-dups on.

        Includes ``manifest_revision`` (mtime ns) so a manifest write is a new
        identity even when the four semantic fields are unchanged (review F-001).
        """
        return (
            self.run_id,
            self.current_step,
            self.current_step_status,
            self.run_status,
            self.revision,
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
    def _event_for(
        self, slug: str, man: Manifest, manifest_path: Path, mtime_ns: int
    ) -> WatchEvent:
        cur = _current_record(man)
        return WatchEvent(
            slug=slug,
            run_id=man.run_id,
            run_status=man.status,
            current_step=man.current_step,
            current_step_status=cur.status if cur else None,
            current_step_notes=cur.notes if cur else None,
            updated=_mtime_iso(manifest_path),
            revision=mtime_ns,
        )

    def poll_once(self) -> list[WatchEvent]:
        """Stat/re-parse every manifest; emit + return each new transition.

        Emits on first observation of a run (it became visible — a transition
        the list view wants live) and on every later identity change. Identity
        includes the manifest revision (mtime), so any manifest rewrite is a
        transition (FR-8.1, review F-001); an unchanged mtime is skipped without
        even re-parsing.
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
            event = self._event_for(slug, man, manifest_path, mtime_ns)
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

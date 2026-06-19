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
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from gauntlet.engine.manifest import Manifest
from gauntlet.web.store import RunStore, _current_record, _mtime_iso

logger = logging.getLogger(__name__)

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
    current_step_type: str | None = None  # step `type` (gate vs cycle) for FR-9.1
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

    def __init__(
        self,
        store: RunStore,
        *,
        interval: float = DEFAULT_INTERVAL_S,
        notifier: Any | None = None,
    ) -> None:
        self.store = store
        self.interval = interval
        # The P6 notifier (duck-typed: ``prime(event)`` / ``notify(event)``) so the
        # watcher carries no import dependency on ``notify.py`` (which imports the
        # watcher). Set here or assigned later (create_app wires it). When None,
        # the watcher is a pure transition observer, exactly as in P2.
        self.notifier = notifier
        # manifest path → (last mtime_ns, last semantic identity-or-None)
        self._seen: dict[Path, tuple[int, Identity | None]] = {}
        # Whether the watcher's *initial* scan has completed. Startup priming
        # (suppress notifications for runs that predate the server) must apply
        # only to that first scan — a run first *discovered* after the watcher is
        # already polling is a real transition and must notify, even if first
        # seen already parked/done (review F-001).
        self._primed = False
        # SSE subscriber queues carry WatchEvent (transitions) AND Notification
        # objects (the in-tab notify channel publishes onto the same queues; the
        # SSE stream type-dispatches them to `transition`/`notify` events, P6).
        self._subscribers: set[asyncio.Queue[Any]] = set()
        self._task: asyncio.Task | None = None

    # ---- event bus -----------------------------------------------------------
    def subscribe(self) -> asyncio.Queue[Any]:
        """Register a subscriber queue (one per open SSE stream).

        The queue carries both :class:`WatchEvent` transitions and (P6)
        ``Notification`` objects; the SSE stream type-dispatches them.
        """
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Any]) -> None:
        self._subscribers.discard(q)

    def _publish(self, event: WatchEvent) -> None:
        # Unbounded queues never raise QueueFull; a disconnecting subscriber is
        # dropped by unsubscribe in the stream's `finally`.
        for q in list(self._subscribers):
            q.put_nowait(event)

    def publish_notification(self, note: Any) -> None:
        """Fan a :class:`~gauntlet.web.notify.Notification` to open SSE streams.

        The in-tab notify channel (P6, FR-9.2) calls this so a deduplicated
        notification reaches every connected browser as a distinct ``notify`` SSE
        event. It rides the same subscriber queues as transitions; the stream
        type-dispatches by object type, so ordering (transition then its notify)
        is preserved.
        """
        for q in list(self._subscribers):
            q.put_nowait(note)

    def _dispatch_notify(self, event: WatchEvent, *, first: bool) -> None:
        """Hand an emitted transition to the notifier, fail-soft (FR-9.3).

        ``first`` means "prime, do not send": the manifest was first observed
        during the watcher's **initial scan**, so its state predates the server
        and notifying would flood the operator (starting ``gauntlet serve`` over
        a tree of already-parked/finished runs). Every transition observed after
        that initial scan — including a run *first discovered* already parked or
        done while the watcher was already polling — is a real ``notify`` (review
        F-001). A notifier error is logged and swallowed here (on top of the
        per-channel guard) so it can never reach the poll loop and affect a run.
        """
        if self.notifier is None:
            return
        try:
            if first:
                self.notifier.prime(event)
            else:
                self.notifier.notify(event)
        except Exception:  # pragma: no cover - defense in depth (FR-9.3)
            logger.exception("notifier raised on %s; swallowed (FR-9.3)", event.run_id)

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
            current_step_type=cur.type if cur else None,
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
                # Hand the transition to the notifier (P6). Prime (suppress) only
                # during the watcher's *initial* scan, and then only for a run we
                # have no valid identity for yet — a tree of pre-existing
                # parked/done runs must not flood the operator on startup. Once
                # the initial scan is done, any newly discovered manifest is a
                # real transition and must notify, even if first seen already
                # parked/done between polls (review F-001).
                startup = not self._primed
                first = startup and (prev is None or prev[1] is None)
                self._dispatch_notify(event, first=first)
            self._seen[manifest_path] = (mtime_ns, identity)
        # Forget runs whose dir vanished (rare), so memory tracks live runs only.
        for gone in set(self._seen) - live:
            del self._seen[gone]
        # The initial scan is complete; subsequent discoveries are real
        # transitions, not startup state (review F-001).
        self._primed = True
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

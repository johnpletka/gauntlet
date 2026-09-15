"""Watcher — poll manifests, emit edge-triggered transitions (P2, FR-8).

A single async task stats each known projection, journal directory and completion
export once per second and publishes transitions to an event bus feeding the SSE
streams (P2) and, later, the notifier (P6). The watcher owns **no** run state —
it only observes journal-authoritative state. A watcher error cannot affect a run.

**Two uses of the manifest mtime (review F-001/F-002):**

- *File-change detection* uses projection/journal-directory/completion-export ``st_mtime_ns``
  as a cheap gate:
  a changed mtime means "re-parse this file", an unchanged mtime means "skip the
  read" without even parsing.
- *Event identity* is the FR-8.1 tuple ``(run_id, current_step,
  current_step_status, run_status, manifest_revision)``, where
  ``manifest_revision`` is the projection mtime, or journal-directory mtime for
  a journal-only update (or completion-export mtime on import) — the PRD's v1 revision
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

from gauntlet.engine.notify import Transition

from gauntlet.engine.manifest import Manifest
from gauntlet.web.store import RunNotFound, RunStore, _mtime_iso

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 1.0

# The FR-8.1 event identity: four semantic fields plus the manifest revision
# (`st_mtime_ns`). The `None` semantic fields stand in for a manifest we failed
# to parse, so a broken→valid recovery still reads as a transition rather than
# being silently swallowed.
Identity = tuple[str, str | None, str | None, str, int | None]


class WatchEvent(Transition):
    """One observed run transition (the SSE/notify payload, FR-8.2/FR-9.2).

    Extends the engine's :class:`~gauntlet.engine.notify.Transition` (the typed
    run/step state plus the persisted park/halt reasons, #134) with the FR-8.1
    identity fields. ``warnings`` is carried so the notifier can push a
    newly-recorded advisory (FR-10.3); it is NOT part of ``identity`` (a
    manifest rewrite already re-fires via its revision/mtime).
    """

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
        # manifest path → (state-source mtimes, last semantic identity)
        self._seen: dict[Path, tuple[tuple[int | None, ...], Identity | None]] = {}
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
        self, slug: str, man: Manifest, manifest_path: Path, mtime_ns: int | None
    ) -> WatchEvent:
        # The typed state (incl. persisted park/halt reasons) comes from the
        # engine's own transition builder so the console classifies exactly
        # what the driver classifies (#134).
        base = Transition.from_manifest(man, slug=slug)
        return WatchEvent(
            **base.model_dump(),
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
                revision = self.store.state_revision(manifest_path)
            except (OSError, ValueError):
                continue
            prev = self._seen.get(manifest_path)
            if prev is not None and prev[0] == revision:
                continue  # cheap gate: file untouched since last tick
            try:
                man = self.store.manifest(slug, _rid)
            except (OSError, ValueError, RunNotFound):
                # Fail closed: record the mtime so we don't spin re-parsing a
                # torn/broken file, but keep the prior identity so a later valid
                # rewrite still reads as a transition.
                self._seen[manifest_path] = (revision, prev[1] if prev else None)
                continue
            # Preserve the projection-mtime contract when it changes. A
            # journal-only append has its own directory revision, including
            # when the projection is missing or was never updated.
            mtime_ns = revision[0]
            if mtime_ns is None or (prev is not None and prev[0][0] == mtime_ns):
                mtime_ns = max(
                    (value for value in revision[1:] if value is not None), default=None,
                )
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
            self._seen[manifest_path] = (revision, identity)
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

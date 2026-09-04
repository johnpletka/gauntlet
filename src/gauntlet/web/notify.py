"""Console notifier — the in-tab channel plus the engine's channels (P6, FR-9).

Since #134 the channel primitives, the kind table, the transition classifier and
the per-run de-dup ledger live in :mod:`gauntlet.engine.notify` (the *driver*
pushes every park / halt / fail / gate / completion itself, whether or not a
console is running). This module keeps the console-only pieces — the in-tab SSE
channel and the watcher-facing :func:`build_notifier` — and re-exports the
engine names so existing imports keep working. ``web/`` depends on ``engine/``,
never the reverse.

The watcher calls ``prime(event)`` on the first observation of a run (record
its de-dup keys, send nothing — a tree of already-parked runs must not flood the
operator at startup) and ``notify(event)`` on every later transition. De-dup is
keyed ``(run_id, kind, current_step)`` in memory AND through the run's
``notifications.jsonl`` ledger, so a driver announcing its own park and a
watching console never both fire for one transition.

**Fail-soft (FR-9.3):** every channel send is wrapped so an error is logged and
swallowed; the I/O channels run on a daemon thread so a slow endpoint can never
stall the watcher's poll loop. The in-tab channel is loop-thread-only (it puts
onto the asyncio subscriber queues) so it runs inline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from gauntlet.engine.notify import (  # noqa: F401 - re-exported console surface
    ALL_KINDS,
    EMITTER_CONSOLE,
    GAUNTLET_NOTIFY_WEBHOOK_ENV,
    GAUNTLET_SLACK_WEBHOOK_ENV,
    KIND_AUTO_APPROVED,
    KIND_COMPLETED,
    KIND_ESCALATION,
    KIND_FAILED,
    KIND_GATE,
    KIND_HALTED,
    KIND_ORPHANED,
    KIND_PARKED_ARTIFACT_INVALID,
    KIND_PARKED_PROVIDER_UNAVAILABLE,
    KIND_PARKED_RESPONSE,
    KIND_PARKED_USAGE_LIMIT,
    KIND_PARKED_USAGE_WINDOW,
    KIND_WARNING,
    LABELS,
    TRANSITION_KINDS,
    DeliveryError,
    DesktopChannel,
    Notification,
    NotificationLedger,
    Notifier,
    SlackChannel,
    SlackDeliveryError,
    Transition,
    WebhookChannel,
    WebhookDeliveryError,
    auto_approval_warnings,
    build_channels,
    classify_kind,
    next_action_for,
    usage_window_warnings,
)
from gauntlet.web.config import WebNotifyConfig

logger = logging.getLogger(__name__)

# Backward-compatible private alias (pre-#134 name).
_LABELS = LABELS


class InTabChannel:
    """Browser in-tab notification: publish the :class:`Notification` onto the
    watcher's SSE subscriber queues so every open browser shows a `Notification`
    (FR-9.2). Loop-thread-only (it touches asyncio queues), so it runs **inline**
    (``background = False``) — never on a worker thread."""

    name = "in_tab"
    background = False

    def __init__(self, watcher) -> None:
        self._watcher = watcher

    def send(self, note: Notification) -> None:
        self._watcher.publish_notification(note)


def _ledger_dir_for(store):
    """Resolve a transition's run dir through the console store (None = no
    ledger, in-memory de-dup only — the pre-#134 behavior)."""
    if store is None:
        return None

    def _resolve(event: Transition) -> Path | None:
        return store.run_dir(event.slug, event.run_id)

    return _resolve


def _summary_for(store):
    """Gate-reached evidence (#134, rec 10) assembled from the store's tree."""
    if store is None:
        return None

    def _summary(event: Transition, kind: str):
        if kind != KIND_GATE:
            return None
        from gauntlet.engine.gate_evidence import gate_summary
        from gauntlet.engine.manifest import Manifest

        run_dir = store.run_dir(event.slug, event.run_id)
        man = Manifest.load(run_dir / "manifest.json")
        return gate_summary(
            man,
            run_dir=run_dir,
            slug_dir=store.run_root_dir / event.slug,
            repo=store.repo_root,
        )

    return _summary


def build_notifier(
    cfg: WebNotifyConfig,
    *,
    watcher,
    base_url: str = "",
    store=None,
) -> Notifier:
    """Assemble the configured channels (FR-9.4) into a :class:`Notifier`.

    Per-channel on/off comes from the ``web.notify`` config block (which
    inherits the engine's ``notify`` block when absent, #134); the Slack /
    generic webhooks resolve from the block then the env fallbacks. A channel
    with no endpoint is simply not constructed. When ``store`` is given the
    notifier de-dups through each run's ``notifications.jsonl`` ledger, so a
    park the driver already announced is not announced again by the console.
    """
    channels: list = []
    if cfg.in_tab:
        channels.append(InTabChannel(watcher))
    channels.extend(build_channels(cfg))
    return Notifier(
        channels,
        base_url=base_url,
        ledger_dir_for=_ledger_dir_for(store),
        by=EMITTER_CONSOLE,
        kinds=cfg.kinds,
        summary_for=_summary_for(store),
    )


__all__ = [
    "Notifier",
    "Notification",
    "Transition",
    "classify_kind",
    "usage_window_warnings",
    "auto_approval_warnings",
    "next_action_for",
    "build_notifier",
    "build_channels",
    "InTabChannel",
    "DesktopChannel",
    "SlackChannel",
    "WebhookChannel",
    "DeliveryError",
    "SlackDeliveryError",
    "WebhookDeliveryError",
    "NotificationLedger",
    "ALL_KINDS",
    "TRANSITION_KINDS",
    "LABELS",
    "KIND_GATE",
    "KIND_ESCALATION",
    "KIND_FAILED",
    "KIND_COMPLETED",
    "KIND_HALTED",
    "KIND_ORPHANED",
    "KIND_PARKED_RESPONSE",
    "KIND_PARKED_USAGE_LIMIT",
    "KIND_PARKED_PROVIDER_UNAVAILABLE",
    "KIND_PARKED_USAGE_WINDOW",
    "KIND_PARKED_ARTIFACT_INVALID",
    "KIND_WARNING",
    "KIND_AUTO_APPROVED",
    "GAUNTLET_SLACK_WEBHOOK_ENV",
    "GAUNTLET_NOTIFY_WEBHOOK_ENV",
]

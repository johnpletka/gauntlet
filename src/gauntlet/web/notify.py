"""Notifier — fan a run transition out to the operator (P6, FR-9).

The console's whole reason to exist is the two moments that need a human — a
**gate** and a **failure** — plus an **escalation park** (a cycle parked for
upstream reconciliation, EXP-1) and a **completion**. FR-9.1 names exactly these
four *transition kinds*; everything else (a step finishing, a halt, a totals
bump) is observable in the UI but is **not** pushed.

This module hangs off the P2 :class:`~gauntlet.web.watcher.Watcher` event bus
(the watcher imports nothing from here — it holds a duck-typed ``notifier`` with
``prime``/``notify``, so the dependency points one way: notify → watcher). For
every emitted transition the watcher calls:

- ``prime(event)`` on the **first** observation of a run — record its de-dup key
  but send nothing, so starting ``gauntlet serve`` over a tree of already-parked
  / long-finished runs does not flood the operator with notifications for states
  that predate the server (without this, 50 historical ``done`` runs would fire
  50 ``run-completed`` desktop pings on startup);
- ``notify(event)`` on every later transition — send **iff** the FR-9.1 de-dup
  key ``(run_id, kind, current_step)`` has not already fired. Keying on
  ``current_step`` (not ``run_status``) means a run parking at successive gates
  notifies **once per gate** (each a distinct step), while a revision-only
  rewrite of the *same* parked state — which the watcher still emits, because its
  identity includes the manifest revision (FR-8.1) — collapses to one
  notification here.

**Fail-soft (FR-9.3):** every channel send is wrapped so an error is logged and
swallowed; the I/O channels (desktop, Slack) run on a daemon thread so a slow or
hung endpoint can never stall the watcher's poll loop (which shares the asyncio
event loop with every SSE stream). The in-tab channel is loop-thread-only (it
puts onto the asyncio subscriber queues) so it runs inline. A notification
failure can never affect a run — the notifier owns no run state.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading

import httpx
from pydantic import BaseModel

from gauntlet.engine.manifest import (
    PARKED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PARKED,
)
from gauntlet.web.config import WebNotifyConfig
from gauntlet.web.watcher import WatchEvent

logger = logging.getLogger(__name__)

# --- the four FR-9.1 transition kinds ---------------------------------------
KIND_GATE = "gate-reached"
KIND_ESCALATION = "escalation-parked"
KIND_FAILED = "run-failed"
KIND_COMPLETED = "run-completed"
# FR-10.3 advisory usage-window shortfall. Distinct from the four state-machine
# transition kinds above: a warning is a run-level advisory record (stamped in
# manifest.warnings), not a run/step state change, so the notifier handles it as
# its own per-warning stream (deduped by warning text), separate from
# classify_kind. Fires while the run keeps going — the warn-don't-park default.
KIND_WARNING = "usage-window-warning"
# FR-4.1 auto-approval advisory: a clean-signal code gate cleared without a
# human. Like KIND_WARNING it is a run-level advisory record (stamped in
# manifest.warnings), not a state transition — an auto-approved gate is DONE and
# the run keeps going, so classify_kind never sees it. Pushed per distinct
# advisory text so the operator learns of the approval when it happens, not at
# the PR (FR-4.1 "a notification is sent").
KIND_AUTO_APPROVED = "gate-auto-approved"

# Human labels for the notification title, by kind.
_LABELS = {
    KIND_GATE: "Gate reached",
    KIND_ESCALATION: "Escalation — reconcile, then resume",
    KIND_FAILED: "Run failed",
    KIND_COMPLETED: "Run completed",
    KIND_WARNING: "Usage-window warning",
    KIND_AUTO_APPROVED: "Gate auto-approved (FR-4.1)",
}

# The engine-grounded marker an advisory usage-window warning carries (the
# orchestrator stamps "[<step>] usage-window admission (FR-10.3): …" into
# manifest.warnings). Kept LLM-free — a closed, table-tested substring (D8).
_WINDOW_WARNING_MARK = "usage-window admission"

# The engine-grounded marker a cycle-escalation note begins with (engine/cycle.py
# always writes "escalation: …" / "escalation (…): …" on a parked
# adversarial_cycle step). Mirrors `intel._ESCALATION_MARK` — the same closed,
# table-tested vocabulary, kept LLM-free (D8).
_ESCALATION_MARK = "escalation"

# The engine-grounded marker an FR-4.1 auto-approval advisory carries (the
# orchestrator stamps "[<gate>] auto-approval (FR-4.1): …" into
# manifest.warnings). Same closed, LLM-free vocabulary as the window marker.
# An auto-approved gate is a DONE step — it never produces a park transition —
# so without this stream the one human-facing signal FR-4.1 requires at
# approval time would sit unpushed in the manifest until the PR (review F-2).
_AUTO_APPROVAL_MARK = "auto-approval (FR-4.1)"

GAUNTLET_SLACK_WEBHOOK_ENV = "GAUNTLET_SLACK_WEBHOOK"

DEFAULT_SLACK_TIMEOUT_S = 5.0
DEFAULT_DESKTOP_TIMEOUT_S = 5.0


def classify_kind(event: WatchEvent) -> str | None:
    """Map a watcher transition to its FR-9.1 notification kind, or ``None``.

    Keyed on the typed run/step status (and step ``type``) the manifest already
    carries — no model call (D8). A halt parks the run but is **not** a
    ``human_gate`` and **not** an escalation, so it deliberately maps to ``None``
    (FR-9.1: gate-reached is "distinct from a halt"); halts are surfaced in the
    resume panel, not pushed.
    """
    status = event.run_status
    if status == RUN_DONE:
        return KIND_COMPLETED
    if status == RUN_FAILED:
        return KIND_FAILED
    if status == RUN_PARKED and event.current_step_status == PARKED:
        if event.current_step_type == "human_gate":
            return KIND_GATE
        if event.current_step_type == "adversarial_cycle":
            notes = (event.current_step_notes or "").lower()
            if notes.startswith(_ESCALATION_MARK):
                return KIND_ESCALATION
    return None


def usage_window_warnings(event: WatchEvent) -> list[str]:
    """The advisory usage-window shortfalls carried by this event (FR-10.3).

    Filters the run's ``manifest.warnings`` (carried on the event) to the
    usage-window ones by their engine-stamped marker — the notifier pushes one
    per distinct warning, deduped by text. Other manifest warnings (e.g. an
    unrenderable final-gate artifact) are surfaced by ``gauntlet status`` but not
    pushed as this kind."""
    return [w for w in (event.warnings or []) if _WINDOW_WARNING_MARK in w]


def auto_approval_warnings(event: WatchEvent) -> list[str]:
    """The FR-4.1 auto-approval advisories carried by this event.

    Filters ``manifest.warnings`` (carried on the event) to the auto-approval
    ones by their engine-stamped marker; the notifier pushes one per distinct
    advisory, deduped by text — same contract as the usage-window stream."""
    return [w for w in (event.warnings or []) if _AUTO_APPROVAL_MARK in w]


class Notification(BaseModel):
    """A ready-to-deliver notification (the in-tab/SSE payload + channel text).

    Carries everything FR-9.2 requires — slug, run_id, new status, current step
    + note, and a deep link to ``/runs/<slug>`` — plus a rendered ``title`` /
    ``body`` the desktop and Slack channels reuse so the message is identical
    across channels.

    **Deep-link auth (review F-002):** ``url`` is the bare ``/runs/<slug>`` path
    with **no token embedded**, because the same payload feeds external channels
    (Slack, desktop) where a leaked serve token would persist in chat history /
    the macOS notification store. The in-tab channel re-authenticates the link in
    the browser using the current tab's token (see ``static/live.js``). Under the
    P1–P6 ``?token=`` scheme an external-channel link therefore resolves only
    once the operator has an active session; the durable cross-channel deep link
    waits on the P7 ``/login`` cookie flow (deferred by plan).
    """

    slug: str
    run_id: str
    kind: str
    run_status: str
    current_step: str | None = None
    note: str | None = None
    url: str
    title: str
    body: str

    @classmethod
    def build(
        cls,
        event: WatchEvent,
        kind: str,
        *,
        base_url: str = "",
        note: str | None = None,
    ) -> "Notification":
        label = _LABELS.get(kind, kind)
        where = event.current_step or "-"
        # An explicit `note` (a usage-window warning string) wins; otherwise fall
        # back to the current step's note (the gate/escalation case).
        note = (note or "").strip() or (event.current_step_notes or "").strip() or None
        body = f"{event.slug}/{event.run_id} — {where}"
        if note:
            body = f"{body}: {note}"
        return cls(
            slug=event.slug,
            run_id=event.run_id,
            kind=kind,
            run_status=event.run_status,
            current_step=event.current_step,
            note=note,
            url=f"{base_url}/runs/{event.slug}",
            title=f"Gauntlet: {label}",
            body=body,
        )

    def slack_text(self) -> str:
        """The Slack message body (markdown-ish, single blob)."""
        return f"*{self.title}*\n{self.body}\n{self.url}"


# --- channels ----------------------------------------------------------------
# Each channel exposes ``name``, ``background`` (run off-thread to keep the
# watcher loop unblocked), and ``send(note)``. ``send`` may raise; the Notifier
# wraps every call fail-soft (FR-9.3).


class DesktopChannel:
    """macOS desktop notification: ``terminal-notifier`` if on PATH, else
    ``osascript`` (FR-9.2). Off-thread (subprocess) so a slow notifier never
    stalls the loop; on a non-macOS host the tools are simply absent and the send
    fails soft."""

    name = "desktop"
    background = True

    def __init__(self, *, timeout: float = DEFAULT_DESKTOP_TIMEOUT_S) -> None:
        self.timeout = timeout

    def _command(self, note: Notification) -> list[str]:
        tn = shutil.which("terminal-notifier")
        if tn:
            return [tn, "-title", note.title, "-message", note.body, "-open", note.url]
        # AppleScript fallback: `display notification "<body>" with title "<title>"`.
        script = (
            f'display notification {_osa_quote(note.body)} '
            f'with title {_osa_quote(note.title)}'
        )
        return ["osascript", "-e", script]

    def send(self, note: Notification) -> None:
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            self._command(note),
            timeout=self.timeout,
            check=False,
            capture_output=True,
        )


def _osa_quote(value: str) -> str:
    """Quote a string for an AppleScript string literal (escape ``\\`` and ``"``)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class SlackDeliveryError(Exception):
    """A Slack send failure, **sanitized** so it never carries the webhook URL.

    A Slack incoming-webhook URL embeds a secret token in its path, and
    ``httpx`` errors (``HTTPStatusError``, ``ConnectError``, …) put the full
    request URL in their message. Since :meth:`Notifier._dispatch` logs a failed
    send with ``logger.exception``, letting a raw ``httpx`` error escape would
    write the secret to the logs (review F-003). :meth:`SlackChannel.send` raises
    this instead, with only a status code / error class — no URL.
    """


class SlackChannel:
    """Slack incoming-webhook POST (FR-9.2). Off-thread (network). The webhook is
    resolved once at build time; an absent webhook means this channel is never
    constructed, so ``slack: true`` with no webhook is a safe no-op (FR-9.4)."""

    name = "slack"
    background = True

    def __init__(
        self,
        webhook_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_SLACK_TIMEOUT_S,
    ) -> None:
        self.webhook_url = webhook_url
        self._transport = transport
        self.timeout = timeout

    def send(self, note: Notification) -> None:
        # Wrap every httpx failure in a sanitized error: the webhook URL embeds a
        # secret, and the caller logs failures with `logger.exception`, so a raw
        # httpx error (whose message includes the URL) would leak it (F-003).
        # `from None` drops the chained httpx exception so the original message
        # (with the URL) never reaches the logged traceback either.
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout) as client:
                resp = client.post(self.webhook_url, json={"text": note.slack_text()})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SlackDeliveryError(_sanitize_slack_error(exc)) from None


def _sanitize_slack_error(exc: httpx.HTTPError) -> str:
    """A webhook-URL-free description of a Slack send failure (review F-003)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"Slack webhook returned HTTP {status}"
    return f"Slack webhook request failed: {type(exc).__name__}"


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


class Notifier:
    """Edge-triggered, de-duplicated fan-out over a set of channels (FR-9.1/9.3).

    De-dup key is ``(run_id, kind, current_step)`` so each distinct decision
    point notifies once. ``prime`` records a key without sending (startup
    suppression); ``notify`` sends a key the first time it is seen.
    """

    def __init__(self, channels: list, *, base_url: str = "") -> None:
        self.channels = channels
        self.base_url = base_url
        self._fired: set[tuple[str, str, str | None]] = set()

    @staticmethod
    def _key(event: WatchEvent, kind: str) -> tuple[str, str, str | None]:
        return (event.run_id, kind, event.current_step)

    @staticmethod
    def _warning_key(
        event: WatchEvent, kind: str, warning: str
    ) -> tuple[str, str, str | None]:
        """De-dup key for an advisory (usage-window / auto-approval) — keyed on
        the advisory TEXT, not ``current_step``, so a distinct advisory notifies
        exactly once and an unchanged one riding later transitions does not
        re-fire (FR-10.3 / FR-4.1)."""
        return (event.run_id, kind, warning)

    @staticmethod
    def _advisories(event: WatchEvent) -> list[tuple[str, str]]:
        """This event's advisory streams as (kind, text) pairs."""
        return [
            *((KIND_WARNING, w) for w in usage_window_warnings(event)),
            *((KIND_AUTO_APPROVED, w) for w in auto_approval_warnings(event)),
        ]

    def prime(self, event: WatchEvent) -> None:
        """Record the current state's de-dup keys without notifying (FR-9.1).

        Primes the classified transition kind AND every advisory (usage-window,
        auto-approval) already present at first observation — so a run whose
        warnings predate the server does not flood the operator on startup."""
        for kind, warning in self._advisories(event):
            self._fired.add(self._warning_key(event, kind, warning))
        kind = classify_kind(event)
        if kind is not None:
            self._fired.add(self._key(event, kind))

    def notify(self, event: WatchEvent) -> None:
        """Fan out this event's new notifications (FR-9.1/10.3/FR-4.1).

        Independent streams: each newly-recorded advisory (usage-window
        shortfall, auto-approved gate — deduped by text), and the classified
        transition kind (deduped by ``(run_id, kind, current_step)``). A single
        event can carry several — an advisory recorded on the same tick the run
        reaches a gate."""
        for kind, warning in self._advisories(event):
            key = self._warning_key(event, kind, warning)
            if key in self._fired:
                continue
            self._fired.add(key)
            self._fanout(
                Notification.build(
                    event, kind, base_url=self.base_url, note=warning
                )
            )
        kind = classify_kind(event)
        if kind is None:
            return
        key = self._key(event, kind)
        if key in self._fired:
            return
        self._fired.add(key)
        self._fanout(Notification.build(event, kind, base_url=self.base_url))

    def _fanout(self, note: Notification) -> None:
        """Dispatch a built notification over every channel (fail-soft)."""
        for channel in self.channels:
            self._dispatch(channel, note)

    def _dispatch(self, channel, note: Notification) -> None:
        """Send on one channel, fail-soft; off-thread when the channel does I/O."""

        def _run() -> None:
            try:
                channel.send(note)
            except Exception:  # FR-9.3: log and swallow — never reach a run
                logger.exception(
                    "notify channel %r raised on %s/%s; swallowed (FR-9.3)",
                    getattr(channel, "name", channel),
                    note.slug,
                    note.run_id,
                )

        if getattr(channel, "background", False):
            threading.Thread(target=_run, daemon=True).start()
        else:
            _run()


def build_notifier(
    cfg: WebNotifyConfig,
    *,
    watcher,
    base_url: str = "",
) -> Notifier:
    """Assemble the configured channels (FR-9.4) into a :class:`Notifier`.

    Per-channel on/off comes from the ``web.notify`` config block; the Slack
    webhook is resolved from ``cfg.slack_webhook`` then the
    ``GAUNTLET_SLACK_WEBHOOK`` env fallback. ``slack: true`` with no webhook is a
    safe no-op — the channel is simply not constructed.
    """
    channels: list = []
    if cfg.in_tab:
        channels.append(InTabChannel(watcher))
    if cfg.desktop:
        channels.append(DesktopChannel())
    if cfg.slack:
        webhook = cfg.slack_webhook or os.environ.get(GAUNTLET_SLACK_WEBHOOK_ENV)
        if webhook:
            channels.append(SlackChannel(webhook))
    return Notifier(channels, base_url=base_url)


__all__ = [
    "Notifier",
    "Notification",
    "classify_kind",
    "usage_window_warnings",
    "auto_approval_warnings",
    "build_notifier",
    "DesktopChannel",
    "SlackChannel",
    "SlackDeliveryError",
    "InTabChannel",
    "KIND_GATE",
    "KIND_ESCALATION",
    "KIND_FAILED",
    "KIND_COMPLETED",
    "KIND_WARNING",
    "KIND_AUTO_APPROVED",
    "GAUNTLET_SLACK_WEBHOOK_ENV",
]

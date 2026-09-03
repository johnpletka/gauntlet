"""Engine notifications — the channel primitives, the kind table, the ledger.

Issue #134 (park latency dominates run wall-clock): a parked run only makes
progress once a human notices it, so the *driver itself* pushes every park /
fail / gate transition the instant it persists one — not only the web console
(``gauntlet serve``), which is optional and often not running. This module is
the engine-side home for everything the console's ``web/notify.py`` used to own
privately; the console imports and re-exports it (``web/`` depends on
``engine/``, never the reverse).

**Kinds.** Every ``RUN_PARKED`` transition maps to a kind by its persisted
``parked_reason`` (FR-7.2 enum) — ``gate-reached``, ``escalation-parked``,
``parked-for-response``, ``parked-usage-limit``, ``parked-provider-unavailable``,
``parked-usage-window``, ``parked-artifact-invalid`` — plus ``run-halted`` for a
halted step, ``run-failed`` / ``run-completed`` for the terminal run states, and
``run-orphaned`` (emitted only by the console watcher when a running run's
driver is proven dead; the driver cannot report its own death). Two advisory
streams (``usage-window-warning``, ``gate-auto-approved``) ride
``manifest.warnings`` and are console-only.

**De-dup ledger.** ``<run_dir>/notifications.jsonl`` is an append-only record of
every successful channel delivery for the run — one JSON line per acknowledgment, keyed
``[run_id, kind, current_step, iteration, episode]`` and stamped with the emitter (``driver`` /
``console``). Both emitters consult it before sending and append what they
sent, so a driver running under a watching console never double-fires, and a
restarted driver never re-announces a park it already announced. Malformed or
unreadable ledger lines are ignored (fail open on the *ledger*: an unreadable
record must not silence a real park).

**Fail-soft (FR-9.3).** Every channel send is wrapped so an error is logged and
swallowed, and the I/O channels run on a daemon thread. A notification failure
can never affect a run — the notifier owns no run state.
"""

from __future__ import annotations

import json
import fcntl
import hashlib
import time
from contextlib import contextmanager
import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import (
    HALTED,
    PARKED,
    RUN_DONE,
    RUN_FAILED,
    RUN_PARKED,
    Manifest,
    StepRecord,
)

if TYPE_CHECKING:  # pragma: no cover - typing only (config imports nothing from here)
    from gauntlet.engine.config import NotifyConfig

logger = logging.getLogger(__name__)

# --- transition kinds --------------------------------------------------------
# The original four FR-9.1 kinds keep their names (existing config / consumers).
KIND_GATE = "gate-reached"
KIND_ESCALATION = "escalation-parked"
KIND_FAILED = "run-failed"
KIND_COMPLETED = "run-completed"
# #134: every remaining park class, by persisted parked_reason.
KIND_PARKED_USAGE_LIMIT = "parked-usage-limit"
KIND_PARKED_PROVIDER_UNAVAILABLE = "parked-provider-unavailable"
KIND_PARKED_USAGE_WINDOW = "parked-usage-window"
KIND_PARKED_ARTIFACT_INVALID = "parked-artifact-invalid"
# A `response` park that is NOT a cycle escalation (an agent_task UPSTREAM
# CONFLICT park): resolved by `gauntlet resume --response`.
KIND_PARKED_RESPONSE = "parked-for-response"
# A halted step parks the run for a human (FR-3.3) but is neither a gate nor an
# escalation; it now has its own kind so a budget/timeout/judge halt is pushed.
KIND_HALTED = "run-halted"
# A running run whose driver is proven dead (FR-2.4 liveness `orphaned`).
# Emitted ONLY by the console watcher, which observes liveness from outside; a
# driver cannot announce its own death.
KIND_ORPHANED = "run-orphaned"
# FR-10.3 advisory usage-window shortfall. Distinct from the state-machine
# transition kinds above: a warning is a run-level advisory record (stamped in
# manifest.warnings), not a run/step state change, so the notifier handles it as
# its own per-warning stream (deduped by warning text), separate from
# classify_kind. Fires while the run keeps going — the warn-don't-park default.
KIND_WARNING = "usage-window-warning"
# FR-4.1 auto-approval advisory: a clean-signal code gate cleared without a
# human. Like KIND_WARNING it is a run-level advisory record, not a state
# transition — an auto-approved gate is DONE and the run keeps going.
KIND_AUTO_APPROVED = "gate-auto-approved"

# The transition kinds classify_kind can return (the driver's whole vocabulary).
TRANSITION_KINDS: tuple[str, ...] = (
    KIND_GATE,
    KIND_ESCALATION,
    KIND_PARKED_RESPONSE,
    KIND_PARKED_USAGE_LIMIT,
    KIND_PARKED_PROVIDER_UNAVAILABLE,
    KIND_PARKED_USAGE_WINDOW,
    KIND_PARKED_ARTIFACT_INVALID,
    KIND_HALTED,
    KIND_FAILED,
    KIND_COMPLETED,
)
# Every kind a `notify.kinds` allowlist may name.
ALL_KINDS: tuple[str, ...] = (
    *TRANSITION_KINDS,
    KIND_ORPHANED,
    KIND_WARNING,
    KIND_AUTO_APPROVED,
)

# Human labels for the notification title, by kind.
LABELS = {
    KIND_GATE: "Gate reached",
    KIND_ESCALATION: "Escalation — reconcile, then resume",
    KIND_PARKED_RESPONSE: "Parked — decision needed",
    KIND_PARKED_USAGE_LIMIT: "Parked — usage limit",
    KIND_PARKED_PROVIDER_UNAVAILABLE: "Parked — provider unavailable",
    KIND_PARKED_USAGE_WINDOW: "Parked — usage window",
    KIND_PARKED_ARTIFACT_INVALID: "Parked — artifact invalid",
    KIND_HALTED: "Run halted",
    KIND_FAILED: "Run failed",
    KIND_COMPLETED: "Run completed",
    KIND_ORPHANED: "Run orphaned — driver gone",
    KIND_WARNING: "Usage-window warning",
    KIND_AUTO_APPROVED: "Gate auto-approved (FR-4.1)",
}

# The engine-grounded marker an advisory usage-window warning carries (the
# orchestrator stamps "[<step>] usage-window admission (FR-10.3): …" into
# manifest.warnings). Kept LLM-free — a closed, table-tested substring (D8).
_WINDOW_WARNING_MARK = "usage-window admission"
# The engine-grounded marker a cycle-escalation note begins with (engine/cycle.py
# always writes "escalation: …" / "escalation (…): …" on a parked
# adversarial_cycle step). Same closed, LLM-free vocabulary.
_ESCALATION_MARK = "escalation"
# The engine-grounded marker an FR-4.1 auto-approval advisory carries.
_AUTO_APPROVAL_MARK = "auto-approval (FR-4.1)"

GAUNTLET_SLACK_WEBHOOK_ENV = "GAUNTLET_SLACK_WEBHOOK"
GAUNTLET_NOTIFY_WEBHOOK_ENV = "GAUNTLET_NOTIFY_WEBHOOK"
# Driver-side kill switch (tests, CI, a dogfood run you are already watching):
# when set to a truthy value the driver builds no channels at all. The console's
# own notifier is governed by `web.notify` and is untouched.
GAUNTLET_NOTIFY_DISABLED_ENV = "GAUNTLET_NOTIFY_DISABLED"

DEFAULT_SLACK_TIMEOUT_S = 5.0
DEFAULT_WEBHOOK_TIMEOUT_S = 5.0
DEFAULT_DESKTOP_TIMEOUT_S = 5.0

LEDGER_NAME = "notifications.jsonl"

EMITTER_DRIVER = "driver"
EMITTER_CONSOLE = "console"

# De-dup key: (run_id, kind, current_step) — FR-9.1.
Key = tuple[str | None, ...]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the observed transition --------------------------------------------------
_TERMINAL_STEP_STATES = frozenset({M.DONE, M.FAILED, M.SKIPPED, M.HALTED})


def current_record(man: Manifest) -> StepRecord | None:
    """The step record ``current_step`` points at, or None.

    A ``foreach`` fan-out stores several records under one ``id`` (one per
    ``iteration``) while ``current_step`` carries only the id. Prefer the
    *active* (non-terminal) matching record so a park reflects the parked
    iteration rather than a completed earlier one; fall back to the last
    matching record when every iteration is terminal.
    """
    if not man.current_step:
        return None
    matches = [rec for rec in man.steps if rec.id == man.current_step]
    if not matches:
        return None
    active = [rec for rec in matches if rec.status not in _TERMINAL_STEP_STATES]
    return active[-1] if active else matches[-1]


class Transition(BaseModel):
    """One observed run state — the classifier's input and the payload's source.

    Built from a persisted manifest (:meth:`from_manifest`) by the driver, and
    subclassed by the console watcher's ``WatchEvent`` (which adds the FR-8.1
    identity fields). Carries the typed reason fields the FR-7.2 enum already
    persists, so classification never parses a note where a reason exists.
    """

    slug: str
    run_id: str
    run_status: str
    current_step: str | None = None
    current_step_status: str | None = None
    current_step_type: str | None = None
    current_step_notes: str | None = None
    iteration: str | None = None
    episode: str | None = None
    parked_reason: str | None = None
    halt_reason: str | None = None
    # The persisted park deadline (usage-limit reset / provider backoff /
    # window replenishment), when the park recorded one.
    quota_reset_at: str | None = None
    # Whether an in-process auto-resume schedule is armed on the parked step
    # (`resume_on_quota: auto`, FR-3.4).
    auto_resume_armed: bool = False
    # Run-level non-fatal anomalies (manifest.warnings) for the advisory streams.
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_manifest(cls, man: Manifest, *, slug: str | None = None) -> "Transition":
        cur = current_record(man)
        return cls(
            slug=slug or man.slug,
            run_id=man.run_id,
            run_status=man.status,
            current_step=man.current_step,
            current_step_status=cur.status if cur else None,
            current_step_type=cur.type if cur else None,
            current_step_notes=cur.notes if cur else None,
            iteration=cur.iteration if cur else None,
            episode=cur.ended if cur else None,
            parked_reason=(
                M.normalize_parked_reason(cur.parked_reason, cur.type, cur.status)
                if cur
                else None
            ),
            halt_reason=cur.halt_reason if cur else None,
            quota_reset_at=cur.quota_reset_at if cur else None,
            auto_resume_armed=bool(cur and cur.scheduled_resume is not None),
            warnings=list(man.warnings),
        )


def classify_kind(event: Transition) -> str | None:
    """Map an observed transition to its notification kind, or ``None``.

    Keyed on the typed run/step status and the persisted ``parked_reason`` /
    step ``type`` — no model call (D8). Every park class maps (#134); a running
    run and a non-terminal step map to ``None``.
    """
    status = event.run_status
    if status == RUN_DONE:
        return KIND_COMPLETED
    if status == RUN_FAILED:
        return KIND_FAILED
    if status != RUN_PARKED:
        return None
    step_status = event.current_step_status
    if step_status == HALTED:
        return KIND_HALTED
    if step_status != PARKED:
        return None
    reason = event.parked_reason
    # A human_gate park is a gate whatever its (possibly legacy-null) reason.
    if event.current_step_type == "human_gate" or reason == M.PARKED_REASON_GATE:
        return KIND_GATE
    if reason == M.PARKED_REASON_USAGE_LIMIT:
        return KIND_PARKED_USAGE_LIMIT
    if reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE:
        return KIND_PARKED_PROVIDER_UNAVAILABLE
    if reason == M.PARKED_REASON_USAGE_WINDOW:
        return KIND_PARKED_USAGE_WINDOW
    if reason == M.PARKED_REASON_ARTIFACT_INVALID:
        return KIND_PARKED_ARTIFACT_INVALID
    # A `response` park is an escalation when it is a cycle parked on an
    # escalation note; any other response park needs `resume --response`.
    notes = (event.current_step_notes or "").lower().lstrip()
    if event.current_step_type == "adversarial_cycle" and notes.startswith(
        _ESCALATION_MARK
    ):
        return KIND_ESCALATION
    if reason == M.PARKED_REASON_RESPONSE:
        return KIND_PARKED_RESPONSE
    return None


def usage_window_warnings(event: Transition) -> list[str]:
    """The advisory usage-window shortfalls carried by this event (FR-10.3)."""
    return [w for w in (event.warnings or []) if _WINDOW_WARNING_MARK in w]


def auto_approval_warnings(event: Transition) -> list[str]:
    """The FR-4.1 auto-approval advisories carried by this event."""
    return [w for w in (event.warnings or []) if _AUTO_APPROVAL_MARK in w]


# --- the notification -----------------------------------------------------------
def next_action_for(event: Transition, kind: str) -> str | None:
    """The operator's next CLI verb for this transition, as one line.

    Grounded in the same next-action vocabulary `gauntlet status` renders — a
    notification should tell the operator what to type, not just that
    something happened.
    """
    slug = event.slug
    deadline = event.quota_reset_at or "no reset time reported"
    if kind == KIND_GATE:
        return f"gauntlet approve {slug}  |  gauntlet reject {slug} --notes '…'"
    if kind in (KIND_ESCALATION, KIND_PARKED_RESPONSE):
        return f"gauntlet resume {slug} --response '<decision>'"
    if kind == KIND_PARKED_USAGE_LIMIT:
        if event.auto_resume_armed:
            return f"auto-resume armed (reset at {deadline}); nothing to do unless it exhausts"
        return f"gauntlet resume {slug} once the limit clears (reset at {deadline})"
    if kind == KIND_PARKED_PROVIDER_UNAVAILABLE:
        return f"gauntlet resume {slug} retries the step (backoff until {deadline})"
    if kind == KIND_PARKED_USAGE_WINDOW:
        return f"gauntlet resume {slug} once the window replenishes ({deadline})"
    if kind == KIND_PARKED_ARTIFACT_INVALID:
        return f"fix the artifact, then gauntlet resume {slug} (re-validates only)"
    if kind == KIND_HALTED:
        return f"gauntlet status {slug} — inspect the halt, then gauntlet resume {slug}"
    if kind == KIND_FAILED:
        return f"gauntlet status {slug} — inspect the failure; gauntlet resume {slug} re-runs the step"
    if kind == KIND_COMPLETED:
        return f"review the PR.md draft, then gauntlet finish {slug}"
    if kind == KIND_ORPHANED:
        return f"gauntlet status {slug} — the driver died mid-step; gauntlet resume {slug} reclaims it"
    return None


class Notification(BaseModel):
    """A ready-to-deliver notification (the in-tab/SSE payload + channel text).

    Carries everything FR-9.2 requires — slug, run_id, new status, current step
    + note, and a deep link to ``/runs/<slug>`` — plus a rendered ``title`` /
    ``body`` the desktop and Slack channels reuse so the message is identical
    across channels, and (#134) the typed reason fields, the next CLI action,
    and an optional structured ``summary`` (gate evidence) the Slack text and
    webhook payload carry. Desktop keeps the short body.

    **Deep-link auth (review F-002):** ``url`` is the bare ``/runs/<slug>`` path
    with **no token embedded**, because the same payload feeds external channels
    (Slack, desktop, webhook) where a leaked serve token would persist.
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
    step_type: str | None = None
    parked_reason: str | None = None
    halt_reason: str | None = None
    next_action: str | None = None
    emitted_at: str = Field(default_factory=_utc_now)
    # Absolute console URL when the emitter knows one (the console does; the
    # driver does not), else None.
    console_url: str | None = None
    summary: dict[str, Any] | None = None

    @classmethod
    def build(
        cls,
        event: Transition,
        kind: str,
        *,
        base_url: str = "",
        note: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> "Notification":
        label = LABELS.get(kind, kind)
        where = event.current_step or "-"
        # An explicit `note` (an advisory warning string) wins; otherwise fall
        # back to the current step's note (the gate/escalation/park case).
        note = (note or "").strip() or (event.current_step_notes or "").strip() or None
        body = f"{event.slug}/{event.run_id} — {where}"
        if note:
            body = f"{body}: {note}"
        if kind in (KIND_PARKED_USAGE_LIMIT, KIND_PARKED_PROVIDER_UNAVAILABLE):
            deadline = event.quota_reset_at or "no reset time reported"
            armed = "armed" if event.auto_resume_armed else "not armed"
            body = f"{body} — deadline {deadline}; auto-resume {armed}"
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
            step_type=event.current_step_type,
            parked_reason=event.parked_reason,
            halt_reason=event.halt_reason,
            next_action=next_action_for(event, kind),
            console_url=f"{base_url}/runs/{event.slug}" if base_url else None,
            summary=summary,
        )

    def slack_text(self) -> str:
        """The Slack message body (markdown-ish, single blob)."""
        lines = [f"*{self.title}*", self.body]
        if self.next_action:
            lines.append(f"next: {self.next_action}")
        if self.summary:
            lines.extend(render_summary(self.summary))
        lines.append(self.url)
        return "\n".join(lines)

    def webhook_payload(self) -> dict[str, Any]:
        """The generic JSON webhook body (#134, rec 6)."""
        return {
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "run_id": self.run_id,
            "slug": self.slug,
            "run_status": self.run_status,
            "current_step": self.current_step,
            "step_type": self.step_type,
            "parked_reason": self.parked_reason,
            "halt_reason": self.halt_reason,
            "next_action": self.next_action,
            "emitted_at": self.emitted_at,
            "console_url": self.console_url,
            "summary": self.summary,
        }


_MAX_DIFF_STAT_LINES = 25


def render_summary(summary: dict[str, Any]) -> list[str]:
    """Render a gate-evidence summary (see ``gate_evidence.gate_summary``) as
    plain lines for a text channel. Tolerates any missing part."""
    lines: list[str] = []
    rng = summary.get("range") or {}
    if rng.get("base") and rng.get("head"):
        lines.append(f"range: {rng['base'][:12]}..{rng['head'][:12]}")
    stat = summary.get("diff_stat")
    if stat:
        stat_lines = stat.rstrip("\n").splitlines()
        if len(stat_lines) > _MAX_DIFF_STAT_LINES:
            omitted = len(stat_lines) - _MAX_DIFF_STAT_LINES + 1
            stat_lines = stat_lines[: _MAX_DIFF_STAT_LINES - 1] + [
                f"… ({omitted} more lines)",
                stat_lines[-1],
            ]
        lines.append("diff --stat:")
        lines.extend(f"  {ln}" for ln in stat_lines)
    findings = summary.get("findings")
    if findings:
        by = findings.get("by_severity") or {}
        parts = ", ".join(f"{k} {v}" for k, v in by.items())
        lines.append(f"findings: {findings.get('total', 0)}" + (f" ({parts})" if parts else ""))
    triage = summary.get("triage")
    if triage:
        by = triage.get("by_action") or {}
        parts = ", ".join(f"{k} {v}" for k, v in by.items())
        lines.append(f"triage: {triage.get('total', 0)}" + (f" ({parts})" if parts else ""))
    usage = summary.get("usage")
    if usage:
        cost = usage.get("cost_usd")
        cost_s = f", ${cost:.2f}" if isinstance(cost, (int, float)) else ""
        lines.append(
            f"usage so far: {usage.get('input_tokens', 0)} in / "
            f"{usage.get('output_tokens', 0)} out tokens{cost_s}"
        )
    elapsed = summary.get("elapsed_s")
    if isinstance(elapsed, (int, float)):
        lines.append(f"elapsed: {_fmt_duration(elapsed)}")
    return lines


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# --- channels ----------------------------------------------------------------
# Each channel exposes ``name``, ``background`` (run off-thread to keep the
# caller unblocked), and ``send(note)``. ``send`` may raise; the Notifier wraps
# every call fail-soft (FR-9.3).


class DesktopChannel:
    """macOS desktop notification: ``terminal-notifier`` if on PATH, else
    ``osascript`` (FR-9.2). Off-thread (subprocess) so a slow notifier never
    stalls the caller; on a non-macOS host the tools are simply absent and the
    send fails soft."""

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
            check=True,
            capture_output=True,
        )


def _osa_quote(value: str) -> str:
    """Quote a string for an AppleScript string literal (escape ``\\`` and ``"``)."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class DeliveryError(Exception):
    """A channel send failure, **sanitized** so it never carries the endpoint URL.

    A Slack incoming-webhook URL (and, typically, a generic webhook URL) embeds a
    secret in its path, and ``httpx`` errors put the full request URL in their
    message. Since :meth:`Notifier._dispatch` logs a failed send with
    ``logger.exception``, letting a raw ``httpx`` error escape would write the
    secret to the logs (review F-003). The HTTP channels raise a subclass of
    this instead, with only a status code / error class — no URL.
    """


class SlackDeliveryError(DeliveryError):
    """A Slack send failure, sanitized (no webhook URL)."""


class WebhookDeliveryError(DeliveryError):
    """A generic-webhook send failure, sanitized (no URL)."""


def _sanitize_http_error(exc: httpx.HTTPError, label: str) -> str:
    """A URL-free description of an HTTP send failure (review F-003)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"{label} returned HTTP {status}"
    return f"{label} request failed: {type(exc).__name__}"


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
        # `from None` drops the chained httpx exception so the original message
        # (with the URL) never reaches the logged traceback either.
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout) as client:
                resp = client.post(self.webhook_url, json={"text": note.slack_text()})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SlackDeliveryError(_sanitize_http_error(exc, "Slack webhook")) from None


class WebhookChannel:
    """Generic JSON webhook POST (#134, rec 6): the :meth:`Notification.webhook_payload`
    object, off-thread, bounded by ``timeout``, errors sanitized (the URL may
    embed a secret and is never logged). Resolved from ``notify.webhook_url`` or
    the ``GAUNTLET_NOTIFY_WEBHOOK`` env fallback; absent → never constructed."""

    name = "webhook"
    background = True

    def __init__(
        self,
        url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_WEBHOOK_TIMEOUT_S,
    ) -> None:
        self.url = url
        self._transport = transport
        self.timeout = timeout

    def send(self, note: Notification) -> None:
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout) as client:
                resp = client.post(self.url, json=note.webhook_payload())
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise WebhookDeliveryError(_sanitize_http_error(exc, "notify webhook")) from None


def resolve_slack_webhook(configured: str | None) -> str | None:
    """``notify.slack_webhook`` then the ``GAUNTLET_SLACK_WEBHOOK`` env fallback."""
    return configured or os.environ.get(GAUNTLET_SLACK_WEBHOOK_ENV) or None


def resolve_webhook_url(configured: str | None) -> str | None:
    """``notify.webhook_url`` then the ``GAUNTLET_NOTIFY_WEBHOOK`` env fallback."""
    return configured or os.environ.get(GAUNTLET_NOTIFY_WEBHOOK_ENV) or None


def build_channels(cfg: "NotifyConfig | Any") -> list:
    """The external channels a ``notify`` block enables (FR-9.4 semantics).

    Slack / webhook are constructed only when a URL resolves, so the defaults
    (`slack: true`, `webhook: true`, no URL) are safe no-ops. Duck-typed over
    the engine ``NotifyConfig`` and the console ``WebNotifyConfig`` (which
    carries the same fields plus ``in_tab``)."""
    channels: list = []
    if getattr(cfg, "desktop", False):
        channels.append(DesktopChannel())
    if getattr(cfg, "slack", False):
        webhook = resolve_slack_webhook(getattr(cfg, "slack_webhook", None))
        if webhook:
            channels.append(SlackChannel(webhook))
    if getattr(cfg, "webhook", False):
        url = resolve_webhook_url(getattr(cfg, "webhook_url", None))
        if url:
            channels.append(WebhookChannel(url))
    return channels


def driver_notifications_disabled() -> bool:
    """The ``GAUNTLET_NOTIFY_DISABLED`` kill switch (driver side only)."""
    return os.environ.get(GAUNTLET_NOTIFY_DISABLED_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


# --- the de-dup ledger ----------------------------------------------------------
class NotificationLedger:
    """``<run_dir>/notifications.jsonl`` — append-only, one line per emitted
    notification: ``{key: [run_id, kind, current_step], kind, emitted_at,
    channels: [names], by: "driver"|"console"}``.

    Read fail-open: an unreadable file or a malformed line is ignored (the
    notification is emitted anyway — an unreadable record must not silence a
    real park). Written fail-soft: an append failure is logged and swallowed."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_run_dir(cls, run_dir: Path) -> "NotificationLedger":
        return cls(Path(run_dir) / LEDGER_NAME)

    def keys(self) -> set[Key]:
        out: set[Key] = set()
        try:
            text = self.path.read_text()
        except OSError:
            return out
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            key = rec.get("key") if isinstance(rec, dict) else None
            if (
                isinstance(key, list)
                and len(key) in (3, 5)
                and isinstance(key[0], str)
                and isinstance(key[1], str)
                and (key[2] is None or isinstance(key[2], str))
            ):
                out.add(tuple(key))
        return out

    def entries(self) -> list[dict]:
        """Every well-formed ledger line, in order (for tests / evidence)."""
        out: list[dict] = []
        try:
            text = self.path.read_text()
        except OSError:
            return out
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def delivered(self, key: Key, channel: str) -> bool:
        return any(rec.get("key") == list(key) and rec.get("status") == "delivered"
                   and channel in rec.get("channels", []) for rec in self.entries())

    @contextmanager
    def delivery_lock(self, channel: str):
        """Serialize check/send/ack across driver and console, with a bound."""
        suffix = hashlib.sha256(channel.encode()).hexdigest()[:12]
        path = self.path.with_name(f"{self.path.name}.{suffix}.lock")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a")
        except OSError:
            # Broken ledger storage must not suppress the notification.
            yield
            return
        try:
            deadline = time.monotonic() + 6
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("notification delivery lock timed out")
                    time.sleep(0.02)
            yield
        finally:
            handle.close()

    def append(self, key: Key, *, kind: str, channels: list[str], by: str) -> None:
        rec = {
            "key": list(key),
            "kind": kind,
            "emitted_at": _utc_now(),
            "channels": list(channels),
            "status": "delivered",
            "by": by,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")
                fh.flush()
        except OSError:
            logger.warning(
                "notification ledger append failed at %s; continuing (FR-9.3)",
                self.path, exc_info=True,
            )


# --- the notifier -----------------------------------------------------------------
SummaryFn = Callable[[Transition, str], "dict[str, Any] | None"]
LedgerDirFn = Callable[[Transition], "Path | None"]


class Notifier:
    """Edge-triggered, de-duplicated fan-out over a set of channels (FR-9.1/9.3).

    De-dup key is ``(run_id, kind, current_step, iteration, episode)`` so each distinct decision
    point notifies once — in memory for this process AND, when ``ledger_dir_for``
    resolves the run's dir, through the run's persisted ledger (#134), so the
    driver and a watching console never both fire for one transition and a
    restarted emitter never re-announces. ``prime`` records a key without
    sending (startup suppression); ``notify`` sends a key the first time it is
    seen. ``kinds`` is an optional allowlist; ``summary_for`` supplies the rich
    evidence block for a kind (fail-soft — a summary failure never blocks the
    short notification).
    """

    def __init__(
        self,
        channels: list,
        *,
        base_url: str = "",
        ledger_dir_for: LedgerDirFn | None = None,
        by: str = EMITTER_CONSOLE,
        kinds: list[str] | None = None,
        summary_for: SummaryFn | None = None,
    ) -> None:
        self.channels = channels
        self.base_url = base_url
        self.ledger_dir_for = ledger_dir_for
        self.by = by
        self.kinds = set(kinds) if kinds is not None else None
        self.summary_for = summary_for
        self._fired: set[Key] = set()  # explicit startup suppression / advisories
        self._delivered: set[tuple[Key, str]] = set()
        self._inflight: set[tuple[Key, str]] = set()
        self._threads: list[threading.Thread] = []
        self._mutex = threading.Lock()

    @staticmethod
    def _key(event: Transition, kind: str) -> Key:
        return (event.run_id, kind, event.current_step, event.iteration, event.episode)

    @staticmethod
    def _warning_key(event: Transition, kind: str, warning: str) -> Key:
        """De-dup key for an advisory (usage-window / auto-approval) — keyed on
        the advisory TEXT, not ``current_step``, so a distinct advisory notifies
        exactly once and an unchanged one riding later transitions does not
        re-fire (FR-10.3 / FR-4.1)."""
        return (event.run_id, kind, warning)

    @staticmethod
    def _advisories(event: Transition) -> list[tuple[str, str]]:
        """This event's advisory streams as (kind, text) pairs."""
        return [
            *((KIND_WARNING, w) for w in usage_window_warnings(event)),
            *((KIND_AUTO_APPROVED, w) for w in auto_approval_warnings(event)),
        ]

    def _ledger(self, event: Transition) -> NotificationLedger | None:
        if self.ledger_dir_for is None:
            return None
        try:
            run_dir = self.ledger_dir_for(event)
        except Exception:  # fail-soft: a path-resolution failure is not a reason to stay silent
            logger.warning(
                "notification ledger dir unresolvable for %s/%s; emitting without it",
                event.slug, event.run_id, exc_info=True,
            )
            return None
        return NotificationLedger.for_run_dir(run_dir) if run_dir is not None else None

    def prime(self, event: Transition) -> None:
        """Record the current state's de-dup keys without notifying (FR-9.1).

        Primes the classified transition kind AND every advisory (usage-window,
        auto-approval) already present at first observation — so a run whose
        state predates the observer does not flood the operator on startup."""
        for kind, warning in self._advisories(event):
            self._fired.add(self._warning_key(event, kind, warning))
        kind = classify_kind(event)
        if kind is not None:
            self._fired.add(self._key(event, kind))

    def prime_kind(self, event: Transition, kind: str) -> None:
        """Record one explicit kind's key without notifying (the watcher's
        orphan stream)."""
        self._fired.add(self._key(event, kind))

    def notify(self, event: Transition) -> None:
        """Fan out this event's new notifications (FR-9.1/10.3/FR-4.1).

        Independent streams: each newly-recorded advisory (usage-window
        shortfall, auto-approved gate — deduped by text, in memory only), and
        the classified transition kind (:meth:`notify_transition`)."""
        for kind, warning in self._advisories(event):
            key = self._warning_key(event, kind, warning)
            if key in self._fired or not self._allowed(kind):
                continue
            self._fired.add(key)
            self._fanout(
                Notification.build(event, kind, base_url=self.base_url, note=warning)
            )
        self.notify_transition(event)

    def notify_transition(self, event: Transition) -> str | None:
        """Emit the classified transition kind, if any and not yet fired.

        The driver's entry point (advisories are console-only). Returns the
        kind emitted, or ``None``."""
        kind = classify_kind(event)
        if kind is None:
            return None
        return self.emit(event, kind)

    def emit(self, event: Transition, kind: str, *, note: str | None = None) -> str | None:
        """Send one explicit kind for this event unless its key already fired
        (memory or ledger). Records the send in the ledger. Returns the kind on
        a send, else ``None``."""
        if not self._allowed(kind):
            return None
        key = self._key(event, kind)
        ledger = self._ledger(event)
        if key in self._fired:
            return None
        channels = [ch for ch in self.channels
                    if (key, ch.name) not in self._delivered
                    and (key, ch.name) not in self._inflight
                    and not (ledger and ledger.delivered(key, ch.name))]
        if not channels:
            return None
        summary = None
        if self.summary_for is not None:
            try:
                summary = self.summary_for(event, kind)
            except Exception:  # FR-9.3: the short notification still goes out
                logger.warning(
                    "notification summary failed for %s/%s (%s); sending without it",
                    event.slug, event.run_id, kind, exc_info=True,
                )
        note_obj = Notification.build(
            event, kind, base_url=self.base_url, note=note, summary=summary
        )
        for channel in channels:
            self._dispatch(channel, note_obj, key=key, ledger=ledger)
        return kind

    def _allowed(self, kind: str) -> bool:
        return self.kinds is None or kind in self.kinds

    def _fanout(self, note: Notification) -> None:
        """Dispatch a built notification over every channel (fail-soft)."""
        for channel in self.channels:
            self._dispatch(channel, note)

    def _dispatch(self, channel, note: Notification, *, key=None, ledger=None) -> None:
        """Acknowledge only successful sends; failures remain retryable."""
        delivery = (key, channel.name)
        with self._mutex:
            if key is not None and (delivery in self._inflight or delivery in self._delivered):
                return
            self._inflight.add(delivery)

        def send_and_ack():
            if ledger and ledger.delivered(key, channel.name):
                return
            channel.send(note)
            if ledger:
                ledger.append(key, kind=note.kind, channels=[channel.name], by=self.by)
            with self._mutex:
                self._delivered.add(delivery)

        def run():
            try:
                if ledger:
                    with ledger.delivery_lock(channel.name):
                        send_and_ack()
                else:
                    send_and_ack()
            except Exception:
                logger.exception("notify channel %r failed; delivery remains retryable", channel.name)
            finally:
                with self._mutex:
                    self._inflight.discard(delivery)

        if getattr(channel, "background", False):
            thread = threading.Thread(target=run, daemon=True)
            with self._mutex:
                self._threads = [t for t in self._threads if t.is_alive()]
                self._threads.append(thread)
                thread.start()
        else:
            run()

    def flush(self, timeout: float = 12.0) -> None:
        """Give pending channels a bounded chance to finish before CLI exit."""
        deadline = time.monotonic() + timeout
        with self._mutex:
            pending = list(self._threads)
        for thread in pending:
            thread.join(max(0, deadline - time.monotonic()))


def emit_driver_notification(
    man: Manifest, *, notifier: Notifier | None, slug: str | None = None
) -> str | None:
    """The driver's post-persist hook body: classify the persisted manifest
    state and emit through ``notifier``. Never raises — a notification failure
    can never affect run state (FR-9.3). Returns the kind emitted, if any."""
    if notifier is None:
        return None
    try:
        event = Transition.from_manifest(man, slug=slug)
        kind = notifier.notify_transition(event)
        notifier.flush()
        return kind
    except Exception:
        logger.warning(
            "driver notification failed for %s/%s; run state unaffected (FR-9.3)",
            getattr(man, "slug", "?"), getattr(man, "run_id", "?"), exc_info=True,
        )
        return None


__all__ = [
    "ALL_KINDS",
    "TRANSITION_KINDS",
    "LABELS",
    "KIND_GATE",
    "KIND_ESCALATION",
    "KIND_FAILED",
    "KIND_COMPLETED",
    "KIND_PARKED_USAGE_LIMIT",
    "KIND_PARKED_PROVIDER_UNAVAILABLE",
    "KIND_PARKED_USAGE_WINDOW",
    "KIND_PARKED_ARTIFACT_INVALID",
    "KIND_PARKED_RESPONSE",
    "KIND_HALTED",
    "KIND_ORPHANED",
    "KIND_WARNING",
    "KIND_AUTO_APPROVED",
    "GAUNTLET_SLACK_WEBHOOK_ENV",
    "GAUNTLET_NOTIFY_WEBHOOK_ENV",
    "GAUNTLET_NOTIFY_DISABLED_ENV",
    "LEDGER_NAME",
    "EMITTER_DRIVER",
    "EMITTER_CONSOLE",
    "Transition",
    "current_record",
    "classify_kind",
    "usage_window_warnings",
    "auto_approval_warnings",
    "next_action_for",
    "Notification",
    "render_summary",
    "DesktopChannel",
    "SlackChannel",
    "WebhookChannel",
    "DeliveryError",
    "SlackDeliveryError",
    "WebhookDeliveryError",
    "resolve_slack_webhook",
    "resolve_webhook_url",
    "build_channels",
    "driver_notifications_disabled",
    "NotificationLedger",
    "Notifier",
    "emit_driver_notification",
]

"""Console-only configuration parsing (`web:` block, FR-9.4).

These models live in `web/` — **not** in `engine/config.py` — because the
console is strictly above the orchestrator: the approved console plan permits
exactly two engine modifications, both in P3's `run.py`/`cli.py`, and forbids
widening any other engine surface (plan ground rules, review F-004). The
engine's :class:`~gauntlet.engine.config.RunConfig` carries ``extra="allow"``,
so a ``web:`` block in ``.gauntlet/config.yaml`` is preserved verbatim as an
extra field and ignored by every engine code path; the console parses and
validates it here, when it builds the notifier.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WebNotifyConfig(BaseModel):
    """Console notification settings (`web.notify`, FR-9.4).

    Per-channel on/off plus the Slack webhook. Additive and backward-compatible:
    an absent ``web:`` block leaves every channel at its default. The Slack
    channel only actually fires when a webhook is resolved — from ``slack_webhook``
    here or the ``GAUNTLET_SLACK_WEBHOOK`` env fallback (FR-9.4) — so ``slack:
    true`` with no webhook is a safe no-op, not an error.
    """

    model_config = ConfigDict(extra="forbid")

    desktop: bool = True  # macOS desktop (terminal-notifier / osascript)
    slack: bool = True  # Slack incoming-webhook POST (needs a webhook to fire)
    in_tab: bool = True  # browser in-tab Notification over SSE
    slack_webhook: str | None = None  # falls back to GAUNTLET_SLACK_WEBHOOK


class WebConfig(BaseModel):
    """Optional `web:` block for the console (FR-9.4). Additive; defaults preserve
    pre-console behavior."""

    model_config = ConfigDict(extra="allow")

    notify: WebNotifyConfig = Field(default_factory=WebNotifyConfig)


def web_config_from(config: object) -> WebConfig:
    """Extract the console :class:`WebConfig` from a loaded ``RunConfig``.

    The engine keeps console settings out of its schema (review F-004), so the
    ``web:`` block rides on ``RunConfig`` as an ``extra="allow"`` field — a raw
    ``dict`` (from YAML) or absent. Validate it here, at the console layer, so a
    malformed ``web.notify`` block fails closed when ``gauntlet serve`` builds
    the notifier rather than silently degrading. An absent block yields defaults.
    """
    raw = getattr(config, "web", None)
    if isinstance(raw, WebConfig):
        return raw
    if isinstance(raw, dict):
        return WebConfig.model_validate(raw)
    return WebConfig()


__all__ = ["WebConfig", "WebNotifyConfig", "web_config_from"]

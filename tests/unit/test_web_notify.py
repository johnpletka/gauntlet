"""Notifications (P6, FR-9): edge-triggered, de-duplicated, fail-soft fan-out.

The notifier hangs off the P2 watcher's event bus. These tests cover the three
load-bearing claims:

- **Edge-triggered + de-duplicated per `(run_id, kind, current_step)`** — a run
  parking at two gates in *different* steps notifies twice (not once); a
  revision-only rewrite of the same parked state notifies zero more times.
- **The four FR-9.1 kinds** — gate-reached / escalation-parked / run-failed /
  run-completed are classified from typed manifest state (no model call), and a
  halt deliberately is **not** a kind.
- **Fail-soft (FR-9.3)** — a raising channel is swallowed; the watcher (and so
  any run) is unaffected.

Plus the Slack call shape via a mocked `httpx` transport, in-tab fan-out onto the
SSE queues, startup priming (no flood over pre-existing state), and the config
block.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.web.config import WebNotifyConfig, web_config_from
from gauntlet.web.notify import (
    GAUNTLET_SLACK_WEBHOOK_ENV,
    KIND_COMPLETED,
    KIND_ESCALATION,
    KIND_FAILED,
    KIND_GATE,
    Notification,
    Notifier,
    SlackChannel,
    SlackDeliveryError,
    build_notifier,
    classify_kind,
)
from gauntlet.web.store import RunStore
from gauntlet.web.watcher import WatchEvent, Watcher


# --- helpers -----------------------------------------------------------------


def _event(
    *,
    run_id: str = "run-1",
    slug: str = "alpha",
    run_status: str,
    current_step: str | None = None,
    current_step_status: str | None = None,
    current_step_type: str | None = None,
    current_step_notes: str | None = None,
    revision: int | None = 1,
) -> WatchEvent:
    return WatchEvent(
        slug=slug,
        run_id=run_id,
        run_status=run_status,
        current_step=current_step,
        current_step_status=current_step_status,
        current_step_type=current_step_type,
        current_step_notes=current_step_notes,
        revision=revision,
    )


def _gate(step: str, *, run_id: str = "run-1") -> WatchEvent:
    return _event(
        run_id=run_id,
        run_status="parked",
        current_step=step,
        current_step_status="parked",
        current_step_type="human_gate",
    )


def _escalation(step: str, notes: str, *, run_id: str = "run-1") -> WatchEvent:
    return _event(
        run_id=run_id,
        run_status="parked",
        current_step=step,
        current_step_status="parked",
        current_step_type="adversarial_cycle",
        current_step_notes=notes,
    )


class RecordingChannel:
    """A synchronous (inline) stub channel that records every send."""

    name = "rec"
    background = False

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, note: Notification) -> None:
        self.sent.append(note)


class RaisingChannel:
    name = "boom"
    background = False

    def send(self, note: Notification) -> None:
        raise RuntimeError("channel exploded")


# --- classify_kind (the four FR-9.1 kinds) -----------------------------------


def test_classify_kind_table():
    assert classify_kind(_gate("gate")) == KIND_GATE
    assert (
        classify_kind(_escalation("impl-cycle", "escalation: F-003 …"))
        == KIND_ESCALATION
    )
    assert classify_kind(_event(run_status="failed", current_step="impl",
                                current_step_status="failed")) == KIND_FAILED
    assert classify_kind(_event(run_status="done")) == KIND_COMPLETED


def test_classify_kind_halt_is_not_a_kind():
    """FR-9.1: a gate is 'distinct from a halt' — a halted/parked step is no kind."""
    halt = _event(
        run_status="parked",
        current_step="impl",
        current_step_status="halted",
        current_step_type="agent_task",
        current_step_notes="timeout halt (FR-3.3): step exceeded 600s",
    )
    assert classify_kind(halt) is None


def test_classify_kind_running_is_not_a_kind():
    assert classify_kind(_event(run_status="running", current_step="impl",
                                current_step_status="running")) is None


def test_classify_kind_parked_cycle_without_escalation_note_is_not_a_kind():
    # A parked adversarial_cycle whose note does NOT begin with "escalation"
    # (e.g. a mid-cycle interrupt) is not an escalation-parked notification.
    ev = _escalation("impl-cycle", "interrupted mid-round")
    assert classify_kind(ev) is None


# --- de-dup / edge-triggering ------------------------------------------------


def test_one_fanout_per_key():
    rec = RecordingChannel()
    n = Notifier([rec])
    n.notify(_gate("gate"))
    # A revision-only rewrite of the SAME parked state (watcher still emits it,
    # FR-8.1) must NOT re-notify — same (run_id, kind, current_step) key.
    n.notify(_event(run_status="parked", current_step="gate",
                    current_step_status="parked", current_step_type="human_gate",
                    revision=2))
    assert len(rec.sent) == 1
    assert rec.sent[0].kind == KIND_GATE
    assert rec.sent[0].current_step == "gate"


def test_two_distinct_gates_notify_twice():
    """A run parking at two gates in different steps notifies once per gate
    (FR-9.1 keys on current_step, not run_status)."""
    rec = RecordingChannel()
    n = Notifier([rec])
    n.notify(_gate("gateA"))
    n.notify(_gate("gateB"))
    assert [note.current_step for note in rec.sent] == ["gateA", "gateB"]
    assert all(note.kind == KIND_GATE for note in rec.sent)


def test_escalation_park_distinct_kind_not_collapsed_with_gate():
    rec = RecordingChannel()
    n = Notifier([rec])
    # Same step id parking first as a gate-reached then re-classified… not real,
    # but assert a gate then an escalation at different steps are two kinds.
    n.notify(_gate("gate"))
    n.notify(_escalation("impl-cycle", "escalation (FR-10.5): max_rounds=3 …"))
    kinds = [note.kind for note in rec.sent]
    assert kinds == [KIND_GATE, KIND_ESCALATION]


def test_sequential_failures_each_surface():
    rec = RecordingChannel()
    n = Notifier([rec])
    n.notify(_event(run_status="failed", current_step="impl-1",
                    current_step_status="failed"))
    n.notify(_event(run_status="failed", current_step="impl-2",
                    current_step_status="failed"))
    assert [note.current_step for note in rec.sent] == ["impl-1", "impl-2"]


# --- priming (startup suppression) -------------------------------------------


def test_prime_suppresses_pre_existing_state():
    """Priming records the de-dup key without sending, so a run already parked
    (or already done) when the server starts does not flood the operator."""
    rec = RecordingChannel()
    n = Notifier([rec])
    n.prime(_gate("gate"))
    # The same state observed again does NOT fire (it was primed).
    n.notify(_gate("gate"))
    assert rec.sent == []
    # …but a NEW decision point (a different gate) still fires.
    n.notify(_gate("gateB"))
    assert len(rec.sent) == 1 and rec.sent[0].current_step == "gateB"


def test_prime_of_non_kind_is_noop():
    rec = RecordingChannel()
    n = Notifier([rec])
    n.prime(_event(run_status="running", current_step="impl",
                   current_step_status="running"))
    # Later parking at a gate still fires (running was no kind to prime).
    n.notify(_gate("gate"))
    assert len(rec.sent) == 1


# --- fail-soft ---------------------------------------------------------------


def test_raising_channel_is_swallowed():
    rec = RecordingChannel()
    # The raising channel comes first; a good channel after it must still fire.
    n = Notifier([RaisingChannel(), rec])
    n.notify(_gate("gate"))  # must not raise
    assert len(rec.sent) == 1


# --- Notification payload (FR-9.2) -------------------------------------------


def test_notification_payload_fields():
    note = Notification.build(
        _escalation("impl-cycle", "escalation: F-003 lands upstream"),
        KIND_ESCALATION,
        base_url="http://127.0.0.1:8765",
    )
    assert note.slug == "alpha"
    assert note.run_id == "run-1"
    assert note.kind == KIND_ESCALATION
    assert note.current_step == "impl-cycle"
    assert "F-003" in (note.note or "")
    assert note.url == "http://127.0.0.1:8765/runs/alpha"
    assert "Gauntlet" in note.title
    # Body carries slug/run_id, step and the note (FR-9.2).
    assert "alpha/run-1" in note.body and "impl-cycle" in note.body


def test_notification_url_relative_without_base():
    note = Notification.build(_gate("gate"), KIND_GATE)
    assert note.url == "/runs/alpha"


# --- Slack channel shape (mocked httpx transport) ----------------------------


def test_slack_channel_posts_expected_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text="ok")

    ch = SlackChannel("https://hooks.slack.test/abc", transport=httpx.MockTransport(handler))
    note = Notification.build(_gate("gate"), KIND_GATE, base_url="http://x")
    ch.send(note)
    assert seen["url"] == "https://hooks.slack.test/abc"
    assert "text" in seen["body"]
    assert "alpha/run-1" in seen["body"]["text"]
    assert "http://x/runs/alpha" in seen["body"]["text"]


def test_slack_channel_raises_on_error_status():
    # A non-2xx surfaces as a SlackDeliveryError, which the Notifier's fail-soft
    # wrapper logs+swallows. The error must NOT carry the webhook URL (F-003).
    secret_path = "T000/B000/XXXXSECRETXXXX"
    webhook = f"https://hooks.slack.test/services/{secret_path}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    ch = SlackChannel(webhook, transport=httpx.MockTransport(handler))
    with pytest.raises(SlackDeliveryError) as excinfo:
        ch.send(Notification.build(_gate("gate"), KIND_GATE))
    # Status code is reported; the webhook path/secret is not.
    assert "500" in str(excinfo.value)
    assert secret_path not in str(excinfo.value)
    assert "hooks.slack.test" not in str(excinfo.value)


def test_slack_error_does_not_leak_webhook_in_logs(caplog: pytest.LogCaptureFixture):
    """Notifier._dispatch logs a failed send with logger.exception; the logged
    output (message + traceback) must not contain the webhook secret (F-003)."""
    secret_path = "T123/B456/SUPERSECRETTOKEN"
    webhook = f"https://hooks.slack.test/services/{secret_path}"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    ch = SlackChannel(webhook, transport=httpx.MockTransport(handler))
    ch.background = False  # run inline so caplog captures it deterministically
    n = Notifier([ch])
    with caplog.at_level("ERROR"):
        n.notify(_gate("gate"))  # must not raise; failure is logged+swallowed
    full_log = caplog.text
    assert secret_path not in full_log
    assert webhook not in full_log
    assert "hooks.slack.test" not in full_log


def test_slack_connect_error_is_sanitized():
    """A transport-level failure (no response) is also sanitized — its httpx
    message would otherwise carry the webhook URL (F-003)."""
    secret_path = "T999/B999/CONNSECRET"
    webhook = f"https://hooks.slack.test/services/{secret_path}"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    ch = SlackChannel(webhook, transport=httpx.MockTransport(handler))
    with pytest.raises(SlackDeliveryError) as excinfo:
        ch.send(Notification.build(_gate("gate"), KIND_GATE))
    assert secret_path not in str(excinfo.value)
    assert webhook not in str(excinfo.value)


# --- build_notifier (config wiring, FR-9.4) ----------------------------------


def test_build_notifier_per_channel_toggle():
    cfg = WebNotifyConfig(desktop=False, slack=False, in_tab=True)
    n = build_notifier(cfg, watcher=object())
    names = {ch.name for ch in n.channels}
    assert names == {"in_tab"}


def test_build_notifier_slack_needs_webhook(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(GAUNTLET_SLACK_WEBHOOK_ENV, raising=False)
    # slack: true but no webhook configured → channel not constructed (safe no-op).
    cfg = WebNotifyConfig(desktop=False, slack=True, in_tab=False)
    n = build_notifier(cfg, watcher=object())
    assert n.channels == []
    # With the env fallback present, the Slack channel is built.
    monkeypatch.setenv(GAUNTLET_SLACK_WEBHOOK_ENV, "https://hooks.slack.test/zzz")
    n2 = build_notifier(cfg, watcher=object())
    assert {ch.name for ch in n2.channels} == {"slack"}


def test_web_config_parses_from_yaml(tmp_path: Path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "asset_root: .\n"
        "web:\n"
        "  notify:\n"
        "    desktop: false\n"
        "    slack: true\n"
        "    slack_webhook: https://hooks.slack.test/y\n"
    )
    cfg = RunConfig.load(cfg_path)
    # The `web:` block rides on RunConfig as an extra field (it is NOT an engine
    # schema field, review F-004); the console parses/validates it via
    # web_config_from at serve time.
    web = web_config_from(cfg)
    assert web.notify.desktop is False
    assert web.notify.slack is True
    assert web.notify.slack_webhook == "https://hooks.slack.test/y"


def test_absent_web_block_uses_defaults():
    web = web_config_from(RunConfig())
    assert web.notify.desktop is True
    assert web.notify.slack is True
    assert web.notify.in_tab is True
    assert web.notify.slack_webhook is None


def test_malformed_web_block_fails_closed_at_console():
    """An unknown `web.notify` key fails closed when the console parses it
    (WebNotifyConfig forbids extras) rather than silently degrading (F-004)."""
    cfg = RunConfig.model_validate({"web": {"notify": {"bogus": True}}})
    with pytest.raises(Exception):
        web_config_from(cfg)


# --- watcher → notifier integration ------------------------------------------


def _manifest(slug, run_id, *, status, steps, current_step):
    return Manifest(
        run_id=run_id, slug=slug, branch=f"gauntlet/{slug}", base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:dead"),
        status=status, current_step=current_step, steps=steps,
    )


def _write(run_dir: Path, man: Manifest) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    man.write_atomic(run_dir / "manifest.json")


def test_watcher_primes_first_then_notifies(tmp_path: Path):
    """The watcher primes a run's first observation (no send) and notifies on
    every later transition — driving the full FR-9.1 two-gate scenario end to end
    through poll_once."""
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    store = RunStore(repo, RunConfig())
    rec = RecordingChannel()
    notifier = Notifier([rec])
    w = Watcher(store, notifier=notifier)

    rd = repo / "runs" / "alpha" / "run-1"
    # First observation: running → primed, no send (and no kind anyway).
    _write(rd, _manifest("alpha", "run-1", status="running",
                         steps=[StepRecord(id="impl", type="agent_task", status="running")],
                         current_step="impl"))
    w.poll_once()
    assert rec.sent == []

    # Park at gate A → one gate notification.
    _write(rd, _manifest("alpha", "run-1", status="parked",
                         steps=[StepRecord(id="impl", type="agent_task", status="done"),
                                StepRecord(id="gateA", type="human_gate", status="parked")],
                         current_step="gateA"))
    w.poll_once()
    assert len(rec.sent) == 1 and rec.sent[0].current_step == "gateA"

    # Park at gate B → a second, distinct notification.
    _write(rd, _manifest("alpha", "run-1", status="parked",
                         steps=[StepRecord(id="gateA", type="human_gate", status="done"),
                                StepRecord(id="gateB", type="human_gate", status="parked")],
                         current_step="gateB"))
    w.poll_once()
    assert [note.current_step for note in rec.sent] == ["gateA", "gateB"]


def test_watcher_primes_pre_existing_parked_run_on_startup(tmp_path: Path):
    """A run already parked when the watcher first sees it is primed, not
    notified — no startup flood (the realistic 'serve over old runs' case)."""
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    store = RunStore(repo, RunConfig())
    rec = RecordingChannel()
    w = Watcher(store, notifier=Notifier([rec]))

    rd = repo / "runs" / "alpha" / "run-1"
    _write(rd, _manifest("alpha", "run-1", status="parked",
                         steps=[StepRecord(id="gate", type="human_gate", status="parked")],
                         current_step="gate"))
    w.poll_once()  # first observation of an already-parked run
    assert rec.sent == []


def test_watcher_notifies_run_first_seen_parked_after_startup(tmp_path: Path):
    """A run first *discovered* already parked AFTER the watcher's initial scan
    must notify — startup flood suppression must not bleed into later discovery
    (review F-001). The bug: priming keyed on `prev is None` suppressed every
    first observation, so a console/external run that started and parked between
    1s polls was silently de-duplicated forever."""
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    store = RunStore(repo, RunConfig())
    rec = RecordingChannel()
    w = Watcher(store, notifier=Notifier([rec]))

    # The watcher is already running: an initial scan over the (empty) tree
    # completes priming with nothing to suppress.
    w.poll_once()
    assert rec.sent == []

    # Now a brand-new run appears and is first seen already parked at a gate (it
    # started and parked between polls). This is a real transition, not startup
    # state — it must notify.
    rd = repo / "runs" / "beta" / "run-9"
    _write(rd, _manifest("beta", "run-9", status="parked",
                         steps=[StepRecord(id="gate", type="human_gate", status="parked")],
                         current_step="gate"))
    w.poll_once()
    assert len(rec.sent) == 1 and rec.sent[0].current_step == "gate"


def test_watcher_notifies_run_first_seen_done_after_startup(tmp_path: Path):
    """Same as above for a completion: a run first observed already `done` after
    startup notifies once (review F-001)."""
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    store = RunStore(repo, RunConfig())
    rec = RecordingChannel()
    w = Watcher(store, notifier=Notifier([rec]))
    w.poll_once()  # initial scan completes; priming done

    rd = repo / "runs" / "gamma" / "run-3"
    _write(rd, _manifest("gamma", "run-3", status="done",
                         steps=[StepRecord(id="impl", type="agent_task", status="done")],
                         current_step="impl"))
    w.poll_once()
    assert len(rec.sent) == 1 and rec.sent[0].kind == KIND_COMPLETED


def test_watcher_unchanged_manifest_no_notify(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    store = RunStore(repo, RunConfig())
    rec = RecordingChannel()
    w = Watcher(store, notifier=Notifier([rec]))
    rd = repo / "runs" / "alpha" / "run-1"
    _write(rd, _manifest("alpha", "run-1", status="running",
                         steps=[StepRecord(id="impl", type="agent_task", status="running")],
                         current_step="impl"))
    w.poll_once()
    n_after_first = len(rec.sent)
    # No write → unchanged mtime → no re-parse, no notify.
    w.poll_once()
    assert len(rec.sent) == n_after_first


def test_watcher_notifier_error_swallowed(tmp_path: Path):
    """A notifier that raises cannot reach the poll loop (FR-9.3)."""
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    store = RunStore(repo, RunConfig())

    class Boom:
        def prime(self, event):
            raise RuntimeError("prime boom")

        def notify(self, event):
            raise RuntimeError("notify boom")

    w = Watcher(store, notifier=Boom())
    rd = repo / "runs" / "alpha" / "run-1"
    _write(rd, _manifest("alpha", "run-1", status="running",
                         steps=[StepRecord(id="impl", type="agent_task", status="running")],
                         current_step="impl"))
    # poll_once must not raise even though prime() raises; it still returns the
    # observed transition.
    evs = w.poll_once()
    assert len(evs) == 1


# --- in-tab channel fans onto the SSE queues ---------------------------------


def test_in_tab_channel_publishes_to_sse_subscribers(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / "runs").mkdir(parents=True)
    store = RunStore(repo, RunConfig())
    w = Watcher(store)
    notifier = build_notifier(
        WebNotifyConfig(desktop=False, slack=False, in_tab=True), watcher=w
    )
    w.notifier = notifier

    async def scenario():
        q = w.subscribe()
        notifier.notify(_gate("gate"))
        return await q.get()

    item = asyncio.run(scenario())
    assert isinstance(item, Notification)
    assert item.kind == KIND_GATE


def test_event_stream_dispatches_notify_distinct_from_transition(tmp_path: Path):
    """The shared SSE queue carries both a WatchEvent (→ `transition`) and a
    Notification (→ `notify`); the stream type-dispatches them."""
    from gauntlet.web.sse import event_stream

    repo = tmp_path / "repo"
    rd = repo / "runs" / "alpha" / "run-1"
    _write(rd, _manifest("alpha", "run-1", status="parked",
                         steps=[StepRecord(id="gate", type="human_gate", status="parked")],
                         current_step="gate"))
    store = RunStore(repo, RunConfig())
    w = Watcher(store)

    async def scenario():
        async def never() -> bool:
            return False

        gen = event_stream(store, w, is_disconnected=never, keepalive=5.0, max_events=2)
        out = [await gen.__anext__()]  # snapshot
        w._publish(_gate("gate"))  # a WatchEvent → transition
        w.publish_notification(Notification.build(_gate("gate"), KIND_GATE))
        out.append(await gen.__anext__())
        out.append(await gen.__anext__())
        return out

    msgs = asyncio.run(scenario())
    assert msgs[0].startswith("event: snapshot")
    assert "event: transition" in msgs[1]
    assert "event: notify" in msgs[2]
    assert KIND_GATE in msgs[2]

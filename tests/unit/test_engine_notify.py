"""Driver-side notifications (#134, recs 6 and 10).

The engine module owns the kind table, the classifier over every park class,
the channel primitives (incl. the new generic webhook), the per-run de-dup
ledger, and the driver's post-drive hook. The console re-exports it; the
console-facing behavior stays covered by test_web_notify.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from gauntlet.engine import gate_evidence as GE
from gauntlet.engine import manifest as M
from gauntlet.engine import notify as N
from gauntlet.engine.config import NotifyConfig, RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.web.config import WebNotifyConfig, web_config_from


# --- helpers -----------------------------------------------------------------
def _man(**overrides) -> Manifest:
    base = dict(
        run_id="r1", slug="demo", pipeline=PipelineRef(name="p", version=1, hash="h"),
        base_branch="main", branch="gauntlet/demo", status=M.RUN_PARKED,
        current_step="s1", steps=[],
    )
    base.update(overrides)
    return Manifest(**base)


def _parked(reason: str, *, step_type: str = "agent_task", notes: str | None = None,
            **rec_kw) -> Manifest:
    rec = StepRecord(id="s1", type=step_type, status=M.PARKED, parked_reason=reason,
                     notes=notes, **rec_kw)
    return _man(steps=[rec])


def _event(man: Manifest) -> N.Transition:
    return N.Transition.from_manifest(man)


class _Capture:
    """A foreground channel that records what it was sent."""

    name = "capture"
    background = False

    def __init__(self) -> None:
        self.sent: list[N.Notification] = []

    def send(self, note: N.Notification) -> None:
        self.sent.append(note)


# --- classify_kind: every park class maps -------------------------------------
@pytest.mark.parametrize(
    "reason, step_type, notes, expected",
    [
        (M.PARKED_REASON_GATE, "human_gate", None, N.KIND_GATE),
        (None, "human_gate", None, N.KIND_GATE),  # legacy null reason on a gate
        (M.PARKED_REASON_USAGE_LIMIT, "agent_task", None, N.KIND_PARKED_USAGE_LIMIT),
        (M.PARKED_REASON_PROVIDER_UNAVAILABLE, "agent_task", None,
         N.KIND_PARKED_PROVIDER_UNAVAILABLE),
        (M.PARKED_REASON_USAGE_WINDOW, "agent_task", None, N.KIND_PARKED_USAGE_WINDOW),
        (M.PARKED_REASON_ARTIFACT_INVALID, "agent_task", None,
         N.KIND_PARKED_ARTIFACT_INVALID),
        (M.PARKED_REASON_RESPONSE, "adversarial_cycle", "escalation: F-001 → plan.md",
         N.KIND_ESCALATION),
        (M.PARKED_REASON_RESPONSE, "agent_task", "UPSTREAM CONFLICT …",
         N.KIND_PARKED_RESPONSE),
    ],
)
def test_classify_kind_maps_every_park_reason(reason, step_type, notes, expected):
    assert N.classify_kind(_event(_parked(reason, step_type=step_type, notes=notes))) == expected


def test_classify_kind_halt_done_failed_running():
    halted = _man(steps=[StepRecord(id="s1", type="shell", status=M.HALTED,
                                    halt_reason="timeout")])
    assert N.classify_kind(_event(halted)) == N.KIND_HALTED
    assert N.classify_kind(_event(_man(status=M.RUN_DONE))) == N.KIND_COMPLETED
    assert N.classify_kind(_event(_man(status=M.RUN_FAILED))) == N.KIND_FAILED
    running = _man(status=M.RUN_RUNNING,
                   steps=[StepRecord(id="s1", type="shell", status=M.RUNNING)])
    assert N.classify_kind(_event(running)) is None


def test_transition_carries_typed_reasons_and_deadline():
    man = _parked(M.PARKED_REASON_PROVIDER_UNAVAILABLE,
                  quota_reset_at="2026-09-03T12:00:00+00:00",
                  scheduled_resume=M.ScheduledResume(attempt_at="2026-09-03T12:00:00+00:00"))
    ev = _event(man)
    assert ev.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert ev.quota_reset_at == "2026-09-03T12:00:00+00:00"
    assert ev.auto_resume_armed is True
    note = N.Notification.build(ev, N.classify_kind(ev))
    assert "deadline 2026-09-03T12:00:00+00:00" in note.body
    assert "auto-resume armed" in note.body
    assert note.next_action and "gauntlet resume demo" in note.next_action


def test_transition_prefers_active_foreach_iteration():
    done = StepRecord(id="impl", type="agent_task", status=M.DONE, iteration="P1")
    parked = StepRecord(id="impl", type="agent_task", status=M.PARKED,
                        parked_reason=M.PARKED_REASON_USAGE_LIMIT, iteration="P2")
    man = _man(current_step="impl", steps=[done, parked])
    assert N.current_record(man) is parked
    assert N.classify_kind(_event(man)) == N.KIND_PARKED_USAGE_LIMIT


def test_kind_tables_are_closed_and_labelled():
    assert set(N.TRANSITION_KINDS) <= set(N.ALL_KINDS)
    assert set(N.ALL_KINDS) == set(N.LABELS)


# --- the ledger ---------------------------------------------------------------
def test_ledger_append_and_keys_round_trip(tmp_path):
    led = N.NotificationLedger.for_run_dir(tmp_path)
    assert led.keys() == set()  # absent file: empty, no error
    led.append(("r1", N.KIND_GATE, "gate"), kind=N.KIND_GATE, channels=["slack"], by="driver")
    assert led.keys() == {("r1", N.KIND_GATE, "gate")}
    entries = led.entries()
    assert entries[0]["by"] == "driver" and entries[0]["channels"] == ["slack"]
    assert entries[0]["emitted_at"]


def test_ledger_ignores_malformed_lines(tmp_path):
    path = tmp_path / N.LEDGER_NAME
    path.write_text('not json\n{"key": "bad"}\n{"key": ["r1", "gate-reached", null]}\n')
    assert N.NotificationLedger(path).keys() == {("r1", "gate-reached", None)}


# --- the notifier: de-dup across emitters, allowlist, fail-soft summary -------
def test_driver_then_console_never_double_fire(tmp_path):
    man = _parked(M.PARKED_REASON_GATE, step_type="human_gate")
    ev = _event(man)
    drv_ch, con_ch = _Capture(), _Capture()
    driver = N.Notifier([drv_ch], ledger_dir_for=lambda e: tmp_path, by=N.EMITTER_DRIVER)
    console = N.Notifier([con_ch], ledger_dir_for=lambda e: tmp_path, by=N.EMITTER_CONSOLE)
    assert driver.notify_transition(ev) == N.KIND_GATE
    assert len(drv_ch.sent) == 1
    # A fresh console process observing the same park consults the ledger.
    assert console.notify_transition(ev) is None
    assert con_ch.sent == []
    # And the reverse order.
    ev2 = _event(_parked(M.PARKED_REASON_USAGE_LIMIT))
    ev2 = ev2.model_copy(update={"current_step": "s2"})
    assert console.notify_transition(ev2) == N.KIND_PARKED_USAGE_LIMIT
    assert driver.notify_transition(ev2) is None
    by = [e["by"] for e in N.NotificationLedger.for_run_dir(tmp_path).entries()]
    assert by == [N.EMITTER_DRIVER, N.EMITTER_CONSOLE]


def test_restarted_driver_does_not_re_announce(tmp_path):
    ev = _event(_parked(M.PARKED_REASON_GATE, step_type="human_gate"))
    first = N.Notifier([_Capture()], ledger_dir_for=lambda e: tmp_path, by=N.EMITTER_DRIVER)
    assert first.notify_transition(ev) == N.KIND_GATE
    ch = _Capture()
    second = N.Notifier([ch], ledger_dir_for=lambda e: tmp_path, by=N.EMITTER_DRIVER)
    assert second.notify_transition(ev) is None
    assert ch.sent == []


def test_next_gate_is_a_new_key(tmp_path):
    ch = _Capture()
    n = N.Notifier([ch], ledger_dir_for=lambda e: tmp_path, by=N.EMITTER_DRIVER)
    g1 = _event(_man(current_step="gate-a", steps=[StepRecord(
        id="gate-a", type="human_gate", status=M.PARKED, parked_reason=M.PARKED_REASON_GATE)]))
    g2 = _event(_man(current_step="gate-b", steps=[StepRecord(
        id="gate-b", type="human_gate", status=M.PARKED, parked_reason=M.PARKED_REASON_GATE)]))
    assert n.notify_transition(g1) == N.KIND_GATE
    assert n.notify_transition(g1) is None
    assert n.notify_transition(g2) == N.KIND_GATE
    assert len(ch.sent) == 2


def test_kinds_allowlist_filters():
    ch = _Capture()
    n = N.Notifier([ch], kinds=[N.KIND_FAILED])
    assert n.notify_transition(_event(_parked(M.PARKED_REASON_GATE, step_type="human_gate"))) is None
    assert n.notify_transition(_event(_man(status=M.RUN_FAILED))) == N.KIND_FAILED
    assert [s.kind for s in ch.sent] == [N.KIND_FAILED]


def test_summary_failure_still_sends_short_notification():
    ch = _Capture()

    def boom(event, kind):
        raise RuntimeError("no git here")

    n = N.Notifier([ch], summary_for=boom)
    ev = _event(_parked(M.PARKED_REASON_GATE, step_type="human_gate"))
    assert n.notify_transition(ev) == N.KIND_GATE
    assert ch.sent[0].summary is None


def test_channel_exception_is_swallowed():
    class Bad:
        name = "bad"
        background = False

        def send(self, note):
            raise RuntimeError("boom")

    ch = _Capture()
    n = N.Notifier([Bad(), ch])
    assert n.notify_transition(_event(_man(status=M.RUN_FAILED))) == N.KIND_FAILED
    assert len(ch.sent) == 1


# --- channels: generic webhook payload + sanitized errors ----------------------
def test_webhook_channel_posts_json_payload():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["json"] = json.loads(request.content)
        return httpx.Response(200)

    ch = N.WebhookChannel("https://hooks.example/secret-token",
                          transport=httpx.MockTransport(handler))
    ev = _event(_parked(M.PARKED_REASON_GATE, step_type="human_gate"))
    note = N.Notification.build(ev, N.KIND_GATE, summary={"gate": "s1"})
    ch.send(note)
    body = seen["json"]
    assert body["kind"] == N.KIND_GATE
    assert body["slug"] == "demo" and body["run_id"] == "r1"
    assert body["parked_reason"] == M.PARKED_REASON_GATE
    assert body["step_type"] == "human_gate"
    assert body["next_action"].startswith("gauntlet approve demo")
    assert body["summary"] == {"gate": "s1"}
    assert body["emitted_at"]


def test_webhook_error_never_carries_the_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    ch = N.WebhookChannel("https://hooks.example/secret-token",
                          transport=httpx.MockTransport(handler))
    note = N.Notification.build(_event(_man(status=M.RUN_FAILED)), N.KIND_FAILED)
    with pytest.raises(N.WebhookDeliveryError) as ei:
        ch.send(note)
    assert "secret-token" not in str(ei.value)
    assert "503" in str(ei.value)
    assert ei.value.__cause__ is None


def test_slack_text_carries_next_action_and_summary():
    ev = _event(_parked(M.PARKED_REASON_GATE, step_type="human_gate"))
    summary = {
        "range": {"base": "a" * 40, "head": "b" * 40},
        "diff_stat": " f.py | 2 +-\n 1 file changed, 1 insertion(+), 1 deletion(-)\n",
        "findings": {"total": 3, "by_severity": {"major": 1, "minor": 2}},
        "triage": {"total": 3, "by_action": {"fix_now": 2, "reject": 1}},
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.5},
        "elapsed_s": 3725,
    }
    text = N.Notification.build(ev, N.KIND_GATE, summary=summary).slack_text()
    assert "next: gauntlet approve demo" in text
    assert "findings: 3 (major 1, minor 2)" in text
    assert "triage: 3 (fix_now 2, reject 1)" in text
    assert "1 file changed" in text
    assert "elapsed: 1h 02m" in text
    assert "$0.50" in text


def test_build_channels_only_with_resolvable_endpoints(monkeypatch):
    monkeypatch.delenv(N.GAUNTLET_SLACK_WEBHOOK_ENV, raising=False)
    monkeypatch.delenv(N.GAUNTLET_NOTIFY_WEBHOOK_ENV, raising=False)
    names = [c.name for c in N.build_channels(NotifyConfig(desktop=False))]
    assert names == []
    monkeypatch.setenv(N.GAUNTLET_NOTIFY_WEBHOOK_ENV, "https://hooks.example/x")
    names = [c.name for c in N.build_channels(NotifyConfig(desktop=False))]
    assert names == ["webhook"]
    names = [c.name for c in N.build_channels(
        NotifyConfig(desktop=True, slack_webhook="https://slack.example/y"))]
    assert names == ["desktop", "slack", "webhook"]


# --- config ---------------------------------------------------------------------
def test_notify_config_defaults_and_kinds_validation():
    cfg = RunConfig.model_validate({})
    assert cfg.notify.desktop and cfg.notify.slack and cfg.notify.webhook
    assert cfg.notify.kinds is None
    ok = RunConfig.model_validate({"notify": {"kinds": ["gate-reached", "run-failed"]}})
    assert ok.notify.kinds == ["gate-reached", "run-failed"]
    with pytest.raises(ValueError, match="unknown kind"):
        RunConfig.model_validate({"notify": {"kinds": ["gate-reachd"]}})
    with pytest.raises(ValueError):
        RunConfig.model_validate({"notify": {"deskto": True}})


def test_web_notify_inherits_engine_block_unless_explicit():
    engine_only = RunConfig.model_validate(
        {"notify": {"desktop": False, "webhook_url": "https://hooks.example/w",
                    "kinds": ["run-failed"]}}
    )
    web = web_config_from(engine_only).notify
    assert web.desktop is False and web.in_tab is True
    assert web.webhook_url == "https://hooks.example/w"
    assert web.kinds == ["run-failed"]
    explicit = RunConfig.model_validate(
        {"notify": {"desktop": False}, "web": {"notify": {"desktop": True}}}
    )
    assert web_config_from(explicit).notify.desktop is True
    assert isinstance(web_config_from(RunConfig.model_validate({})).notify, WebNotifyConfig)


def test_driver_kill_switch(monkeypatch):
    monkeypatch.setenv(N.GAUNTLET_NOTIFY_DISABLED_ENV, "1")
    assert N.driver_notifications_disabled()
    monkeypatch.setenv(N.GAUNTLET_NOTIFY_DISABLED_ENV, "0")
    assert not N.driver_notifications_disabled()


# --- gate evidence ----------------------------------------------------------------
def test_findings_and_triage_counts_are_ordered_and_tolerant():
    f = GE.findings_counts({"findings": [
        {"id": "F-1", "severity": "minor"}, {"id": "F-2", "severity": "blocking"},
        {"id": "F-3", "severity": "minor"}, "junk",
    ]})
    assert f == {"total": 3, "by_severity": {"blocking": 1, "minor": 2}}
    t = GE.triage_counts({"verdicts": [
        {"finding_id": "F-1", "verdict": "legitimate", "action": "fix_now"},
        {"finding_id": "F-2", "verdict": "wrong", "action": "reject"},
    ]})
    assert t["total"] == 2 and t["by_action"] == {"fix_now": 1, "reject": 1}
    assert GE.findings_counts(None) is None and GE.triage_counts("x") is None


def test_resolve_gate_artifact_rejects_traversal(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "artifacts").mkdir(parents=True)
    (run_dir / "artifacts" / "findings.json").write_text("{}")
    slug_dir = tmp_path / "slug"
    slug_dir.mkdir()
    assert GE.resolve_gate_artifact(run_dir, slug_dir, "findings.json")[1] == "artifacts"
    assert GE.resolve_gate_artifact(run_dir, slug_dir, "../findings.json") == (None, None)
    assert GE.resolve_gate_artifact(run_dir, slug_dir, "missing.json") == (None, None)


# --- the driver hook: every driving verb pushes the persisted transition -------
def _arm_capture(monkeypatch) -> _Capture:
    """Enable driver notifications for this test and capture the sends."""
    monkeypatch.delenv(N.GAUNTLET_NOTIFY_DISABLED_ENV, raising=False)
    ch = _Capture()
    monkeypatch.setattr(N, "build_channels", lambda cfg: [ch])
    return ch


def test_driver_pushes_gate_then_completion_with_ledger(fixture_repo, monkeypatch):
    from test_run_lifecycle import GATED, _author_prd, _prepare, _write_pipeline

    ch = _arm_capture(monkeypatch)
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    assert [n.kind for n in ch.sent] == [N.KIND_GATE]
    gate = ch.sent[0]
    assert gate.current_step == "gate" and gate.step_type == "human_gate"
    assert gate.next_action and "gauntlet approve demo" in gate.next_action
    # rec 10: the gate notification carries the pre-built review bundle.
    assert gate.summary is not None
    assert gate.summary["gate"] == "gate"
    assert gate.summary["range"] is not None
    assert "usage" in gate.summary and gate.summary["elapsed_s"] is not None
    run_dir = mgr.layout("demo").active_run_dir()
    ledger = N.NotificationLedger.for_run_dir(run_dir)
    assert [(e["kind"], e["by"]) for e in ledger.entries()] == [
        (N.KIND_GATE, N.EMITTER_DRIVER)
    ]
    # A re-classification of the same persisted state (e.g. a restarted
    # driver, or the console) is de-duplicated through the ledger.
    mgr._notify_transition(mgr.layout("demo"), run_dir)
    assert len(ch.sent) == 1
    assert mgr.approve("demo", notes="ok", use_judge=False) == M.RUN_DONE
    assert [n.kind for n in ch.sent] == [N.KIND_GATE, N.KIND_COMPLETED]
    assert ch.sent[1].summary is None  # only gates carry the bundle
    assert len(ledger.entries()) == 2


def test_driver_kill_switch_builds_nothing(fixture_repo):
    from test_run_lifecycle import GATED, _author_prd, _prepare, _write_pipeline

    # The suite-wide autouse fixture sets GAUNTLET_NOTIFY_DISABLED=1.
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    run_dir = mgr.layout("demo").active_run_dir()
    assert not (run_dir / N.LEDGER_NAME).exists()


def test_driver_honors_kinds_allowlist(fixture_repo, monkeypatch):
    from test_run_lifecycle import GATED, _author_prd, _prepare, _write_pipeline

    ch = _arm_capture(monkeypatch)
    mgr = _prepare(fixture_repo)
    mgr.config = mgr.config.model_copy(
        update={"notify": NotifyConfig(kinds=[N.KIND_COMPLETED])}
    )
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    assert ch.sent == []
    assert mgr.approve("demo", notes="ok", use_judge=False) == M.RUN_DONE
    assert [n.kind for n in ch.sent] == [N.KIND_COMPLETED]


def test_each_iteration_and_repark_notifies_once(tmp_path):
    channel = _Capture()
    notifier = N.Notifier([channel], ledger_dir_for=lambda e: tmp_path)
    for phase, ended in [("P1", "first"), ("P2", "first"), ("P2", "second")]:
        event = _event(_parked(M.PARKED_REASON_GATE, step_type="human_gate",
                               iteration=phase, ended=ended))
        notifier.notify_transition(event)
        notifier.notify_transition(event)
    assert len(channel.sent) == 3


def test_console_channel_is_not_suppressed_by_driver_delivery(tmp_path):
    event = _event(_parked(M.PARKED_REASON_GATE, step_type="human_gate"))
    driver, console = _Capture(), _Capture()
    driver.name, console.name = "webhook", "in_tab"
    for channel in (driver, console):
        N.Notifier([channel], ledger_dir_for=lambda e: tmp_path).notify_transition(event)
    assert len(driver.sent) == len(console.sent) == 1


def test_failed_channel_retries_without_resending_success(tmp_path):
    event = _event(_parked(M.PARKED_REASON_USAGE_LIMIT))
    good, bad = _Capture(), _Capture()
    good.name, bad.name = "good", "bad"
    original = bad.send
    bad.send = lambda note: (_ for _ in ()).throw(RuntimeError("failed"))
    notifier = N.Notifier([good, bad], ledger_dir_for=lambda e: tmp_path)
    notifier.notify_transition(event)
    assert len(N.NotificationLedger.for_run_dir(tmp_path).entries()) == 1
    bad.send = original
    notifier.notify_transition(event)
    assert len(good.sent) == len(bad.sent) == 1
    assert len(N.NotificationLedger.for_run_dir(tmp_path).entries()) == 2


def test_driver_flushes_background_delivery_before_process_exit(tmp_path):
    import subprocess
    import sys
    script = """
import sys, time
from pathlib import Path
from gauntlet.engine import notify as N
from gauntlet.engine.manifest import Manifest, PipelineRef
class Channel:
    name = 'test'
    background = True
    def send(self, note):
        time.sleep(0.15)
        Path(sys.argv[1]).write_text('delivered')
man = Manifest(run_id='r', slug='s', branch='b', base_branch='main', status='done',
               pipeline=PipelineRef(name='p', version=1, hash='h'))
N.emit_driver_notification(man, notifier=N.Notifier([Channel()],
    ledger_dir_for=lambda e: Path(sys.argv[1]).parent))
"""
    target = tmp_path / "delivery"
    subprocess.run([sys.executable, "-B", "-c", script, str(target)], check=True)
    assert target.read_text() == "delivered"
    assert N.NotificationLedger.for_run_dir(tmp_path).entries()[0]["status"] == "delivered"


def test_concurrent_emitters_serialize_one_channel(tmp_path):
    import threading
    import time
    event = _event(_parked(M.PARKED_REASON_USAGE_LIMIT))
    channel = _Capture()
    original = channel.send
    def slow(note):
        time.sleep(0.05)
        original(note)
    channel.send = slow
    notifiers = [N.Notifier([channel], ledger_dir_for=lambda e: tmp_path) for _ in range(2)]
    threads = [threading.Thread(target=n.notify_transition, args=(event,)) for n in notifiers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(channel.sent) == 1


def test_gate_summary_includes_implementation_before_review(fixture_repo):
    from conftest import git
    from gauntlet.engine import gitops
    base = gitops.head_sha(fixture_repo)
    git(fixture_repo, "checkout", "-qb", "gauntlet/demo")
    (fixture_repo / "implementation.py").write_text("implementation\n")
    git(fixture_repo, "add", "implementation.py")
    git(fixture_repo, "commit", "-qm", "P1: Implement phase")
    handoff = gitops.head_sha(fixture_repo)
    cycle = StepRecord(id="review", type="adversarial_cycle", status=M.DONE, base_sha=handoff)
    gate = StepRecord(id="gate", type="human_gate", status=M.PARKED)
    man = _man(steps=[cycle, gate], current_step="gate", commits=[
        M.CommitRecord(step_id="commit", phase="P1", sha=handoff)])
    assert GE.reviewed_range(fixture_repo, man, gate) == (f"{handoff}^", handoff)
    assert "implementation.py" in gitops.range_diff(fixture_repo, *GE.reviewed_range(fixture_repo, man, gate))


def test_malformed_delivery_identity_and_channels_are_ignored(tmp_path):
    key = ["r1", N.KIND_GATE, "gate", "P1", "ended"]
    path = tmp_path / N.LEDGER_NAME
    path.write_text(json.dumps({"key": key[:4] + [{}]}) + "\n" +
                    json.dumps({"key": key, "status": "delivered", "channels": "webhook"}))
    ledger = N.NotificationLedger(path)
    assert ledger.keys() == {tuple(key)}
    assert not ledger.delivered(tuple(key), "webhook")


@pytest.mark.parametrize("unavailable", ["module", "filesystem"])
def test_delivery_survives_unavailable_advisory_lock(tmp_path, monkeypatch, unavailable):
    if unavailable == "module":
        monkeypatch.setattr(N, "fcntl", None)
    else:
        from types import SimpleNamespace
        def unsupported(*args):
            raise OSError("advisory locking unavailable")
        monkeypatch.setattr(N, "fcntl", SimpleNamespace(
            flock=unsupported, LOCK_EX=1, LOCK_NB=2))
    channel = _Capture()
    notifier = N.Notifier([channel], ledger_dir_for=lambda event: tmp_path)
    event = _event(_parked(M.PARKED_REASON_GATE, step_type="human_gate"))
    notifier.notify_transition(event)
    notifier.notify_transition(event)
    assert len(channel.sent) == 1
    assert N.NotificationLedger.for_run_dir(tmp_path).entries()[0]["status"] == "delivered"

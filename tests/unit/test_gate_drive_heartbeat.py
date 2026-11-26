"""Gate verbs keep the run's FR-5 liveness signals up.

`approve` / `reject` drive the rest of a run but used to do it with no heartbeat
writer, so a run continued from a gate kept the previous driver's stale
heartbeat (status: `driver_orphaned`). They now wrap the gate drive in the same
writer + keep-awake that `_drive` uses. Also covers the heartbeat thread
surviving a failed write (Windows refuses to replace a file a reader holds).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from gauntlet.engine import heartbeat as HB
from gauntlet.engine.run import RunManager


def _fake_manager(**extra):
    cfg = SimpleNamespace(
        heartbeat_interval_s=0.05, suspend_credit_cap_s=60.0, keep_awake=False
    )
    drained = []
    fake = SimpleNamespace(
        config=cfg,
        _drain_suspensions=lambda writer, man, run_dir: drained.append(run_dir),
        drained=drained,
        **extra,
    )
    fake._gate_drive_liveness = (
        lambda run_dir, man: RunManager._gate_drive_liveness(fake, run_dir, man)
    )
    return fake


def _heartbeat_pid(run_dir):
    return json.loads((run_dir / HB.HEARTBEAT_FILENAME).read_text())["pid"]


def test_gate_drive_liveness_writes_this_drivers_heartbeat(tmp_path):
    (tmp_path / HB.HEARTBEAT_FILENAME).write_text(
        json.dumps({"monotonic_s": 1.0, "wallclock_utc": "2020-01-01T00-00-00Z",
                    "pid": 999999})
    )
    fake = _fake_manager()
    with RunManager._gate_drive_liveness(fake, tmp_path, man=None):
        assert _heartbeat_pid(tmp_path) == os.getpid()
    assert fake.drained == [tmp_path]


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_gate_drives_run_inside_the_heartbeat(tmp_path, verb):
    seen = {}

    class Orch:
        def approve_gate(self, gate, notes):
            seen["pid"] = _heartbeat_pid(tmp_path)
            return "running"

        def reject_gate(self, gate, notes, user, allow_terminal=False):
            seen["pid"] = _heartbeat_pid(tmp_path)
            return "running"

    fake = _fake_manager(
        _orchestrator=lambda *a, **k: Orch(),
        _finish_drive=lambda *a, **k: seen.setdefault("finished_after", True),
    )
    if verb == "approve":
        RunManager._approve_drive(fake, None, tmp_path, None, None, "g", "n", {}, None)
    else:
        RunManager._reject_drive(fake, None, tmp_path, None, None, "g", "n", "u", {}, None)
    assert seen["pid"] == os.getpid()
    assert fake.drained == [tmp_path]  # drained before _finish_drive ran
    assert seen["finished_after"] is True


def test_heartbeat_loop_survives_a_failed_write(tmp_path, monkeypatch):
    w = HB.HeartbeatWriter(tmp_path, interval_s=0.01)
    calls = {"n": 0}
    real = w._write_sample

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:  # first loop tick fails
            raise PermissionError(13, "held by a reader")
        real()

    monkeypatch.setattr(w, "_write_sample", flaky)
    w.start()
    try:
        deadline = __import__("time").monotonic() + 2
        while calls["n"] < 4 and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
    finally:
        w.stop()
    assert calls["n"] >= 4  # kept ticking after the failure


def test_replace_with_retry_retries_on_windows_permission_error(tmp_path, monkeypatch):
    monkeypatch.setattr(HB.os, "name", "nt")
    monkeypatch.setattr(HB.time, "sleep", lambda s: None)
    attempts = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise PermissionError(13, "in use")
        real_replace(src, dst)

    monkeypatch.setattr(HB.os, "replace", flaky_replace)
    src = tmp_path / "a.tmp"
    src.write_text("x")
    HB._replace_with_retry(str(src), tmp_path / "b")
    assert (tmp_path / "b").read_text() == "x"
    assert attempts["n"] == 3


def test_replace_with_retry_does_not_mask_posix_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(HB.os, "name", "posix")

    def deny(src, dst):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(HB.os, "replace", deny)
    with pytest.raises(PermissionError):
        HB._replace_with_retry("a", tmp_path / "b")


def test_failed_writes_do_not_fabricate_suspension_credit(tmp_path, monkeypatch):
    """#178 review F-004: a run of failed writes followed by a success must
    NOT be read as a host suspension — both clocks advanced continuously, so
    no deadline credit is owed (a false credit stretches the FR-3.3 timeout)."""
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    tick = [0]
    w = HB.HeartbeatWriter(
        tmp_path,
        monotonic_clock=lambda: 100.0 + 15.0 * tick[0],
        wall_clock=lambda: t0 + timedelta(seconds=15 * tick[0]),
    )
    w._write_sample()  # t=0 ok
    real_persist = w._persist

    def failing(_sample):
        raise PermissionError("held open by a reader")

    monkeypatch.setattr(w, "_persist", failing)
    for t in (1, 2, 3):  # t=15, 30, 45: three failed writes in a row
        tick[0] = t
        with pytest.raises(OSError):
            w._write_sample()
    monkeypatch.setattr(w, "_persist", real_persist)
    tick[0] = 4  # t=60: the next write succeeds
    w._write_sample()
    assert w.drain_suspensions() == []
    assert w.detected_suspension_s() == 0.0


def test_genuine_suspension_is_detected_even_when_that_write_fails(tmp_path, monkeypatch):
    """Detection runs on the observation, before the write: a real suspension
    is still credited when the tick that sees it cannot persist."""
    from datetime import datetime, timedelta, timezone

    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    walls = [t0, t0 + timedelta(minutes=40)]
    monos = [100.0, 115.0]
    i = [0]
    w = HB.HeartbeatWriter(
        tmp_path, monotonic_clock=lambda: monos[i[0]], wall_clock=lambda: walls[i[0]]
    )
    w._write_sample()

    def failing(_sample):
        raise PermissionError("held open by a reader")

    monkeypatch.setattr(w, "_persist", failing)
    i[0] = 1
    with pytest.raises(OSError):
        w._write_sample()
    assert [s.gap_s for s in w.drain_suspensions()] == [2400]

"""Non-POSIX (native Windows) path of the hard-timeout wrapper.

The POSIX path relies on ``getpgid``/``killpg``/``start_new_session`` and
``selectors`` over pipes, none of which exist on Windows. These tests pin the
Windows fallbacks: no process-group calls, a fail-closed vanish watchdog, a
``taskkill /T`` tree kill, and sink replay after a buffered run.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from gauntlet.adapters import process
from gauntlet.adapters.process import StreamSinkError, run_with_timeout


@pytest.fixture
def non_posix(monkeypatch):
    monkeypatch.setattr(process, "_POSIX", False)


class _FakeProc:
    pid = 4242

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive
        self.killed = False

    def poll(self):
        return None if self._alive else 1

    def kill(self) -> None:
        self.killed = True


def test_vanish_watch_never_calls_getpgid_and_fails_closed(non_posix, monkeypatch):
    def boom(_pid):
        raise AssertionError("getpgid must not be called off POSIX")

    monkeypatch.setattr(process.os, "getpgid", boom, raising=False)
    watch = process._VanishWatch(_FakeProc(alive=False), bound_s=0.1)
    assert watch.pgid is None
    # Dead leader + long silence: still never fires, because group emptiness
    # is unprovable without process groups (fail closed → keep waiting).
    time.sleep(0.25)
    assert watch.check() is False


def test_group_alive_unknowable_without_pgid():
    assert process._group_alive(None) is None


def test_spawn_isolation_kwargs(non_posix):
    kwargs = process._spawn_isolation_kwargs()
    assert "start_new_session" not in kwargs
    assert "creationflags" in kwargs


def test_kill_uses_taskkill_tree(non_posix, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    proc = _FakeProc(alive=True)
    process._kill_process_group(proc)
    assert calls == [["taskkill", "/T", "/F", "/PID", "4242"]]
    assert proc.killed  # leader kill backstop


def test_kill_survives_missing_taskkill(non_posix, monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(process.subprocess, "run", fake_run)
    proc = _FakeProc(alive=True)
    process._kill_process_group(proc)
    assert proc.killed


def test_sink_replay_frames_complete_lines_only():
    got = []
    process._replay_lines_to_sink("a\nb\npartial", got.append)
    assert got == ["a\n", "b\n"]


def test_sink_replay_fault_is_fail_closed():
    def bad_sink(_line):
        raise RuntimeError("disk full")

    with pytest.raises(StreamSinkError):
        process._replay_lines_to_sink("a\n", bad_sink)


def test_streaming_sink_falls_back_to_buffered_off_posix(non_posix, monkeypatch):
    # Off POSIX the selectors-based streaming path must never run.
    def no_streaming(*a, **k):
        raise AssertionError("selectors streaming path used off POSIX")

    monkeypatch.setattr(process, "_run_streaming", no_streaming)
    got = []
    out = run_with_timeout(
        [sys.executable, "-c", "print('one'); print('two')"],
        timeout_s=30,
        sink=got.append,
    )
    assert out.exit_code == 0
    assert [line.strip() for line in got] == ["one", "two"]


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows spawn/kill")
def test_windows_real_watchdog_run_and_timeout_tree_kill():
    # The doctor crash: the default watchdog is armed on every spawn.
    ok = run_with_timeout([sys.executable, "-c", "print('done')"], timeout_s=30)
    assert ok.exit_code == 0 and "done" in ok.stdout

    out = run_with_timeout(
        [
            sys.executable,
            "-c",
            (
                "import subprocess, sys, time;"
                "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
                "print(p.pid, flush=True); time.sleep(60)"
            ),
        ],
        timeout_s=2,
    )
    assert out.timed_out
    grandchild = out.stdout.strip().splitlines()[0]
    deadline = time.monotonic() + 5
    alive = True
    while time.monotonic() < deadline:
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {grandchild}", "/NH"],
            capture_output=True, text=True,
        ).stdout
        if grandchild not in listing:
            alive = False
            break
        time.sleep(0.2)
    assert not alive, f"grandchild {grandchild} survived the tree kill"


def test_resolve_argv0_uses_which_off_posix(non_posix, monkeypatch):
    monkeypatch.setattr(process.shutil, "which", lambda n: rf"C:\bin\{n}.CMD")
    assert process.resolve_argv0(["codex", "exec", "-"]) == [
        r"C:\bin\codex.CMD", "exec", "-"]


def test_resolve_argv0_leaves_qualified_and_unknown_names(non_posix, monkeypatch):
    monkeypatch.setattr(process.shutil, "which", lambda n: None)
    assert process.resolve_argv0(["nope", "x"]) == ["nope", "x"]
    assert process.resolve_argv0([r"C:\t\codex.cmd"]) == [r"C:\t\codex.cmd"]
    assert process.resolve_argv0([]) == []


def test_resolve_argv0_is_identity_on_posix(monkeypatch):
    monkeypatch.setattr(process, "_POSIX", True)
    monkeypatch.setattr(process.shutil, "which", lambda n: "/should/not/use")
    assert process.resolve_argv0(["codex"]) == ["codex"]


@pytest.mark.skipif(sys.platform != "win32", reason="real .cmd shim spawn")
def test_windows_cmd_shim_runs_by_bare_name(tmp_path, monkeypatch):
    shim = tmp_path / "gauntlet-test-shim.cmd"
    shim.write_text("@echo off\r\necho shim-ran %*\r\n")
    monkeypatch.setenv("PATH", f"{tmp_path};{process.os.environ['PATH']}")
    out = run_with_timeout(["gauntlet-test-shim", "--version"], timeout_s=30)
    assert out.exit_code == 0
    assert "shim-ran --version" in out.stdout

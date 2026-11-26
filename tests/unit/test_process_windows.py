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
    monkeypatch.setattr(process.shutil, "which", lambda n, path=None: rf"C:\bin\{n}.exe")
    assert process.resolve_argv0(["codex", "exec", "-"]) == [
        r"C:\bin\codex.exe", "exec", "-"]


def test_resolve_argv0_resolved_batch_file_is_quoted_not_listed(non_posix, monkeypatch):
    # A resolved .cmd that is not an npm shim is launched as a quoted command
    # line (a string), never as a bare list cmd.exe would re-parse.
    monkeypatch.setattr(process.shutil, "which", lambda n, path=None: rf"C:\bin\{n}.CMD")
    assert process.resolve_argv0(["codex", "exec", "-"]) == r"C:\bin\codex.CMD exec -"


def test_resolve_argv0_leaves_qualified_and_unknown_names(non_posix, monkeypatch):
    monkeypatch.setattr(process.shutil, "which", lambda n, path=None: None)
    assert process.resolve_argv0(["nope", "x"]) == ["nope", "x"]
    assert process.resolve_argv0([r"C:\t\codex.exe"]) == [r"C:\t\codex.exe"]
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


# --- #178 review fixes ------------------------------------------------------
def test_post_kill_drain_is_bounded_off_posix(non_posix, monkeypatch):
    """F-001: a descendant outside the kill's reach can hold the pipes open, so
    the Windows post-kill drain must be bounded (FR-3.3), never communicate()."""
    called = []
    monkeypatch.setattr(process, "_drain_bounded", lambda p: called.append(p) or ("o", "e"))

    class _P:
        def communicate(self):
            raise AssertionError("unbounded drain off POSIX")

    assert process._drain_after_kill(_P()) == ("o", "e")
    assert len(called) == 1


def test_post_kill_drain_unchanged_on_posix(monkeypatch):
    monkeypatch.setattr(process, "_POSIX", True)

    class _P:
        def communicate(self):
            return "o", "e"

    assert process._drain_after_kill(_P()) == ("o", "e")


def test_kill_terminates_the_job_first(non_posix, monkeypatch):
    calls = []
    monkeypatch.setattr(process.subprocess, "run",
                        lambda argv, **k: calls.append(argv))
    proc = _FakeProc(alive=True)

    def job_kill():
        proc._alive = False  # the job took the whole tree, leader included
        return True

    proc._gauntlet_job_kill = job_kill
    process._kill_process_group(proc)
    assert calls == []  # nothing left for the taskkill backstop
    assert proc.killed


def test_assign_windows_job_is_inert_on_posix(monkeypatch):
    monkeypatch.setattr(process, "_POSIX", True)
    proc = _FakeProc()
    process._assign_windows_job(proc)
    assert not hasattr(proc, "_gauntlet_job_kill")


def test_assign_windows_job_failure_leaves_child_unjobbed(non_posix):
    # No kernel32 off Windows: the best-effort assignment must not raise.
    proc = _FakeProc()
    process._assign_windows_job(proc)
    assert not hasattr(proc, "_gauntlet_job_kill")


def test_text_mode_is_utf8_off_posix(non_posix):
    """F-003: cp1252 cannot encode prompt characters such as '→'."""
    assert process._text_kwargs() == {"text": True, "encoding": "utf-8", "errors": "replace"}


def test_text_mode_unchanged_on_posix(monkeypatch):
    monkeypatch.setattr(process, "_POSIX", True)
    assert process._text_kwargs() == {"text": True}


def test_sink_replay_frames_on_newline_only():
    """U+2028 (unescaped by JSON printers) must not split a line, and must not
    truncate every later line (second-round review)."""
    got = []
    process._replay_lines_to_sink('{"t": "a\u2028b"}\n{"t": "c"}\n', got.append)
    assert got == ['{"t": "a\u2028b"}\n', '{"t": "c"}\n']


def test_resolve_searches_the_childs_path_not_the_cwd(non_posix, monkeypatch):
    seen = {}

    def fake_which(name, path=None):
        seen["path"] = path
        return None

    monkeypatch.setattr(process.shutil, "which", fake_which)
    process.resolve_argv0(["codex"], env={"PATH": r"C:\tools"})
    assert seen["path"] == r"C:\tools"


def _npm_shim(dir_, script_rel):
    shim = dir_ / "codex.cmd"
    shim.write_text(
        "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n"
        ":start\r\nSETLOCAL\r\nCALL :find_dp0\r\n\r\n"
        'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n) ELSE (\r\n'
        '  SET "_prog=node"\r\n  SET PATHEXT=%PATHEXT:;.JS;=;%\r\n)\r\n\r\n'
        f'endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  '
        f'"%dp0%\\{script_rel}" %*\r\n'
    )
    return shim


def test_npm_shim_is_unwrapped_to_node_and_script(non_posix, tmp_path):
    """F-002: an npm shim runs as `node <script>`, bypassing cmd.exe, so an
    argument like a path under `C:\\Users\\R&D` reaches the CLI intact."""
    script = tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("// cli\n")
    (tmp_path / "node.exe").write_text("")
    shim = _npm_shim(tmp_path, r"node_modules\@openai\codex\bin\codex.js")
    argv = process.resolve_argv0([str(shim), "exec", "-o", r"C:\Users\R&D\out.txt"])
    assert isinstance(argv, list)
    assert argv[0] == str(tmp_path / "node.exe")
    assert argv[1].endswith("codex.js")
    assert argv[2:] == ["exec", "-o", r"C:\Users\R&D\out.txt"]


def test_unrecognised_batch_file_args_are_quoted(non_posix, tmp_path):
    bat = tmp_path / "tool.cmd"
    bat.write_text("@echo off\r\necho %*\r\n")
    line = process.resolve_argv0([str(bat), "--version", r"C:\Users\R&D\o", "a|b"])
    assert isinstance(line, str)
    assert '"C:\\Users\\R&D\\o"' in line and '"a|b"' in line
    assert " --version " in line  # plain flags stay unquoted


@pytest.mark.parametrize("bad", ['{"a": "b"}', "100%", "two\nlines"])
def test_batch_file_refuses_args_cmd_would_reparse(non_posix, tmp_path, bad):
    bat = tmp_path / "tool.cmd"
    bat.write_text("@echo off\r\n")
    with pytest.raises(process.UnsafeBatchArgumentsError):
        process.resolve_argv0([str(bat), "--json-schema", bad])


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows orphan kill")
def test_windows_timeout_with_orphaned_pipe_holder_is_bounded():
    """F-001: the launcher exits at once, leaving a grandchild that inherited
    stdout. The timeout must still return promptly (job kill + bounded drain)."""
    start = time.monotonic()
    out = run_with_timeout(
        [
            sys.executable, "-c",
            (
                "import subprocess, sys;"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']);"
                "print('launched', flush=True)"
            ),
        ],
        timeout_s=2,
    )
    assert time.monotonic() - start < 30
    assert out.timed_out


@pytest.mark.skipif(sys.platform != "win32", reason="real .cmd metacharacters")
def test_windows_cmd_shim_preserves_metacharacter_args(tmp_path, monkeypatch):
    shim = tmp_path / "gauntlet-meta-shim.cmd"
    shim.write_text('@echo off\r\necho [%1] [%2]\r\n')
    monkeypatch.setenv("PATH", f"{tmp_path};{process.os.environ['PATH']}")
    out = run_with_timeout(
        ["gauntlet-meta-shim", r"C:\Users\R&D\x", "a>b"], timeout_s=30
    )
    assert out.exit_code == 0
    assert "R&D" in out.stdout and "a>b" in out.stdout
    assert not (tmp_path / "b").exists()  # `>` was not a redirect

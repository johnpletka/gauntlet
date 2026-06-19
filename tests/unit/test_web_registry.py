"""P7 console registry + `run --watch` discovery/reuse (FR-12.1/12.4).

Two layers:

* **Pure helpers** — ``ConsoleRecord`` round-trip + PID-reuse-safe liveness,
  ``read/write/remove_registry``, ``is_reusable``, ``port_is_free`` — run anywhere.
* **A subprocess lifecycle test** (mirroring ``test_resume_crash`` style: real
  ``Popen`` + signal) that boots a detached console via ``ensure_console``,
  asserts the registry is recorded and ``/healthz`` answers, that a **second**
  ``ensure_console`` *reuses* it (no second process), and that after the console
  is killed its **stale** registry entry is reclaimed by a fresh boot.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time

import pytest

from gauntlet.procident import ProcessIdentity, read_process_identity
from gauntlet.web.registry import (
    ConsoleRecord,
    build_record,
    ensure_console,
    is_reusable,
    port_is_free,
    read_registry,
    remove_registry,
    write_registry,
)


# --- pure helpers ------------------------------------------------------------


def _record(pid: int, *, port: int = 9999, identity=None) -> ConsoleRecord:
    ident = identity if identity is not None else read_process_identity(pid)
    return ConsoleRecord(
        pid=pid,
        pgid=pid,
        proc_identity=ident.to_dict() if ident is not None else None,
        host="127.0.0.1",
        port=port,
        url=f"http://127.0.0.1:{port}",
        token_fingerprint="abc",
        started_at="2026-06-18T00:00:00+00:00",
        log_path="/tmp/x.log",
    )


def test_console_record_json_roundtrip():
    rec = _record(os.getpid())
    back = ConsoleRecord.from_json(rec.to_json())
    assert back is not None
    assert back.pid == rec.pid
    assert back.port == rec.port
    assert back.url == rec.url
    assert back.token_fingerprint == "abc"


def test_console_record_from_json_malformed_is_none():
    assert ConsoleRecord.from_json("not json") is None
    assert ConsoleRecord.from_json("{}") is None


def test_record_liveness_pid_reuse_safe():
    # The current process is live and its identity matches.
    assert _record(os.getpid()).is_live() is True
    # A mismatched identity (simulated PID reuse) reads as not-live.
    bogus = ProcessIdentity(platform="linux", value=1, unit="boot_ticks")
    assert _record(os.getpid(), identity=bogus).is_live() is False
    # A null identity is unverifiable → not-live (fail closed).
    rec = _record(os.getpid())
    rec.proc_identity = None
    assert rec.is_live() is False


def test_registry_read_write_remove(tmp_path):
    run_root = tmp_path / "runs"
    run_root.mkdir()
    assert read_registry(run_root) is None
    rec = _record(os.getpid())
    write_registry(run_root, rec)
    back = read_registry(run_root)
    assert back is not None and back.pid == rec.pid
    remove_registry(run_root)
    assert read_registry(run_root) is None
    remove_registry(run_root)  # idempotent


def test_write_registry_ignores_console_artifacts(tmp_path):
    # The registry + console log must never dirty the worktree (clean-worktree
    # invariant): writing the registry appends them to the run-root .gitignore.
    run_root = tmp_path / "runs"
    run_root.mkdir()
    write_registry(run_root, _record(os.getpid()))
    ignored = (run_root / ".gitignore").read_text().split()
    assert ".console.json" in ignored
    assert ".console.log" in ignored
    assert ".gitignore" in ignored


def test_registry_gitignore_preserves_existing_entries(tmp_path):
    # Coexists with the engine's run-root .gitignore (additive, order-independent).
    run_root = tmp_path / "runs"
    run_root.mkdir()
    (run_root / ".gitignore").write_text(".gitignore\n.driving.lock\n")
    write_registry(run_root, _record(os.getpid()))
    ignored = (run_root / ".gitignore").read_text().split()
    assert ".driving.lock" in ignored  # engine entry preserved
    assert ".console.json" in ignored  # console entry added


def test_is_reusable_dead_pid(tmp_path):
    # A record whose pid is not us (and whose identity won't match) is not live,
    # so it is never reusable regardless of healthz.
    rec = _record(os.getpid())
    rec.proc_identity = None  # force unverifiable → not live
    assert is_reusable(rec) is False
    assert is_reusable(None) is False


def test_port_is_free_detects_a_bound_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        assert port_is_free("127.0.0.1", port) is False
    finally:
        sock.close()
    # Once closed, the port is free again.
    assert port_is_free("127.0.0.1", port) is True


def test_build_record_is_for_current_process():
    rec = build_record(
        host="127.0.0.1", port=1234, token="sekret", log_path=tmp_logpath()
    )
    assert rec.pid == os.getpid()
    assert rec.is_live() is True  # us, identity matches
    assert "sekret" not in rec.token_fingerprint  # non-reversible


def tmp_logpath():
    from pathlib import Path

    return Path("/tmp/console.log")


# --- subprocess lifecycle: boot / reuse / reclaim ----------------------------


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _git_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "runs").mkdir()
    return path


def _kill_pgid(pid: int) -> None:
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


@pytest.mark.skipif(
    not __import__("shutil").which("git"), reason="git required to boot a console"
)
def test_ensure_console_boots_reuses_and_reclaims(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    run_root = repo / "runs"
    port = _free_port()
    booted_pids: list[int] = []
    try:
        # 1) No console yet → boot a detached one; registry + healthz come up.
        h1 = ensure_console(
            repo, run_root, host="127.0.0.1", port=port, token="watch-tok",
            boot_timeout=20.0,
        )
        assert h1.reused is False
        assert h1.pid is not None
        booted_pids.append(h1.pid)
        rec1 = read_registry(run_root)
        assert rec1 is not None
        assert rec1.port == port
        assert rec1.is_live() is True
        assert h1.login_url.endswith("/login")

        # 2) A second --watch reuses the live console — no new process.
        h2 = ensure_console(
            repo, run_root, host="127.0.0.1", port=port, token="watch-tok"
        )
        assert h2.reused is True
        assert h2.pid is None  # nothing booted
        assert h2.token_mismatch is False  # same token
        rec2 = read_registry(run_root)
        assert rec2 is not None and rec2.pid == rec1.pid  # same console

        # 2b) Reuse with a *different* supplied token flags the mismatch but does
        #     NOT restart the running console (FR-12.4 token compatibility).
        h2b = ensure_console(
            repo, run_root, host="127.0.0.1", port=port, token="other-tok"
        )
        assert h2b.reused is True and h2b.token_mismatch is True
        assert read_registry(run_root).pid == rec1.pid  # still the same console

        # 3) Kill the console → its registry entry is now stale. A fresh
        #    ensure_console reclaims it and boots a new console.
        _kill_pgid(rec1.pid)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not port_is_free("127.0.0.1", port):
            time.sleep(0.1)
        assert is_reusable(read_registry(run_root)) is False  # stale

        h3 = ensure_console(
            repo, run_root, host="127.0.0.1", port=port, token="watch-tok",
            boot_timeout=20.0,
        )
        assert h3.reused is False
        assert h3.pid is not None and h3.pid != rec1.pid
        booted_pids.append(h3.pid)
        rec3 = read_registry(run_root)
        assert rec3 is not None and rec3.pid == h3.pid
    finally:
        for pid in booted_pids:
            _kill_pgid(pid)


@pytest.mark.skipif(
    not __import__("shutil").which("git"), reason="git required to boot a console"
)
def test_ensure_console_mints_token_when_none_supplied(tmp_path, monkeypatch):
    # The default `run --watch` flow passes no token and may have no
    # GAUNTLET_WEB_TOKEN in env. ensure_console must then MINT a token, hand it to
    # the detached child, and return it — otherwise the CLI prints /login but not
    # the token needed to sign in (F-001).
    from gauntlet.web.auth import token_fingerprint

    monkeypatch.delenv("GAUNTLET_WEB_TOKEN", raising=False)
    repo = _git_repo(tmp_path / "repo")
    run_root = repo / "runs"
    port = _free_port()
    booted_pids: list[int] = []
    try:
        handle = ensure_console(
            repo, run_root, host="127.0.0.1", port=port, token=None,
            boot_timeout=20.0,
        )
        assert handle.reused is False
        assert handle.pid is not None
        booted_pids.append(handle.pid)
        # A usable, non-empty token came back...
        assert handle.token
        # ...and it is the token the booted console actually authenticates with:
        # the registry fingerprint matches it (the child used OUR token, not a
        # private one it generated).
        rec = read_registry(run_root)
        assert rec is not None
        assert rec.token_fingerprint == token_fingerprint(handle.token)
    finally:
        for pid in booted_pids:
            _kill_pgid(pid)


def test_serve_reuses_live_registered_console(tmp_path, monkeypatch, capsys):
    # FR-12.4: a second `serve` against a port a live console already owns must
    # REUSE it (report its URL and exit cleanly), not start a duplicate uvicorn
    # that would fail to bind (F-002).
    from gauntlet.web import runner

    repo = _git_repo(tmp_path / "repo")
    rec = _record(os.getpid(), port=4321)
    monkeypatch.setattr(runner, "read_registry", lambda _run_root: rec)
    monkeypatch.setattr(runner, "is_reusable", lambda _record: True)

    def _no_bind(*_a, **_k):
        raise AssertionError("serve must not start uvicorn when a console is reusable")

    monkeypatch.setattr("uvicorn.run", _no_bind)

    runner.serve(repo, host="127.0.0.1", port=8765)

    out = capsys.readouterr()
    assert rec.url in out.out  # reports the existing console URL on stdout
    assert f"{rec.url}/login" in out.err  # and its token-free login URL on stderr

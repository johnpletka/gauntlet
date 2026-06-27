"""`gauntlet run --watch` console-ensure wiring (P7, FR-12.1/12.4).

Unit-level: the `run --watch` flag calls :func:`ensure_console` and reports the
URL, and is **fail-soft** — a console boot failure surfaces a warning but never
aborts the foreground run. The full boot/reuse/reclaim lifecycle is covered by
``test_web_registry``; here we only assert the CLI wiring.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import gauntlet.cli as cli
from gauntlet.web.registry import ConsoleBootError, ConsoleHandle


def _fake_manager(tmp_path: Path):
    return SimpleNamespace(
        repo_root=tmp_path,
        config=SimpleNamespace(run_root="runs"),
    )


def test_watch_reuses_existing_console(tmp_path, monkeypatch, capsys):
    calls = {}

    def fake_ensure(repo_root, run_root, *, host, port, **kw):
        calls["repo_root"] = repo_root
        calls["run_root"] = run_root
        return ConsoleHandle(
            host=host, port=port, url=f"http://{host}:{port}", reused=True
        )

    monkeypatch.setattr("gauntlet.web.registry.ensure_console", fake_ensure)
    cli._ensure_watch_console(_fake_manager(tmp_path), host="127.0.0.1", port=8765)
    out = capsys.readouterr().out
    assert "reusing the running console" in out
    # A reused console with no persisted token (legacy record) surfaces /login,
    # never an authenticated ?p= URL (FR-1.2).
    assert "/login" in out
    assert calls["run_root"] == tmp_path / "runs"


def test_watch_reports_booted_console_and_token(tmp_path, monkeypatch, capsys):
    def fake_ensure(repo_root, run_root, *, host, port, **kw):
        return ConsoleHandle(
            host=host, port=port, url=f"http://{host}:{port}", reused=False,
            token="tok-123", pid=4321,
        )

    monkeypatch.setattr("gauntlet.web.registry.ensure_console", fake_ensure)
    cli._ensure_watch_console(_fake_manager(tmp_path), host="127.0.0.1", port=8765)
    err = capsys.readouterr()
    assert "console started" in err.out
    assert "GAUNTLET_WEB_TOKEN=tok-123" in err.err


def test_watch_is_fail_soft_on_boot_error(tmp_path, monkeypatch, capsys):
    # A port conflict (or any boot failure) must NOT abort the run — it warns and
    # returns so the foreground pipeline still runs (FR-12.1).
    def boom(*a, **k):
        raise ConsoleBootError("port 8765 in use by an unrelated process")

    monkeypatch.setattr("gauntlet.web.registry.ensure_console", boom)
    # No exception propagates.
    cli._ensure_watch_console(_fake_manager(tmp_path), host="127.0.0.1", port=8765)
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "continuing without a --watch console" in err

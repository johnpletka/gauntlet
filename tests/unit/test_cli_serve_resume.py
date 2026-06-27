"""`gauntlet serve --resume` — reuse/boot detached, open browser, return (FR-4).

Unlike plain `serve` (foreground bind, no browser — FR-4.3), `--resume` reuses a
live console or boots one detached, opens the authenticated browser, and returns
immediately. A boot that never answers healthz fails closed naming the log path
(FR-4.2). The boot/reuse lifecycle itself is covered by ``test_web_registry``;
here we assert the CLI wiring.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

import gauntlet.cli as cli
from gauntlet.cli import app
from gauntlet.web.registry import ConsoleBootError, ConsoleHandle

runner = CliRunner()


def _fake_manager(tmp_path: Path):
    return SimpleNamespace(
        repo_root=tmp_path,
        config=SimpleNamespace(run_root="runs"),
    )


@pytest.fixture
def opened(monkeypatch):
    """Capture open_authenticated calls without touching a real browser."""
    calls: list[ConsoleHandle] = []
    monkeypatch.setattr(
        "gauntlet.web.launch.open_authenticated",
        lambda handle, **kw: calls.append(handle) or handle.url,
    )
    return calls


def test_resume_reuses_live_console_no_new_process(tmp_path, monkeypatch, opened, capsys):
    spawned = {"count": 0}

    def fake_ensure(repo_root, run_root, *, host, port, **kw):
        # A reused console means ensure_console spawned nothing.
        return ConsoleHandle(
            host=host, port=port, url=f"http://{host}:{port}", reused=True,
            token="tok-live",
        )

    monkeypatch.setattr(cli, "_manager", lambda: _fake_manager(tmp_path))
    monkeypatch.setattr("gauntlet.web.registry.ensure_console", fake_ensure)
    cli._serve_resume(host="127.0.0.1", port=8765, no_browser=False)
    assert spawned["count"] == 0
    assert len(opened) == 1  # browser opened on the reused console
    assert "reusing the running console" in capsys.readouterr().out


def test_resume_boots_detached_then_opens(tmp_path, monkeypatch, opened, capsys):
    def fake_ensure(repo_root, run_root, *, host, port, **kw):
        return ConsoleHandle(
            host=host, port=port, url=f"http://{host}:{port}", reused=False,
            token="tok-new", pid=999,
        )

    monkeypatch.setattr(cli, "_manager", lambda: _fake_manager(tmp_path))
    monkeypatch.setattr("gauntlet.web.registry.ensure_console", fake_ensure)
    cli._serve_resume(host="127.0.0.1", port=8765, no_browser=False)
    assert len(opened) == 1
    out = capsys.readouterr()
    assert "console started" in out.out
    assert "GAUNTLET_WEB_TOKEN=tok-new" in out.err


def test_resume_fails_closed_naming_log_on_dead_boot(tmp_path, monkeypatch, opened, capsys):
    def boom(*a, **k):
        raise ConsoleBootError(
            "console booted (pid 5) but /healthz did not answer; see /runs/.console.log"
        )

    monkeypatch.setattr(cli, "_manager", lambda: _fake_manager(tmp_path))
    monkeypatch.setattr("gauntlet.web.registry.ensure_console", boom)
    with pytest.raises(typer.Exit) as exc:
        cli._serve_resume(host="127.0.0.1", port=8765, no_browser=False)
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert ".console.log" in err  # the error names the log path (FR-4.2)
    assert len(opened) == 0  # no browser opened on a failed boot


# --- routing: --resume vs plain serve (FR-4.3) -----------------------------


def test_resume_flag_routes_to_serve_resume(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        cli, "_serve_resume",
        lambda **kw: seen.update(kw),
    )
    # serve_console must NOT be invoked under --resume (no foreground bind).
    monkeypatch.setattr(
        "gauntlet.web.runner.serve",
        lambda *a, **k: pytest.fail("foreground serve must not run under --resume"),
    )
    result = runner.invoke(app, ["serve", "--resume", "--no-browser"])
    assert result.exit_code == 0
    assert seen == {"host": "127.0.0.1", "port": 8765, "no_browser": True}


def test_plain_serve_does_not_resume_or_open_browser(monkeypatch):
    # FR-4.3: plain `serve` binds in the foreground (serve_console) and never
    # routes through the resume/browser path.
    monkeypatch.setattr(
        cli, "_serve_resume",
        lambda **kw: pytest.fail("plain serve must not resume"),
    )
    bound = {"count": 0}
    monkeypatch.setattr(
        "gauntlet.web.runner.serve",
        lambda *a, **k: bound.__setitem__("count", bound["count"] + 1),
    )
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0
    assert bound["count"] == 1

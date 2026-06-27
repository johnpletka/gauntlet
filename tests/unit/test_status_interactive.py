"""`gauntlet status --interactive` — attach the monitor to an existing run (P4, FR-8/FR-9).

The attach entry point starts **no** new run; it foregrounds the *same* P3 monitor
(`interactive.launch_monitor` + `build_monitor_command`) on a run started without
`--interactive`, selecting operator-vs-prompted authz purely from driver liveness:

- FR-8.1: the launcher is invoked for the deterministically-resolved run instance
  (`active-run.txt` else lexically-greatest `run-*`) and **no** `RunProcess` is
  started; an unknown agent value or an absent/unsafe run errors before launch.
- FR-8.2: a live driver + readable `judge.json` → operator-session env (§6.3); any
  other liveness or unreadable record → a normal prompted session (no judge env),
  and the command still succeeds.
- FR-8.3: `--interactive` is additive — `status <slug>` with no flag still renders
  status and execs no agent; `--interactive` + `--json` are mutually exclusive.
- the attach launch reuses P3's `build_monitor_command` (review F-002): the
  executable/argv/cwd/env-overlay contract is identical to `run --interactive`,
  with no one-shot adapter flags.

The interactive `exec`/`chdir`/`liveness` are stubbed throughout — no agent is ever
launched and no real liveness probe runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from gauntlet import interactive
from gauntlet.cli import app
from gauntlet.engine.judgeproc import JudgeRecord

runner = CliRunner()

JUDGE_ENV_VARS = (
    "GAUNTLET_RUN_ID",
    "GAUNTLET_JUDGE_URL",
    "GAUNTLET_JUDGE_TOKEN",
    "GAUNTLET_STEP_ID",
)

_ONE_SHOT_TOKENS = ("-p", "--print", "--output-schema", "exec")


def _clean_judge_env(monkeypatch) -> None:
    """The only source of any judge var in the execed env must be the overlay."""
    for var in JUDGE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _setup_repo(tmp_path: Path) -> Path:
    """A minimal repo with `.gauntlet/config.yaml` + a resolvable `runs/demo` run."""
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    slug_dir = tmp_path / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    man = {
        "run_id": "run-1",
        "slug": "demo",
        "branch": "gauntlet/demo",
        "base_branch": "main",
        "pipeline": {"name": "p", "version": 1, "hash": "h"},
        "status": "running",
        "steps": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))
    (slug_dir / "active-run.txt").write_text("run-1\n")
    return run_dir


def _write_judge_json(run_dir: Path, *, url="http://127.0.0.1:9100", token="jtok") -> None:
    record = JudgeRecord(
        pid=1234, pgid=1234, proc_identity=None, host="h", port=9100,
        url=url, token=token, run_id="run-1", started_at="2026-01-01T00-00-00",
    )
    (run_dir / "judge.json").write_text(record.to_json())


def _no_runprocess(monkeypatch) -> list:
    """Assert `status --interactive` never starts a RunProcess (it only attaches)."""
    started: list = []
    monkeypatch.setattr(
        "gauntlet.web.jobproc.RunProcess.start",
        lambda self: started.append(self) or self,
    )
    return started


def _patch_launcher_capturing_exec(monkeypatch, *, liveness: str) -> dict:
    """Drive the REAL launcher through the CLI but inject its test seams.

    Wraps `interactive.launch_monitor` so the CLI's own call still runs the real
    fail-closed wiring (FR-8.2), while the exec/chdir/liveness injection points the
    launcher already exposes capture the launch vector instead of replacing the
    process — proving the attach path's argv/env/cwd contract end to end.
    """
    captured: dict = {}
    real = interactive.launch_monitor

    def wrapper(**kwargs):
        kwargs["exec_fn"] = lambda e, a, env: captured.update(
            executable=e, argv=a, env=env
        )
        kwargs["chdir_fn"] = lambda p: captured.update(cwd=p)
        kwargs["liveness_fn"] = lambda run_root, slug: liveness
        captured["kwargs"] = kwargs
        return real(**kwargs)

    monkeypatch.setattr("gauntlet.interactive.launch_monitor", wrapper)
    return captured


# --- FR-8.1: attach the resolved run, start no RunProcess --------------------

def test_status_interactive_attaches_resolved_run_starts_no_runprocess(tmp_path, monkeypatch):
    run_dir = _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    started = _no_runprocess(monkeypatch)

    calls: dict = {}
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: calls.update(kw)
    )

    result = runner.invoke(app, ["status", "demo", "--interactive"])
    assert result.exit_code == 0, result.output

    # FR-8.1: the launcher is invoked for the deterministically-resolved instance…
    assert calls["run_dir"] == run_dir
    assert calls["slug"] == "demo"
    assert calls["agent"] == "claude"  # bare --interactive selects claude (FR-7.1)
    assert calls["use_judge"] is True  # attach always tries the judge; degrades by liveness
    assert calls["judge_wait_s"] == 0.0  # no detached-launch race to wait through
    # …and NO new run is started (the run already exists).
    assert started == []


def test_status_interactive_codex_selects_codex(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    _no_runprocess(monkeypatch)
    calls: dict = {}
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: calls.update(kw)
    )

    result = runner.invoke(app, ["status", "demo", "--interactive=codex"])
    assert result.exit_code == 0, result.output
    assert calls["agent"] == "codex"


def test_status_interactive_unknown_agent_errors_before_launch(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    started = _no_runprocess(monkeypatch)
    calls: dict = {}
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: calls.update(kw)
    )

    result = runner.invoke(app, ["status", "demo", "--interactive=bogus"])
    assert result.exit_code != 0
    assert "claude" in result.output and "codex" in result.output  # names choices
    assert calls == {}  # launcher never invoked
    assert started == []


def test_status_interactive_absent_run_errors(tmp_path, monkeypatch):
    # A repo with no run instance for the slug → the attach errors (FR-8.1), and
    # the launcher is never invoked.
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    monkeypatch.chdir(tmp_path)
    calls: dict = {}
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: calls.update(kw)
    )

    result = runner.invoke(app, ["status", "ghost", "--interactive"])
    assert result.exit_code != 0
    assert calls == {}


def test_status_interactive_run_dir_without_manifest_errors(tmp_path, monkeypatch):
    # F-001: a resolvable `runs/<slug>/run-*` (a stale reservation or a hand-made
    # dir) with NO manifest must error per the FR-8.1 absent-run contract, not
    # launch a monitor against a non-run.
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    slug_dir = tmp_path / "runs" / "demo"
    (slug_dir / "run-1").mkdir(parents=True)  # resolvable dir, but no manifest.json
    (slug_dir / "active-run.txt").write_text("run-1\n")
    monkeypatch.chdir(tmp_path)
    started = _no_runprocess(monkeypatch)
    calls: dict = {}
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: calls.update(kw)
    )

    result = runner.invoke(app, ["status", "demo", "--interactive"])
    assert result.exit_code == 1
    assert "cannot load manifest" in result.output
    assert calls == {}  # launcher never invoked
    assert started == []


def test_status_interactive_mismatched_manifest_errors(tmp_path, monkeypatch):
    # F-001: a manifest whose slug/run_id does not match the resolved instance is
    # not this run; refuse to attach rather than launch against a foreign manifest.
    run_dir = _setup_repo(tmp_path)
    man = json.loads((run_dir / "manifest.json").read_text())
    man["slug"] = "other"  # mismatched slug
    (run_dir / "manifest.json").write_text(json.dumps(man))
    monkeypatch.chdir(tmp_path)
    calls: dict = {}
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: calls.update(kw)
    )

    result = runner.invoke(app, ["status", "demo", "--interactive"])
    assert result.exit_code == 1
    assert "does not match run" in result.output
    assert calls == {}  # launcher never invoked


def test_status_interactive_refuses_symlinked_slug_dir_escaping_run_root(tmp_path, monkeypatch):
    # F-002: the attach path shares the safe resolver, so a slug dir that is a
    # symlink out of the configured run_root must error before any launch — the
    # child-of-slug containment check must not pass vacuously on an escaped slug.
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")
    (tmp_path / "runs").mkdir()
    outside_slug = tmp_path / "outside_slug"
    run_dir = outside_slug / "run-1"
    run_dir.mkdir(parents=True)
    man = {
        "run_id": "run-1", "slug": "demo", "branch": "gauntlet/demo",
        "base_branch": "main", "pipeline": {"name": "p", "version": 1, "hash": "h"},
        "status": "running", "steps": [],
    }
    (run_dir / "manifest.json").write_text(json.dumps(man))
    (outside_slug / "active-run.txt").write_text("run-1\n")
    (tmp_path / "runs" / "demo").symlink_to(outside_slug, target_is_directory=True)

    monkeypatch.chdir(tmp_path)
    started = _no_runprocess(monkeypatch)
    calls: dict = {}
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: calls.update(kw)
    )

    result = runner.invoke(app, ["status", "demo", "--interactive"])
    assert result.exit_code == 1
    assert "escapes the run tree" in result.output
    assert calls == {}  # launcher never invoked
    assert started == []


# --- FR-8.2: judge wiring follows driver liveness ----------------------------

def test_status_interactive_gated_when_alive_and_judge_json(tmp_path, monkeypatch):
    run_dir = _setup_repo(tmp_path)
    _write_judge_json(run_dir)
    monkeypatch.chdir(tmp_path)
    _clean_judge_env(monkeypatch)
    started = _no_runprocess(monkeypatch)
    captured = _patch_launcher_capturing_exec(monkeypatch, liveness="alive")

    result = runner.invoke(app, ["status", "demo", "--interactive"])
    assert result.exit_code == 0, result.output

    env = captured["env"]
    assert env["GAUNTLET_RUN_ID"] == "run-1"
    assert env["GAUNTLET_JUDGE_URL"] == "http://127.0.0.1:9100"
    assert env["GAUNTLET_JUDGE_TOKEN"] == "jtok"
    assert "GAUNTLET_STEP_ID" not in env  # operator session — never set (§6.3)
    # Reuses P3's build_monitor_command: same executable/argv/cwd contract (F-002).
    assert captured["executable"] == "claude"
    assert captured["argv"][0] == "claude"
    assert captured["cwd"] == tmp_path.resolve()  # repo root, not the run dir
    for tok in _ONE_SHOT_TOKENS:
        assert tok not in captured["argv"]  # bare interactive, never the one-shot path
    assert started == []


def test_status_interactive_degraded_when_driver_not_alive(tmp_path, monkeypatch):
    # A parked / none-liveness run → a NORMAL prompted session (no judge env), and
    # the command still succeeds for diagnosis (FR-8.2). judge.json present proves
    # liveness alone gates: a readable record does not gate a non-alive driver.
    run_dir = _setup_repo(tmp_path)
    _write_judge_json(run_dir)
    monkeypatch.chdir(tmp_path)
    _clean_judge_env(monkeypatch)
    captured = _patch_launcher_capturing_exec(monkeypatch, liveness="none")

    result = runner.invoke(app, ["status", "demo", "--interactive"])
    assert result.exit_code == 0, result.output

    env = captured["env"]
    for var in JUDGE_ENV_VARS:
        assert var not in env  # normal prompted session — none of the judge env
    # Still launches the monitor (degraded), not an error.
    assert captured["executable"] == "claude"


def test_status_interactive_degraded_when_judge_json_absent(tmp_path, monkeypatch):
    # Alive driver but no readable judge.json (the run is not serving a live judge)
    # → degraded, with the zero bounded wait the attach path uses (no hang).
    _setup_repo(tmp_path)  # no judge.json written
    monkeypatch.chdir(tmp_path)
    _clean_judge_env(monkeypatch)
    captured = _patch_launcher_capturing_exec(monkeypatch, liveness="alive")

    result = runner.invoke(app, ["status", "demo", "--interactive"])
    assert result.exit_code == 0, result.output
    env = captured["env"]
    for var in JUDGE_ENV_VARS:
        assert var not in env


# --- FR-8.3: --interactive is additive ---------------------------------------

def test_status_no_flag_does_not_exec_agent(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    invoked: list = []
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: invoked.append(kw)
    )

    result = runner.invoke(app, ["status", "demo"])
    assert result.exit_code == 0, result.output
    assert "demo: running" in result.output  # normal status rendering, unchanged
    assert invoked == []  # no agent execed when the flag is absent


def test_status_interactive_and_json_mutually_exclusive(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    invoked: list = []
    monkeypatch.setattr(
        "gauntlet.interactive.launch_monitor", lambda **kw: invoked.append(kw)
    )

    result = runner.invoke(app, ["status", "demo", "--interactive", "--json"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
    assert invoked == []  # neither output mode runs


def test_status_interactive_starter_prompt_routes_to_operator(tmp_path, monkeypatch):
    # FR-9.1 over the attach path: the composed starter prompt the monitor is
    # seeded with names the slug + run dir and routes to the operator playbook.
    run_dir = _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    _clean_judge_env(monkeypatch)
    captured = _patch_launcher_capturing_exec(monkeypatch, liveness="alive")

    result = runner.invoke(app, ["status", "demo", "--interactive"])
    assert result.exit_code == 0, result.output
    prompt = captured["argv"][-1]  # the trailing positional starter prompt
    assert "demo" in prompt
    assert str(run_dir) in prompt
    assert "gauntlet-operator" in prompt
    assert "prompts/operator.md" in prompt

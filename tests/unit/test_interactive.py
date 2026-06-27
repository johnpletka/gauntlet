"""The interactive run monitor launcher + `run --interactive` wiring (P3, FR-7/FR-9).

Covers the shared launcher (`interactive.py`) and the `gauntlet run --interactive`
CLI path:

- `build_monitor_command` (review F-002): the bare interactive launch vector —
  executable + argv (positional prompt), **repo root as cwd** (plan F-002,
  human-ratified amendment 8855546), operator-session env overlay (empty when
  degraded), and the one-shot-adapter-flag guard.
- `launch_monitor` fail-closed wiring (FR-7.3): operator-session env only when
  `judge.json` is readable AND the driver is alive; degraded (no judge env)
  otherwise — driver not alive, judge.json absent, or `--no-judge`.
- the FR-9.1 starter prompt routes to the `gauntlet-operator` playbook and names
  no autonomous push/merge action.
- `run --interactive` selects claude (bare) / codex (`=codex`), errors on an
  unknown value before any launch (FR-7.1), and launches the run detached via
  `RunProcess` then foregrounds the monitor on that run's dir (FR-7.2).

The interactive `exec` is stubbed throughout — no agent is ever launched.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from gauntlet import interactive
from gauntlet.cli import app
from gauntlet.engine.judgeproc import JudgeRecord
from gauntlet.interactive import (
    _CLAUDE_PROJECT_SCOPE_FLAGS,
    MonitorAgentError,
    MonitorContractError,
    assert_interactive_argv,
    build_monitor_command,
    compose_starter_prompt,
    launch_monitor,
    validate_monitor_agent,
)

runner = CliRunner()

JUDGE_ENV_VARS = (
    "GAUNTLET_RUN_ID",
    "GAUNTLET_JUDGE_URL",
    "GAUNTLET_JUDGE_TOKEN",
    "GAUNTLET_STEP_ID",
)


def _clean_judge_env(monkeypatch) -> None:
    """So the only source of any judge var in the execed env is the overlay."""
    for var in JUDGE_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# Every engine-managed GAUNTLET_* var the run may have set in the parent env
# (matches judgeproc._MANAGED_ENV_VARS), used to seed a *stale* parent env and
# prove the launcher scrubs it before exec (review F-001).
_STALE_PARENT_JUDGE_ENV = {
    "GAUNTLET_RUN_ID": "stale-run",
    "GAUNTLET_JUDGE_URL": "http://stale:1234",
    "GAUNTLET_JUDGE_TOKEN": "stale-token",
    "GAUNTLET_JUDGE_MODE": "unattended",
    "GAUNTLET_STEP_ID": "stale-step",
    "GAUNTLET_REPO_ROOT": "/stale/repo",
}


def _seed_stale_judge_env(monkeypatch) -> None:
    """Seed the parent process with stale managed judge vars (review F-001)."""
    for var, value in _STALE_PARENT_JUDGE_ENV.items():
        monkeypatch.setenv(var, value)


def _write_judge_json(
    run_dir: Path, *, url: str = "http://127.0.0.1:8787", token: str = "jtok",
    run_id: str = "run-1",
) -> None:
    record = JudgeRecord(
        pid=1234, pgid=1234, proc_identity=None, host="h", port=8787,
        url=url, token=token, run_id=run_id, started_at="2026-01-01T00-00-00",
    )
    (run_dir / "judge.json").write_text(record.to_json())


# --- build_monitor_command (review F-002) ------------------------------------

def test_build_monitor_command_claude_positional_prompt_repo_root_cwd():
    env = {
        "GAUNTLET_RUN_ID": "run-1",
        "GAUNTLET_JUDGE_URL": "http://127.0.0.1:8787",
        "GAUNTLET_JUDGE_TOKEN": "tok",
    }
    cmd = build_monitor_command(
        "claude", prompt="PROMPT-TEXT", repo_root=Path("/repo"), judge_env=env
    )
    assert cmd.executable == "claude"
    # `--setting-sources project` loads this repo's `.claude/` config (the judge
    # hook + the `gauntlet-operator` skill the starter prompt routes to); the
    # composed prompt stays the trailing positional (fix/gauntlet-operator-scope).
    assert cmd.argv == ["claude", "--setting-sources", "project", "PROMPT-TEXT"]
    assert cmd.argv[-1] == "PROMPT-TEXT"  # prompt remains the positional arg
    assert cmd.cwd == Path("/repo")  # the repo root, NOT the run dir (plan F-002)
    assert cmd.env_overlay == env
    assert cmd.prompt_delivery == "positional"
    # None of the one-shot adapter flags ever reach a bare interactive argv.
    for tok in ("-p", "--print", "--output-schema", "exec"):
        assert tok not in cmd.argv


def test_build_monitor_command_claude_scopes_project_for_skill_discovery():
    # fix/gauntlet-operator-scope: without `--setting-sources project` the bare
    # interactive claude session does not load the repo's `.claude/` config, so
    # the `gauntlet-operator` skill is out of scope and the agent cannot run it.
    cmd = build_monitor_command(
        "claude", prompt="P", repo_root=Path("/repo"), judge_env={}
    )
    assert list(_CLAUDE_PROJECT_SCOPE_FLAGS) == ["--setting-sources", "project"]
    # The flags appear contiguously, ahead of the trailing positional prompt.
    assert cmd.argv == ["claude", "--setting-sources", "project", "P"]


def test_build_monitor_command_codex_carries_no_setting_sources_flag():
    # codex has no skills/settings-source concept (and fires no PreToolUse hooks),
    # so the project scope flag is claude-only — codex stays a bare positional.
    cmd = build_monitor_command(
        "codex", prompt="P", repo_root=Path("/repo"), judge_env={}
    )
    assert cmd.argv == ["codex", "P"]
    assert "--setting-sources" not in cmd.argv


def test_build_monitor_command_degraded_has_empty_env_overlay():
    cmd = build_monitor_command(
        "claude", prompt="P", repo_root=Path("/repo"), judge_env={}
    )
    assert cmd.env_overlay == {}  # never partial — degraded sets none


def test_build_monitor_command_codex_wired_like_claude_positional_prompt():
    # OQ-2 spike outcome (a): `codex [PROMPT]` takes a positional initial prompt,
    # so codex is wired exactly like claude — bare interactive, prompt positional,
    # never the `codex exec` one-shot subcommand. (BOOTSTRAP-NOTES #53)
    cmd = build_monitor_command(
        "codex", prompt="P", repo_root=Path("/repo"), judge_env={}
    )
    assert cmd.executable == "codex"
    assert cmd.argv == ["codex", "P"]
    assert cmd.prompt_delivery == "positional"
    assert "exec" not in cmd.argv  # bare interactive codex, not `codex exec`


def test_build_monitor_command_rejects_unknown_agent():
    with pytest.raises(MonitorAgentError) as excinfo:
        build_monitor_command("bogus", prompt="P", repo_root=Path("/r"), judge_env={})
    assert "claude" in str(excinfo.value) and "codex" in str(excinfo.value)


# --- agent validation + the one-shot-flag guard ------------------------------

def test_validate_monitor_agent():
    validate_monitor_agent("claude")  # no raise
    validate_monitor_agent("codex")  # no raise
    with pytest.raises(MonitorAgentError) as excinfo:
        validate_monitor_agent("gpt")
    assert "claude" in str(excinfo.value) and "codex" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["-p", "--print", "--output-schema", "exec"])
def test_assert_interactive_argv_rejects_one_shot_tokens(bad):
    with pytest.raises(MonitorContractError):
        assert_interactive_argv(["claude", bad, "PROMPT"], prompt="PROMPT")


def test_assert_interactive_argv_accepts_bare_interactive_and_prompt_text():
    # The bare interactive invocation passes.
    assert_interactive_argv(["claude", "PROMPT"], prompt="PROMPT")
    # A one-shot token appearing *inside the prompt* (one argv element) is fine —
    # only standalone flag tokens are rejected.
    assert_interactive_argv(["codex", "use -p with care"], prompt="use -p with care")


# --- FR-9.1 starter prompt ----------------------------------------------------

def test_compose_starter_prompt_routes_to_operator_playbook():
    run_dir = Path("/repo/runs/demo/run-1")
    prompt = compose_starter_prompt("demo", run_dir, asset_root=".")
    assert "demo" in prompt  # the slug
    assert str(run_dir) in prompt  # the run dir
    assert "gauntlet-operator" in prompt  # the skill
    assert "prompts/operator.md" in prompt  # a reference resolving to the playbook
    # Names no autonomous push/merge action — it is a supervised assistant.
    low = prompt.lower()
    assert "operator's explicit direction" in low
    assert "no autonomous action" in low
    assert "merge nothing without being told" in low


def test_compose_starter_prompt_resolves_playbook_under_asset_root():
    prompt = compose_starter_prompt("demo", Path("/r/runs/demo/run-1"), asset_root=".gauntlet")
    assert ".gauntlet/prompts/operator.md" in prompt


# --- launch_monitor fail-closed wiring (FR-7.3) ------------------------------

def _run_launch_monitor(tmp_path, *, with_judge_json, liveness, use_judge=True):
    run_dir = tmp_path / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True)
    if with_judge_json:
        _write_judge_json(run_dir, url="http://127.0.0.1:9000", token="jtok", run_id="run-1")
    captured: dict = {}

    def fake_exec(executable, argv, env):
        captured["executable"] = executable
        captured["argv"] = argv
        captured["env"] = env

    def fake_chdir(path):
        captured["cwd"] = path

    launch_monitor(
        repo_root=tmp_path,
        run_root=tmp_path / "runs",
        slug="demo",
        run_dir=run_dir,
        agent="claude",
        use_judge=use_judge,
        asset_root=".",
        liveness_fn=lambda rr, slug: liveness,
        exec_fn=fake_exec,
        chdir_fn=fake_chdir,
        judge_wait_s=0,
        poll_interval_s=0,
    )
    return captured


def test_launch_monitor_gated_when_judge_present_and_driver_alive(tmp_path, monkeypatch):
    _clean_judge_env(monkeypatch)
    captured = _run_launch_monitor(tmp_path, with_judge_json=True, liveness="alive")
    env = captured["env"]
    assert env["GAUNTLET_RUN_ID"] == "run-1"
    assert env["GAUNTLET_JUDGE_URL"] == "http://127.0.0.1:9000"
    assert env["GAUNTLET_JUDGE_TOKEN"] == "jtok"
    assert "GAUNTLET_STEP_ID" not in env  # operator session — never set (§6.3)
    assert captured["executable"] == "claude"
    assert captured["argv"][0] == "claude"
    assert captured["cwd"] == tmp_path  # repo root cwd (plan F-002)


@pytest.mark.parametrize("liveness", ["none", "orphaned", "indeterminate"])
def test_launch_monitor_degraded_when_driver_not_alive(tmp_path, monkeypatch, liveness):
    _clean_judge_env(monkeypatch)
    captured = _run_launch_monitor(tmp_path, with_judge_json=True, liveness=liveness)
    env = captured["env"]
    for var in JUDGE_ENV_VARS:
        assert var not in env  # normal prompted session — none of the judge env


def test_launch_monitor_degraded_when_judge_json_absent(tmp_path, monkeypatch):
    _clean_judge_env(monkeypatch)
    # judge.json never appears within the (zero) bounded wait, even though alive.
    captured = _run_launch_monitor(tmp_path, with_judge_json=False, liveness="alive")
    env = captured["env"]
    for var in JUDGE_ENV_VARS:
        assert var not in env


def test_launch_monitor_degraded_under_no_judge_even_with_record(tmp_path, monkeypatch):
    _clean_judge_env(monkeypatch)
    captured = _run_launch_monitor(
        tmp_path, with_judge_json=True, liveness="alive", use_judge=False
    )
    env = captured["env"]
    for var in JUDGE_ENV_VARS:
        assert var not in env


def test_launch_monitor_degraded_scrubs_stale_parent_judge_env(tmp_path, monkeypatch):
    # Review F-001: a degraded session must carry NO judge env even when the parent
    # process (a driver shell, a stale operator env) already has managed vars set.
    # The empty overlay does not remove them — the launcher must scrub them first.
    _seed_stale_judge_env(monkeypatch)
    captured = _run_launch_monitor(tmp_path, with_judge_json=True, liveness="orphaned")
    env = captured["env"]
    for var in _STALE_PARENT_JUDGE_ENV:
        assert var not in env, f"stale parent {var} leaked into a degraded monitor"


def test_launch_monitor_gated_overrides_stale_and_omits_step_id(tmp_path, monkeypatch):
    # Review F-001: on the gated path a stale parent GAUNTLET_STEP_ID must NOT
    # survive — its presence would make the judge classify the operator's own
    # session as an in-run agent (FR-7.3 / FR-10). The other managed vars take the
    # overlay's authoritative values, not the parent's stale ones.
    _seed_stale_judge_env(monkeypatch)
    captured = _run_launch_monitor(tmp_path, with_judge_json=True, liveness="alive")
    env = captured["env"]
    assert "GAUNTLET_STEP_ID" not in env  # operator session — stale value scrubbed
    assert env["GAUNTLET_RUN_ID"] == "run-1"  # overlay value, not the stale parent's
    assert env["GAUNTLET_JUDGE_URL"] == "http://127.0.0.1:9000"
    assert env["GAUNTLET_JUDGE_TOKEN"] == "jtok"
    # Managed vars not in the operator overlay are scrubbed, never inherited stale.
    assert "GAUNTLET_JUDGE_MODE" not in env
    assert "GAUNTLET_REPO_ROOT" not in env


# --- `gauntlet run --interactive` CLI wiring (FR-7.1, FR-7.2) ----------------

def _setup_repo(tmp_path: Path) -> None:
    (tmp_path / ".gauntlet").mkdir()
    (tmp_path / ".gauntlet" / "config.yaml").write_text("{}\n")


def _patch_launch(monkeypatch):
    """Stub the detached RunProcess + the foreground monitor so nothing spawns."""
    started: list = []

    def fake_start(self):
        started.append(self)
        return self

    monkeypatch.setattr("gauntlet.web.jobproc.RunProcess.start", fake_start)

    monitor_calls: dict = {}

    def fake_launch_monitor(**kwargs):
        monitor_calls.update(kwargs)

    monkeypatch.setattr("gauntlet.interactive.launch_monitor", fake_launch_monitor)
    return started, monitor_calls


def test_run_interactive_bare_selects_claude_detaches_and_foregrounds(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    started, monitor_calls = _patch_launch(monkeypatch)

    result = runner.invoke(app, ["run", "demo", "--interactive"])
    assert result.exit_code == 0, result.output

    # FR-7.2: a detached RunProcess started for the pre-allocated run-id + token.
    assert len(started) == 1
    rp = started[0]
    assert rp.run_id.startswith("run-")
    assert rp.reservation_token
    assert "--run-id" in rp.flags
    assert "--reservation-token" in rp.flags
    # FR-7.2: the monitor launcher is invoked with that run's dir.
    assert monitor_calls["run_dir"] == rp.run_dir
    assert monitor_calls["slug"] == "demo"
    # FR-7.1: bare --interactive selects claude.
    assert monitor_calls["agent"] == "claude"
    assert monitor_calls["use_judge"] is True


def test_run_interactive_codex_launches_seeded(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    started, monitor_calls = _patch_launch(monkeypatch)

    result = runner.invoke(app, ["run", "demo", "--interactive=codex"])
    assert result.exit_code == 0, result.output
    # Spike outcome (a): codex is launched (seeded via the positional prompt the
    # launcher composes), never failed-closed and never unseeded.
    assert len(started) == 1
    assert monitor_calls["agent"] == "codex"


def test_run_interactive_unknown_value_errors_before_launch(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    started, monitor_calls = _patch_launch(monkeypatch)

    result = runner.invoke(app, ["run", "demo", "--interactive=bogus"])
    assert result.exit_code != 0
    assert "claude" in result.output and "codex" in result.output  # names choices
    assert started == []  # no run launched
    assert monitor_calls == {}  # monitor never invoked


def test_run_interactive_no_judge_threads_use_judge_false(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    started, monitor_calls = _patch_launch(monkeypatch)

    result = runner.invoke(app, ["run", "demo", "--interactive", "--no-judge"])
    assert result.exit_code == 0, result.output
    assert monitor_calls["use_judge"] is False


def test_run_interactive_rejects_manual_run_id(tmp_path, monkeypatch):
    _setup_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    started, monitor_calls = _patch_launch(monkeypatch)

    result = runner.invoke(
        app, ["run", "demo", "--interactive", "--run-id", "run-x"]
    )
    assert result.exit_code != 0
    assert "managed automatically" in result.output
    assert started == []


def test_cli_bare_interactive_default_matches_launcher_default():
    # Drift guard: the bare-flag normalization default must equal the launcher's
    # validated default, so `--interactive` and the validator never disagree.
    from gauntlet import cli

    assert cli._BARE_INTERACTIVE_VALUE == interactive.DEFAULT_MONITOR_AGENT

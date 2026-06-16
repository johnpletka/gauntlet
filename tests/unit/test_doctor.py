"""`gauntlet doctor` — environment validation against simulated environments.

Each broken environment must produce a FAIL (or WARN) with an actionable
remedy, and a healthy one must pass clean (plan P6 test strategy). The agent-CLI
probes are injected so these run offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from gauntlet.engine.doctor import (
    FAIL,
    OK,
    WARN,
    DoctorProbes,
    has_failure,
    run_doctor,
)
from gauntlet.engine.init import init_repo

# A pin file matching the versions the healthy probe reports.
_PINS = """\
verified_date: "2026-06-10"
clis:
  claude:
    version: "2.1.172"
    verified_flags:
      - {flag: "-p", verified: "works"}
  codex:
    version: "codex-cli 0.139.0"
    verified_flags:
      - {flag: "exec --json", verified: "works"}
    notes:
      - "codex 0.139.0 exec PreToolUse hook never fires; sandbox-primary."
"""


def _healthy_repo(tmp_path: Path) -> Path:
    init_repo(tmp_path)
    (tmp_path / ".gauntlet/pins.yaml").write_text(_PINS)
    return tmp_path


def _probes(
    versions: dict[str, str | None],
    env: dict[str, str],
    *,
    authed: dict[str, bool | None] | None = None,
    which: object | None = None,
    judge_model_resolvable: object | None = None,
) -> DoctorProbes:
    # Default: every present CLI is authenticated and the hook binary is on PATH,
    # so a "healthy" environment passes without a real subprocess/PATH probe.
    # Default judge model resolver says "resolvable" so the classifier check
    # never reaches into LiteLLM during offline tests.
    auth_map = authed if authed is not None else {c: True for c in versions}
    return DoctorProbes(
        cli_version=lambda name: versions.get(name),
        env=env,
        cli_authenticated=lambda name: auth_map.get(name),
        which=which if which is not None else (lambda name: f"/usr/bin/{name}"),
        judge_model_resolvable=(
            judge_model_resolvable
            if judge_model_resolvable is not None
            else (lambda _model: None)
        ),
    )


def _set_judge_llm(repo: Path, model: str | None, *, adapter: str = "api") -> None:
    """Set (or, with model=None, remove) the scaffold's `judge_llm` profile."""
    cfg_path = repo / ".gauntlet/config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    agents = cfg.setdefault("agents", {})
    if model is None:
        agents.pop("judge_llm", None)
    else:
        agents["judge_llm"] = {"adapter": adapter, "model": model}
    cfg_path.write_text(yaml.safe_dump(cfg))


_GOOD_VERSIONS = {"claude": "2.1.172", "codex": "codex-cli 0.139.0"}
_GOOD_ENV = {"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y"}


def _by_name(results) -> dict:
    return {r.name: r for r in results}


def test_healthy_environment_passes(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    assert not has_failure(results)
    names = _by_name(results)
    assert names["claude"].status == OK
    assert names["codex"].status == OK
    assert names["claude-hook"].status == OK
    assert names["judge"].status == OK
    assert names["api-keys"].status == OK
    # CLIs probe as authenticated (FR-1.3)
    assert names["claude-auth"].status == OK
    assert names["codex-auth"].status == OK
    # codex hook present-but-inert is healthy, not a failure
    assert names["codex-hook"].status == OK
    assert "inert" in names["codex-hook"].detail


def test_judge_classifier_ok_when_model_resolvable(tmp_path):
    repo = _healthy_repo(tmp_path)
    _set_judge_llm(repo, "gpt-5-mini")
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    jc = _by_name(results)["judge-classifier"]
    assert jc.status == OK
    assert "gpt-5-mini" in jc.detail
    assert not has_failure(results)


def test_judge_classifier_fails_when_adapter_not_api(tmp_path):
    # The engine always runs the classifier as an `api` (LiteLLM) call; a non-api
    # judge_llm would pass api-keys (no key required) yet fail closed at runtime.
    # doctor must FAIL, not silently OK it (PR #13 review).
    repo = _healthy_repo(tmp_path)
    _set_judge_llm(repo, "opus", adapter="claude-code")
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    jc = _by_name(results)["judge-classifier"]
    assert jc.status == FAIL
    assert "claude-code" in jc.detail
    assert jc.remedy and "adapter: api" in jc.remedy
    assert has_failure(results)


def test_judge_classifier_warns_when_no_profile(tmp_path):
    # Without a judge_llm profile, the engine-managed judge runs with the
    # classifier disabled (fail-closed on everything off the fast-path).
    repo = _healthy_repo(tmp_path)
    _set_judge_llm(repo, None)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    jc = _by_name(results)["judge-classifier"]
    assert jc.status == WARN
    assert "fail closed" in jc.detail
    assert jc.remedy and "judge_llm" in jc.remedy
    assert not has_failure(results)  # a missing classifier WARNs, never blocks


def test_judge_classifier_warns_on_unresolvable_model(tmp_path):
    # An invalid LiteLLM id (e.g. `claude-heroku`) makes the classifier fail
    # every call closed — doctor catches it before a run, not via deny errors.
    repo = _healthy_repo(tmp_path)
    _set_judge_llm(repo, "claude-heroku")
    results = run_doctor(
        repo,
        probes=_probes(
            _GOOD_VERSIONS, _GOOD_ENV,
            judge_model_resolvable=lambda m: (
                "LLM Provider NOT provided" if m == "claude-heroku" else None
            ),
        ),
    )
    jc = _by_name(results)["judge-classifier"]
    assert jc.status == WARN
    assert "claude-heroku" in jc.detail
    assert "not resolvable" in jc.detail
    assert jc.remedy and "valid LiteLLM model id" in jc.remedy
    assert not has_failure(results)


def test_test_command_placeholder_warns(tmp_path):
    # issue #18: an un-configured (placeholder) test_command WARNs before a run,
    # rather than failing every phase's test gate mid-pipeline.
    repo = _healthy_repo(tmp_path)  # init on an empty repo -> placeholder command
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    tc = _by_name(results)["test-command"]
    assert tc.status == WARN
    assert tc.remedy and "test_command" in tc.remedy
    assert not has_failure(results)  # un-configured test command WARNs, never blocks


def test_test_command_ok_when_detected(tmp_path):
    # A repo with a detectable stack gets a real command and doctor reports OK.
    (tmp_path / "pyproject.toml").write_text("[tool.uv]\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_x.py").write_text("def test_x():\n    assert True\n")
    repo = _healthy_repo(tmp_path)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    tc = _by_name(results)["test-command"]
    assert tc.status == OK
    assert "uv run pytest" in tc.detail


def test_missing_claude_cli_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(
        repo, probes=_probes({"claude": None, "codex": "codex-cli 0.139.0"}, _GOOD_ENV)
    )
    claude = _by_name(results)["claude"]
    assert claude.status == FAIL
    assert claude.remedy and "install" in claude.remedy
    assert has_failure(results)


def test_version_mismatch_warns_not_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(
        repo, probes=_probes({"claude": "2.0.1", "codex": "codex-cli 0.139.0"}, _GOOD_ENV)
    )
    claude = _by_name(results)["claude"]
    assert claude.status == WARN
    assert "pin-verified" in claude.detail
    # a version skew alone does not block the run (FR-1.5 "warns")
    assert not has_failure(results)


def test_missing_claude_hook_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / ".claude/settings.json").unlink()
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    hook = _by_name(results)["claude-hook"]
    assert hook.status == FAIL
    assert "gauntlet init" in (hook.remedy or "")


def test_unwired_claude_hook_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / ".claude/settings.json").write_text('{"hooks": {}}')
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    assert _by_name(results)["claude-hook"].status == FAIL


def test_no_api_key_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, {}))
    keys = _by_name(results)["api-keys"]
    assert keys.status == FAIL
    assert "FR-1.4" in (keys.remedy or "")
    assert has_failure(results)


def _set_model(repo: Path, profile: str, model: str) -> None:
    p = repo / ".gauntlet/config.yaml"
    data = yaml.safe_load(p.read_text())
    data["agents"][profile]["model"] = model
    p.write_text(yaml.safe_dump(data))


def test_referenced_profile_missing_key_fails(tmp_path):
    # A profile the default pipeline references (escalation_agent) needs a key
    # the env lacks: doctor must FAIL, not WARN — the pipeline cannot run (F-006).
    repo = _healthy_repo(tmp_path)
    _set_model(repo, "escalation", "anthropic/claude-x")
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, {"OPENAI_API_KEY": "x"}))
    keys = _by_name(results)["api-keys"]
    assert keys.status == FAIL
    assert "ANTHROPIC_API_KEY" in keys.detail
    assert "escalation" in keys.detail
    assert has_failure(results)


def test_unused_profile_missing_key_warns(tmp_path):
    # An api profile no pipeline step references is not run-blocking: a missing
    # key for it is a WARN, while the referenced profiles stay satisfied (F-006).
    repo = _healthy_repo(tmp_path)
    p = repo / ".gauntlet/config.yaml"
    data = yaml.safe_load(p.read_text())
    data["agents"]["spare"] = {"adapter": "api", "model": "anthropic/claude-x"}
    p.write_text(yaml.safe_dump(data))
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, {"OPENAI_API_KEY": "x"}))
    keys = _by_name(results)["api-keys"]
    assert keys.status == WARN
    assert "spare" in keys.detail
    assert not has_failure(results)


# ---- CLI authentication (FR-1.3, review F-004) ------------------------------

def test_logged_out_cli_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(
        repo,
        probes=_probes(_GOOD_VERSIONS, _GOOD_ENV, authed={"claude": False, "codex": True}),
    )
    auth = _by_name(results)["claude-auth"]
    assert auth.status == FAIL
    assert auth.remedy and "log in" in auth.remedy.lower()
    assert has_failure(results)


def test_unverifiable_auth_warns(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(
        repo,
        probes=_probes(_GOOD_VERSIONS, _GOOD_ENV, authed={"claude": None, "codex": True}),
    )
    assert _by_name(results)["claude-auth"].status == WARN
    assert not has_failure(results)


def test_absent_cli_has_no_auth_row(tmp_path):
    # The version check owns the "not found" FAIL; auth does not double-report.
    repo = _healthy_repo(tmp_path)
    results = run_doctor(
        repo, probes=_probes({"claude": None, "codex": _GOOD_VERSIONS["codex"]}, _GOOD_ENV)
    )
    assert "claude-auth" not in _by_name(results)
    assert "codex-auth" in _by_name(results)


# ---- structural hook validation (FR-7.3, review F-005) ----------------------

def test_malformed_claude_settings_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / ".claude/settings.json").write_text("{not valid json")
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    assert _by_name(results)["claude-hook"].status == FAIL
    assert has_failure(results)


def test_narrow_matcher_claude_hook_fails(tmp_path):
    # The judge must see every tool call; a Bash-only matcher leaves tools ungated.
    repo = _healthy_repo(tmp_path)
    (repo / ".claude/settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [
            {"matcher": "Bash",
             "hooks": [{"type": "command", "command": "gauntlet-judge-hook", "timeout": 15}]},
        ]}
    }))
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    hook = _by_name(results)["claude-hook"]
    assert hook.status == FAIL
    assert "*" in (hook.detail + (hook.remedy or ""))


def test_hook_binary_not_on_path_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(
        repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV, which=lambda name: None)
    )
    hook = _by_name(results)["claude-hook"]
    assert hook.status == FAIL
    assert "PATH" in hook.detail


def test_codex_hook_present_but_unwired_warns(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / ".codex/hooks.json").write_text('{"hooks": {}}')
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    codex = _by_name(results)["codex-hook"]
    assert codex.status == WARN
    assert not has_failure(results)


def test_secret_literal_in_repo_config_fails(tmp_path):
    repo = _healthy_repo(tmp_path)
    cfg = repo / ".gauntlet/config.yaml"
    cfg.write_text(cfg.read_text() + '\n    api_key: "sk-abcd1234efgh5678ijkl"\n')
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    secrets = _by_name(results)["repo-secrets"]
    assert secrets.status == FAIL
    assert ".gauntlet/config.yaml" in secrets.detail
    assert has_failure(results)


def test_missing_policy_fails_judge_check(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / ".gauntlet/policy.yaml").unlink()  # init scaffolds policy under .gauntlet/
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    judge = _by_name(results)["judge"]
    assert judge.status == FAIL
    assert has_failure(results)


def test_missing_config_fails_cleanly(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / ".gauntlet/config.yaml").unlink()
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    assert _by_name(results)["config"].status == FAIL
    assert has_failure(results)


def test_missing_pin_file_warns(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / ".gauntlet/pins.yaml").unlink()
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    assert _by_name(results)["pin-file"].status == WARN
    # a missing pin file is a soft check; it does not block on its own
    # (the CLIs are still found by the version probe)
    assert not has_failure(results)


def test_version_check_surfaces_gauntlet_version(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    ver = _by_name(results)["gauntlet"]
    assert ver.status == OK
    assert "version" in ver.detail

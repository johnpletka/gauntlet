"""`gauntlet doctor` — environment validation against simulated environments.

Each broken environment must produce a FAIL (or WARN) with an actionable
remedy, and a healthy one must pass clean (plan P6 test strategy). The agent-CLI
probes are injected so these run offline.
"""

from __future__ import annotations

from pathlib import Path

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


def _probes(versions: dict[str, str | None], env: dict[str, str]) -> DoctorProbes:
    return DoctorProbes(cli_version=lambda name: versions.get(name), env=env)


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
    # codex hook present-but-inert is healthy, not a failure
    assert names["codex-hook"].status == OK
    assert "inert" in names["codex-hook"].detail


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


def test_partial_api_key_warns(tmp_path):
    repo = _healthy_repo(tmp_path)
    # OpenAI present (judge_llm) but Anthropic absent (triage/escalation)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, {"OPENAI_API_KEY": "x"}))
    keys = _by_name(results)["api-keys"]
    assert keys.status == WARN
    assert "ANTHROPIC_API_KEY" in keys.detail
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
    (repo / "policy.yaml").unlink()
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

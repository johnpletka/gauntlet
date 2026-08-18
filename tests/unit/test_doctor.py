"""`gauntlet doctor` — environment validation against simulated environments.

Each broken environment must produce a FAIL (or WARN) with an actionable
remedy, and a healthy one must pass clean (plan P6 test strategy). The agent-CLI
probes are injected so these run offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from gauntlet.engine import skill as S
from gauntlet.engine.doctor import (
    FAIL,
    OK,
    WARN,
    DoctorProbes,
    ProbeResult,
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
    tracker_auth_probe: object | None = None,
    profile_model_probe: object | None = None,
    profile_read_probe: object | None = None,
) -> DoctorProbes:
    # Default: every present CLI is authenticated and the hook binary is on PATH,
    # so a "healthy" environment passes without a real subprocess/PATH probe.
    # Default judge model resolver says "resolvable" so the classifier check
    # never reaches into LiteLLM during offline tests.
    auth_map = authed if authed is not None else {c: True for c in versions}
    # Default the FR-6.4 per-profile probes to fakes that pass — the REAL probes
    # do live CLI round trips / LiteLLM lookups, which must never fire in the
    # offline unit suite. Tests that exercise FR-6.4 inject their own.
    from gauntlet.engine.doctor import OK, ProbeResult
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
        tracker_auth_probe=(
            tracker_auth_probe
            if tracker_auth_probe is not None
            else (lambda _cfg, _env: None)
        ),
        profile_model_probe=(
            profile_model_probe
            if profile_model_probe is not None
            else (lambda name, profile: ProbeResult(OK, f"{name} model ok"))
        ),
        profile_read_probe=(
            profile_read_probe
            if profile_read_probe is not None
            else (lambda name, profile, root: ProbeResult(OK, f"{name} read ok"))
        ),
    )


def _set_judge_llm(
    repo: Path, model: str | None, *, adapter: str = "api", effort: str | None = None
) -> None:
    """Set (or, with model=None, remove) the scaffold's `judge_llm` profile."""
    cfg_path = repo / ".gauntlet/config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    agents = cfg.setdefault("agents", {})
    if model is None:
        agents.pop("judge_llm", None)
    else:
        agents["judge_llm"] = {"adapter": adapter, "model": model}
        if effort is not None:
            agents["judge_llm"]["effort"] = effort
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


def test_judge_classifier_live_probe_failure_is_a_doctor_failure(tmp_path):
    # Issue #83: provider resolution alone is insufficient. The specialized
    # classifier row must surface a runtime reasoning_effort rejection and must
    # not also pay for a duplicate generic profile probe.
    repo = _healthy_repo(tmp_path)
    _set_judge_llm(repo, "gpt-5.6-luna", effort="minimal")
    seen: list[str] = []

    def probe(name, profile):
        seen.append(name)
        if name == "judge_llm":
            return ProbeResult(
                FAIL,
                "judge classifier cannot evaluate: reasoning_effort=minimal unsupported",
                remedy="set judge_llm.effort: low",
            )
        return ProbeResult(OK, f"{name} ok")

    results = run_doctor(
        repo,
        probes=_probes(
            _GOOD_VERSIONS, _GOOD_ENV, profile_model_probe=probe
        ),
    )
    names = _by_name(results)
    jc = names["judge-classifier"]
    assert jc.status == FAIL
    assert "cannot evaluate" in jc.detail
    assert jc.remedy and "effort" in jc.remedy
    assert seen.count("judge_llm") == 1
    assert "profile:judge_llm" not in names
    assert has_failure(results)


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


def test_test_command_empty_warns(tmp_path):
    # An empty test_command runs nothing under shell=True yet exits 0 — a
    # fail-open the test gate would silently pass. doctor must WARN, not OK it.
    repo = _healthy_repo(tmp_path)
    cfg_path = repo / ".gauntlet/config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["test_command"] = "   "
    cfg_path.write_text(yaml.safe_dump(cfg))
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    tc = _by_name(results)["test-command"]
    assert tc.status == WARN
    assert "empty" in tc.detail
    assert not has_failure(results)


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


def test_codex_version_mismatch_warns_not_fails(tmp_path):
    # Issue #119 observed PATH codex 0.144.4 against the 0.139.0 behavior pin.
    # Keep an explicit codex-prefix regression rather than relying on the Claude
    # mismatch test to prove `codex-cli X.Y.Z` is normalized and surfaced.
    repo = _healthy_repo(tmp_path)
    versions = {"claude": "2.1.172", "codex": "codex-cli 0.144.4"}
    results = run_doctor(repo, probes=_probes(versions, _GOOD_ENV))
    codex = _by_name(results)["codex"]
    assert codex.status == WARN
    assert "0.144.4" in codex.detail and "0.139.0" in codex.detail
    assert not has_failure(results)


def _write_codex_cache(root: Path, payload: object, *, codex_home=False) -> dict[str, str]:
    cache_dir = root if codex_home else root / ".codex"
    cache_dir.mkdir(parents=True)
    (cache_dir / "models_cache.json").write_text(json.dumps(payload))
    key = "CODEX_HOME" if codex_home else "HOME"
    return {**_GOOD_ENV, key: str(root)}


def test_codex_cache_matching_writer_and_schema_is_ok(tmp_path):
    repo = _healthy_repo(tmp_path / "repo")
    env = _write_codex_cache(
        tmp_path / "home",
        {
            "client_version": "0.139.0",
            "models": [{"base_instructions": "not surfaced by doctor"}],
        },
    )
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, env))
    cache = _by_name(results)["codex-cache"]
    assert cache.status == OK
    assert "base_instructions=1" in cache.detail
    assert "not surfaced by doctor" not in cache.detail


def test_codex_cache_newer_writer_and_issue_119_schema_warn(tmp_path):
    repo = _healthy_repo(tmp_path / "repo")
    env = _write_codex_cache(
        tmp_path / "codex-home",
        {
            "client_version": "0.147.0",
            "models": [{"model_messages": {"base": "private cache content"}}],
        },
        codex_home=True,
    )
    versions = {"claude": "2.1.172", "codex": "codex-cli 0.144.4"}
    results = run_doctor(repo, probes=_probes(versions, env))
    cache = _by_name(results)["codex-cache"]
    assert cache.status == WARN
    assert "cache writer 0.147.0" in cache.detail
    assert "PATH codex 0.144.4" in cache.detail
    assert "model_messages schema omits base_instructions" in cache.detail
    assert "private cache content" not in cache.detail
    assert cache.remedy and "CODEX_HOME" in cache.remedy


def test_codex_cache_partial_json_warns_actionably(tmp_path):
    repo = _healthy_repo(tmp_path / "repo")
    home = tmp_path / "home"
    cache_dir = home / ".codex"
    cache_dir.mkdir(parents=True)
    (cache_dir / "models_cache.json").write_text('{"client_version":')
    env = {**_GOOD_ENV, "HOME": str(home)}
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, env))
    cache = _by_name(results)["codex-cache"]
    assert cache.status == WARN
    assert "mid-rewrite" in cache.detail
    assert cache.remedy and "retry doctor" in cache.remedy


def test_codex_cache_invalid_utf8_warns_actionably(tmp_path):
    repo = _healthy_repo(tmp_path / "repo")
    home = tmp_path / "home"
    cache_dir = home / ".codex"
    cache_dir.mkdir(parents=True)
    (cache_dir / "models_cache.json").write_bytes(
        b'{"client_version":"0.139.0","models":["\xe2\x82'
    )
    env = {**_GOOD_ENV, "HOME": str(home)}
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, env))
    cache = _by_name(results)["codex-cache"]
    assert cache.status == WARN
    assert "UnicodeDecodeError" in cache.detail
    assert "mid-rewrite" in cache.detail
    assert cache.remedy and "retry doctor" in cache.remedy


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
    results = run_doctor(
        repo, probes=_probes(_GOOD_VERSIONS, {"OPENAI_API_KEY": "x"})
    )
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


# ---- P3: doctor warn-only PRD-authoring skill check (FR-1.5, OQ-3) ----------

def test_skill_check_ok_when_well_formed(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    assert _by_name(results)["prd-skill"].status == OK


def test_skill_check_warns_and_never_fails_when_missing(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / S.SKILL_REL).unlink()
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["prd-skill"]
    assert skill.status == WARN  # the skill gates nothing — never a blocker
    assert skill.status != FAIL


def test_skill_check_warns_on_malformed_frontmatter(tmp_path):
    repo = _healthy_repo(tmp_path)
    (repo / S.SKILL_REL).write_text("no frontmatter at all\n")
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["prd-skill"]
    assert skill.status == WARN
    assert skill.status != FAIL


def test_skill_check_warns_on_stale_provenance(tmp_path):
    # A *customized* skill (edited body) that carries provenance and whose playbook
    # ref drifted after an asset_root change → classify=customization + looks_stale
    # → WARN (§4.5). An unmodified generated file is recognized as generated and is
    # refreshable instead of stale (F-001); see the test below.
    repo = _healthy_repo(tmp_path)
    skill_file = repo / S.SKILL_REL
    skill_file.write_text(skill_file.read_text() + "\n<!-- maintainer note -->\n")
    cfg = repo / ".gauntlet/config.yaml"
    cfg.write_text(cfg.read_text().replace("asset_root: .gauntlet", 'asset_root: "."'))
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["prd-skill"]
    assert skill.status == WARN
    assert "stale" in skill.detail.lower()


def test_skill_check_ok_for_unmodified_generated_after_asset_root_change(tmp_path):
    # F-001: an unmodified generated skill whose asset_root later changed is still
    # a generated file (refreshable by `gauntlet init`), so doctor does not flag it
    # as a stale customization — it is not misclassified as a customization.
    repo = _healthy_repo(tmp_path)
    cfg = repo / ".gauntlet/config.yaml"
    cfg.write_text(cfg.read_text().replace("asset_root: .gauntlet", 'asset_root: "."'))
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["prd-skill"]
    assert skill.status != FAIL
    assert "stale" not in skill.detail.lower()


def test_skill_check_warns_and_never_fails_on_non_utf8(tmp_path):
    # F-002: a non-UTF-8 SKILL.md must not crash the warn-only check (FR-1.5).
    repo = _healthy_repo(tmp_path)
    (repo / S.SKILL_REL).write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["prd-skill"]
    assert skill.status == WARN
    assert skill.status != FAIL


def test_skill_check_warns_and_never_fails_on_unreadable(tmp_path):
    # F-002: an unreadable SKILL.md (PermissionError) must WARN, not hard-fail.
    import os

    if os.geteuid() == 0:
        import pytest

        pytest.skip("root can read any file regardless of mode")
    repo = _healthy_repo(tmp_path)
    skill_file = repo / S.SKILL_REL
    skill_file.chmod(0o000)
    try:
        results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    finally:
        skill_file.chmod(0o644)  # restore so tmp cleanup can proceed
    skill = _by_name(results)["prd-skill"]
    assert skill.status == WARN
    assert skill.status != FAIL


def test_skill_check_warns_and_does_not_dereference_symlinked_leaf(tmp_path):
    # F-003: a symlinked skill path must not be dereferenced — doctor WARNs without
    # reading the (possibly external) target, so its contents never reach output.
    repo = _healthy_repo(tmp_path)
    skill_file = repo / S.SKILL_REL
    outside = tmp_path / "outside.md"
    outside.write_text("secret outside the repo\n")
    skill_file.unlink()
    skill_file.symlink_to(outside)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["prd-skill"]
    assert skill.status == WARN
    assert skill.status != FAIL
    assert "symlink" in skill.detail.lower()
    assert "secret" not in skill.detail


def test_skill_check_warns_on_name_mismatch_operator(tmp_path):
    # F-003: an otherwise-valid SKILL.md whose frontmatter `name` does not match
    # the spec installed at this path is a broken discovery surface. The schema
    # only pins `name` to *some* kebab id, so this must be caught explicitly:
    # WARN (never FAIL — the skill gates nothing), and never silently OK.
    repo = _healthy_repo(tmp_path)
    skill_file = repo / S.OPERATOR_SPEC.skill_rel
    skill_file.write_text(
        skill_file.read_text().replace(
            "name: gauntlet-operator", "name: some-other-skill"
        )
    )
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["operator-skill"]
    assert skill.status == WARN
    assert skill.status != FAIL
    assert "some-other-skill" in skill.detail
    assert "gauntlet-operator" in skill.detail


def test_skill_check_warns_on_name_mismatch_prd_author(tmp_path):
    # F-003: the same name-consistency check guards the prd-author skill too.
    repo = _healthy_repo(tmp_path)
    skill_file = repo / S.SKILL_REL
    skill_file.write_text(
        skill_file.read_text().replace(
            "name: gauntlet-prd-author", "name: some-other-skill"
        )
    )
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["prd-skill"]
    assert skill.status == WARN
    assert skill.status != FAIL
    assert "some-other-skill" in skill.detail


def test_skill_check_warns_and_does_not_dereference_symlinked_parent(tmp_path):
    # F-003: a symlinked *parent* directory must also abort dereferencing — the
    # leaf is a regular file but reading it would follow the parent link out.
    repo = _healthy_repo(tmp_path)
    skills_dir = repo / ".claude/skills"
    outside_dir = tmp_path / "outside_skills"
    # Move the real skill tree outside and point .claude/skills at it via symlink.
    import shutil

    shutil.move(str(skills_dir), str(outside_dir))
    skills_dir.symlink_to(outside_dir)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    skill = _by_name(results)["prd-skill"]
    assert skill.status == WARN
    assert skill.status != FAIL
    assert "symlink" in skill.detail.lower()


# ---- issue-tracker check (FR-10.1) ------------------------------------------


def _set_issue_tracker(repo: Path, block: dict | None) -> None:
    """Set (or, with block=None, remove) the `issue_tracker` config block."""
    cfg_path = repo / ".gauntlet/config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    if block is None:
        cfg.pop("issue_tracker", None)
    else:
        cfg["issue_tracker"] = block
    cfg_path.write_text(yaml.safe_dump(cfg))


def test_no_tracker_block_emits_no_check(tmp_path):
    repo = _healthy_repo(tmp_path)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    assert "issue-tracker" not in _by_name(results)


def test_tracker_ok_when_env_set_and_probe_succeeds(tmp_path):
    repo = _healthy_repo(tmp_path)
    _set_issue_tracker(repo, {"provider": "linear", "api_key_env": "LINEAR_API_KEY"})
    env = {**_GOOD_ENV, "LINEAR_API_KEY": "lin_secret"}
    results = run_doctor(
        repo,
        probes=_probes(_GOOD_VERSIONS, env, tracker_auth_probe=lambda _c, _e: None),
    )
    tr = _by_name(results)["issue-tracker"]
    assert tr.status == OK
    assert not has_failure(results)


def test_tracker_fails_when_env_var_unset(tmp_path):
    repo = _healthy_repo(tmp_path)
    _set_issue_tracker(repo, {"provider": "linear", "api_key_env": "LINEAR_API_KEY"})
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    tr = _by_name(results)["issue-tracker"]
    assert tr.status == FAIL
    assert "LINEAR_API_KEY" in tr.detail
    assert has_failure(results)


def test_tracker_fails_when_probe_reports_auth_error(tmp_path):
    repo = _healthy_repo(tmp_path)
    _set_issue_tracker(repo, {"provider": "linear", "api_key_env": "LINEAR_API_KEY"})
    env = {**_GOOD_ENV, "LINEAR_API_KEY": "bad"}
    results = run_doctor(
        repo,
        probes=_probes(
            _GOOD_VERSIONS, env,
            tracker_auth_probe=lambda _c, _e: "HTTP 401",
        ),
    )
    tr = _by_name(results)["issue-tracker"]
    assert tr.status == FAIL
    assert "401" in tr.detail
    assert has_failure(results)


def test_tracker_none_provider_emits_no_check(tmp_path):
    repo = _healthy_repo(tmp_path)
    _set_issue_tracker(repo, {"provider": "none"})
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    assert "issue-tracker" not in _by_name(results)


# --- FR-6.4: per-profile model/effort + repo-read probes ---------------------
def test_healthy_environment_probes_every_profile_ok(tmp_path):
    # Every configured profile gets a model probe; the reference-mode profile
    # (builder, used by the implement step) also gets a repo-read probe.
    repo = _healthy_repo(tmp_path)
    results = run_doctor(repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV))
    names = _by_name(results)
    for profile in ("builder", "reviewer", "triage", "escalation", "mechanic"):
        assert names[f"profile:{profile}"].status == OK, profile
    # the builder is bound to reference/phase inputs -> it is read-probed too
    assert names["profile:builder-read"].status == OK
    assert not has_failure(results)


def test_bad_model_alias_in_one_profile_fails_only_that_profile(tmp_path):
    # FR-6.4 acceptance: a deliberately bad alias in one profile FAILs that
    # profile's model probe; the others PASS.
    repo = _healthy_repo(tmp_path)

    def probe(name, profile):
        if name == "reviewer":
            return ProbeResult(FAIL, "model 'gpt-bogus' rejected by codex: 404")
        return ProbeResult(OK, f"{name} ok")

    results = run_doctor(
        repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV, profile_model_probe=probe)
    )
    names = _by_name(results)
    assert names["profile:reviewer"].status == FAIL
    assert names["profile:builder"].status == OK
    assert names["profile:triage"].status == OK
    assert has_failure(results)


# --- pipeline-effectiveness P1-A5: the Gemini api panel member is doctor-covered
def test_gemini_panel_profile_bad_model_id_fails_probe():
    # A misspelled/unavailable Gemini panel model id FAILs the per-profile model
    # probe via the offline LiteLLM-resolvability branch (no network) — so a bad
    # panel model id is caught before the first ensemble review, not at runtime.
    from gauntlet.engine.config import AgentProfile
    from gauntlet.engine.doctor import _real_profile_model_probe

    profile = AgentProfile(adapter="api", model="gemini-totally-bogus-xyzzy")
    result = _real_profile_model_probe("gemini", profile)
    assert result.status == FAIL
    assert "not resolvable" in result.detail


def test_gemini_panel_profile_valid_model_id_probes_ok(monkeypatch):
    # A resolvable Gemini id passes the probe (offline resolvability + a stubbed
    # bounded round trip standing in for the live call).
    from types import SimpleNamespace

    from gauntlet.adapters.api import ApiAdapter
    from gauntlet.engine.config import AgentProfile
    from gauntlet.engine.doctor import _real_profile_model_probe

    monkeypatch.setattr("gauntlet.adapters.api.model_provider_error", lambda _m: None)
    monkeypatch.setattr(
        ApiAdapter, "_complete",
        lambda self, messages: SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))],
            usage=None,
        ),
    )
    profile = AgentProfile(adapter="api", model="gemini/gemini-2.5-pro")
    assert _real_profile_model_probe("gemini", profile).status == OK


def test_shipped_gemini_profile_is_an_inactive_opt_in_example():
    # Fresh installs do not require Gemini, but retain an adjacent example that
    # users can uncomment before selecting it in the standard panel.
    from gauntlet.engine.config import RunConfig

    repo = Path(__file__).resolve().parents[2]
    path = repo / "src" / "gauntlet" / "scaffold" / "config.yaml"
    cfg = RunConfig.model_validate(yaml.safe_load(path.read_text()))
    assert "gemini" not in cfg.agents
    text = path.read_text()
    assert "# gemini:" in text
    assert "#   model: gemini/gemini-2.5-pro" in text


def test_reference_profile_blind_sandbox_fails_read_probe(tmp_path):
    # FR-1.3/FR-6.4: a reference-capable profile whose sandbox cannot read a repo
    # file is a read-probe FAIL (its model probe can still pass).
    repo = _healthy_repo(tmp_path)

    def read_probe(name, profile, root):
        return ProbeResult(FAIL, f"{name} sandbox could not read a repo file")

    results = run_doctor(
        repo, probes=_probes(_GOOD_VERSIONS, _GOOD_ENV, profile_read_probe=read_probe)
    )
    names = _by_name(results)
    assert names["profile:builder-read"].status == FAIL
    assert names["profile:builder"].status == OK  # model probe unaffected
    assert has_failure(results)


def test_unauthenticated_cli_skips_profile_probes_with_warn(tmp_path):
    # FR-6.4: an unauthenticated CLI skips its profiles' live probes with a WARN —
    # never a silent PASS (and never a live subprocess call).
    def boom(name, profile):  # the unauthenticated claude CLI must not be probed
        if profile.adapter == "claude-code":
            raise AssertionError("model probe must not run for an unauthenticated CLI")
        return ProbeResult(OK, f"{name} ok")  # authed codex + api profiles are fine

    def boom_read(name, profile, root):
        raise AssertionError("read probe must not run for an unauthenticated CLI")

    repo = _healthy_repo(tmp_path)
    results = run_doctor(
        repo,
        probes=_probes(
            _GOOD_VERSIONS, _GOOD_ENV,
            authed={"claude": False, "codex": True},
            profile_model_probe=boom,
            profile_read_probe=boom_read,
        ),
    )
    names = _by_name(results)
    # builder (claude) is unauthenticated -> WARN skip, not PASS, not FAIL
    assert names["profile:builder"].status == WARN
    assert "not authenticated" in names["profile:builder"].detail
    assert names["profile:builder-read"].status == WARN


# --- review F-002: the REAL api model probe does a live effort round trip ------
def test_api_model_probe_sends_mapped_effort_and_passes(monkeypatch, tmp_path):
    # FR-6.4 / review F-002: the real api probe must do a LIVE completion carrying
    # the profile's mapped reasoning_effort — not stop at the offline resolvability
    # lookup — so a model that resolves but would reject the effort is caught.
    from types import SimpleNamespace

    from gauntlet.adapters.api import ApiAdapter
    from gauntlet.engine.config import AgentProfile
    from gauntlet.engine.doctor import OK, _real_profile_model_probe

    # the model resolves offline; the live call is the thing under test
    monkeypatch.setattr("gauntlet.adapters.api.model_provider_error", lambda _m: None)
    captured: dict = {}

    def fake_complete(self, messages):
        captured["reasoning_effort"] = self.reasoning_effort
        captured["model"] = self.model
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="pong"))],
            usage=None,
        )

    monkeypatch.setattr(ApiAdapter, "_complete", fake_complete)
    profile = AgentProfile(adapter="api", model="gpt-5-mini", effort="low")
    result = _real_profile_model_probe("triage", profile)
    assert result.status == OK
    assert captured["model"] == "gpt-5-mini"
    assert captured["reasoning_effort"] == "low"  # the mapped effort was sent live


def test_api_model_probe_fails_when_effort_rejected(monkeypatch, tmp_path):
    # A resolvable model whose provider REJECTS the configured reasoning_effort at
    # call time is a FAIL row (not a silent OK on the offline lookup).
    from gauntlet.adapters.api import ApiAdapter
    from gauntlet.engine.config import AgentProfile
    from gauntlet.engine.doctor import FAIL, _real_profile_model_probe

    monkeypatch.setattr("gauntlet.adapters.api.model_provider_error", lambda _m: None)

    def boom(self, messages):
        raise ValueError("reasoning_effort is not supported by this model")

    monkeypatch.setattr(ApiAdapter, "_complete", boom)
    profile = AgentProfile(adapter="api", model="gpt-5-mini", effort="high")
    result = _real_profile_model_probe("triage", profile)
    assert result.status == FAIL
    assert "rejected by the provider" in result.detail


def test_real_judge_probe_uses_classifier_schema_timeout_and_profile_effort(monkeypatch):
    # The doctor probe is the runtime classifier construction, not a generic
    # tool-less ping. A valid structured verdict proves the schema path worked.
    from types import SimpleNamespace

    from gauntlet.adapters.api import ApiAdapter
    from gauntlet.engine.config import AgentProfile
    from gauntlet.engine.doctor import OK, _real_profile_model_probe
    from gauntlet.judge.runner import JUDGE_LLM_TIMEOUT_S

    monkeypatch.setattr("gauntlet.adapters.api.model_provider_error", lambda _m: None)
    captured: dict = {}

    def fake_complete(self, messages):
        captured.update(
            model=self.model,
            effort=self.reasoning_effort,
            timeout=self.timeout_s,
            retries=self.max_schema_retries,
            prompt=messages[0]["content"],
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=(
                '{"decision":"allow","risk_category":"none",'
                '"rationale":"harmless read"}'
            )))],
            usage=None,
        )

    monkeypatch.setattr(ApiAdapter, "_complete", fake_complete)
    profile = AgentProfile(adapter="api", model="gpt-5.6-luna", effort="low")
    result = _real_profile_model_probe("judge_llm", profile)
    assert result.status == OK
    assert captured["model"] == "gpt-5.6-luna"
    assert captured["effort"] == "low"
    assert captured["timeout"] == JUDGE_LLM_TIMEOUT_S
    assert captured["retries"] == 0
    assert "risk_category" in captured["prompt"]  # classifier schema appended


# --- review F-003: the REAL read probe uses the profile's OWN config -----------
def test_read_probe_built_from_profile_configuration(monkeypatch, tmp_path):
    # FR-1.3/FR-6.4 / review F-003: the read probe must drive the profile's real
    # adapter (its allowed_tools/base_flags/model), not a fixed `--allowedTools
    # Read` command — otherwise a profile whose config cannot read passes.
    import re

    from gauntlet.adapters.base import AgentResult
    from gauntlet.adapters.claude_code import ClaudeCodeAdapter
    from gauntlet.engine.config import AgentProfile
    from gauntlet.engine.doctor import OK, _real_profile_read_probe

    captured: dict = {}

    def fake_run(self, prompt, *, session=None, schema=None, cwd=None,
                 extra_flags=None, sink=None):
        # prove the adapter was built from the PROFILE, not a hardcoded command
        captured["model"] = self.model
        captured["allowed_tools"] = self.allowed_tools
        captured["base_flags"] = self.base_flags
        # simulate a real, successful read of the sentinel the probe staged
        m = re.search(r"Read the file (\S+) ", prompt)
        text = ""
        if m and cwd is not None:
            p = Path(cwd) / m.group(1)
            if p.exists():
                text = p.read_text()
        return AgentResult(text=text, session_id="s", exit_code=0)

    monkeypatch.setattr(ClaudeCodeAdapter, "run", fake_run)
    profile = AgentProfile(
        adapter="claude-code", model="opus",
        allowed_tools=["Read"], base_flags=["--append-system-prompt", "PROBE-CFG"],
    )
    result = _real_profile_read_probe("builder", profile, tmp_path)
    assert result.status == OK
    # the probe reflected the profile's OWN configuration
    assert captured["model"] == "opus"
    assert captured["allowed_tools"] == ["Read"]
    assert captured["base_flags"] == ["--append-system-prompt", "PROBE-CFG"]
    # the transient sentinel was cleaned up
    assert not list(tmp_path.glob(".gauntlet-doctor-read-*.txt"))


def test_read_probe_fails_when_profile_config_cannot_read(monkeypatch, tmp_path):
    # A profile whose real invocation returns no marker (its sandbox/tool config
    # withholds Read) FAILs the read probe — the F-003 signal a fixed command hid.
    from gauntlet.adapters.base import AgentResult
    from gauntlet.adapters.claude_code import ClaudeCodeAdapter
    from gauntlet.engine.config import AgentProfile
    from gauntlet.engine.doctor import FAIL, _real_profile_read_probe

    def blind_run(self, prompt, *, session=None, schema=None, cwd=None,
                  extra_flags=None, sink=None):
        return AgentResult(text="I do not have permission to read files.",
                           session_id="s", exit_code=0)

    monkeypatch.setattr(ClaudeCodeAdapter, "run", blind_run)
    profile = AgentProfile(adapter="claude-code", model="opus", tools=[])
    result = _real_profile_read_probe("builder", profile, tmp_path)
    assert result.status == FAIL
    assert "could not read a repo file" in result.detail


def test_writability_check_refuses_a_symlinked_worktrees_root(fixture_repo, tmp_path):
    """Post-review F-001, third site: `doctor --writability` creates its own
    probe directory under the derived root, so a symlinked segment would put
    the probe — and the writability seed bytes inside it — outside the
    repository on a *diagnostic* command. It refuses before creating anything.
    """
    from gauntlet.engine import worktree as WT
    from gauntlet.engine.doctor import check_writability

    repo = _healthy_repo(fixture_repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = WT.worktrees_root(repo)
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(outside, target_is_directory=True)

    results = check_writability(repo)

    assert [r.status for r in results] == [FAIL]
    assert "symlink" in results[0].detail
    assert list(outside.iterdir()) == []

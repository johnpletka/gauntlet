"""#160: real Git ranges and subprocesses, without provider/network dependencies."""
import json
import os
from types import SimpleNamespace

import pytest

from conftest import git
from gauntlet.engine import test_scope, verify
from gauntlet.engine.config import RunConfig
from gauntlet.engine.pipeline import Step
from gauntlet.engine.steptypes import handle_shell


def config(**kw):
    return RunConfig(test_command="exit 0", phase_test_command="exit 7", **kw)


def commit(repo, name, body):
    (repo / name).write_text(body)
    git(repo, "add", name)
    git(repo, "commit", "-qm", "fixture")
    return git(repo, "rev-parse", "HEAD").strip()


def test_range_contains_checkpoints_fixes_and_all_dirty_planes(fixture_repo):
    r = fixture_repo
    base = commit(r, "first.py", "base")
    commit(r, "checkpoint.py", "checkpoint")
    commit(r, "fix.py", "review fix")
    (r / "first.py").write_text("staged")
    git(r, "add", "first.py")
    (r / "first.py").write_text("unstaged")
    (r / "new file\nwith newline.py").write_text("new")
    result = test_scope.prepare(r, config(), base)
    assert result["mode"] == "phase"
    assert set(result["changed_paths"]) == {"first.py", "checkpoint.py", "fix.py", "new file\nwith newline.py"}
    original = result["worktree_fingerprint"]
    (r / "first.py").write_text("a later edit")
    assert test_scope.prepare(r, config(), base)["worktree_fingerprint"] != original
    # A reconstructed/resumed record uses exactly the same immutable base.
    assert test_scope.prepare(r, config(), base)["base_sha"] == base


@pytest.mark.parametrize("base", [None, "HEAD~1", "-x", "a" * 40])
def test_bad_or_missing_anchor_falls_back(fixture_repo, base):
    result = test_scope.prepare(fixture_repo, config(), base)
    assert result["mode"] == "full" and result["command"] == "exit 0"


def test_nonancestor_and_deletions_fall_back(fixture_repo):
    r = fixture_repo
    base = commit(r, "one.py", "1")
    other = commit(r, "two.py", "2")
    git(r, "reset", "--hard", base)
    commit(r, "three.py", "3")
    assert test_scope.prepare(r, config(), other)["mode"] == "full"
    (r / "one.py").unlink()
    result = test_scope.prepare(r, config(), base)
    assert result["reason"] == "deleted_paths"
    assert result["deleted_paths"] == ["one.py"]


def test_defaults_and_full_override(fixture_repo):
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    (fixture_repo / "new.py").write_text("new")
    assert test_scope.prepare(fixture_repo, RunConfig(), base)["mode"] == "full"
    assert test_scope.prepare(fixture_repo, config(), base, full=True)["reason"] == "full_requested"


def test_copy_revalidates_baseline_and_does_not_reintroduce_secrets(fixture_repo):
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    commit(fixture_repo, "new.py", "new")
    copy = verify.make_disposable_copy(fixture_repo)
    try:
        result = test_scope.prepare(copy.path, config(), base)
        env = test_scope.environment(result, verify.build_sandbox_env({
            "PATH": os.environ["PATH"], "OPENAI_API_KEY": "fixture-secret",
            "GAUNTLET_TEST_BASE_SHA": "spoofed",
        }))
        assert env["GAUNTLET_TEST_BASE_SHA"] == base
        assert json.loads(env["GAUNTLET_TEST_CONTEXT"])["changed_paths"] == ["new.py"]
        assert "OPENAI_API_KEY" not in env
    finally:
        verify.discard_disposable_copy(fixture_repo, copy)


def test_full_mode_clears_ambient_selection():
    env = test_scope.environment({"mode": "full"}, {
        "PATH": "path", "GAUNTLET_TEST_BASE_SHA": "spoof", "GAUNTLET_TEST_CONTEXT": "spoof",
        "GAUNTLET_TEST_MODE": "phase",
    })
    assert env == {"PATH": "path", "GAUNTLET_TEST_MODE": "full"}


def test_phase_process_failure_is_not_replaced_by_green_full_run(fixture_repo, monkeypatch):
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    (fixture_repo / "new.py").write_text("new")
    records = {}
    monkeypatch.setattr("gauntlet.engine.steptypes._write_step_log", lambda ctx, name, text: records.update({name: text}))
    ctx = SimpleNamespace(work_root=fixture_repo, config=config(), record=SimpleNamespace(phase_start_sha=base))
    result = handle_shell(Step(id="tests", type="shell", run="{{config.test_command}}", test_scope="phase"), ctx)
    assert result.status == "failed"
    evidence = json.loads(records["test-selection.json"])
    assert evidence["mode"] == "phase" and evidence["exit_code"] == 7


def test_empty_change_set_is_full(fixture_repo):
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    assert test_scope.prepare(fixture_repo, config(), base)["reason"] == "empty_change_set"


def test_whitespace_command_rejected():
    with pytest.raises(ValueError):
        RunConfig(phase_test_command="  ")


@pytest.mark.parametrize("scope,run", [("typo", "{{config.test_command}}"), ("phase", "echo unsafe")])
def test_invalid_scope_rejected_at_load(scope, run):
    from gauntlet.engine.pipeline import Pipeline
    from gauntlet.engine.validate import validate_pipeline, PipelineValidationError
    pipe = Pipeline.model_validate({"name": "test", "version": 1, "stages": [{"id": "s", "steps": [
        {"id": "tests", "type": "shell", "test_scope": scope, "run": run},
    ]}]})
    with pytest.raises(PipelineValidationError, match="test_scope"):
        validate_pipeline(pipe, config())


def test_rename_selects_full_and_records_both_names(fixture_repo):
    base = commit(fixture_repo, "before.py", "a")
    git(fixture_repo, "mv", "before.py", "after.py")
    result = test_scope.prepare(fixture_repo, config(), base)
    assert result["mode"] == "full"
    assert result["changed_paths"] == ["after.py", "before.py"]


def test_shell_receives_validated_context_and_scrubs_spoof(fixture_repo, monkeypatch):
    import shlex
    import sys
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    (fixture_repo / "new.py").write_text("a")
    monkeypatch.setenv("GAUNTLET_TEST_BASE_SHA", "spoof")
    monkeypatch.setenv("GAUNTLET_TEST_UNKNOWN", "spoof")
    records = {}
    monkeypatch.setattr("gauntlet.engine.steptypes._write_step_log", lambda ctx, name, text: records.update({name: text}))
    code = "import os,json; print(json.dumps(dict((k,v) for k,v in os.environ.items() if k.startswith('GAUNTLET_TEST_'))))"
    cfg = config()
    cfg.phase_test_command = shlex.quote(sys.executable) + " -c " + shlex.quote(code)
    ctx = SimpleNamespace(work_root=fixture_repo, config=cfg, record=SimpleNamespace(phase_start_sha=base))
    result = handle_shell(Step(id="tests", type="shell", run="{{config.test_command}}", test_scope="phase"), ctx)
    assert result.status == "done"
    assert base in records["output.txt"]
    assert "spoof" not in records["output.txt"]


def test_orchestrator_rechecks_share_phase_start_and_final_is_full(fixture_repo):
    from test_steptypes import _orch
    from gauntlet.engine import manifest as M
    pipe = """
name: selection
version: 1
stages:
  - id: phases
    foreach: vars.phases
    steps:
      - {id: implement, type: shell, run: "echo work >> feature.py && git add feature.py && git commit -qm checkpoint"}
      - {id: tests, type: shell, run: "{{config.test_command}}", test_scope: phase}
      - {id: recheck, type: shell, run: "{{config.test_command}}", test_scope: phase}
  - id: final
    steps:
      - {id: final-tests, type: shell, run: "{{config.test_command}}", test_scope: full}
"""
    original = git(fixture_repo, "rev-parse", "HEAD").strip()
    orch = _orch(fixture_repo, pipe, config={"test_command": "echo FULL", "phase_test_command": "echo PHASE"},
                 extra_context={"phases": [{"id": "P1"}, {"id": "P2"}]})
    assert orch.drive() == M.RUN_DONE
    evidence = [json.loads(p.read_text()) for p in (fixture_repo / "runs").rglob("test-selection.json")]
    phases = [e for e in evidence if e["mode"] == "phase"]
    assert len(phases) == 4
    assert sum(e["base_sha"] == original for e in phases) == 2
    assert len({e["base_sha"] for e in phases}) == 2
    assert len([e for e in evidence if e["mode"] == "full"]) == 1


def test_verifier_cycle_gets_phase_context_inside_real_copy(fixture_repo, tmp_path, monkeypatch):
    from test_verify import _code_repo, _stub_sandbox, _drive_single, _VCONFIG
    from test_cycle import SeqAdapter, REVIEW
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    repo, sha = _code_repo(fixture_repo)
    make_copy, discard = verify.make_disposable_copy, verify.discard_disposable_copy
    _stub_sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(verify, "make_disposable_copy", make_copy)
    monkeypatch.setattr(verify, "discard_disposable_copy", discard)
    monkeypatch.setenv("GAUNTLET_TEST_BASE_SHA", "spoofed")
    captured = {}
    configure = verify.configure_claude_verifier
    def capture(adapter, *, env):
        captured.update(env)
        return configure(adapter, env=env)
    monkeypatch.setattr(verify, "configure_claude_verifier", capture)
    adapters = {"reviewer": SeqAdapter(REVIEW()), "verifier": SeqAdapter(REVIEW()),
                "triage": SeqAdapter(), "builder": SeqAdapter()}
    cfg = {**_VCONFIG, "test_command": "echo FULL", "phase_test_command": "echo PHASE"}
    result, _, run_dir = _drive_single(repo, sha, adapters, config=cfg, phase_start_sha=base)
    assert result.status == "done"
    assert captured["GAUNTLET_TEST_BASE_SHA"] == base
    assert "echo PHASE" in adapters["verifier"].calls[0]["prompt"]
    evidence = [json.loads(p.read_text()) for p in run_dir.rglob("test-selection.json")]
    assert len(evidence) == 1 and evidence[0]["mode"] == "phase"
    assert "feature.py" in evidence[0]["changed_paths"]


def test_opt_in_pipeline_cannot_omit_final_full_validation():
    from gauntlet.engine.pipeline import Pipeline
    from gauntlet.engine.validate import validate_pipeline, PipelineValidationError
    pipe = Pipeline.model_validate({"name": "test", "version": 1, "stages": [{"id": "s", "steps": [
        {"id": "tests", "type": "shell", "test_scope": "phase", "run": "{{config.test_command}}"},
    ]}]})
    with pytest.raises(PipelineValidationError, match="final test_scope: full"):
        validate_pipeline(pipe, config())
    # Existing full-only configuration does not need a new final stage.
    assert validate_pipeline(pipe, RunConfig()).ok()


def test_shell_missing_base_executes_full_fallback(fixture_repo, monkeypatch):
    records = {}
    monkeypatch.setattr("gauntlet.engine.steptypes._write_step_log", lambda ctx, name, text: records.update({name: text}))
    ctx = SimpleNamespace(work_root=fixture_repo, config=config(), record=SimpleNamespace(phase_start_sha=None))
    result = handle_shell(Step(id="tests", type="shell", run="{{config.test_command}}", test_scope="phase"), ctx)
    assert result.status == "done"  # full is exit 0, phase would be exit 7
    evidence = json.loads(records["test-selection.json"])
    assert evidence["mode"] == "full" and evidence["exit_code"] == 0


def test_large_context_does_not_exceed_subprocess_environment_budget(fixture_repo, monkeypatch):
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    original = test_scope._git
    paths = ["x" * 200 + str(i) for i in range(150)]
    def many_paths(root, *args):
        if args[0] == "ls-files":
            return "\0".join(paths) + "\0"
        return original(root, *args)
    monkeypatch.setattr(test_scope, "_git", many_paths)
    result = test_scope.prepare(fixture_repo, config(), base)
    assert result["reason"] == "change_set_too_large"
    assert test_scope.environment(result, {}) == {"GAUNTLET_TEST_MODE": "full"}

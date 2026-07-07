"""P5 — behavioral verifier + sandbox contract (FR-2.1/2.2/2.3/2.5).

Unit coverage for the sandbox primitives (env stripping, backend probe, copy-
pointed judge env, disposable-copy fail-closed) and for the verifier sub-step
wired into ``adversarial_cycle``: a behavioral finding joins the merged panel and
receives a triage verdict (P5-A1 surrogate / FR-2.2), a stubbed copy-creation
failure and a stubbed backend-probe (non-firing-hook) failure park the cycle
(P5-A2, P5-A5), the run worktree hash is unchanged after verification (P5-A4),
and ``metrics.verifier.legit_findings`` + the verifier ``agent_usage`` are
emitted to the manifest (P5-A6). The real-sandbox enforcement tests (outside-copy
read denial, network deny, secret-env absence, over-limit kill, seeded behavioral
bug) live in ``tests/integration/test_verifier_sandbox.py`` (P5-A3, P5-A1).

Any cycle here pins ``max_rounds: 2`` in-fixture (P9 `max_rounds` coupling).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.engine import gitops, manifest as M, verify
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import CommitRecord, Manifest, PipelineRef
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline
from gauntlet.judge.hook_client import REPO_ROOT_ENV_VAR
from gauntlet.judge.service import TOKEN_ENV_VAR

from conftest import git
from test_cycle import REVIEW, SeqAdapter, V

REPO = Path(__file__).resolve().parents[2]

_JUDGE_ENV = {TOKEN_ENV_VAR: "tok", "GAUNTLET_JUDGE_MODE": "unattended"}


# ===========================================================================
# sandbox primitives (verify.py) — pure units
# ===========================================================================
def test_build_sandbox_env_keeps_allowlist_strips_secrets():
    """FR-2.5: the rebuilt env carries only allowlisted vars; every secret/token/
    credential-shaped var is absent by construction (strip-by-construction)."""
    base = {
        "PATH": "/usr/bin", "HOME": "/home/x", "LANG": "en_US.UTF-8",
        "ANTHROPIC_API_KEY": "sk-secret", "OPENAI_API_KEY": "sk-2",
        "AWS_SECRET_ACCESS_KEY": "aws", "GAUNTLET_JUDGE_TOKEN": "tok",
        "MY_DEPLOY_TOKEN": "t", "SOME_PASSWORD": "p", "RANDOM_UNLISTED": "v",
    }
    env = verify.build_sandbox_env(base)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/x"
    assert env["LANG"] == "en_US.UTF-8"
    for leaked in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY",
                   "GAUNTLET_JUDGE_TOKEN", "MY_DEPLOY_TOKEN", "SOME_PASSWORD",
                   "RANDOM_UNLISTED"):
        assert leaked not in env
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_is_secret_key_classification():
    for k in ("ANTHROPIC_API_KEY", "OPENAI_KEY", "X_TOKEN", "Y_SECRET",
              "DB_PASSWORD", "AWS_ACCESS_KEY_ID", "GAUNTLET_JUDGE_TOKEN"):
        assert verify.is_secret_key(k), k
    for k in ("PATH", "HOME", "LANG", "TERM", "CLAUDE_CONFIG_DIR"):
        assert not verify.is_secret_key(k), k


def test_verifier_env_points_judge_root_at_copy(tmp_path):
    """FR-2.5: the verifier env is the stripped allowlist PLUS the run's judge env,
    with the judge repo-root boundary re-pointed at the disposable copy — that is
    how the PreToolUse hook denies any tool call escaping the copy."""
    copy = tmp_path / "copy"
    env = verify.verifier_env(_JUDGE_ENV, copy)
    assert env[REPO_ROOT_ENV_VAR] == str(copy)   # hook confines to the copy
    assert env[TOKEN_ENV_VAR] == "tok"           # judge token present so hook fires
    assert "PATH" in env and env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_verifier_env_drops_secrets_from_judge_env(tmp_path):
    """FR-2.5 / review F-003: verifier_env re-adds ONLY the judge hook-infrastructure
    keys, never the caller's whole judge_env — a secret-shaped var handed in
    judge_env (as the integration harness passes ``dict(os.environ)``) is dropped,
    not copied back over the top of the allowlist strip."""
    copy = tmp_path / "copy"
    judge_env = {
        TOKEN_ENV_VAR: "tok",
        "GAUNTLET_JUDGE_URL": "http://127.0.0.1:9",
        "GAUNTLET_JUDGE_MODE": "unattended",
        "GAUNTLET_RUN_ID": "r1",
        # secret-shaped vars a caller / os.environ could carry:
        "ANTHROPIC_API_KEY": "sk-secret", "AWS_SECRET_ACCESS_KEY": "aws",
        "OPENAI_API_KEY": "sk-2", "MY_DEPLOY_TOKEN": "t",
    }
    env = verify.verifier_env(judge_env, copy)
    # the hook infrastructure the judge needs survives
    assert env[TOKEN_ENV_VAR] == "tok"
    assert env["GAUNTLET_JUDGE_URL"] == "http://127.0.0.1:9"
    assert env["GAUNTLET_JUDGE_MODE"] == "unattended"
    assert env["GAUNTLET_RUN_ID"] == "r1"
    assert env[REPO_ROOT_ENV_VAR] == str(copy)
    # every secret-shaped var in judge_env is dropped, not re-added
    for leaked in ("ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY",
                   "MY_DEPLOY_TOKEN"):
        assert leaked not in env


def test_detect_backend_none_without_claude(monkeypatch, tmp_path):
    """FR-2.5 / P5-A5: with no claude-code CLI the probe finds no backend."""
    monkeypatch.setattr(verify.shutil, "which", lambda name: None)
    assert verify.detect_backend(_JUDGE_ENV) is None
    with pytest.raises(verify.SandboxUnavailableError):
        verify.probe_backend(_JUDGE_ENV, repo_root=tmp_path)


def test_detect_backend_none_without_active_judge(monkeypatch, tmp_path):
    """FR-2.5 / P5-A5: even with claude present, an ABSENT judge (no token — the
    hook has nothing to call, so it cannot fire) parks closed — the verifier never
    runs unhooked."""
    monkeypatch.setattr(verify.shutil, "which", lambda name: "/bin/claude")
    assert verify.detect_backend({}) is None            # no judge token → no hook
    with pytest.raises(verify.SandboxUnavailableError):
        verify.probe_backend({"GAUNTLET_JUDGE_MODE": "unattended"}, repo_root=tmp_path)
    assert verify.detect_backend(_JUDGE_ENV) is not None  # claude + judge → usable


_PROBE_ENV = {TOKEN_ENV_VAR: "tok", "GAUNTLET_RUN_ID": "r1"}


def test_probe_backend_confirms_judge_enforcement_path(monkeypatch, tmp_path):
    """review F-001 / P5-A5: probe_backend does more than the passive presence
    check — it actively exercises the run's judge enforcement path (the service the
    PreToolUse hook calls) with an authenticated decision round-trip and returns a
    backend only when the judge answers this run with a well-formed decision. (The
    end-to-end hook-loading proof is stubbed here; it has its own tests below.)"""
    monkeypatch.setattr(verify.shutil, "which", lambda name: "/bin/claude")
    calls = {}

    def _ok(url, token, body):
        calls.update(url=url, token=token, body=body)
        return {"decision": "deny", "source": "policy", "rationale": "canary"}

    monkeypatch.setattr(verify, "_judge_roundtrip", _ok)
    monkeypatch.setattr(verify, "confirm_hook_loaded", lambda *a, **k: None)
    backend = verify.probe_backend(_PROBE_ENV, repo_root=tmp_path)
    assert backend.claude_path == "/bin/claude"
    assert calls["token"] == "tok"
    assert calls["body"]["run_id"] == "r1"   # bound to THIS run


def test_probe_backend_parks_when_judge_unreachable(monkeypatch, tmp_path):
    """review F-001 / FR-2.5: a claude binary + a token string is NOT enough — if
    the judge enforcement service is unreachable, the verifier would run unconfined,
    so the probe parks closed."""
    monkeypatch.setattr(verify.shutil, "which", lambda name: "/bin/claude")

    def _boom(url, token, body):
        raise OSError("connection refused")

    monkeypatch.setattr(verify, "_judge_roundtrip", _boom)
    with pytest.raises(verify.SandboxUnavailableError):
        verify.probe_backend(_PROBE_ENV, repo_root=tmp_path)


def test_probe_backend_parks_when_hook_never_fires(monkeypatch, tmp_path):
    """review F-001 / P5-A5: the decisive gate — a live judge answering /decide is
    NOT enough; probe_backend launches a real canary claude turn and parks closed
    when the judge never observes its PreToolUse callback (claude did not load/fire
    the hook), so the verifier is never run unconfined."""
    monkeypatch.setattr(verify.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(
        verify, "_judge_roundtrip",
        lambda url, token, body: {"decision": "allow", "rationale": "canary"})
    copy = verify.DisposableCopy(path=tmp_path / "c", root=tmp_path / "c")
    monkeypatch.setattr(verify, "make_disposable_copy", lambda repo, **k: copy)
    monkeypatch.setattr(verify, "discard_disposable_copy", lambda repo, c: None)
    monkeypatch.setattr(verify, "_run_backend_bash", lambda *a, **k: object())
    monkeypatch.setattr(verify, "_judge_observed",
                        lambda url, token, run_id, nonce: False)   # hook never fired
    with pytest.raises(verify.SandboxUnavailableError):
        verify.probe_backend(_PROBE_ENV, repo_root=tmp_path)


def test_exercise_judge_hook_parks_on_malformed_decision(monkeypatch):
    """review F-001: a judge that answers but returns no well-formed permission
    decision leaves enforcement unconfirmed — park closed."""
    monkeypatch.setattr(verify, "_judge_roundtrip", lambda url, token, body: {"nope": 1})
    with pytest.raises(verify.SandboxUnavailableError):
        verify.exercise_judge_hook(_PROBE_ENV)


def test_exercise_judge_hook_parks_without_run_id():
    """review F-001: a token but no run id — the enforcement path cannot be bound to
    this run, so the probe parks closed rather than exercising a foreign/absent
    judge."""
    with pytest.raises(verify.SandboxUnavailableError):
        verify.exercise_judge_hook({TOKEN_ENV_VAR: "tok"})


def _confirm_stubs(monkeypatch, tmp_path, *, launch=None, observed=True):
    """Stub the disposable copy + backend launch + observed-query seams so
    confirm_hook_loaded exercises its own logic without a claude CLI or live judge.
    Returns (copy, launched, discarded) for assertions."""
    copy = verify.DisposableCopy(path=tmp_path / "c", root=tmp_path / "c")
    monkeypatch.setattr(verify, "make_disposable_copy", lambda repo, **k: copy)
    discarded = []
    monkeypatch.setattr(verify, "discard_disposable_copy",
                        lambda repo, c: discarded.append(c))
    launched = {}

    def _default_launch(be, *, prompt, cwd, env, allowed_tools, timeout_s):
        launched.update(env=env, allowed_tools=allowed_tools, prompt=prompt, cwd=cwd)
        return object()

    monkeypatch.setattr(verify, "_run_backend_bash", launch or _default_launch)

    seen = {}

    def _observed(url, token, run_id, nonce):
        seen.update(url=url, token=token, run_id=run_id, nonce=nonce)
        if isinstance(observed, Exception):
            raise observed
        return observed

    monkeypatch.setattr(verify, "_judge_observed", _observed)
    launched["seen"] = seen
    return copy, launched, discarded


def test_confirm_hook_loaded_requires_observed_callback(monkeypatch, tmp_path):
    """review F-001 / P5-A5: confirm_hook_loaded launches a real canary claude turn
    in the confined Bash-only posture, tagged with a probe step_id, and returns only
    when the judge confirms it OBSERVED that turn's PreToolUse callback for THIS run
    — the end-to-end proof the hook actually loaded and fired."""
    backend = verify.SandboxBackend(claude_path="/bin/claude")
    copy, launched, discarded = _confirm_stubs(monkeypatch, tmp_path, observed=True)
    verify.confirm_hook_loaded(backend, _PROBE_ENV, repo_root=tmp_path)
    assert launched["allowed_tools"] == ("Bash",)           # confined, no network
    step = launched["env"][verify.STEP_ID_ENV_VAR]
    assert step.startswith(verify.PROBE_STEP_PREFIX)          # probe-tagged turn
    nonce = step[len(verify.PROBE_STEP_PREFIX):]
    assert launched["seen"]["nonce"] == nonce                # same nonce queried back
    assert launched["seen"]["run_id"] == "r1"                # bound to THIS run
    assert discarded == [copy]                               # copy always discarded


def test_confirm_hook_loaded_parks_when_hook_not_observed(monkeypatch, tmp_path):
    """review F-001: the canary turn ran but the judge never saw its callback (the
    hook did not fire) — park closed, never run the verifier unhooked."""
    backend = verify.SandboxBackend(claude_path="/bin/claude")
    copy, launched, discarded = _confirm_stubs(monkeypatch, tmp_path, observed=False)
    with pytest.raises(verify.SandboxUnavailableError):
        verify.confirm_hook_loaded(backend, _PROBE_ENV, repo_root=tmp_path)
    assert discarded == [copy]


def test_confirm_hook_loaded_parks_on_launch_failure(monkeypatch, tmp_path):
    """review F-001 / FR-2.3: a launch/configuration fault of the canary turn parks
    closed (and never queries the judge), and always discards the copy."""
    backend = verify.SandboxBackend(claude_path="/bin/claude")

    def _boom_launch(be, *, prompt, cwd, env, allowed_tools, timeout_s):
        raise RuntimeError("cannot launch canary claude")

    copy, launched, discarded = _confirm_stubs(
        monkeypatch, tmp_path, launch=_boom_launch)
    with pytest.raises(verify.SandboxUnavailableError):
        verify.confirm_hook_loaded(backend, _PROBE_ENV, repo_root=tmp_path)
    assert discarded == [copy]
    assert launched["seen"] == {}   # judge never queried after a launch failure


def test_confirm_hook_loaded_parks_on_copy_failure(monkeypatch, tmp_path):
    """review F-001 / FR-2.3: a disposable-copy failure while setting up the probe
    parks closed as SandboxUnavailableError (so probe_backend's caller catches it)."""
    backend = verify.SandboxBackend(claude_path="/bin/claude")

    def _boom(repo_root, **k):
        raise verify.CopyCreationError("copy could not be created")

    monkeypatch.setattr(verify, "make_disposable_copy", _boom)
    with pytest.raises(verify.SandboxUnavailableError):
        verify.confirm_hook_loaded(backend, _PROBE_ENV, repo_root=tmp_path)


def test_confirm_hook_loaded_parks_on_observed_query_error(monkeypatch, tmp_path):
    """review F-001 / FR-2.5: an unreachable/erroring /observed query leaves the
    hook-firing unconfirmed — park closed."""
    backend = verify.SandboxBackend(claude_path="/bin/claude")
    copy, launched, discarded = _confirm_stubs(
        monkeypatch, tmp_path, observed=OSError("connection refused"))
    with pytest.raises(verify.SandboxUnavailableError):
        verify.confirm_hook_loaded(backend, _PROBE_ENV, repo_root=tmp_path)
    assert discarded == [copy]


def test_confirm_hook_loaded_parks_without_run_id(monkeypatch, tmp_path):
    """review F-001: no run id — the canary turn cannot be bound to this run, so it
    parks closed before launching anything."""
    backend = verify.SandboxBackend(claude_path="/bin/claude")
    monkeypatch.setattr(verify, "make_disposable_copy",
                        lambda *a, **k: pytest.fail("must not copy without a run id"))
    with pytest.raises(verify.SandboxUnavailableError):
        verify.confirm_hook_loaded(backend, {TOKEN_ENV_VAR: "tok"}, repo_root=tmp_path)


def test_make_disposable_copy_fails_closed(monkeypatch, tmp_path):
    """FR-2.3 / P5-A2: a git-worktree failure raises CopyCreationError — an absent
    copy is never treated as 'verify skipped, proceed'."""
    def _boom(repo, path, ref):
        raise gitops.GitError(["worktree", "add"], 128, "fatal: cannot add worktree")

    monkeypatch.setattr(verify.gitops, "add_worktree", _boom)
    with pytest.raises(verify.CopyCreationError):
        verify.make_disposable_copy(tmp_path)


def test_configure_claude_verifier_pins_tools_env_and_hook():
    """FR-2.5: an already-built claude-code adapter is pinned to the confined
    tool allowlist (no network tools), the accept-edits mode, the setting-sources
    project flag (so the judge hook fires), and the rebuilt copy-pointed env."""
    from gauntlet.adapters.claude_code import ClaudeCodeAdapter

    adapter = ClaudeCodeAdapter(allowed_tools=["Bash", "WebFetch"], base_flags=[])
    verify.configure_claude_verifier(adapter, env={"PATH": "/usr/bin"})
    assert "WebFetch" not in adapter.allowed_tools and "WebSearch" not in adapter.allowed_tools
    assert "Bash" in adapter.allowed_tools
    assert adapter.permission_mode == verify.VERIFIER_PERMISSION_MODE
    assert "--setting-sources" in adapter.base_flags and "project" in adapter.base_flags
    assert adapter.env == {"PATH": "/usr/bin"}


def test_configure_claude_verifier_is_noop_on_non_claude_adapter():
    """A test double / non-claude adapter carries none of those attributes, so the
    configuration is a safe no-op (the profile validation is the real guard)."""
    assert verify.configure_claude_verifier(SeqAdapter(), env={}) == []


# ===========================================================================
# verifier wired into the cycle (code_review mode)
# ===========================================================================
_VCONFIG = {
    "triage_concurrency": 1,
    "agents": {
        "reviewer": {"adapter": "codex"},
        "verifier": {"adapter": "claude-code", "model": "opus",
                     "base_flags": ["--setting-sources", "project"]},
        "triage": {"adapter": "api", "model": "h"},
        "builder": {"adapter": "claude-code"},
        "esc": {"adapter": "api", "model": "strong"},
    },
    "identities": {
        "reviewer": {"name": "Gauntlet Reviewer (codex)", "email": "reviewer@gauntlet.local"},
        "builder": {"name": "Gauntlet Builder (claude)", "email": "builder@gauntlet.local"},
    },
}

_PHASE_ITEM = {
    "id": "P5", "title": "verifier phase", "goal": "run the feature",
    "acceptance": [{"id": "P5-A1", "clause": "the feature works at runtime"}],
}


def BEHAV(fid, claim="wrong runtime output on real input"):
    return {"id": fid, "severity": "major", "category": "behavioral",
            "location": "feature.py:1", "claim": claim,
            "evidence": "ran: gauntlet feature; got exit 1, expected the summary",
            "suggested_fix": None}


def _code_repo(fixture_repo):
    """A git repo with the real schemas + a committed phase deliverable."""
    import shutil

    shutil.copytree(REPO / "schemas", fixture_repo / "schemas")
    (fixture_repo / "feature.py").write_text("PHASE-WORK\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "P5: work")
    return fixture_repo, gitops.head_sha(fixture_repo)


def _stub_sandbox(monkeypatch, tmp_path):
    """Stub the real sandbox/copy machinery so the verifier wiring runs without a
    claude CLI or a real worktree copy — the fake verifier adapter stands in for
    the sandboxed agent. Returns the copy dir the stub hands the verifier."""
    copy_dir = tmp_path / "verify-copy"
    copy_dir.mkdir()
    monkeypatch.setattr(verify, "probe_backend",
                        lambda judge_env, **k: verify.SandboxBackend(claude_path="claude"))
    monkeypatch.setattr(verify, "make_disposable_copy",
                        lambda repo, **k: verify.DisposableCopy(path=copy_dir, root=copy_dir))
    monkeypatch.setattr(verify, "discard_disposable_copy", lambda repo, copy: None)
    return copy_dir


def _drive_single(repo, phase_sha, adapters, *, iteration_item=_PHASE_ITEM):
    """Drive one code_review cycle step directly through the handler with a
    StepContext carrying ``iteration_item`` (the foreach phase)."""
    from gauntlet.engine.cycle import handle_adversarial_cycle
    from gauntlet.engine.execution import StepContext
    from gauntlet.engine.manifest import StepRecord
    from gauntlet.engine.pipeline import Step
    from gauntlet.logging.redact import RedactingWriter

    step = {"id": "cycle", "type": "adversarial_cycle", "mode": "code_review",
            "reviewer": "reviewer", "triager": "triage", "fixer": "builder",
            "verifier": "verifier", "max_rounds": 2,
            "review_prompt": "prompts/review-code.md"}
    cfg = RunConfig.model_validate(_VCONFIG)
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    man.commits.append(CommitRecord(step_id="commit", phase="P5", sha=phase_sha))
    run_dir = repo / "runs" / "demo" / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = StepContext(
        repo_root=repo, run_dir=run_dir, artifact_root=repo,
        config=cfg, pipeline=Pipeline.model_validate(
            {"name": "demo", "version": 1, "stages": []}),
        manifest=man, record=StepRecord(id="cycle", type="adversarial_cycle"),
        writer=RedactingWriter(), excludes=["runs"],
        judge_env=dict(_JUDGE_ENV),
        iteration_item=iteration_item,
        adapter_factory=lambda n: adapters[n],
    )
    result = handle_adversarial_cycle(Step.model_validate(step), ctx)
    return result, man, run_dir


def test_behavioral_finding_joins_panel_and_is_triaged(fixture_repo, monkeypatch, tmp_path):
    """P5-A1 (unit) / FR-2.2: a verifier behavioral finding appears in findings.json
    alongside review findings and receives a triage verdict."""
    repo, sha = _code_repo(fixture_repo)
    _stub_sandbox(monkeypatch, tmp_path)
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),           # no diff-review findings
        "verifier": SeqAdapter(REVIEW(BEHAV("F-b1"))),
        "triage": SeqAdapter(V("verifier:F-b1", verdict="legitimate", action="reject")),
        "builder": SeqAdapter(),
    }
    result, man, run_dir = _drive_single(repo, sha, adapters)
    assert result.status == M.DONE
    findings = json.loads((run_dir / "artifacts" / "findings.json").read_text())["findings"]
    behavioral = [f for f in findings if f.get("source") == "verifier"]
    assert len(behavioral) == 1
    assert behavioral[0]["id"] == "verifier:F-b1"
    assert behavioral[0]["category"] == "behavioral"
    triage = json.loads((run_dir / "artifacts" / "triage.json").read_text())
    assert triage["verdicts"][0]["finding_id"] == "verifier:F-b1"


def test_verifier_metrics_emitted(fixture_repo, monkeypatch, tmp_path):
    """P5-A6 / review F-001: metrics.verifier.legit_findings and the verifier
    agent_usage cost ride on the cycle result (the orchestrator persists them to
    the manifest step record)."""
    repo, sha = _code_repo(fixture_repo)
    _stub_sandbox(monkeypatch, tmp_path)
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),
        "verifier": SeqAdapter(REVIEW(BEHAV("F-b1"))),
        "triage": SeqAdapter(V("verifier:F-b1", verdict="legitimate", action="reject")),
        "builder": SeqAdapter(),
    }
    result, man, run_dir = _drive_single(repo, sha, adapters)
    assert result.status == M.DONE
    v = result.metrics["verifier"]
    assert v["profile"] == "verifier"
    assert v["findings_total"] == 1
    assert v["legit_findings"] == 1          # the behavioral finding was legitimate
    assert v["agent_usage"]["input_tokens"] >= 10   # the verifier's own spend


def test_verifier_metrics_land_in_manifest_via_orchestrator(fixture_repo, monkeypatch, tmp_path):
    """P5-A6 (end-to-end): driven through the orchestrator, the verifier metrics are
    persisted to the manifest step record — readable without transcript access."""
    repo, sha = _code_repo(fixture_repo)
    _stub_sandbox(monkeypatch, tmp_path)
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),
        "verifier": SeqAdapter(REVIEW(BEHAV("F-b1"))),
        "triage": SeqAdapter(V("verifier:F-b1", verdict="legitimate", action="reject")),
        "builder": SeqAdapter(),
    }
    step = {"id": "cycle", "type": "adversarial_cycle", "mode": "code_review",
            "reviewer": "reviewer", "triager": "triage", "fixer": "builder",
            "verifier": "verifier", "max_rounds": 2,
            "review_prompt": "prompts/review-code.md"}
    pipeline = Pipeline.model_validate({
        "name": "demo", "version": 1, "stages": [{"id": "s", "steps": [step]}]})
    cfg = RunConfig.model_validate(_VCONFIG)
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    man.commits.append(CommitRecord(step_id="commit", phase="P5", sha=sha))
    orch = Orchestrator(
        repo_root=repo, run_dir=repo / "runs" / "demo" / "run-1", artifact_root=repo,
        config=cfg, pipeline=pipeline, manifest=man,
        adapter_factory=lambda n: adapters[n],
    )
    assert orch.drive() == M.RUN_DONE
    v = man.record("cycle").metrics["verifier"]
    assert v["profile"] == "verifier" and v["findings_total"] == 1
    assert v["legit_findings"] == 1 and v["agent_usage"]["input_tokens"] >= 10


def test_stubbed_copy_failure_parks_cycle(fixture_repo, monkeypatch, tmp_path):
    """P5-A2 / FR-2.3: a copy-creation failure parks the cycle with the failure in
    notes — never degrades to 'skipped, proceed'."""
    repo, sha = _code_repo(fixture_repo)
    monkeypatch.setattr(verify, "probe_backend",
                        lambda judge_env, **k: verify.SandboxBackend(claude_path="claude"))

    def _boom(repo_root, **k):
        raise verify.CopyCreationError("disposable worktree copy could not be created")

    monkeypatch.setattr(verify, "make_disposable_copy", _boom)
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),
        "verifier": SeqAdapter(REVIEW()),   # must never be called
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    result, man, run_dir = _drive_single(repo, sha, adapters)
    assert result.status == M.PARKED
    assert "fail-closed" in result.notes and "FR-2.3" in result.notes
    assert adapters["verifier"].calls == []


def test_stubbed_non_firing_hook_parks_cycle(fixture_repo, monkeypatch, tmp_path):
    """P5-A5 / FR-2.5: a backend whose judge hook cannot be confirmed firing (probe
    fails) parks the cycle closed — the verifier never runs unhooked."""
    repo, sha = _code_repo(fixture_repo)

    def _no_backend(judge_env, **k):
        raise verify.SandboxUnavailableError(
            "no usable v1 verifier backend: the judge PreToolUse hook cannot be "
            "confirmed firing")

    monkeypatch.setattr(verify, "probe_backend", _no_backend)
    monkeypatch.setattr(verify, "make_disposable_copy",
                        lambda *a, **k: pytest.fail("copy must not run without a backend"))
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),
        "verifier": SeqAdapter(REVIEW()),
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    result, man, run_dir = _drive_single(repo, sha, adapters)
    assert result.status == M.PARKED
    assert "FR-2.5" in result.notes
    assert adapters["verifier"].calls == []


def test_verifier_launch_failure_parks_not_crashes(fixture_repo, monkeypatch, tmp_path):
    """review F-004 / FR-2.3: a verifier setup/launch fault BEFORE the sub-step
    returns (here: verifier-posture configuration) parks the cycle fail-closed,
    rather than escaping as an internal crash or falling through to dereference an
    unbound ``review`` in the post-run block."""
    repo, sha = _code_repo(fixture_repo)
    _stub_sandbox(monkeypatch, tmp_path)

    def _boom(adapter, *, env):
        raise RuntimeError("adapter misconfigured: cannot launch verifier")

    monkeypatch.setattr(verify, "configure_claude_verifier", _boom)
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),
        "verifier": SeqAdapter(REVIEW(BEHAV("F-b1"))),   # must never be reached
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    result, man, run_dir = _drive_single(repo, sha, adapters)
    assert result.status == M.PARKED
    assert "fail-closed" in result.notes and "FR-2.3" in result.notes
    assert adapters["verifier"].calls == []


def test_run_worktree_hash_unchanged_after_verification(fixture_repo, monkeypatch, tmp_path):
    """P5-A4: the real run worktree's HEAD tree hash is identical before and after
    verification (the verifier executed only in the disposable copy)."""
    repo, sha = _code_repo(fixture_repo)
    before = gitops.worktree_tree_hash(repo)
    _stub_sandbox(monkeypatch, tmp_path)
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),
        "verifier": SeqAdapter(REVIEW(BEHAV("F-b1"))),
        "triage": SeqAdapter(V("verifier:F-b1", verdict="legitimate", action="reject")),
        "builder": SeqAdapter(),
    }
    result, man, run_dir = _drive_single(repo, sha, adapters)
    assert result.status == M.DONE
    assert gitops.worktree_tree_hash(repo) == before
    assert gitops.is_clean(repo, exclude=["runs", "schemas"])


def test_verifier_timeout_parks_cycle(fixture_repo, monkeypatch, tmp_path):
    """P5-A3 (resource/wall-clock limit) / FR-2.5: an over-limit verifier execution
    surfaces as an AgentTimeoutError (run_with_timeout kills the process group on
    expiry) and parks the cycle closed — never 'skipped, proceed'."""
    from gauntlet.adapters.base import AgentTimeoutError

    repo, sha = _code_repo(fixture_repo)
    _stub_sandbox(monkeypatch, tmp_path)

    class _TimeoutAdapter(SeqAdapter):
        def run(self, prompt, **k):
            self.calls.append({"prompt": prompt})
            raise AgentTimeoutError("verifier killed after wall-clock timeout")

    adapters = {
        "reviewer": SeqAdapter(REVIEW()),
        "verifier": _TimeoutAdapter(),
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    result, man, run_dir = _drive_single(repo, sha, adapters)
    assert result.status == M.PARKED
    assert "fail-closed" in result.notes and "FR-2.3" in result.notes


def test_worktree_mutation_across_verification_parks(fixture_repo, monkeypatch, tmp_path):
    """P5-A4 (fail-closed): if the real worktree tree hash changes across
    verification, the cycle parks rather than handing a mutated tree to triage."""
    repo, sha = _code_repo(fixture_repo)
    _stub_sandbox(monkeypatch, tmp_path)
    seq = iter(["hash-before", "hash-after"])  # different → simulated mutation
    monkeypatch.setattr(gitops, "worktree_tree_hash", lambda repo: next(seq))
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),
        "verifier": SeqAdapter(REVIEW()),
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    result, man, run_dir = _drive_single(repo, sha, adapters)
    assert result.status == M.PARKED
    assert "tree hash changed" in result.notes

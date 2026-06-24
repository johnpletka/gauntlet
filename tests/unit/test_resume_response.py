"""P3 — `gauntlet resume --response`: idempotent recording, commit ownership,
retry-budget decoupling (FR-1, FR-1.1, FR-2, FR-2.2, FR-6, FR-7.1, FR-8, FR-9).

These exercise the determinism / crash-recovery core with fake adapters: a run
is driven to a real UPSTREAM CONFLICT park, then resumed with `--response`. The
manifest is the source of truth; the orchestrator-owned manifest-checkpoint
commit makes both the `pending` and `consumed` states reachable from git
history. Crash points across the append→launch→consume window are simulated by
mutating the on-disk manifest (the durable state a `kill -9` would leave) and
re-running the resume — asserting exactly one entry and one logical re-execution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.adapters.base import AdapterCapabilities, AdapterError, AgentResult
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.identity import GAUNTLET_USER_EMAIL
from gauntlet.engine.manifest import HumanResponse, Manifest
from gauntlet.engine.orchestrator import ENGINE_IDENTITY
from gauntlet.engine.run import RunManager

from conftest import git

CONFIG = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
"""

# One agent_task that halts on UPSTREAM CONFLICT, then a commit step so the
# proceed path lands a real phase commit (the conflict park leaves a clean tree).
PIPELINE = """
name: respond
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go,
         halt_on: "UPSTREAM CONFLICT"}
      - {id: commit, type: commit, message: "P1: implement phase\\n\\nthe body."}
"""

# A single agent_task (no commit) for the crash / counter tests, where reaching a
# commit step would only add noise.
PIPELINE_SOLO = """
name: respond
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go,
         halt_on: "UPSTREAM CONFLICT"}
"""

# An always-failing shell step that retries itself, for the persisted-retry-budget
# tests (FR-6 / review F-003). `run` exits non-zero every time; on_fail reroutes
# back to the same step with a budget of 2 retries (so the 3rd failure exhausts).
PIPELINE_RETRY = """
name: respond
version: 1
stages:
  - id: phase
    steps:
      - {id: tests, type: shell, run: "exit 7",
         on_fail: {route_to: tests, max_retries: 2}}
"""

CONFLICT_TEXT = "UPSTREAM CONFLICT\nPhase: P1\nplan says X; impl reveals Y\n"


class ScriptedAdapter:
    """A builder whose behavior is flipped between drive() invocations.

    ``behavior``: ``conflict`` (halt on UPSTREAM CONFLICT, write nothing —
    clean park), ``proceed`` (write the file, complete), or ``fail`` (raise an
    AdapterError → a genuine FAILED outcome).
    """

    name = "scripted"
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True
    )

    def __init__(self, behavior: str = "conflict") -> None:
        self.behavior = behavior
        self.prompts: list[str] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.prompts.append(prompt)
        if self.behavior == "conflict":
            return AgentResult(text=CONFLICT_TEXT, session_id="s", exit_code=0)
        if self.behavior == "fail":
            raise AdapterError("genuine agent failure (not a conflict)")
        (Path(cwd) / "feature.py").write_text("implemented\n")
        return AgentResult(text="done implementing", session_id="s", exit_code=0)


def _clock():
    seq = iter(range(1, 100000))
    return lambda: f"2026-06-24T00:00:{next(seq):05d}+00:00"


def _build_repo(
    tmp: Path, pipeline: str = PIPELINE, *, config: str = CONFIG
) -> tuple[Path, RunManager]:
    repo = tmp
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@gauntlet.local")
    git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("response fixture\n")
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(config)
    (repo / "pipelines").mkdir()
    (repo / "pipelines" / "respond.yaml").write_text(pipeline)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "init")
    git(repo, "branch", "-M", "main")
    mgr = RunManager(repo)
    mgr.new("demo")
    mgr.layout("demo").prd_path.write_text("# PRD\n\nReal human-authored PRD.\n")
    return repo, mgr


def _drive_to_conflict(repo: Path, mgr: RunManager, pipeline: str = PIPELINE):
    """Start a run whose builder halts on an UPSTREAM CONFLICT; return adapter."""
    adapter = ScriptedAdapter("conflict")
    status = mgr.start(
        "demo", repo / "pipelines" / "respond.yaml",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_UPSTREAM_CONFLICT
    return adapter


def _run_dir(mgr: RunManager) -> Path:
    return mgr.layout("demo").active_run_dir()


def _checkpoint_log(repo: Path) -> list[str]:
    """`author|subject` for every engine response-checkpoint commit, oldest→newest."""
    out = gitops._run(repo, "log", "--reverse", "--format=%an|%s")
    return [ln for ln in out.splitlines() if ln.split("|", 1)[-1].startswith("gauntlet: response")]


# --- happy path: proceed in place -------------------------------------------
def test_response_proceeds_records_pending_then_consumed(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)

    adapter = ScriptedAdapter("proceed")
    status = mgr.resume(
        "demo", response="Ratify option 1: no contradiction remains.",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE

    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1
    entry = rec.human_responses[0]
    assert entry.state == M.RESPONSE_CONSUMED
    assert entry.response_id == "implement-resp-1"
    assert entry.response_attempt == 1
    assert entry.response_text == "Ratify option 1: no contradiction remains."
    assert entry.user == "fixture@gauntlet.local"  # FR-9 git-config fallback
    # FR-6: a conflict→proceed cycle is not a failure; the retry counter is 0.
    assert rec.attempts == 0
    # proceed resolved the conflict in place: discriminator cleared (FR-2.1).
    assert rec.parked_reason is None
    # A real phase commit landed (no re-park).
    assert gitops.commit_subject(repo, "HEAD") == "P1: implement phase"


def test_response_checkpoints_reach_git_history_in_order(tmp_path):
    # FR-2.2 / F-002: a distinct `pending` checkpoint commit must precede the
    # `consumed` one in git history, both authored by the fixed engine identity.
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
    )
    checkpoints = _checkpoint_log(repo)
    assert checkpoints == [
        "Gauntlet Engine|gauntlet: response implement-resp-1 pending",
        "Gauntlet Engine|gauntlet: response implement-resp-1 consumed",
    ]


def test_checkpoint_commit_touches_only_bookkeeping(tmp_path):
    # FR-2.2: a response checkpoint commits ONLY manifest.json + RUN.md — never
    # the implementation diff — under the engine identity (not the operator).
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
    )
    run_rel = _run_dir(mgr).resolve().relative_to(repo.resolve()).as_posix()
    for sha_line in gitops._run(
        repo, "log", "--format=%H|%an|%ae|%s"
    ).splitlines():
        sha, an, ae, subject = sha_line.split("|", 3)
        if not subject.startswith("gauntlet: response"):
            continue
        assert (an, ae) == ("Gauntlet Engine", "engine@gauntlet.local")
        files = gitops._run(
            repo, "show", "--name-only", "--format=", sha
        ).split()
        assert sorted(files) == sorted(
            [f"{run_rel}/manifest.json", f"{run_rel}/RUN.md"]
        )


# --- re-park: response leads to a new conflict ------------------------------
def test_response_reparks_consumes_entry_and_keeps_discriminator(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)

    # The resumed builder still conflicts → re-park; the entry is still consumed
    # (a terminal outcome), and the conflict discriminator stays set so a *new*
    # --response is required next time.
    status = mgr.resume(
        "demo", response="this does not actually resolve it",
        use_judge=False, adapter_factory=lambda n: ScriptedAdapter("conflict"),
        clock=_clock(),
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED
    assert rec.parked_reason == M.PARKED_REASON_UPSTREAM_CONFLICT
    assert rec.attempts == 0  # a re-park is not a failure (FR-6)

    # A second --response appends a distinct entry (latest is consumed, not
    # pending), and the history accumulates (append-only, FR-2).
    status = mgr.resume(
        "demo", response="now it is genuinely resolved",
        use_judge=False, adapter_factory=lambda n: ScriptedAdapter("proceed"),
        clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert [r.response_id for r in rec.human_responses] == [
        "implement-resp-1", "implement-resp-2"
    ]
    assert [r.response_attempt for r in rec.human_responses] == [1, 2]
    assert all(r.state == M.RESPONSE_CONSUMED for r in rec.human_responses)


# --- guards (FR-1, FR-1.1, FR-8) --------------------------------------------
def test_conflict_park_without_response_errors(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    with pytest.raises(ValueError) as exc:
        mgr.resume("demo", use_judge=False)
    assert (
        "step 'implement' parked on an upstream conflict; resume it with "
        '--response "<decision>" (see `gauntlet resume --help`)'
    ) == str(exc.value)


def test_not_parked_with_response_errors(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    # Resolve the conflict so the run completes (not parked), then a --response
    # has nothing to attach to.
    mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
    )
    man = mgr.status("demo")
    assert man.status == M.RUN_DONE
    with pytest.raises(ValueError) as exc:
        mgr.resume("demo", response="too late", use_judge=False)
    assert str(exc.value) == (
        f"run '{man.run_id}' is not parked; cannot resume with --response"
    )


def test_human_gate_park_with_response_errors(tmp_path):
    text = """
name: respond
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [prd.md]}
"""
    repo, mgr = _build_repo(tmp_path / "repo", text)
    status = mgr.start(
        "demo", repo / "pipelines" / "respond.yaml", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
    )
    assert status == M.RUN_PARKED
    with pytest.raises(ValueError) as exc:
        mgr.resume("demo", response="decide", use_judge=False)
    assert str(exc.value) == (
        "use `gauntlet approve` or `gauntlet reject` for human_gate steps; "
        "--response is for agent_task steps"
    )


def test_non_conflict_agent_park_resumes_without_response(tmp_path):
    # FR-1.1: a generic (non-conflict) agent_task park keeps its existing
    # response-less re-run behavior. A `halt_on` marker that is NOT the canonical
    # UPSTREAM CONFLICT parks with parked_reason unset.
    text = """
name: respond
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go,
         halt_on: "NEEDS REVIEW"}
"""
    repo, mgr = _build_repo(tmp_path / "repo", text)

    class HaltAdapter(ScriptedAdapter):
        def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
            self.prompts.append(prompt)
            if self.behavior == "halt":
                return AgentResult(text="NEEDS REVIEW: please look\n", exit_code=0)
            (Path(cwd) / "feature.py").write_text("done\n")
            return AgentResult(text="done", exit_code=0)

    halt = HaltAdapter("halt")
    status = mgr.start(
        "demo", repo / "pipelines" / "respond.yaml", use_judge=False,
        adapter_factory=lambda n: halt, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason is None  # not a conflict park

    # Response-less resume re-runs the agent exactly as before this feature.
    halt.behavior = "proceed"
    status = mgr.resume("demo", use_judge=False, adapter_factory=lambda n: halt)
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.DONE
    assert rec.human_responses == []  # nothing recorded; not a conflict path


# --- operator identity (FR-9) -----------------------------------------------
def test_identity_env_override_trimmed(tmp_path, monkeypatch):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    monkeypatch.setenv(GAUNTLET_USER_EMAIL, "  operator@example.com  ")
    mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
    )
    rec = mgr.status("demo").record("implement")
    assert rec.human_responses[0].user == "operator@example.com"


def test_identity_unresolvable_appends_nothing(tmp_path, monkeypatch):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    # Blank env + no git config (local unset + global/system isolated) → fail
    # closed, manifest unchanged (FR-9).
    monkeypatch.setenv(GAUNTLET_USER_EMAIL, "   ")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(repo, "config", "--unset", "user.email")
    with pytest.raises(Exception) as exc:
        mgr.resume("demo", response="proceed", use_judge=False,
                   adapter_factory=lambda n: ScriptedAdapter("proceed"))
    assert "cannot resolve operator identity" in str(exc.value)
    rec = mgr.status("demo").record("implement")
    assert rec.human_responses == []  # no entry appended


# --- FR-6: retry-budget decoupling ------------------------------------------
def test_many_conflict_resumes_never_exhaust_budget(tmp_path):
    # FR-6: arbitrarily many --response cycles that keep parking on conflicts
    # never advance the failure-retry counter or fail the run.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    for i in range(4):
        status = mgr.resume(
            "demo", response=f"attempt {i} still unresolved",
            use_judge=False, adapter_factory=lambda n: ScriptedAdapter("conflict"),
            clock=_clock(),
        )
        assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 4  # audit history grows
    assert rec.attempts == 0  # ... but the failure-retry counter never moves


def test_genuine_response_failure_increments_attempts_once(tmp_path):
    # FR-6: a --response re-run that GENUINELY fails (not a conflict) counts as
    # exactly one failure; the response entry is still consumed (terminal).
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    status = mgr.resume(
        "demo", response="proceed please", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("fail"), clock=_clock(),
    )
    assert status == M.RUN_FAILED
    rec = mgr.status("demo").record("implement")
    assert rec.attempts == 1
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED


def test_retry_budget_uses_persisted_attempts(tmp_path):
    # FR-6 / review F-003: the retry budget is the PERSISTED `StepRecord.attempts`,
    # not an invocation-local tally. A single drive of an always-failing step with
    # max_retries=2 exhausts on the N+1-th (3rd) genuine failure.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_RETRY)
    status = mgr.start(
        "demo", repo / "pipelines" / "respond.yaml",
        use_judge=False, adapter_factory=lambda n: ScriptedAdapter("proceed"),
        clock=_clock(),
    )
    assert status == M.RUN_FAILED
    rec = mgr.status("demo").record("tests")
    assert rec.attempts == 3  # 1 initial + 2 retries = 3 failures, then exhausted


def test_retry_budget_does_not_reset_across_resume(tmp_path):
    # FR-6 / review F-003: a resume must NOT hand the step a fresh budget. With an
    # invocation-local counter (the bug) a resumed exhausted step would retry 2
    # more times; with the persisted counter it fails again immediately.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_RETRY)
    mgr.start(
        "demo", repo / "pipelines" / "respond.yaml",
        use_judge=False, adapter_factory=lambda n: ScriptedAdapter("proceed"),
        clock=_clock(),
    )
    assert mgr.status("demo").record("tests").attempts == 3

    # Resume the failed run: the exhausted budget stays exhausted, so the step
    # fails exactly ONE more time (no fresh reroutes) — attempts advances by 1,
    # not by another full budget of 3.
    status = mgr.resume(
        "demo", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
    )
    assert status == M.RUN_FAILED
    assert mgr.status("demo").record("tests").attempts == 4


def test_consumed_failure_is_not_reexecuted_on_resume(tmp_path):
    # FR-7.1 / review F-002: a step persisted as FAILED with its --response
    # already CONSUMED is a terminal, reconciled outcome. A later (response-less)
    # resume must reconcile bookkeeping only — never re-invoke the adapter or
    # advance the failure counter a second time.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    # Genuinely fail the responded re-run: persists implement FAILED, attempts 1,
    # response consumed, run FAILED.
    status = mgr.resume(
        "demo", response="proceed please", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("fail"), clock=_clock(),
    )
    assert status == M.RUN_FAILED

    # A plain resume over that terminal failure must not re-run the agent.
    adapter = ScriptedAdapter("proceed")  # would WRITE + succeed if re-executed
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_FAILED  # still failed; not silently driven to done
    assert adapter.prompts == []  # adapter never invoked
    rec = mgr.status("demo").record("implement")
    assert rec.attempts == 1  # exactly one failure, not double-counted
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED


# --- FR-7.1: idempotent crash recovery --------------------------------------
def _seed_pending(mgr: RunManager, text: str = "the human decision") -> str:
    """Simulate a crash AFTER the atomic pending append but BEFORE its checkpoint
    commit: the on-disk manifest carries a pending entry; git has no commit yet.
    Returns the response_id."""
    run_dir = _run_dir(mgr)
    man = Manifest.load(run_dir / "manifest.json")
    rec = man.record("implement")
    entry = HumanResponse(
        response_id="implement-resp-1", response_text=text,
        timestamp="2026-06-24T00:00:00+00:00", user="fixture@gauntlet.local",
        response_attempt=1, state=M.RESPONSE_PENDING,
    )
    rec.human_responses.append(entry)
    man.write_atomic(run_dir / "manifest.json")
    return entry.response_id


def test_recovery_pending_relaunches_without_duplicate(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    rid = _seed_pending(mgr, "Ratify option 1.")

    # Re-running resume with the IDENTICAL text recovers the pending entry: it is
    # re-launched (not re-appended), and a `pending` checkpoint is flushed to git
    # BEFORE the later `consumed` one (F-002).
    adapter = ScriptedAdapter("proceed")
    status = mgr.resume(
        "demo", response="Ratify option 1.", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1  # NOT duplicated
    assert rec.human_responses[0].response_id == rid
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED
    assert rec.attempts == 0
    assert len(adapter.prompts) == 1  # exactly one logical re-execution
    assert _checkpoint_log(repo) == [
        "Gauntlet Engine|gauntlet: response implement-resp-1 pending",
        "Gauntlet Engine|gauntlet: response implement-resp-1 consumed",
    ]


def test_recovery_pending_with_no_response_arg_relaunches(tmp_path):
    # A plain `gauntlet resume` (no --response) over a still-pending entry
    # recovers it rather than erroring for the missing --response.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    _seed_pending(mgr)
    status = mgr.resume(
        "demo", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED


def test_recovery_different_response_over_pending_errors(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    _seed_pending(mgr, "the original decision")
    with pytest.raises(ValueError) as exc:
        mgr.resume("demo", response="a DIFFERENT decision", use_judge=False)
    assert str(exc.value) == (
        "a pending response (implement-resp-1) is awaiting processing; re-run "
        "`gauntlet resume demo` to finish it, or abort the run — do not supply "
        "a new response over a pending one."
    )
    # The pending entry is untouched; nothing appended.
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_PENDING


def test_recovery_consumed_flushes_commit_without_reexecuting(tmp_path):
    # Crash AFTER the atomic finalize-and-consume write but BEFORE its checkpoint
    # commit: the on-disk entry already reads `consumed` and the step is DONE.
    # Recovery flushes only the commit; it does not re-execute or re-count.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    run_dir = _run_dir(mgr)
    man = Manifest.load(run_dir / "manifest.json")
    rec = man.record("implement")
    rec.human_responses.append(HumanResponse(
        response_id="implement-resp-1", response_text="proceed",
        timestamp="2026-06-24T00:00:00+00:00", user="fixture@gauntlet.local",
        response_attempt=1, state=M.RESPONSE_CONSUMED,
    ))
    rec.status = M.DONE  # the consume rode the same write as the DONE status
    rec.parked_reason = None
    man.status = M.RUN_RUNNING
    man.write_atomic(run_dir / "manifest.json")

    adapter = ScriptedAdapter("proceed")
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED
    assert rec.attempts == 0
    assert adapter.prompts == []  # implement was DONE → never re-executed
    # The consumed checkpoint was flushed to git on recovery.
    assert "Gauntlet Engine|gauntlet: response implement-resp-1 consumed" in (
        _checkpoint_log(repo)
    )


def _seed_dirty_running_pending(repo: Path, mgr: RunManager) -> None:
    """Simulate a crash that left the record RUNNING with a dirty worktree and a
    still-`pending` response (the kill landed after the write-ahead RUNNING
    checkpoint, mid-adapter-edit)."""
    run_dir = _run_dir(mgr)
    man = Manifest.load(run_dir / "manifest.json")
    rec = man.record("implement")
    rec.status = M.RUNNING
    rec.base_sha = gitops.head_sha(repo)
    rec.human_responses.append(HumanResponse(
        response_id="implement-resp-1", response_text="Ratify option 1.",
        timestamp="2026-06-24T00:00:00+00:00", user="fixture@gauntlet.local",
        response_attempt=1, state=M.RESPONSE_PENDING,
    ))
    man.write_atomic(run_dir / "manifest.json")
    (repo / "partial.py").write_text("half-written before the kill")  # dirty base


def test_recovery_dirty_base_reset_relaunches_pending(tmp_path):
    # FR-7.1: a dirty-base crash under reset_to_base snapshots the partial work,
    # resets, and re-launches the still-pending response cleanly to one consumed
    # entry — never re-appended, never double-counted.
    repo, mgr = _build_repo(
        tmp_path / "repo", PIPELINE_SOLO,
        config=CONFIG + "interrupted_step: reset_to_base\n",
    )
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    _seed_dirty_running_pending(repo, mgr)

    adapter = ScriptedAdapter("proceed")
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED
    assert rec.attempts == 0
    assert not (repo / "partial.py").exists()  # partial work discarded
    assert len(adapter.prompts) == 1  # one logical re-execution


def test_recovery_dirty_base_reset_preserves_response_checkpoints(tmp_path):
    # FR-2.2/FR-7.1 / review F-001: a real pending checkpoint commit precedes a
    # dirty-base crash. Recovery under reset_to_base rewinds the worktree to the
    # implementation baseline (base_sha), which sits BEHIND that engine
    # bookkeeping commit — so the reset must not leave the pending (or the later
    # consumed) checkpoint unreachable. Both must remain ancestors of HEAD.
    repo, mgr = _build_repo(
        tmp_path / "repo", PIPELINE_SOLO,
        config=CONFIG + "interrupted_step: reset_to_base\n",
    )
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)

    # Seed the realistic sequence a crash leaves: base_sha is the pre-agent
    # baseline (HEAD before the pending checkpoint), the manifest carries a real
    # *committed* pending checkpoint on top of it, the record is RUNNING, and the
    # worktree is dirty mid-edit.
    run_dir = _run_dir(mgr)
    man = Manifest.load(run_dir / "manifest.json")
    rec = man.record("implement")
    rec.base_sha = gitops.head_sha(repo)  # implementation baseline, pre-checkpoint
    rec.status = M.RUNNING
    rec.human_responses.append(HumanResponse(
        response_id="implement-resp-1", response_text="Ratify option 1.",
        timestamp="2026-06-24T00:00:00+00:00", user="fixture@gauntlet.local",
        response_attempt=1, state=M.RESPONSE_PENDING,
    ))
    man.write_atomic(run_dir / "manifest.json")
    # A real pending checkpoint commit on top of base_sha (the state a crash
    # AFTER the pending flush but mid-edit would have on disk).
    run_rel = run_dir.resolve().relative_to(repo.resolve()).as_posix()
    gitops.commit_run_bookkeeping(
        repo, "gauntlet: response implement-resp-1 pending",
        [f"{run_rel}/manifest.json"], identity=ENGINE_IDENTITY,
    )
    pending_sha = gitops.head_sha(repo)
    assert pending_sha != rec.base_sha  # the checkpoint really advanced HEAD
    (repo / "partial.py").write_text("half-written before the kill")  # dirty base

    adapter = ScriptedAdapter("proceed")
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED
    assert rec.attempts == 0
    assert not (repo / "partial.py").exists()  # partial work discarded
    assert len(adapter.prompts) == 1  # one logical re-execution

    # Both checkpoints are reachable from HEAD, in order — the reset did not drop
    # the pending checkpoint that base_sha sits behind (F-001).
    assert _checkpoint_log(repo) == [
        "Gauntlet Engine|gauntlet: response implement-resp-1 pending",
        "Gauntlet Engine|gauntlet: response implement-resp-1 consumed",
    ]


def _seed_dirty_running_with_committed_pending(repo: Path, mgr: RunManager) -> str:
    """Realistic dirty-base-with-checkpoint crash state: base_sha is the pre-agent
    implementation baseline, a real *committed* pending checkpoint sits on top of
    it, the record is RUNNING, and the worktree is dirty mid-edit. Returns the
    pending checkpoint sha."""
    run_dir = _run_dir(mgr)
    man = Manifest.load(run_dir / "manifest.json")
    rec = man.record("implement")
    rec.base_sha = gitops.head_sha(repo)  # implementation baseline, pre-checkpoint
    rec.status = M.RUNNING
    rec.human_responses.append(HumanResponse(
        response_id="implement-resp-1", response_text="Ratify option 1.",
        timestamp="2026-06-24T00:00:00+00:00", user="fixture@gauntlet.local",
        response_attempt=1, state=M.RESPONSE_PENDING,
    ))
    man.write_atomic(run_dir / "manifest.json")
    run_rel = run_dir.resolve().relative_to(repo.resolve()).as_posix()
    gitops.commit_run_bookkeeping(
        repo, "gauntlet: response implement-resp-1 pending",
        [f"{run_rel}/manifest.json"], identity=ENGINE_IDENTITY,
    )
    pending_sha = gitops.head_sha(repo)
    assert pending_sha != rec.base_sha  # the checkpoint really advanced HEAD
    (repo / "partial.py").write_text("half-written before the kill")  # dirty base
    return pending_sha


def test_recovery_dirty_base_reset_survives_crash_in_rewind_window(tmp_path, monkeypatch):
    # FR-2.2/FR-7.1 / review F-001: inject a kill -9 in the dirty-base recovery
    # window — right after the implementation tree is rewound, BEFORE the manifest
    # is re-persisted/reconciled. The pending response must still be on disk after
    # the crash (the rewind's reset target carried the manifest, so it was never
    # momentarily deleted), and a subsequent process must recover it to exactly
    # one consumed entry — never lost, never re-appended, never double-counted.
    repo, mgr = _build_repo(
        tmp_path / "repo", PIPELINE_SOLO,
        config=CONFIG + "interrupted_step: reset_to_base\n",
    )
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    _seed_dirty_running_with_committed_pending(repo, mgr)

    # `clean_untracked` runs immediately after the rewind reset and before the
    # final persist/reconcile — patch it to die there, simulating the kill in the
    # exact F-001 window (with the old reset-to-base code this is the instant the
    # response was unrecoverable: manifest deleted from disk, checkpoint orphaned).
    def _crash(*a, **k):
        raise RuntimeError("simulated kill -9 mid-rewind")
    monkeypatch.setattr(gitops, "clean_untracked", _crash)

    with pytest.raises(RuntimeError, match="simulated kill -9"):
        mgr.resume(
            "demo", use_judge=False,
            adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
        )

    # Durability: despite the crash, the pending response is still on disk.
    crashed = Manifest.load(_run_dir(mgr) / "manifest.json").record("implement")
    assert len(crashed.human_responses) == 1
    assert crashed.human_responses[0].state == M.RESPONSE_PENDING

    # A subsequent process recovers it cleanly.
    monkeypatch.undo()
    adapter = ScriptedAdapter("proceed")
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("implement")
    assert len(rec.human_responses) == 1  # NOT re-appended
    assert rec.human_responses[0].state == M.RESPONSE_CONSUMED
    assert rec.attempts == 0
    assert not (repo / "partial.py").exists()  # partial work discarded
    assert len(adapter.prompts) == 1  # one logical re-execution
    assert _checkpoint_log(repo) == [
        "Gauntlet Engine|gauntlet: response implement-resp-1 pending",
        "Gauntlet Engine|gauntlet: response implement-resp-1 consumed",
    ]


def test_recovery_dirty_base_park_keeps_pending(tmp_path):
    # FR-7.1: under the park policy a dirty-base crash leaves the step
    # INTERRUPTED with the response STILL pending for a human to reconcile — the
    # response is neither lost nor consumed.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)  # CONFIG defaults to park
    _seed_dirty_running_pending(repo, mgr)

    adapter = ScriptedAdapter("proceed")
    status = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert status == M.RUN_PARKED
    rec = mgr.status("demo").record("implement")
    assert rec.status == M.INTERRUPTED
    assert len(rec.human_responses) == 1
    assert rec.human_responses[0].state == M.RESPONSE_PENDING  # not consumed
    assert adapter.prompts == []  # never re-ran over the dirty tree
    assert rec.attempts == 0


# --- FR-4: chronological human-response.md prompt injection -----------------
def test_render_human_responses_block_format(tmp_path):
    # FR-4 block format, asserted on the pure renderer (no resume needed).
    from gauntlet.engine.steptypes import render_human_responses

    responses = [
        HumanResponse(
            response_id="implement-resp-1", response_text="first",
            timestamp="2026-06-24T00:00:01+00:00", user="a@b.c",
            response_attempt=1, state=M.RESPONSE_CONSUMED,
        ),
        HumanResponse(
            response_id="implement-resp-2", response_text="second",
            timestamp="2026-06-24T00:00:02+00:00", user="a@b.c",
            response_attempt=2, state=M.RESPONSE_PENDING,
        ),
    ]
    rendered = render_human_responses(responses)
    assert rendered == (
        "# Human decisions (chronological)\n\n"
        "## Response implement-resp-1 — attempt 1\n"
        "Response: first\n"
        "Timestamp: 2026-06-24T00:00:01+00:00\n"
        "User: a@b.c\n\n"
        "## Response implement-resp-2 — attempt 2\n"
        "Response: second\n"
        "Timestamp: 2026-06-24T00:00:02+00:00\n"
        "User: a@b.c\n"
    )



def _history_artifact(mgr: RunManager) -> Path:
    """The regenerated render input under the (gitignored) step log dir."""
    return _run_dir(mgr) / "steps" / "implement" / "human-response.md"


def test_response_injected_as_single_artifact_block(tmp_path):
    # FR-4: the builder receives the decision via the EXISTING input-artifact
    # path as one `--- input artifact: human-response.md ---` block.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = ScriptedAdapter("proceed")
    mgr.resume(
        "demo", response="Ratify option 1.", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    prompt = adapter.prompts[-1]
    assert prompt.count("--- input artifact: human-response.md ---") == 1
    assert "# Human decisions (chronological)" in prompt
    assert "## Response implement-resp-1 — attempt 1" in prompt
    assert "Response: Ratify option 1." in prompt
    assert "User: fixture@gauntlet.local" in prompt
    # The artifact is written to the gitignored render area, not committed.
    assert _history_artifact(mgr).exists()


def test_credential_shaped_response_reaches_builder_verbatim(tmp_path):
    # FR-1 / review F-001: a human response containing credential-shaped text
    # must reach the builder EXACTLY as recorded — never as a redaction
    # placeholder. The on-disk copy is redacted for the audit trail, but the
    # invocation prompt carries the verbatim original. The token is assembled at
    # runtime from an obviously-synthetic body (no real-looking secret literal in
    # the source) yet still trips the openai-style-key fallback regex.
    secret = "sk-" + ("z" * 36)  # matches r"\bsk-[A-Za-z0-9_-]{20,}\b"
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    adapter = ScriptedAdapter("proceed")
    mgr.resume(
        "demo", response=f"Proceed using {secret} for the call.",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    prompt = adapter.prompts[-1]
    # The builder sees the verbatim token, not a placeholder.
    assert secret in prompt
    assert "[REDACTED" not in prompt
    # The on-disk render copy IS redacted (audit trail).
    on_disk = _history_artifact(mgr).read_text()
    assert secret not in on_disk
    assert "[REDACTED:openai-style-key]" in on_disk


def test_response_history_accumulates_chronologically(tmp_path):
    # FR-4: repeated resumes regenerate ONE file holding the full ordered
    # history, oldest first — not one differently-named file per response.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    mgr.resume(
        "demo", response="first decision", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("conflict"), clock=_clock(),
    )
    adapter = ScriptedAdapter("proceed")
    mgr.resume(
        "demo", response="second decision", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    prompt = adapter.prompts[-1]
    assert prompt.count("--- input artifact: human-response.md ---") == 1
    # both responses present, oldest first (chronological)
    assert prompt.index("implement-resp-1") < prompt.index("implement-resp-2")
    assert "Response: first decision" in prompt
    assert "Response: second decision" in prompt
    assert "## Response implement-resp-1 — attempt 1" in prompt
    assert "## Response implement-resp-2 — attempt 2" in prompt


def test_injection_does_not_mutate_definition_or_persist_artifact(tmp_path):
    # FR-4/FR-4.1: the synthetic artifact is invocation-local — the pipeline
    # definition, the persisted run-copy pipeline, the step's `inputs`, and
    # manifest.json are all unchanged; the file has no manifest entry.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    run_dir = _run_dir(mgr)
    src_pipeline = (repo / "pipelines" / "respond.yaml").read_bytes()
    run_pipeline = (run_dir / "pipeline.yaml").read_bytes()

    mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("proceed"), clock=_clock(),
    )

    # No byte changed in either pipeline definition.
    assert (repo / "pipelines" / "respond.yaml").read_bytes() == src_pipeline
    assert (run_dir / "pipeline.yaml").read_bytes() == run_pipeline
    # The implement step's persisted `inputs:` never gained the synthetic name.
    from gauntlet.engine.pipeline import load_pipeline
    pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
    implement = next(
        s for stage in pipeline.stages for s in stage.steps if s.id == "implement"
    )
    assert "human-response.md" not in (implement.get("inputs", []) or [])
    # The manifest never records the derived render input as an artifact.
    assert "human-response.md" not in (run_dir / "manifest.json").read_text()


def test_artifact_regenerated_from_manifest_when_stale_copy_gone(tmp_path):
    # FR-4.1: the file is fully derived from `human_responses`; a missing/stale
    # on-disk copy is rebuilt from the manifest on the next resume.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    mgr.resume(
        "demo", response="first", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("conflict"), clock=_clock(),
    )
    art = _history_artifact(mgr)
    assert art.exists()
    art.unlink()  # simulate a vanished render input between resumes

    adapter = ScriptedAdapter("proceed")
    mgr.resume(
        "demo", response="second", use_judge=False,
        adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert art.exists()  # regenerated from the durable array, not relied upon
    prompt = adapter.prompts[-1]
    assert "Response: first" in prompt
    assert "Response: second" in prompt


def test_first_run_has_no_human_response_block(tmp_path):
    # FR-4: an ordinary run with no recorded responses injects no block.
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    adapter = ScriptedAdapter("conflict")
    mgr.start(
        "demo", repo / "pipelines" / "respond.yaml",
        use_judge=False, adapter_factory=lambda n: adapter, clock=_clock(),
    )
    assert "human-response.md" not in adapter.prompts[-1]
    assert not _history_artifact(mgr).exists()


def test_manifest_is_human_readable_json(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo", PIPELINE_SOLO)
    _drive_to_conflict(repo, mgr, PIPELINE_SOLO)
    mgr.resume(
        "demo", response="proceed", use_judge=False,
        adapter_factory=lambda n: ScriptedAdapter("conflict"), clock=_clock(),
    )
    raw = (_run_dir(mgr) / "manifest.json").read_text()
    data = json.loads(raw)  # structure, not binary
    entry = data["steps"][0]["human_responses"][0]
    assert set(entry) == {
        "response_id", "response_text", "timestamp", "user",
        "response_attempt", "state",
    }

"""P5 — recover artifact and infrastructure failures (plan §5.1/§5.2/§6 P5).

Deterministic layers over real throwaway Git repositories, extending the P4
agreement/no-progress harnesses (test_recovery_unification):

* **Outcome taxonomy (plan §4.1/§6)** — the orthogonal cause/disposition pair
  is stamped on every terminal/parked outcome, refines the planner's coarse
  state map, and a pre-P5 manifest (no fields) classifies exactly as before.
* **artifact_invalid replay (issue #64 class, plan §5.1)** — invalid YAML and
  an invalid plan phase ID park artifact_invalid with the artifact path,
  validator, diagnostic, and content fingerprint recorded; a file edit
  recovers via plain resume; an UNCHANGED file exits nonzero
  (NoProgressError) — never a successful no-op loop; and the run is never
  left RUN_RUNNING after a parser exception.
* **Dependency retries + provider_unavailable park (issue #63, plan §5.2)** —
  simulated timeout / connection / 5xx / overload get bounded retries with
  PERSISTED attempt counts (a crash between retries does not reset the
  budget), then park provider_unavailable with a concrete backoff deadline;
  plain resume continues WITHOUT --response; the deadline exempts the park
  from the no-progress guard and a deadline-less park raises.
* **Fan-out leaf persistence (issue #63, plan §5.2)** — a failing triage
  leaf's evidence lands in THAT leaf's directory, `gauntlet logs` resolves
  the failing leaf (not a successful sibling), and resume retries ONLY the
  incomplete leaves.
* **Status/resume agreement over the new cause classes (R4/R7)** — the
  rendered action is exactly what resume does; every dead-driver row keeps a
  mutating action; no retry path consumes or recommends `--response`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeAdapter, git

from gauntlet.adapters.base import (
    FAILURE_TERMINAL,
    FAILURE_TRANSIENT_DEPENDENCY,
    FAILURE_TRANSIENT_OVERLOAD,
    FAILURE_TRANSIENT_USAGE_LIMIT,
    AgentFailedError,
    AgentResult,
    AgentTimeoutError,
    FailureInfo,
)
from gauntlet.engine import depretry, gitops, manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine import recovery_exec as RX
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.recovery import (
    NoProgressError,
    RecoveryCause,
    RecoveryDisposition,
)
from gauntlet.engine.run import RunManager


# =============================================================================
# Harness: a real repo + RunManager run, mirroring test_recovery_unification
# =============================================================================

CONFIG = """
base_branch: main
run_root: runs
interrupted_step: park
dependency_retry_attempts: 2
dependency_retry_base_s: 0.01
dependency_retry_max_delay_s: 5.0
agents:
  builder: {adapter: claude-code}
"""

AGENT_PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""

LINT_PIPELINE = """
name: demo
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-lint, type: phase_lint, artifact: plan.md}
"""

FOREACH_PIPELINE = """
name: demo
version: 1
stages:
  - id: phases
    foreach: plan.phases
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""

VALID_PLAN = (
    "# Plan\n\n"
    "## P1 — Build it\nImplement the widget end-to-end.\n\n"
    "```gauntlet-phases\n"
    "- id: P1\n  title: Build it\n  goal: Implement the widget.\n"
    "  acceptance:\n"
    "    - id: P1-A1\n      clause: The widget builds end-to-end.\n"
    "```\n"
)

# The §7 artifact-defect dimensions: (defect id, plan text, diagnostic marker).
ARTIFACT_DEFECTS = {
    "invalid_yaml": (
        "# Plan\n\n```gauntlet-phases\n- id: P1\n  title: [broken\n```\n",
        "not valid YAML",
    ),
    "invalid_phase_id": (
        # The literal issue-#64-class defect: `Phase 1` where `P1` is required.
        "# Plan\n\n```gauntlet-phases\n"
        "- id: Phase 1\n  title: Build\n  goal: Do it.\n```\n",
        "phase ids must match",
    ),
}


def _seed(repo: Path, pipeline: str, *, plan: str | None = None,
          steps: list[StepRecord] | None = None,
          run_status: str = M.RUN_RUNNING):
    """A fresh runnable shape: config on main, run branch checked out, one
    durable run dir + manifest, optionally a plan.md in the slug dir."""
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed config")
    git(repo, "checkout", "-qb", "gauntlet/demo")

    slug_dir = repo / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")
    (run_dir / "pipeline.yaml").write_text(pipeline)
    (slug_dir / ".gitignore").write_text(".gitignore\nactive-run.txt\n")
    (slug_dir / "active-run.txt").write_text("run-1")
    if plan is not None:
        (slug_dir / "plan.md").write_text(plan)
    _, phash = load_pipeline(run_dir / "pipeline.yaml")
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash=phash),
        status=run_status, steps=list(steps or []),
    )
    man.write_atomic(run_dir / "manifest.json")
    return RunManager(repo), man, run_dir


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Dependency-retry waits never sleep in tests; delays are recorded."""
    waits: list[float] = []
    monkeypatch.setattr(depretry, "_sleep", waits.append)
    return waits


class DependencyAdapter:
    """Raises a classified dependency failure ``fail_times`` times, then
    succeeds (or fails forever with ``fail_times=None``)."""

    name = "fake"
    capabilities = FakeAdapter.capabilities
    timeout_s = 600.0

    def __init__(self, kind: str, *, fail_times: int | None = None,
                 retry_after_s: int | None = None, marker: str = "api_timeout"):
        self.kind = kind
        self.fail_times = fail_times
        self.retry_after_s = retry_after_s
        self.marker = marker
        self.calls: list[dict] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls.append({"prompt": prompt, "session": session})
        if self.fail_times is None or len(self.calls) <= self.fail_times:
            raise AgentFailedError(
                f"api call failed: simulated {self.kind}",
                partial=AgentResult(text="", session_id="s1", exit_code=1),
                failure_info=FailureInfo(
                    kind=self.kind, marker=self.marker,
                    retry_after_s=self.retry_after_s,
                ),
            )
        return AgentResult(text="done", session_id="s1", exit_code=0)


def _status_view(repo: Path, run_dir: Path):
    man = Manifest.load(run_dir / "manifest.json")
    liveness = op.driver_liveness(repo / "runs", "demo")
    assessment = op.compute_status_assessment(
        repo, man, liveness, run_instance_dir=run_dir
    )
    return op.compute_run_state(man, liveness, assessment=assessment), man


# =============================================================================
# Outcome taxonomy (plan §4.1 / §6 P5)
# =============================================================================


@pytest.mark.parametrize(
    "status, kwargs, expected",
    [
        (M.PARKED, {"parked_reason": M.PARKED_REASON_USAGE_LIMIT},
         ("quota_exhausted", "retry")),
        (M.PARKED, {"parked_reason": M.PARKED_REASON_USAGE_WINDOW},
         ("quota_exhausted", "retry")),
        (M.PARKED, {"parked_reason": M.PARKED_REASON_PROVIDER_UNAVAILABLE},
         ("provider_unavailable", "retry")),
        (M.PARKED, {"parked_reason": M.PARKED_REASON_ARTIFACT_INVALID},
         ("artifact_invalid", "edit_then_retry")),
        (M.PARKED, {"parked_reason": M.PARKED_REASON_RESPONSE},
         ("none", "human_decision")),
        (M.PARKED, {"parked_reason": M.PARKED_REASON_GATE},
         ("none", "human_decision")),
        (M.FAILED, {"halt_reason": M.HALT_REASON_ADAPTER_ERROR},
         ("internal_error", "human_decision")),
        (M.FAILED, {"halt_reason": M.HALT_REASON_JUDGE_DENY},
         ("policy_denied", "human_decision")),
        (M.FAILED, {"failure_kind": M.FAILURE_KIND_CLEAN_HANDOFF},
         ("precondition_unsatisfied", "retry")),
        (M.FAILED, {"failure_kind": M.FAILURE_KIND_SIDE_EFFECT_FREE},
         ("internal_error", "retry")),
        (M.HALTED, {"halt_reason": M.HALT_REASON_TIMEOUT},
         ("provider_unavailable", "retry")),
        (M.HALTED, {"halt_reason": M.HALT_REASON_BUDGET},
         ("policy_denied", "retry")),
        (M.HALTED, {"halt_reason": M.HALT_REASON_PRECONDITION},
         ("precondition_unsatisfied", "retry")),
        (M.INTERRUPTED, {}, ("process_lost", "retry")),
        (M.DONE, {}, (None, None)),
        (M.SKIPPED, {}, (None, None)),
        # An unrecognized reason records nothing (fail closed to the coarse map).
        (M.PARKED, {"parked_reason": "mystery"}, (None, None)),
        (M.HALTED, {"halt_reason": "mystery"}, (None, None)),
    ],
)
def test_outcome_classification_table(status, kwargs, expected):
    """The total (status, reasons) → (cause, disposition) map (plan §6 P5)."""
    assert RX.outcome_classification(status, **kwargs) == expected
    cause, disposition = expected
    # Every recorded value is a member of the closed P1 enums.
    if cause is not None:
        RecoveryCause(cause)
    if disposition is not None:
        RecoveryDisposition(disposition)


def test_pre_p5_manifest_loads_and_classifies_as_before(fixture_repo):
    """Plan §8: a manifest with NO cause/disposition/dependency fields loads
    unchanged (nullable, never required) and the planner classifies it from
    the coarse state map exactly as P4 did."""
    raw = {
        "run_id": "run-1", "slug": "demo", "branch": "gauntlet/demo",
        "base_branch": "main",
        "pipeline": {"name": "p", "version": 1, "hash": "h"},
        "status": "parked",
        "steps": [{
            "id": "implement", "type": "agent_task", "status": "parked",
            "parked_reason": "usage_limit", "started": "t0", "ended": "t1",
        }],
    }
    man = Manifest.model_validate(raw)
    rec = man.steps[0]
    assert rec.recovery_cause is None
    assert rec.recovery_disposition is None
    assert rec.dependency_attempts == 0
    state, parked, _failure = RX.classify_composite(man, "none")
    assert state == RX.STATE_PARKED_USAGE_LIMIT
    fp = RX.build_progress_fingerprint(fixture_repo, manifest=man, record=rec)
    assessment = RX.RecoveryPlanner(fixture_repo).assess(
        manifest=man, liveness="none", git_obs=None, fingerprint=fp,
    )
    assert assessment.cause is RecoveryCause.QUOTA_EXHAUSTED  # coarse map
    assert assessment.disposition is RecoveryDisposition.RETRY
    assert not any("recorded outcome classification" in e for e in assessment.evidence)


@pytest.mark.parametrize(
    "record_kwargs, expected_cause",
    [
        # judge_deny FAILED: coarse map says internal_error; the recorded pair
        # refines it to policy_denied.
        (
            {"status": M.FAILED, "halt_reason": M.HALT_REASON_JUDGE_DENY,
             "recovery_cause": "policy_denied",
             "recovery_disposition": "human_decision"},
            RecoveryCause.POLICY_DENIED,
        ),
        # An unrecognized persisted value is ignored — coarse map retained.
        (
            {"status": M.FAILED, "halt_reason": M.HALT_REASON_ADAPTER_ERROR,
             "recovery_cause": "not-a-cause",
             "recovery_disposition": "human_decision"},
            RecoveryCause.INTERNAL_ERROR,
        ),
    ],
)
def test_assess_refines_cause_from_recorded_evidence(
    fixture_repo, record_kwargs, expected_cause
):
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/none", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_FAILED,
        steps=[StepRecord(id="cyc", type="adversarial_cycle",
                          started="t0", ended="t1", **record_kwargs)],
    )
    fp = RX.build_progress_fingerprint(
        fixture_repo, manifest=man, record=man.steps[0]
    )
    assessment = RX.RecoveryPlanner(fixture_repo).assess(
        manifest=man, liveness="none", git_obs=None, fingerprint=fp,
    )
    assert assessment.cause is expected_cause


# =============================================================================
# Issue #64 class replay: artifact_invalid end-to-end (plan §5.1)
# =============================================================================


@pytest.mark.parametrize("site", ["phase_lint", "drive_foreach"])
@pytest.mark.parametrize(
    "defect", sorted(ARTIFACT_DEFECTS), ids=sorted(ARTIFACT_DEFECTS)
)
def test_artifact_defect_parks_edit_recovers_unchanged_raises(
    fixture_repo, site, defect
):
    """The full §5.1 acceptance, per validator site × defect class: the defect
    parks artifact_invalid (artifact + validator + diagnostic + fingerprint
    recorded; run NEVER left RUN_RUNNING); an UNCHANGED plain resume exits
    nonzero via NoProgressError; a fixed file recovers via plain resume with
    no --response and no `--reset`/git surgery."""
    plan_text, diagnostic_marker = ARTIFACT_DEFECTS[defect]
    pipeline = LINT_PIPELINE if site == "phase_lint" else FOREACH_PIPELINE
    step_id = "plan-lint" if site == "phase_lint" else "implement"
    validator = "phase_lint" if site == "phase_lint" else "plan_phases"
    mgr, man, run_dir = _seed(fixture_repo, pipeline, plan=plan_text)

    adapter = FakeAdapter(writes={"clean.py": "out\n"})
    first = mgr.resume("demo", use_judge=False, adapter_factory=lambda n: adapter)
    assert first == M.RUN_PARKED
    final = Manifest.load(run_dir / "manifest.json")
    assert final.status == M.RUN_PARKED  # never RUN_RUNNING after the parse
    rec = final.record(step_id)
    assert rec is not None and rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID
    assert rec.recovery_cause == "artifact_invalid"
    assert rec.recovery_disposition == "edit_then_retry"
    reval = rec.revalidation
    assert reval is not None
    assert reval.validator == validator
    assert "plan.md" in reval.artifact
    assert reval.hash_at_park.startswith("sha256:")
    assert diagnostic_marker in (reval.diagnostic or "")
    assert adapter.calls == []  # no agent ran for an artifact defect

    # Status classifies the park and renders a plain resume — no --response.
    rstate, _ = _status_view(fixture_repo, run_dir)
    assert rstate.state == op.STATE_PARKED_ARTIFACT_INVALID
    mutating = [a for a in rstate.next_actions if a.kind != "observe"]
    assert mutating and mutating[0].argv[:3] == ["gauntlet", "resume", "demo"]
    assert not any("--response" in a.command for a in rstate.next_actions)

    # UNCHANGED artifact: the repeat resume must exit nonzero, never a
    # successful no-op re-park loop (R5; the issue-#64 "zero-cost loop").
    with pytest.raises(NoProgressError) as exc:
        mgr.resume("demo", use_judge=False, adapter_factory=lambda n: adapter)
    assert "sha256:" in str(exc.value)  # names the unchanged fingerprint
    assert adapter.calls == []  # still no agent invocation

    # Hand-edit (sanctioned) → plain resume re-runs ONLY the validator and
    # continues to completion.
    (fixture_repo / "runs" / "demo" / "plan.md").write_text(VALID_PLAN)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_DONE
    done = Manifest.load(run_dir / "manifest.json")
    assert done.status == M.RUN_DONE
    if site == "drive_foreach":
        assert adapter.calls  # the phases fan-out actually ran the agent
        # P5.1 review F-004: the post-approval hand-edit is adopted LOUDLY —
        # a durable manifest warning retains BOTH content fingerprints
        # (park → adoption) as the governance audit trail, never silently.
        note = next(
            w for w in done.warnings
            if "hand-edited while parked artifact_invalid" in w
        )
        assert "APPROVED" in note and reval.hash_at_park in note
        assert "SANCTIONED" in note and "->" in note


def test_changed_but_still_invalid_artifact_reparks_exit_clean(fixture_repo):
    """An edit that is still invalid is PROGRESS (the fingerprint moved): the
    resume re-parks exit-clean with refreshed evidence instead of raising."""
    plan_text, _ = ARTIFACT_DEFECTS["invalid_yaml"]
    mgr, man, run_dir = _seed(fixture_repo, LINT_PIPELINE, plan=plan_text)
    adapter = FakeAdapter()
    assert mgr.resume("demo", use_judge=False,
                      adapter_factory=lambda n: adapter) == M.RUN_PARKED
    hash1 = Manifest.load(run_dir / "manifest.json").record(
        "plan-lint").revalidation.hash_at_park
    # A different-but-still-broken edit registers as progress, not a loop.
    (fixture_repo / "runs" / "demo" / "plan.md").write_text(
        ARTIFACT_DEFECTS["invalid_phase_id"][0]
    )
    assert mgr.resume("demo", use_judge=False,
                      adapter_factory=lambda n: adapter) == M.RUN_PARKED
    hash2 = Manifest.load(run_dir / "manifest.json").record(
        "plan-lint").revalidation.hash_at_park
    assert hash1 != hash2  # the hand-edit is auditable in the record


# =============================================================================
# Issue #63 replay: dependency retries + provider_unavailable park (plan §5.2)
# =============================================================================

# The §7 failure-dimension matrix: kind × marker for the simulated envelopes.
DEPENDENCY_MATRIX = {
    "timeout": (FAILURE_TRANSIENT_DEPENDENCY, "api_timeout", None),
    "connection_dns": (FAILURE_TRANSIENT_DEPENDENCY, "api_connection", None),
    "overload_5xx": (FAILURE_TRANSIENT_OVERLOAD, "api_overload", None),
    "overload_retry_after": (FAILURE_TRANSIENT_OVERLOAD, "api_overload", 3),
    # Issue #96: a codex transport 503 surfaced through the agent's own error
    # event ("Reconnecting... 2/5 (unexpected status 503 ...)") classifies to
    # this marker and must take the SAME persisted retry budget +
    # provider_unavailable park path as the transport-layer shapes — never a
    # terminal halt that forces `--response`.
    "codex_agent_error_503": (
        FAILURE_TRANSIENT_DEPENDENCY, "codex_provider_unavailable_message", None
    ),
    # Issue #119 live Codex shapes: final model-capacity verdict and the
    # pre-event shared models-cache schema fatal take this same persisted budget.
    "codex_capacity": (
        FAILURE_TRANSIENT_OVERLOAD, "codex_capacity_message", None
    ),
    "codex_models_cache_schema": (
        FAILURE_TRANSIENT_DEPENDENCY,
        "codex_models_cache_schema_startup",
        None,
    ),
}


@pytest.mark.parametrize(
    "row", sorted(DEPENDENCY_MATRIX), ids=sorted(DEPENDENCY_MATRIX)
)
def test_dependency_failure_retries_then_succeeds(
    fixture_repo, row, _no_retry_sleep
):
    """A transient dependency failure is retried in-process with backoff and
    the step completes — no park, no human, no --response (issue #63)."""
    kind, marker, retry_after = DEPENDENCY_MATRIX[row]
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = DependencyAdapter(
        kind, fail_times=2, retry_after_s=retry_after, marker=marker
    )
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_DONE
    assert len(adapter.calls) == 3  # initial + the 2 configured retries
    assert len(_no_retry_sleep) == 2 and all(d > 0 for d in _no_retry_sleep)
    if retry_after is not None:
        assert all(d >= retry_after for d in _no_retry_sleep)  # Retry-After floor
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.DONE
    assert rec.dependency_attempts == 0  # episode ended; budget reset


@pytest.mark.parametrize(
    "row", sorted(DEPENDENCY_MATRIX), ids=sorted(DEPENDENCY_MATRIX)
)
def test_dependency_exhaustion_parks_with_deadline_and_plain_resume(
    fixture_repo, row
):
    """Exhausted retries park provider_unavailable with the persisted attempt
    count and a concrete deadline; the repeat plain resume retries (exit
    clean under the deadline exemption) and a recovered provider completes
    the run — no --response anywhere (R7)."""
    kind, marker, retry_after = DEPENDENCY_MATRIX[row]
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = DependencyAdapter(
        kind, fail_times=None, retry_after_s=retry_after, marker=marker
    )
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_PARKED
    assert len(adapter.calls) == 3  # initial + 2 retries, then the park
    final = Manifest.load(run_dir / "manifest.json")
    rec = final.record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert rec.dependency_attempts == 2  # the persisted, exhausted budget
    assert rec.quota_reset_at is not None  # the concrete deadline (F-006)
    assert rec.recovery_cause == "provider_unavailable"
    assert rec.recovery_disposition == "retry"
    # retry_after_s stays STRUCTURED-only: present iff the envelope carried it.
    assert rec.retry_after_s == retry_after
    # The note names the plain-resume exit and explicitly disclaims --response.
    assert "no `--response` decision is at stake" in (rec.notes or "")
    assert "resume" in (rec.notes or "")

    # Status agreement (R4/R7): the state renders, the action is a plain
    # executable resume, and --response is nowhere.
    rstate, _ = _status_view(fixture_repo, run_dir)
    assert rstate.state == op.STATE_PARKED_PROVIDER_UNAVAILABLE
    assert rstate.parked is not None
    assert rstate.parked.reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    mutating = [a for a in rstate.next_actions if a.kind != "observe"]
    assert mutating, "dead-driver provider park must keep a mutating action (R1)"
    assert mutating[0].argv[:3] == ["gauntlet", "resume", "demo"]
    assert mutating[0].executable
    assert not any("--response" in a.command for a in rstate.next_actions)

    # The recorded deadline makes the still-failing repeat a legitimate wait:
    # it re-parks exit-clean instead of raising (F-006 exemption).
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_PARKED

    # Provider recovers → the SAME rendered plain resume completes the run.
    recovered = DependencyAdapter(kind, fail_times=0, marker=marker)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: recovered
    ) == M.RUN_DONE
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.dependency_attempts == 0 and rec.quota_reset_at is None


def test_crash_between_retries_does_not_reset_the_budget(fixture_repo):
    """The persisted count survives a crash mid-episode: a step killed with
    dependency_attempts already consumed resumes with the REMAINING budget,
    not a fresh one (plan §5.2)."""
    steps = [StepRecord(
        id="implement", type="agent_task", agent="builder",
        status=M.RUNNING, started="t0", dependency_attempts=1,
    )]
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE, steps=steps)
    adapter = DependencyAdapter(FAILURE_TRANSIENT_DEPENDENCY, fail_times=None)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_PARKED
    # Budget 2, 1 already consumed pre-crash → exactly ONE further retry:
    # initial call + 1 retry = 2 calls, never 3.
    assert len(adapter.calls) == 2
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.dependency_attempts == 2


def test_plain_resume_of_provider_park_starts_a_fresh_episode(fixture_repo):
    """A deliberate resume of the park resets the budget: the new episode gets
    the full bounded retry allowance again."""
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = DependencyAdapter(FAILURE_TRANSIENT_DEPENDENCY, fail_times=None)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_PARKED
    again = DependencyAdapter(FAILURE_TRANSIENT_DEPENDENCY, fail_times=None)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: again
    ) == M.RUN_PARKED
    assert len(again.calls) == 3  # full fresh budget: initial + 2 retries


def test_deadline_less_exhausted_park_raises_no_progress(fixture_repo):
    """P4.1 F-006 extended to the new park class: an unchanged
    provider_unavailable park with NO recorded deadline and no armed schedule
    is indistinguishable from a wedge — the no-progress guard raises."""
    steps = [StepRecord(
        id="implement", type="agent_task", agent="builder", status=M.PARKED,
        parked_reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE,
        started="t0", ended="t1", dependency_attempts=2,
    )]
    mgr, man, run_dir = _seed(
        fixture_repo, AGENT_PIPELINE, steps=steps, run_status=M.RUN_PARKED
    )
    layout = mgr.layout("demo")
    before = mgr._capture_progress("demo")
    assert before is not None
    with pytest.raises(NoProgressError):
        mgr._require_progress_after("demo", before, verb="resume")
    # The same shape WITH a deadline is a legitimate wait — no raise.
    man2 = Manifest.load(run_dir / "manifest.json")
    man2.steps[0].quota_reset_at = "2099-01-01T00:00:00+00:00"
    man2.write_atomic(run_dir / "manifest.json")
    before2 = mgr._capture_progress("demo")
    mgr._require_progress_after("demo", before2, verb="resume")


def test_usage_limit_park_keeps_the_existing_immediate_park_path(fixture_repo):
    """A 429/quota failure keeps the FR-3.2 contract unchanged: an immediate
    usage_limit park (no in-process hot retries — its persisted budget is the
    FR-3.4 schedule), session preserved, plain resume continues."""
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = DependencyAdapter(
        FAILURE_TRANSIENT_USAGE_LIMIT, fail_times=None,
        retry_after_s=1234, marker="api_rate_limit",
    )
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_PARKED
    assert len(adapter.calls) == 1  # parked immediately, never hot-retried
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.parked_reason == M.PARKED_REASON_USAGE_LIMIT
    assert rec.recovery_cause == "quota_exhausted"
    assert rec.quota_reset_at is not None


# =============================================================================
# Unknown adapter failures: side-effect assessment, not exception-name policy
# =============================================================================


class UnknownFailureAdapter:
    """An UNCLASSIFIED terminal failure, optionally dirtying the worktree."""

    name = "fake"
    capabilities = FakeAdapter.capabilities
    timeout_s = 600.0

    def __init__(self, *, writes: dict[str, str] | None = None,
                 fail_times: int | None = None):
        self.writes = writes or {}
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls.append({"prompt": prompt})
        if self.fail_times is None or len(self.calls) <= self.fail_times:
            for rel, content in self.writes.items():
                (Path(cwd) / rel).write_text(content)
            raise AgentFailedError(
                "mystery explosion",
                partial=AgentResult(text="", exit_code=1),
                failure_info=FailureInfo(kind=FAILURE_TERMINAL, marker="unmatched"),
            )
        return AgentResult(text="done", exit_code=0)


def test_unknown_failure_without_side_effects_is_plain_resumable(fixture_repo):
    """Plan §5.2: retryability comes from the side-effect assessment, not the
    exception name. A clean-tree unknown failure records
    failure_kind=side_effect_free_unknown; status renders plain resume (never
    --response), resume retries it, and a recovered call completes."""
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = UnknownFailureAdapter(fail_times=None)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_FAILED
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.FAILED
    assert rec.failure_kind == M.FAILURE_KIND_SIDE_EFFECT_FREE
    assert rec.recovery_cause == "internal_error"
    assert rec.recovery_disposition == "retry"
    assert "no Git/worktree side effects" in (rec.notes or "")

    rstate, _ = _status_view(fixture_repo, run_dir)
    assert rstate.state == op.STATE_FAILED
    mutating = [a for a in rstate.next_actions if a.kind != "observe"]
    assert [a.argv[:3] for a in mutating] == [["gauntlet", "resume", "demo"]]
    assert not any("--response" in a.command for a in rstate.next_actions)

    recovered = UnknownFailureAdapter(fail_times=0)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: recovered
    ) == M.RUN_DONE


def test_unknown_failure_with_only_engine_bookkeeping_is_plain_resumable(
    fixture_repo,
):
    """Issue #96 (2): an attempt whose only Git/worktree change is the
    engine's OWN bookkeeping — a `gauntlet:` manifest checkpoint commit plus
    the re-projected manifest.json on disk — is side-effect-free. The
    assessment must record failure_kind=side_effect_free_unknown (plain
    resume retries) instead of "left Git/worktree changes" steering the
    operator toward snapshot verbs for the engine's own state."""
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)

    class BookkeepingThenFailAdapter(UnknownFailureAdapter):
        def run(self, prompt, **kwargs):
            # Simulate the engine's own mid-attempt bookkeeping: a
            # force-tracked manifest checkpoint commit (FR-2.2 shape) and a
            # later on-disk re-projection that outruns the last flush.
            gitops.commit_run_bookkeeping(
                fixture_repo, "gauntlet: response implement-resp-1 pending",
                ["runs/demo/run-1/manifest.json"],
                identity=gitops.ENGINE_IDENTITY,
            )
            (run_dir / "manifest.json").write_text(
                (run_dir / "manifest.json").read_text() + "\n"
            )
            return super().run(prompt, **kwargs)

    adapter = BookkeepingThenFailAdapter(fail_times=None)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_FAILED
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.failure_kind == M.FAILURE_KIND_SIDE_EFFECT_FREE
    assert "no Git/worktree side effects" in (rec.notes or "")
    assert "left Git/worktree changes" not in (rec.notes or "")
    # The plain resume the note promises actually re-runs the step.
    recovered = UnknownFailureAdapter(fail_times=0)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: recovered
    ) == M.RUN_DONE


def test_unknown_failure_with_side_effects_stays_terminal(fixture_repo):
    """A side-effecting unknown failure stays terminal: no blind re-run; the
    refusal names --response/abort, and the evidence names the assessment."""
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = UnknownFailureAdapter(writes={"partial.py": "half\n"})
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_FAILED
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.failure_kind is None  # NOT re-runnable
    assert "left Git/worktree changes" in (rec.notes or "")
    with pytest.raises(ValueError, match="terminal"):
        mgr.resume("demo", use_judge=False, adapter_factory=lambda n: adapter)


# =============================================================================
# Issue #63 replay: fan-out leaf evidence + incomplete-leaf-only resume
# =============================================================================


def _cycle_fixture(fixture_repo):
    """The cycle harness from test_cycle, seeded for a 3-finding triage round."""
    import shutil
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]
    shutil.copytree(repo_root / "schemas", fixture_repo / "schemas")
    (fixture_repo / "prd.md").write_text("ARTIFACT-BODY-SENTINEL\n")
    subprocess.run(["git", "-C", str(fixture_repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(fixture_repo), "commit", "-qm", "seed"], check=True
    )
    return fixture_repo


def _run_cycle(repo, adapters, *, manifest=None, dependency_retry_attempts=0):
    import yaml as _yaml

    from gauntlet.engine.config import RunConfig
    from gauntlet.engine.orchestrator import Orchestrator
    from gauntlet.engine.pipeline import Pipeline

    config = {
        "triage_concurrency": 1,
        # Default 0 = exhaust immediately: park on first hit. The in-process
        # fan-out retry test raises it to exercise the retry branch.
        "dependency_retry_attempts": dependency_retry_attempts,
        "agents": {
            "reviewer": {"adapter": "codex"},
            "triage": {"adapter": "api", "model": "h"},
            "builder": {"adapter": "claude-code"},
        },
        "identities": {
            "builder": {"name": "Gauntlet Builder", "email": "builder@g.local"},
        },
    }
    pipeline = Pipeline.model_validate({
        "name": "demo", "version": 1,
        "stages": [{"id": "s", "steps": [{
            "id": "cycle", "type": "adversarial_cycle", "mode": "artifact",
            "artifact": "prd.md", "phase": "P5", "reviewer": "reviewer",
            "triager": "triage", "fixer": "builder", "max_rounds": 2,
        }]}],
    })
    man = manifest or Manifest(
        run_id="r", slug="demo", branch="b", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="h"),
    )
    run_dir = repo / "runs" / "demo" / "run-1"
    orch = Orchestrator(
        repo_root=repo, run_dir=run_dir, artifact_root=repo,
        config=RunConfig.model_validate(config), pipeline=pipeline,
        manifest=man, adapter_factory=lambda n: adapters[n],
    )
    return orch.drive(), man, run_dir


def test_failing_fanout_leaf_evidence_logs_and_incomplete_only_resume(
    fixture_repo,
):
    """The end-to-end issue-#63 replay: a timed-out triage leaf (F-002) parks
    the cycle provider_unavailable; the failure evidence lands in F-002's OWN
    leaf dir; `gauntlet logs` default-leaf selection resolves F-002 (not the
    successful lexicographically-greater sibling F-003); and the plain resume
    re-triages ONLY F-002 — the completed leaves' effects provably not
    re-executed."""
    from test_cycle import CONFIRM, CV, F, REVIEW, SeqAdapter, V, writer

    repo = _cycle_fixture(fixture_repo)
    timeout_exc = AgentFailedError(
        "api call failed: Timeout: litellm.Timeout: APITimeoutError - "
        "Request timed out.",
        partial=AgentResult(text="", exit_code=1),
        failure_info=FailureInfo(
            kind=FAILURE_TRANSIENT_DEPENDENCY, marker="api_timeout"
        ),
    )
    reviewer = SeqAdapter(REVIEW(F("F-001"), F("F-002"), F("F-003")))
    triager = SeqAdapter(V("F-001"), timeout_exc, V("F-003"))
    adapters = {"reviewer": reviewer, "triage": triager,
                "builder": SeqAdapter()}
    status, man, run_dir = _run_cycle(repo, adapters)
    assert status == M.RUN_PARKED
    rec = man.record("cycle")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_PROVIDER_UNAVAILABLE
    assert rec.parked_substep == "r1-triage"
    assert rec.quota_reset_at is not None  # the concrete deadline

    # The write-ahead fragment persisted the COMPLETED leaves (FR-9.2) —
    # F-001 and F-003 decided, F-002 pending.
    frag = json.loads(
        (run_dir / "artifacts" / "r1" / "triage-fragment.json").read_text()
    )
    assert frag["pending"] == ["F-002"]
    assert sorted(v["finding_id"] for v in frag["verdicts"]) == ["F-001", "F-003"]

    # The FAILING leaf's directory carries the evidence (issue #63: it used to
    # be empty, and logs surfaced a successful sibling).
    leaf = run_dir / "steps" / "cycle" / "r1-triage" / "F-002"
    assert (leaf / "failure.json").is_file()
    assert (leaf / "events.jsonl").is_file()
    events = [json.loads(line) for line in
              (leaf / "events.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "gauntlet.failure" for e in events)
    failure = json.loads((leaf / "failure.json").read_text())
    assert failure["failure_kind"] == FAILURE_TRANSIENT_DEPENDENCY
    assert "Request timed out" in failure["error"]
    assert (leaf / "transcript.md").is_file()

    # `gauntlet logs` default-leaf selection resolves the FAILING leaf, not
    # the successful lexicographically-greater sibling (F-003).
    resolved = op.resolve_transcript_dir(run_dir, rec)
    assert resolved == leaf

    # Plain resume (no --response): only the incomplete leaf re-runs.
    resumed_triager = SeqAdapter(V("F-002"))
    resumed_reviewer = SeqAdapter(CONFIRM(CV("F-001"), CV("F-002"), CV("F-003")))
    fixer = SeqAdapter(writer("src.py", "fixed\n", {"done": True}))
    status2, man2, _ = _run_cycle(
        repo,
        {"reviewer": resumed_reviewer, "triage": resumed_triager,
         "builder": fixer},
        manifest=man,
    )
    assert status2 == M.RUN_DONE
    assert len(resumed_triager.calls) == 1  # ONLY the incomplete leaf
    assert "F-002" in resumed_triager.calls[0]["prompt"]
    assert len(resumed_reviewer.calls) == 1  # review reused; only confirm ran
    triage = json.loads((run_dir / "artifacts" / "triage.json").read_text())
    assert sorted(v["finding_id"] for v in triage["verdicts"]) == [
        "F-001", "F-002", "F-003",
    ]
    # A recovered leaf no longer reads as failing (marker cleared).
    assert not (leaf / "failure.json").exists()
    assert op.resolve_transcript_dir(run_dir, man2.record("cycle")) != leaf


def test_fanout_leaf_transient_timeout_retries_in_process_and_completes(
    fixture_repo, _no_retry_sleep
):
    """The retry half of issue #63 on its OWN code path: the bounded
    in-process retry branch at the fan-out call site (cycle.py). A triage
    leaf's transient timeout consumes one persisted retry, waits a real
    backoff delay, and the SAME leaf re-runs in-process — the cycle completes
    with no park, no human, and no re-run of the completed sibling leaves."""
    from test_cycle import CONFIRM, CV, F, REVIEW, SeqAdapter, V, writer

    repo = _cycle_fixture(fixture_repo)
    timeout_exc = AgentFailedError(
        "api call failed: Timeout: litellm.Timeout: APITimeoutError - "
        "Request timed out.",
        partial=AgentResult(text="", exit_code=1),
        failure_info=FailureInfo(
            kind=FAILURE_TRANSIENT_DEPENDENCY, marker="api_timeout"
        ),
    )
    reviewer = SeqAdapter(
        REVIEW(F("F-001"), F("F-002"), F("F-003")),
        CONFIRM(CV("F-001"), CV("F-002"), CV("F-003")),
    )
    triager = SeqAdapter(V("F-001"), timeout_exc, V("F-002"), V("F-003"))
    fixer = SeqAdapter(writer("src.py", "fixed\n", {"done": True}))
    status, man, run_dir = _run_cycle(
        repo,
        {"reviewer": reviewer, "triage": triager, "builder": fixer},
        dependency_retry_attempts=2,
    )
    assert status == M.RUN_DONE  # no park, no --response, no operator page
    assert len(triager.calls) == 4  # 3 leaves + exactly one in-process retry
    assert "F-002" in triager.calls[1]["prompt"]  # the leaf that timed out...
    assert "F-002" in triager.calls[2]["prompt"]  # ...is the leaf that retried
    assert len(_no_retry_sleep) == 1 and _no_retry_sleep[0] > 0  # real backoff
    triage = json.loads((run_dir / "artifacts" / "triage.json").read_text())
    assert sorted(v["finding_id"] for v in triage["verdicts"]) == [
        "F-001", "F-002", "F-003",
    ]
    rec = man.record("cycle")
    assert rec.status == M.DONE
    # A leaf recovered by an in-process retry never reads as failing.
    leaf = run_dir / "steps" / "cycle" / "r1-triage" / "F-002"
    assert not (leaf / "failure.json").exists()


def test_dependency_retry_policy_defaults_are_the_shipped_fix():
    """The shipped defaults ARE the #63 fix for real runs: every behavioral
    test sets the knobs explicitly, so only this pin fails if the default
    silently reverts to 0/off."""
    from gauntlet.engine.config import RunConfig

    cfg = RunConfig.model_validate({})
    assert cfg.dependency_retry_attempts == 3
    assert cfg.dependency_retry_base_s == 2.0
    assert cfg.dependency_retry_max_delay_s == 30.0


# =============================================================================
# Status/resume agreement extension + destructive-verb boundary (R4, plan §9)
# =============================================================================


@pytest.mark.parametrize(
    "steps, run_status, state, expected_first_action",
    [
        (
            [StepRecord(id="s", type="agent_task", status=M.PARKED,
                        parked_reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE,
                        quota_reset_at="2099-01-01T00:00:00+00:00",
                        started="t0", ended="t1")],
            M.RUN_PARKED, op.STATE_PARKED_PROVIDER_UNAVAILABLE,
            ["gauntlet", "resume", "demo"],
        ),
        (
            [StepRecord(id="s", type="phase_lint", status=M.PARKED,
                        parked_reason=M.PARKED_REASON_ARTIFACT_INVALID,
                        started="t0", ended="t1")],
            M.RUN_PARKED, op.STATE_PARKED_ARTIFACT_INVALID,
            ["gauntlet", "resume", "demo"],
        ),
        (
            [StepRecord(id="s", type="agent_task", status=M.FAILED,
                        failure_kind=M.FAILURE_KIND_SIDE_EFFECT_FREE,
                        started="t0", ended="t1")],
            M.RUN_FAILED, op.STATE_FAILED,
            ["gauntlet", "resume", "demo"],
        ),
    ],
)
def test_new_cause_rows_render_plain_resume_and_keep_r1(
    steps, run_status, state, expected_first_action
):
    """Every new dead-driver cause row: ≥1 executable mutating action (R1),
    the recommendation is a PLAIN resume, and --response never appears —
    the same action the mutating path takes in the e2e tests above."""
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=run_status, steps=steps,
    )
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    assert rstate.state == state
    mutating = [a for a in rstate.next_actions if a.kind != "observe"]
    assert mutating, "dead-driver row lost its mutating action (R1)"
    assert mutating[0].argv[:3] == expected_first_action
    assert mutating[0].executable
    assert not any("--response" in a.command for a in rstate.next_actions)
    # The schema-validated payload emits the new fields additively.
    payload = op.status_payload(
        man,
        op.DriverInfo(op.LIVENESS_NONE, None, None, None),
        rstate,
        None,
        run_root=Path("/tmp/x"),
        run_instance_dir=Path("/tmp/x/demo/run-1"),
    )
    assert {"recovery_cause", "recovery_disposition"} <= set(payload["steps"][0])


def test_provider_park_payload_carries_quota_deadline():
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_PARKED,
        steps=[StepRecord(
            id="s", type="agent_task", status=M.PARKED,
            parked_reason=M.PARKED_REASON_PROVIDER_UNAVAILABLE,
            quota_reset_at="2099-01-01T00:00:00+00:00",
            recovery_cause="provider_unavailable",
            recovery_disposition="retry",
            started="t0", ended="t1",
        )],
    )
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    payload = op.status_payload(
        man,
        op.DriverInfo(op.LIVENESS_NONE, None, None, None),
        rstate,
        None,
        run_root=Path("/tmp/x"),
        run_instance_dir=Path("/tmp/x/demo/run-1"),
    )
    assert payload["state"] == "parked_provider_unavailable"
    assert payload["quota"] == {"reset_at": "2099-01-01T00:00:00+00:00"}
    assert payload["steps"][0]["recovery_cause"] == "provider_unavailable"
    assert payload["steps"][0]["recovery_disposition"] == "retry"


def test_p5_call_sites_stay_free_of_direct_destructive_git_verbs():
    """Extends the P3/P4 static boundary check to every P5 call site: the
    taxonomy, retry, park, and evidence paths are observation + manifest +
    log-file only — every Git mutation stays behind RecoveryExecutor."""
    import inspect

    from gauntlet.engine import cycle as cycle_mod
    from gauntlet.engine import orchestrator as orch_mod
    from gauntlet.engine import steptypes as steptypes_mod
    from gauntlet.logging import transcript as transcript_mod

    for func in (
        RX.outcome_classification,
        depretry.consume_retry,
        depretry.backoff_delay_s,
        depretry.park_deadline_s,
        orch_mod.Orchestrator._agent_failure_result,
        orch_mod.Orchestrator._attempt_side_effects,
        orch_mod.Orchestrator._park_plan_artifact_invalid,
        orch_mod.Orchestrator._reconcile_plan_parse_park,
        orch_mod.Orchestrator._walk_stop_record,
        steptypes_mod._phase_lint_park,
        cycle_mod._log_leaf_failure,
        transcript_mod.StepLogger.log_failure,
        op.resolve_transcript_dir,
    ):
        source = inspect.getsource(func)
        for verb in (
            "gitops.reset_hard(",
            "gitops.clean_untracked(",
            "gitops.rewind_impl_preserving_bookkeeping(",
            "gitops.checkout_branch(",
        ):
            assert verb not in source, (
                f"{func.__qualname__} calls {verb} directly; every rewind "
                "mutation must route through RecoveryExecutor (plan §9)"
            )


# =============================================================================
# P5.1 post-review fixes (F-001, F-002, F-003, F-006)
# =============================================================================


class TimeoutAdapter:
    """Optionally writes partial work, then raises the CLI hard-timeout error."""

    name = "fake"
    capabilities = FakeAdapter.capabilities
    timeout_s = 600.0

    def __init__(self, *, writes: dict[str, str] | None = None):
        self.writes = writes or {}
        self.calls: list[dict] = []

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls.append({"prompt": prompt})
        for rel, content in self.writes.items():
            (Path(cwd) / rel).write_text(content)
        raise AgentTimeoutError(
            "agent timed out after 600s",
            partial=AgentResult(text="", session_id="s1", exit_code=1),
        )


def test_timeout_over_partial_work_parks_interrupted_never_reruns(fixture_repo):
    """F-001: a repo-writing agent killed by its hard timeout MID-EDIT must
    not be blindly re-run over the partial work. The timeout halt re-enters
    through the interruption disposition: the dirty tree parks INTERRUPTED
    (snapshot-backed reset advertised) and the agent is NOT re-invoked."""
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = TimeoutAdapter(writes={"partial.py": "half written\n"})
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_PARKED
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.HALTED and rec.halt_reason == M.HALT_REASON_TIMEOUT

    would_succeed = FakeAdapter()
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: would_succeed
    ) == M.RUN_PARKED
    assert would_succeed.calls == []  # NEVER re-run over the partial work
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.INTERRUPTED  # the F-003 disposition, with its exits
    assert "--reset-interrupted" in (rec.notes or "")
    assert (fixture_repo / "partial.py").exists()  # nothing discarded


def test_clean_timeout_still_reruns_via_plain_resume(fixture_repo):
    """F-001 complement: a timeout that provably left NO partial work keeps
    the existing behavior — plain resume re-runs the step cleanly."""
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = TimeoutAdapter()
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_PARKED
    recovered = FakeAdapter(writes={"clean.py": "out\n"})
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: recovered
    ) == M.RUN_DONE
    assert recovered.calls  # the clean re-run actually happened


def test_repeated_side_effect_free_failure_raises_no_progress(fixture_repo):
    """F-002: the FAILED retry counter is bookkeeping, not progress — a
    deterministic side-effect-free failure repeated unchanged raises
    NoProgressError (nonzero, actions named) instead of minting a fresh
    fingerprint per failure and exiting successfully forever (R5)."""
    mgr, man, run_dir = _seed(fixture_repo, AGENT_PIPELINE)
    adapter = UnknownFailureAdapter(fail_times=None)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_FAILED  # first failure: a real transition (progress)
    with pytest.raises(NoProgressError) as exc:
        mgr.resume("demo", use_judge=False, adapter_factory=lambda n: adapter)
    assert exc.value.safe_actions  # executable retry/abort actions named
    assert len(adapter.calls) == 2  # each resume ran once; the loop then stopped
    rec = Manifest.load(run_dir / "manifest.json").record("implement")
    assert rec.status == M.FAILED
    assert rec.failure_kind == M.FAILURE_KIND_SIDE_EFFECT_FREE


def test_fanout_leaf_completion_is_durable_before_the_pool_drains(
    fixture_repo, monkeypatch
):
    """F-003: each successful leaf's verdict is persisted to the checkpoint
    fragment the moment its future completes — NOT after the whole pool
    drains — so a kill mid-round can lose only in-flight leaves, never a
    completed verdict."""
    from test_cycle import F, REVIEW, SeqAdapter, V

    from gauntlet.engine import cycle as cycle_mod

    snapshots: list[list[str]] = []
    real_persist = cycle_mod._persist_triage_fragment

    def spy(ctx, rnd, findings, done):
        snapshots.append(sorted(done))
        real_persist(ctx, rnd, findings, done)

    monkeypatch.setattr(cycle_mod, "_persist_triage_fragment", spy)
    repo = _cycle_fixture(fixture_repo)
    timeout_exc = AgentFailedError(
        "api call failed: Timeout",
        partial=AgentResult(text="", exit_code=1),
        failure_info=FailureInfo(
            kind=FAILURE_TRANSIENT_DEPENDENCY, marker="api_timeout"
        ),
    )
    reviewer = SeqAdapter(REVIEW(F("F-001"), F("F-002"), F("F-003")))
    triager = SeqAdapter(V("F-001"), timeout_exc, V("F-003"))
    status, man, run_dir = _run_cycle(
        repo, {"reviewer": reviewer, "triage": triager, "builder": SeqAdapter()}
    )
    assert status == M.RUN_PARKED
    # The FIRST persist carried exactly the first completed leaf — durable
    # while its siblings were still pending/in flight.
    assert snapshots[0] == ["F-001"]
    assert snapshots[-1] == ["F-001", "F-003"]


def test_unreadable_plan_at_lint_parks_artifact_invalid_and_recovers(
    fixture_repo,
):
    """F-006: an unreadable/wrong-kind plan.md (here: a directory) at the
    plan gate is a REPAIRABLE artifact defect — it parks artifact_invalid
    (not a terminal abort-only failure), and repairing the file resumes."""
    mgr, man, run_dir = _seed(fixture_repo, LINT_PIPELINE)
    plan_path = fixture_repo / "runs" / "demo" / "plan.md"
    plan_path.mkdir()  # exists, but read_text raises IsADirectoryError
    adapter = FakeAdapter()
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_PARKED
    rec = Manifest.load(run_dir / "manifest.json").record("plan-lint")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID
    assert "unreadable" in (rec.revalidation.diagnostic or "")
    plan_path.rmdir()
    plan_path.write_text(VALID_PLAN)
    assert mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter
    ) == M.RUN_DONE


def test_unreadable_schema_asset_is_a_named_misconfiguration(tmp_path):
    """F-006: a malformed SCHEMA file is a pipeline-asset misconfiguration
    (UnknownValidatorError naming the asset), never an artifact defect —
    editing the artifact cannot repair a broken schema."""
    from gauntlet.engine.validators import UnknownValidatorError, validate_artifact

    (tmp_path / "schemas").mkdir()
    (tmp_path / "schemas" / "bad.json").write_text("{not json")
    with pytest.raises(UnknownValidatorError, match="could not be read/parsed"):
        validate_artifact(
            "schema:schemas/bad.json", "{}", repo_root=tmp_path, asset_root="."
        )


def test_jitter_and_backoff_are_deterministic():
    """The retry delays are replayable: same identity → same delay; the
    Retry-After floor wins upward; the cap bounds the exponent."""
    a = depretry.backoff_delay_s(2, base_s=2.0, cap_s=30.0,
                                 retry_after_s=None, key="run|site|2")
    b = depretry.backoff_delay_s(2, base_s=2.0, cap_s=30.0,
                                 retry_after_s=None, key="run|site|2")
    assert a == b
    assert 4.0 <= a <= 6.0  # base*2 with ≤50% jitter
    assert depretry.backoff_delay_s(
        1, base_s=2.0, cap_s=30.0, retry_after_s=10, key="k"
    ) >= 10.0
    assert depretry.backoff_delay_s(
        10, base_s=2.0, cap_s=30.0, retry_after_s=None, key="k"
    ) <= 30.0

"""P4 — one assessment for status/resume/recover/rollback (plan §4.2/§4.5/§5.3/§5.4).

Four deterministic layers over real throwaway Git repositories:

* **Kill-window boundaries** — a real subprocess run self-signals SIGTERM/
  SIGKILL exactly before/after every orchestrator manifest persist
  (`_crash_child.py boundary:<n>:<when>:<sig>`); every killed state must
  classify recoverably (never `unknown`, never a dead-driver row without a
  mutating action) and a plain resume must converge to exactly one set of
  effects (issue #62 bug 2 + plan §5.3).
* **Status/resume agreement** — a table over branch-relation × lifecycle rows:
  the action the read-only status surface renders is exactly the action resume
  takes (adoption rows) or refuses with (diverged rows) — one assessment, zero
  drift (R4).
* **Branch-ahead adoption** — plain resume reconciles a proven-linear ahead
  range by class (plan §5.4/R6): checkpoint continuation, implementation
  adoption into the manifest, operator adoption as the next attempt's base,
  and the LOUD-but-never-refused governed prd.md/plan.md edit (operator
  direction on the post-P3 F-004 review).
* **No successful no-op loops** — a repeated resume against an unchanged
  deterministic failure raises :class:`NoProgressError` (nonzero at the CLI)
  naming the unchanged fingerprint and executable safe actions; a legitimate
  quota wait and a human-decision park stay exit-clean (R5, plan §4.5).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FakeAdapter, git

from gauntlet.adapters.base import AgentFailedError, AgentResult
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine import recovery_exec as RX
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.pipeline import load_pipeline
from gauntlet.engine.recovery import NoProgressError
from gauntlet.engine.run import RunBranchStateError, RunManager

from test_resume_crash import RecoverAdapter, _build_repo

CHILD = Path(__file__).parent / "_crash_child.py"

CONFIG = """
base_branch: main
run_root: runs
interrupted_step: park
agents:
  builder: {adapter: claude-code}
"""

PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""


def _seed(repo: Path, *, step_status: str = M.RUNNING,
          run_status: str = M.RUN_RUNNING, ended: str | None = None):
    """A killed-driver run shape on a real branch: config committed on main,
    the run branch checked out, one in-flight step with a stamped attempt
    boundary, the manifest + pipeline snapshot durable in the run dir."""
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed config")
    git(repo, "checkout", "-qb", "gauntlet/demo")

    slug_dir = repo / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")  # real layout: self-ignoring
    (run_dir / "pipeline.yaml").write_text(PIPELINE)
    (slug_dir / ".gitignore").write_text(".gitignore\nactive-run.txt\n")
    (slug_dir / "active-run.txt").write_text("run-1")
    (slug_dir / "prd.md").write_text("# PRD\n\nReal human-authored PRD.\n")
    # prd.md is committed at the attempt boundary (the FR-5.1 baseline shape),
    # so the seeded tree is clean vs base and only test-added state is dirt.
    git(repo, "add", "--", "runs/demo/prd.md")
    git(repo, "commit", "-qm", "P0: baseline the PRD")
    base = gitops.head_sha(repo)
    _, phash = load_pipeline(run_dir / "pipeline.yaml")
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash=phash),
        status=run_status, current_step="implement",
        steps=[StepRecord(
            id="implement", type="agent_task", agent="builder",
            status=step_status, base_sha=base, started="t0", ended=ended,
        )],
    )
    man.write_atomic(run_dir / "manifest.json")
    return RunManager(repo), man, base, run_dir


def _commit(repo: Path, subject: str, files: dict[str, str],
            author: tuple[str, str] = ("Human", "h@h.local")) -> str:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git(repo, "add", "--", *files)  # only the named paths, never a sweep
    git(repo, "-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}",
        "commit", "-qm", subject)
    return gitops.head_sha(repo)


def _status_actions(repo: Path, run_dir: Path):
    """The read-only surface: status → assess → render (plan §4.2)."""
    man = Manifest.load(run_dir / "manifest.json")
    liveness = op.driver_liveness(repo / "runs", "demo")  # no lock → none
    assessment = op.compute_status_assessment(
        repo, man, liveness, run_instance_dir=run_dir
    )
    rstate = op.compute_run_state(man, liveness, assessment=assessment)
    return rstate, assessment


def _resume(mgr: RunManager, writes: dict[str, str] | None = None) -> str:
    adapter = FakeAdapter(writes=writes or {"clean.py": "out\n"})
    return mgr.resume("demo", use_judge=False, adapter_factory=lambda n: adapter)


# =============================================================================
# Kill-window boundaries: SIGTERM/SIGKILL at every orchestrator persist
# =============================================================================


def _kill_at_boundary(tmp_path: Path, n: int, when: str, sig: str):
    """One child run killed at persist boundary (n, when) with ``sig``.

    Returns ``(repo, mgr, exhausted)`` where ``exhausted`` means the run
    completed before persist ``n`` fired — the boundary matrix is done.
    """
    # reset_to_base: an uncommitted-dirt kill window recovers by the
    # checkpoint-preserving reset (same policy as the existing kill-9 loop).
    repo, mgr = _build_repo(tmp_path / f"{sig}-{when}-{n}", policy="reset_to_base")
    proc = subprocess.run(
        [sys.executable, str(CHILD), str(repo), "demo",
         f"boundary:{n}:{when}:{sig}"],
        timeout=120, capture_output=True,
    )
    exhausted = (repo / ".crash_boundary_done").exists()
    if exhausted:
        assert proc.returncode == 0, proc.stderr.decode()
    else:
        assert proc.returncode != 0  # the self-signal genuinely killed it
    return repo, mgr, exhausted


@pytest.mark.parametrize("sig", ["kill", "term"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_kill_at_every_persist_boundary_classifies_and_converges(
    tmp_path, sig, when
):
    """Issue #62 bug 2 + plan §5.3, the global acceptance property (§7): at
    EVERY durable persist boundary in a real run, a real SIGKILL/SIGTERM
    leaves a state the classifier reads as recoverable — never ``unknown``,
    never a dead-driver nonterminal row without a mutating next action, and
    never a state whose only remedy is hand-editing manifest.json — and one
    plain resume converges to exactly one set of effects."""
    n = 0
    covered = 0
    while True:
        n += 1
        assert n < 30, "runaway boundary loop — persist count exploded"
        repo, mgr, exhausted = _kill_at_boundary(tmp_path, n, when, sig)
        if exhausted:
            break
        run_dir = mgr.layout("demo").active_run_dir()
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            # Killed before the first durable persist: the run never began
            # durably — there is no state to recover (or corrupt).
            continue
        man = Manifest.load(manifest_path)  # atomic write ⇒ never torn
        liveness = op.driver_liveness(repo / "runs", "demo")
        assert liveness in (op.LIVENESS_ORPHANED, op.LIVENESS_NONE)
        rstate = op.compute_run_state(man, liveness)
        assert rstate.state != op.STATE_UNKNOWN, (
            f"boundary {when}-persist-{n} ({sig}) left an unclassifiable "
            f"state: run={man.status} steps="
            f"{[(s.id, s.status) for s in man.steps]}"
        )
        assert rstate.state != op.STATE_INDETERMINATE
        if rstate.state not in (op.STATE_DONE, op.STATE_ABORTED):
            mutating = [
                a for a in rstate.next_actions if a.kind != "observe"
            ]
            assert mutating, (
                f"dead-driver nonterminal state {rstate.state} at boundary "
                f"{when}-persist-{n} exposes no safe mutating action (R1)"
            )
        # Convergence: one plain resume completes the run with exactly one
        # set of effects — no lost work, no duplicated commit.
        status = mgr.resume(
            "demo", use_judge=False, adapter_factory=lambda a: RecoverAdapter()
        )
        assert status == M.RUN_DONE
        final = mgr.status("demo")
        assert [c.phase for c in final.commits] == ["P1"]
        assert (repo / "feature.py").read_text().endswith("final content\n")
        covered += 1
    assert covered >= 4, f"only {covered} real persist boundaries exercised"


# =============================================================================
# Status/resume agreement over branch-relation × lifecycle rows (R4)
# =============================================================================

# Each row: (relation-shape builder, lifecycle shape) → expectations asserted
# against BOTH surfaces from the one assessment.


def _shape_equal(repo, base):
    return None


def _shape_checkpoint(repo, base):
    return _commit(repo, "P1 wip: milestone", {"milestone.py": "wip\n"},
                   author=("Builder", "b@g.local"))


def _shape_implementation(repo, base):
    return _commit(repo, "P1: land the phase", {"phase.py": "done\n"},
                   author=("Builder", "b@g.local"))


def _shape_operator(repo, base):
    return _commit(repo, "tweak the docs by hand", {"notes.txt": "note\n"})


def _shape_operator_governed(repo, base):
    return _commit(repo, "revise the plan by hand",
                   {"runs/demo/plan.md": "manually amended plan\n"})


def _shape_behind(repo, man, run_dir, base):
    # record a tip in the manifest, then reset the branch behind it
    tip = _commit(repo, "P1: land the phase", {"phase.py": "done\n"},
                  author=("Builder", "b@g.local"))
    man.commits.append(M.CommitRecord(step_id="implement", phase="P1", sha=tip))
    man.steps[0].base_sha = tip
    man.write_atomic(run_dir / "manifest.json")
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-qf", "gauntlet/demo", base)


def _shape_forked(repo, man, run_dir, base):
    # record a boundary the branch tip then does NOT descend from
    recorded = _commit(repo, "P1: land the phase", {"phase.py": "done\n"},
                       author=("Builder", "b@g.local"))
    man.steps[0].base_sha = recorded
    man.write_atomic(run_dir / "manifest.json")
    git(repo, "reset", "-q", "--hard", base)
    _commit(repo, "fork commit", {"forked.py": "other line\n"})


def _shape_missing(repo, man, run_dir, base):
    git(repo, "checkout", "-q", "main")
    git(repo, "branch", "-qD", "gauntlet/demo")


_ADOPT_ROWS = {
    "checkpoint_ahead": (_shape_checkpoint, "adopted checkpoint"),
    "implementation_ahead": (_shape_implementation, "implementation commit"),
    "operator_ahead": (_shape_operator, "adopted operator work"),
    "operator_governed": (_shape_operator_governed, "governed artifact"),
}

_DIVERGED_ROWS = {
    "behind": _shape_behind,
    "forked": _shape_forked,
    "missing": _shape_missing,
}


@pytest.mark.parametrize("step_status", [M.RUNNING, M.INTERRUPTED])
@pytest.mark.parametrize("row", sorted(_ADOPT_ROWS), ids=sorted(_ADOPT_ROWS))
def test_agreement_adoptable_rows_status_renders_what_resume_does(
    fixture_repo, row, step_status
):
    """For every adoptable branch-relation × lifecycle row: status renders a
    plain-resume action whose consequence spells out the adoption, resume
    performs exactly that adoption (loud manifest audit), and the run
    continues — no rollback, no git surgery, no re-park (plan §5.4/R6)."""
    run_status = M.RUN_RUNNING if step_status == M.RUNNING else M.RUN_PARKED
    mgr, man, base, run_dir = _seed(
        fixture_repo, step_status=step_status, run_status=run_status,
        ended=("t1" if step_status == M.INTERRUPTED else None),
    )
    shape, audit_marker = _ADOPT_ROWS[row]
    shape(fixture_repo, base)

    rstate, assessment = _status_actions(fixture_repo, run_dir)
    assert assessment is not None
    mutating = [a for a in rstate.next_actions if a.kind != "observe"]
    assert mutating, f"dead-driver row {row}/{step_status} has no action (R1)"
    resume_rows = [a for a in mutating if a.argv[:3] == ["gauntlet", "resume", "demo"]]
    assert resume_rows and resume_rows[0].executable
    assert "adopt" in (resume_rows[0].consequence or "")

    status = _resume(mgr)
    assert status == M.RUN_DONE  # the exact rendered action, taken — it works
    final = Manifest.load(run_dir / "manifest.json")
    assert any(audit_marker in w for w in final.warnings), final.warnings


@pytest.mark.parametrize("row", sorted(_DIVERGED_ROWS), ids=sorted(_DIVERGED_ROWS))
def test_agreement_diverged_rows_status_renders_what_resume_refuses_with(
    fixture_repo, row
):
    """behind/forked/missing rows: resume refuses, and the refusal names the
    SAME executable recovery-ref actions status renders — never only
    "reconcile manually" (plan §5.4, R4)."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    _DIVERGED_ROWS[row](fixture_repo, man, run_dir, base)

    rstate, assessment = _status_actions(fixture_repo, run_dir)
    assert assessment is not None
    rendered = [a for a in rstate.next_actions if a.kind != "observe"]
    assert rendered, f"diverged row {row} renders no action (R1/plan §5.4)"
    assert any(a.kind == "recover" and a.argv[0] == "git" for a in rendered), (
        f"diverged row {row} offers no recovery-ref action: "
        f"{[a.command for a in rendered]}"
    )

    with pytest.raises(RunBranchStateError) as exc:
        _resume(mgr)
    message = str(exc.value)
    for action in rendered:
        if action.kind == "observe" or action.argv[:2] == ["gauntlet", "abort"]:
            continue
        assert action.command in message, (
            f"status renders {action.command!r} but the resume refusal for "
            f"{row} does not name it:\n{message}"
        )


def test_agreement_base_states_pure_and_assessed_render_identically(
    fixture_repo,
):
    """With NO branch-relation deviation, the assessment-rendered actions are
    exactly the pure table's — the two rendering paths cannot drift (R4)."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    rstate, assessment = _status_actions(fixture_repo, run_dir)
    assert assessment is not None
    pure = op.compute_run_state(
        Manifest.load(run_dir / "manifest.json"),
        op.driver_liveness(fixture_repo / "runs", "demo"),
    )
    assert [a.to_dict() for a in rstate.next_actions] == [
        a.to_dict() for a in pure.next_actions
    ]


# =============================================================================
# Branch-ahead adoption details (plan §5.4/R6, issue #72)
# =============================================================================


def test_builder_killed_after_wip_commit_resumes_without_repark(fixture_repo):
    """The #72 headline: a builder killed after committing a `P<N> wip:`
    checkpoint but before the manifest flush. Plain resume adopts the
    checkpoint as the attempt boundary and CONTINUES — never the old
    INTERRUPTED re-park whose only exits were rollback or git surgery."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    wip = _commit(fixture_repo, "P1 wip: milestone", {"milestone.py": "wip\n"},
                  author=("Builder", "b@g.local"))
    adapter = FakeAdapter(writes={"clean.py": "out\n"})
    status = mgr.resume("demo", use_judge=False, adapter_factory=lambda n: adapter)
    assert status == M.RUN_DONE
    assert adapter.calls  # the step re-ran (from the checkpoint, not a park)
    assert (fixture_repo / "milestone.py").exists()  # committed work survives
    final = Manifest.load(run_dir / "manifest.json")
    note = next(w for w in final.warnings if "adopted checkpoint" in w)
    assert wip[:10] in note and f"{base[:10]}..{wip[:10]}" in note


def test_builder_killed_after_phase_commit_adopts_into_manifest(fixture_repo):
    """implementation_ahead: recognized phase/fix commits are adopted into
    manifest.commits (AdoptCommitsAction semantics) so the audit trail and
    later rollback boundaries see them — no rollback, no git surgery."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    sha = _commit(fixture_repo, "P1: land the phase", {"phase.py": "done\n"},
                  author=("Builder", "b@g.local"))
    assert _resume(mgr) == M.RUN_DONE
    final = Manifest.load(run_dir / "manifest.json")
    assert [(c.phase, c.sha) for c in final.commits] == [("P1", sha)]
    note = next(w for w in final.warnings if "implementation commit" in w)
    assert sha[:10] in note


def test_operator_commit_without_governed_change_becomes_attempt_base(
    fixture_repo,
):
    mgr, man, base, run_dir = _seed(fixture_repo)
    sha = _commit(fixture_repo, "tweak the docs by hand", {"notes.txt": "n\n"})
    assert _resume(mgr) == M.RUN_DONE
    final = Manifest.load(run_dir / "manifest.json")
    note = next(w for w in final.warnings if "adopted operator work" in w)
    assert f"{base[:10]}..{sha[:10]}" in note
    assert final.commits == []  # operator work is not an engine phase commit
    assert (fixture_repo / "notes.txt").exists()  # nothing rewound/discarded


def test_operator_governed_edit_is_loud_never_refused_never_discarded(
    fixture_repo,
):
    """Operator direction on the post-P3 F-004 review: hand-editing and
    committing prd.md/plan.md is a SANCTIONED workflow. Resume adopts the
    commit, surfaces the governed edit loudly as the artifact's own
    gate/response path, and neither refuses nor rewinds it."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    plan = fixture_repo / "runs" / "demo" / "plan.md"
    _commit(fixture_repo, "revise the plan by hand",
            {"runs/demo/plan.md": "manually amended plan\n"})
    assert _resume(mgr) == M.RUN_DONE  # proceeds — never refused
    assert plan.read_text() == "manually amended plan\n"  # never discarded
    final = Manifest.load(run_dir / "manifest.json")
    note = next(
        w for w in final.warnings
        if "governed artifact" in w and "plan.md" in w
    )
    assert "never refused" in note or "SANCTIONED" in note


def test_engine_bookkeeping_ahead_is_tolerated_without_adoption(fixture_repo):
    """Existing behavior unchanged: pure engine-bookkeeping advance is not
    partial work and is not adopted — no warning, no boundary move."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    bk = gitops.commit_run_bookkeeping(
        fixture_repo, "gauntlet: response implement-resp-1 pending",
        ["runs/demo/run-1/manifest.json"], identity=gitops.ENGINE_IDENTITY,
    )
    assert _resume(mgr) == M.RUN_DONE
    final = Manifest.load(run_dir / "manifest.json")
    assert not [w for w in final.warnings if "adopt" in w]


def test_recover_warning_names_adoption_as_a_way_out(fixture_repo):
    """recover's finalization consumes the same observation machinery: its
    branch-ahead warning names the proven relation and that a plain resume
    adopts the range (plan §4.2: recover → finalize → assess)."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    _commit(fixture_repo, "P1 wip: milestone", {"milestone.py": "wip\n"},
            author=("Builder", "b@g.local"))
    from gauntlet.engine.run import _RecoveryIntent, _utc_stamp

    intent = _RecoveryIntent(
        ts=_utc_stamp(), actor="op", actor_source="os_user", reason=None,
        lock_nonce="n1", pid=1, pgid=1, host="h",
        step_id="implement", prior_step_status=M.RUNNING,
        prior_run_status=M.RUN_RUNNING, proc_identity=None,
    )
    assert mgr._finalize_recovery(run_dir, intent, "already_dead") is True
    final = Manifest.load(run_dir / "manifest.json")
    note = next(w for w in final.warnings if "ahead of the recorded" in w)
    assert "checkpoint_ahead" in note
    assert "adopts the range" in note


# =============================================================================
# R5 — no successful no-op loops (plan §4.5)
# =============================================================================


def test_repeated_resume_on_unchanged_interrupted_park_raises(fixture_repo):
    """The #72 loop, closed: the FIRST resume transitions the killed step to
    the interrupted park (progress — it prints the dirty verdict); an
    UNCHANGED repeat raises NoProgressError naming the fingerprint and the
    executable safe actions instead of exiting 0 re-parked."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")  # killed mid-edit

    adapter = FakeAdapter()
    first = mgr.resume("demo", use_judge=False, adapter_factory=lambda n: adapter)
    assert first == M.RUN_PARKED  # transition RUNNING→INTERRUPTED = progress
    assert adapter.calls == []  # parked before any agent work

    with pytest.raises(NoProgressError) as exc:
        mgr.resume("demo", use_judge=False, adapter_factory=lambda n: adapter)
    message = str(exc.value)
    assert "no progress" in message
    assert "sha256:" in message  # names the unchanged fingerprint
    assert "snapshot_and_restart" in message  # lists the sanctioned exit
    assert exc.value.safe_actions  # executable actions, not just prose


def _transient_adapter(retry_after_s=None):
    from gauntlet.adapters.failure_markers import FAILURE_TRANSIENT_USAGE_LIMIT
    from gauntlet.adapters.base import AdapterCapabilities, FailureInfo

    class _A:
        name = "fake"
        capabilities = AdapterCapabilities(
            repo_write=True, structured_output="native", resume=True
        )
        timeout_s = 600.0

        def run(self, prompt, *, session=None, schema=None, cwd=None,
                extra_flags=None):
            raise AgentFailedError(
                "usage limit hit",
                partial=AgentResult(text="", session_id="s1", exit_code=1),
                failure_info=FailureInfo(
                    kind=FAILURE_TRANSIENT_USAGE_LIMIT,
                    marker="claude_usage_limit_message",
                    retry_after_s=retry_after_s,
                ),
            )
    return _A()


def test_quota_park_with_deadline_is_a_legitimate_wait_and_exits_cleanly(
    fixture_repo,
):
    """A usage-limit re-park CARRYING a concrete reset deadline is a provider
    wait, not a no-op loop: the repeat resume returns parked (exit 0) even
    though the fingerprint is unchanged (R5 exemption; FR-3.2/FR-3.3,
    post-review F-006: the deadline is what makes the wait legitimate)."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    first = mgr.resume(
        "demo", use_judge=False,
        adapter_factory=lambda n: _transient_adapter(retry_after_s=1234),
    )
    assert first == M.RUN_PARKED  # RUNNING → usage-limit park (progress)
    second = mgr.resume(
        "demo", use_judge=False,
        adapter_factory=lambda n: _transient_adapter(retry_after_s=1234),
    )
    assert second == M.RUN_PARKED  # unchanged repeat, but a deadline wait
    final = Manifest.load(run_dir / "manifest.json")
    rec = final.record("implement")
    assert rec.parked_reason == M.PARKED_REASON_USAGE_LIMIT
    assert rec.quota_reset_at is not None  # the exemption's evidence


def test_quota_park_without_deadline_raises_on_unchanged_repeat(fixture_repo):
    """Post-review F-006: an unchanged usage-limit park with NO recorded
    reset time and no armed schedule is indistinguishable from a wedge — the
    repeat raises NoProgressError instead of exiting 0 forever."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    first = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: _transient_adapter()
    )
    assert first == M.RUN_PARKED  # the park itself is progress
    final = Manifest.load(run_dir / "manifest.json")
    assert final.record("implement").quota_reset_at is None  # no deadline
    with pytest.raises(NoProgressError):
        mgr.resume(
            "demo", use_judge=False,
            adapter_factory=lambda n: _transient_adapter(),
        )


def test_gate_park_resume_repeat_stays_exit_clean(fixture_repo):
    """A human-decision park is a legitimate wait (R7): resuming a gate-parked
    run repeatedly does not raise — the gate's own verbs are the exit."""
    gated = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: gate, type: human_gate, show: []}
"""
    mgr, man, base, run_dir = _seed(fixture_repo)
    (run_dir / "pipeline.yaml").write_text(gated)
    _, phash = load_pipeline(run_dir / "pipeline.yaml")
    man.pipeline = PipelineRef(name="demo", version=1, hash=phash)
    man.steps = [StepRecord(id="gate", type="human_gate", status=M.PENDING)]
    man.current_step = None
    man.write_atomic(run_dir / "manifest.json")
    assert _resume(mgr) == M.RUN_PARKED  # parks at the gate (progress)
    assert _resume(mgr) == M.RUN_PARKED  # unchanged repeat — human wait, no raise


def test_rollback_repeat_to_same_boundary_raises_no_progress(fixture_repo):
    """R5 on rollback: a second rollback to the same phase boundary changes
    nothing — branch, manifest, index, and worktree identical — and raises
    instead of succeeding as a no-op."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    p1 = _commit(fixture_repo, "P1: land the phase", {"phase.py": "v1\n"},
                 author=("Builder", "b@g.local"))
    p2 = _commit(fixture_repo, "P2: next phase", {"phase2.py": "v2\n"},
                 author=("Builder", "b@g.local"))
    man.commits = [
        M.CommitRecord(step_id="implement", phase="P1", sha=p1),
        M.CommitRecord(step_id="implement", phase="P2", sha=p2),
    ]
    man.steps[0].status = M.DONE
    man.steps[0].base_sha = None
    man.status = M.RUN_PARKED
    man.steps[0].ended = "t1"
    man.write_atomic(run_dir / "manifest.json")

    assert mgr.rollback("demo", phase=1) == p1  # first rollback: progress
    with pytest.raises(NoProgressError):
        mgr.rollback("demo", phase=1)  # identical repeat: a no-op loop


# =============================================================================
# Plan §5.3 — recognized historical kill-window shapes
# =============================================================================


@pytest.mark.parametrize("step_status", [M.FAILED, M.HALTED, M.INTERRUPTED])
@pytest.mark.parametrize(
    "liveness, expected_kind",
    [
        (op.LIVENESS_ALIVE, "in_progress"),
        (op.LIVENESS_ORPHANED, "step_state"),
        (op.LIVENESS_NONE, "step_state"),
        (op.LIVENESS_INDETERMINATE, "indeterminate"),
    ],
)
def test_historical_shape_classifies_by_liveness(step_status, liveness, expected_kind):
    """RUN_RUNNING + exactly one ENDED interrupted/halted/failed step — the
    pre-P4 kill-window persist shape — maps to the corresponding RECOVERABLE
    state by liveness (plan §5.3), never ``unknown``."""
    man = Manifest(
        run_id="run-x", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_RUNNING,
        steps=[StepRecord(id="s", type="agent_task", status=step_status,
                          started="t0", ended="t1")],
    )
    rstate = op.compute_run_state(man, liveness)
    if expected_kind == "in_progress":
        assert rstate.state == op.STATE_IN_PROGRESS
    elif expected_kind == "indeterminate":
        assert rstate.state == op.STATE_INDETERMINATE
    else:
        assert rstate.state == step_status  # failed/halted/interrupted
        assert rstate.failure is not None
        assert rstate.failure.step_id == "s"
        mutating = [a for a in rstate.next_actions if a.kind != "observe"]
        assert mutating, "recognized dead-driver shape must offer an action (R1)"


def test_historical_shape_without_end_timestamp_stays_unknown():
    """The shape requires an ENDED step: a FAILED step with no end timestamp
    under RUN_RUNNING is still a contradiction → unknown, read-only."""
    man = Manifest(
        run_id="run-x", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_RUNNING,
        steps=[StepRecord(id="s", type="agent_task", status=M.FAILED)],
    )
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    assert rstate.state == op.STATE_UNKNOWN
    assert {a.kind for a in rstate.next_actions} == {"observe"}


def test_historical_shape_recovers_through_plain_resume(fixture_repo):
    """A persisted historical kill-window shape (RUN_RUNNING + ended
    INTERRUPTED step + dead driver) recovers end-to-end via plain resume."""
    mgr, man, base, run_dir = _seed(
        fixture_repo, step_status=M.INTERRUPTED, run_status=M.RUN_RUNNING,
        ended="t1",
    )
    rstate, _ = _status_actions(fixture_repo, run_dir)
    assert rstate.state == op.STATE_INTERRUPTED
    assert _resume(mgr) == M.RUN_DONE


# =============================================================================
# The single-write terminalization (issue #62 bug 2) — in-process proof
# =============================================================================


def test_terminal_step_and_run_status_land_in_one_persist(fixture_repo, monkeypatch):
    """Every persisted manifest is coherent: a terminal step status never
    lands in a write that still says RUN_RUNNING (except the recognized
    historical shape, which requires no manifest edit to recover)."""
    from gauntlet.engine.orchestrator import Orchestrator

    observed: list[tuple[str, tuple]] = []
    real = Orchestrator._persist

    def spy(self):
        real(self)
        man = Manifest.load(self.manifest_path)
        observed.append(
            (man.status, tuple((s.id, s.status, bool(s.ended)) for s in man.steps))
        )

    monkeypatch.setattr(Orchestrator, "_persist", spy)
    mgr, man, base, run_dir = _seed(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")  # forces a park
    assert mgr.resume("demo", use_judge=False,
                      adapter_factory=lambda n: FakeAdapter()) == M.RUN_PARKED
    terminal_states = {M.FAILED, M.HALTED, M.INTERRUPTED, M.PARKED}
    for run_status, steps in observed:
        for _sid, status, _ended in steps:
            if status in terminal_states:
                assert run_status != M.RUN_RUNNING, (
                    "a terminal step status was persisted under RUN_RUNNING: "
                    f"{run_status} {steps}"
                )


# =============================================================================
# P4.1 post-review fixes (F-002..F-007)
# =============================================================================


def test_live_driver_gets_no_branch_reconciliation_actions(fixture_repo):
    """F-002: a verifiably ALIVE driver legitimately runs the branch ahead of
    the manifest mid-step. The assessment must not advertise adoption resumes
    or raw ref restores that would race (or bypass the lock of) the live
    process — live and indeterminate rows stay observe-only."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    _commit(fixture_repo, "P1 wip: milestone", {"m.py": "wip\n"},
            author=("Builder", "b@g.local"))
    man2 = Manifest.load(run_dir / "manifest.json")
    for liveness, expected_state in (
        (op.LIVENESS_ALIVE, op.STATE_IN_PROGRESS),
        (op.LIVENESS_INDETERMINATE, op.STATE_INDETERMINATE),
    ):
        assessment = op.compute_status_assessment(
            fixture_repo, man2, liveness, run_instance_dir=run_dir
        )
        assert assessment is not None
        rstate = op.compute_run_state(man2, liveness, assessment=assessment)
        assert rstate.state == expected_state
        assert {a.kind for a in rstate.next_actions} == {"observe"}, (
            f"{liveness}: {[a.command for a in rstate.next_actions]}"
        )
        assert any("advisory only" in e for e in assessment.evidence)


@pytest.mark.parametrize("checked_out", ["run_branch", "other_branch"])
def test_behind_action_executes_and_resume_converges(fixture_repo, checked_out):
    """F-003: the advertised behind-branch restore is genuinely executable
    from BOTH checkout positions — `git merge --ff-only` when standing on the
    run branch (a forced branch move is invalid there), `git branch -f`
    otherwise — and a subsequent plain resume converges."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    tip = _commit(fixture_repo, "P1: land the phase", {"phase.py": "done\n"},
                  author=("Builder", "b@g.local"))
    man.commits.append(M.CommitRecord(step_id="implement", phase="P1", sha=tip))
    man.steps[0].base_sha = tip
    man.write_atomic(run_dir / "manifest.json")
    git(fixture_repo, "checkout", "-q", "main")
    git(fixture_repo, "branch", "-qf", "gauntlet/demo", base)  # now BEHIND
    if checked_out == "run_branch":
        git(fixture_repo, "checkout", "-q", "gauntlet/demo")

    rstate, assessment = _status_actions(fixture_repo, run_dir)
    restore = next(a for a in rstate.next_actions if a.kind == "recover")
    if checked_out == "run_branch":
        assert restore.argv[:3] == ["git", "merge", "--ff-only"]
    else:
        assert restore.argv[:3] == ["git", "branch", "-f"]

    # Execute exactly the advertised argv, then resume to convergence.
    subprocess.run(
        ["git", "-C", str(fixture_repo), *restore.argv[1:]],
        check=True, capture_output=True,
    )
    assert gitops.rev_parse(fixture_repo, "refs/heads/gauntlet/demo") == tip
    assert _resume(mgr) == M.RUN_DONE


def test_missing_branch_action_executes_and_resume_converges(fixture_repo):
    """F-003: the advertised missing-branch restore executes and the next
    plain resume converges."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    git(fixture_repo, "checkout", "-q", "main")
    git(fixture_repo, "branch", "-qD", "gauntlet/demo")
    rstate, _ = _status_actions(fixture_repo, run_dir)
    restore = next(a for a in rstate.next_actions if a.kind == "recover")
    assert restore.argv[:3] == ["git", "branch", "-f"]
    subprocess.run(
        ["git", "-C", str(fixture_repo), *restore.argv[1:]],
        check=True, capture_output=True,
    )
    assert _resume(mgr) == M.RUN_DONE


def test_fork_action_description_matches_its_payload(fixture_repo):
    """F-003: the fork action promises exactly what it does — preserve the
    forked tip on a recovery branch — and names the snapshot-backed verb for
    the run branch itself, never a bare reset."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    _shape_forked(fixture_repo, man, run_dir, base)
    rstate, assessment = _status_actions(fixture_repo, run_dir)
    act = next(
        a for a in assessment.safe_actions
        if a.kind is op.RXM.RecoveryActionKind.CONTINUE_ON_RECOVERY_BRANCH
    )
    assert "creates a ref only" in act.description
    assert "stays forked" in act.description
    assert "rollback" in act.description
    # Executing it preserves the tip and mutates nothing else.
    fork_tip = gitops.head_sha(fixture_repo)
    rendered = next(a for a in rstate.next_actions if a.kind == "recover")
    subprocess.run(
        ["git", "-C", str(fixture_repo), *rendered.argv[1:]],
        check=True, capture_output=True,
    )
    assert gitops.rev_parse(fixture_repo, f"refs/heads/{act.branch_name}") == fork_tip


def test_unobservable_range_renders_fail_closed_like_resume(fixture_repo):
    """F-004: a merge commit inside the inventoried range makes the observer
    refuse. Status must render the SAME fail-closed posture resume takes —
    an evidence-retaining abort, never the mutating base table."""
    mgr, man, base, run_dir = _seed(fixture_repo)
    _commit(fixture_repo, "side work", {"side.py": "x\n"})
    side = gitops.head_sha(fixture_repo)
    git(fixture_repo, "reset", "-q", "--hard", base)
    _commit(fixture_repo, "mainline work", {"mainline.py": "y\n"})
    git(fixture_repo, "merge", "-q", "--no-ff", "-m", "merge the side line", side)

    man2 = Manifest.load(run_dir / "manifest.json")
    assessment = op.compute_status_assessment(
        fixture_repo, man2, op.LIVENESS_NONE, run_instance_dir=run_dir
    )
    assert assessment is not None
    assert assessment.cause is op.RXM.RecoveryCause.STATE_INCONSISTENT
    rstate = op.compute_run_state(man2, op.LIVENESS_NONE, assessment=assessment)
    mutating = [a for a in rstate.next_actions if a.kind != "observe"]
    assert [a.argv[:2] for a in mutating] == [["gauntlet", "abort"]]

    with pytest.raises(RX.RecoveryObservationError):
        _resume(mgr)  # the mutating path refuses on the identical evidence


def test_fingerprint_registers_cycle_checkpoint_and_revalidation_progress():
    """F-005: a new durable cycle sub-step checkpoint, or a revalidation
    record whose content hashes moved (a hand-edited artifact), changes the
    progress fingerprint — commit-less durable progress is never a no-op."""
    import copy

    from gauntlet.engine.recovery import ProgressFingerprint

    base_kwargs = dict(
        run_id="run-1", run_status=op.RXM.RunStatus.PARKED,
        index_fingerprint="sha256:i", worktree_fingerprint="sha256:w",
    )
    plain = ProgressFingerprint(**base_kwargs)
    with_substep = ProgressFingerprint(**base_kwargs, latest_cycle_substep="r2-fix")
    assert plain.digest != with_substep.digest
    a = ProgressFingerprint(**base_kwargs, artifact_fingerprint="sha256:v1")
    b = ProgressFingerprint(**base_kwargs, artifact_fingerprint="sha256:v2")
    assert a.digest != b.digest

    # And build_progress_fingerprint derives both from the step record.
    rec = StepRecord(
        id="cyc", type="adversarial_cycle", status=M.PARKED,
        checkpoints=[M.Checkpoint(sub_step="fix", round=2, handoff_sha="a" * 40)],
        revalidation=M.RevalidationRecord(
            artifact="artifacts/plan.md", hash_at_park="sha256:p",
        ),
    )
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/none", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_PARKED, steps=[rec],
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=F", "-c", "user.email=f@l",
             "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
             "-m", "seed"],
            check=True, capture_output=True,
        )
        # the manifest's branch is absent here → run_branch_sha stays None
        fp1 = RX.build_progress_fingerprint(repo, manifest=man, record=rec)
        assert fp1.latest_cycle_substep == "r2-fix"
        assert fp1.artifact_fingerprint is not None
        rec2 = copy.deepcopy(rec)
        rec2.revalidation.hash_at_resume = "sha256:edited"
        rec2.revalidation.changed_while_parked = True
        fp2 = RX.build_progress_fingerprint(repo, manifest=man, record=rec2)
        assert fp1.digest != fp2.digest  # the hand-edit registers as progress


def test_failed_nonrespondable_step_advertises_abort_not_response(fixture_repo):
    """F-007: a terminally failed step whose type rejects `--response` (a
    shell step) must not advertise it — the executable exit is abort, on both
    surfaces."""
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_FAILED,
        steps=[StepRecord(id="tests", type="shell", status=M.FAILED,
                          started="t0", ended="t1")],
    )
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    assert rstate.state == op.STATE_FAILED
    cmds = [a.command for a in rstate.next_actions]
    assert cmds == ["gauntlet logs demo", "gauntlet abort demo"]
    assert not any("--response" in c for c in cmds)


def test_failed_respondable_step_keeps_response_action():
    """F-007 guard-rail: respondable failed types keep the exact pre-P4.1
    `--response` recommendation."""
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        status=M.RUN_FAILED,
        steps=[StepRecord(id="cyc", type="adversarial_cycle", status=M.FAILED,
                          started="t0", ended="t1")],
    )
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    assert [a.command for a in rstate.next_actions] == [
        "gauntlet logs demo",
        'gauntlet resume demo --response "<your decision>"',
    ]


# =============================================================================
# Plan §9 — the destructive-verb boundary holds for the new P4 call sites
# =============================================================================


def test_p4_call_sites_stay_free_of_direct_destructive_git_verbs():
    """Extends the P3 static check to every P4-introduced call site: the
    unification, adoption, and no-progress paths are observation +
    manifest-only — every Git mutation stays behind RecoveryExecutor."""
    import inspect

    from gauntlet.engine import operator as op_mod
    from gauntlet.engine import run as run_mod

    for func in (
        RX.RecoveryPlanner.assess,
        RX.classify_composite,
        RX.reconcile_branch_ahead,
        RX.relation_recovery_actions,
        op_mod.compute_status_assessment,
        op_mod.render_assessment_actions,
        run_mod.RunManager._observe_resume_branch,
        run_mod.RunManager._relation_action_detail,
        run_mod.RunManager._require_progress_after,
        run_mod.RunManager._record_recovery_reconciliation,
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

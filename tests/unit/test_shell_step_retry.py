"""Shell-step failures get the plan §5.2 side-effect assessment (#121).

A shell step's nonzero exit (a failing test suite, a flaky build) was
previously always terminal: `gauntlet resume` refused with "would only repeat
it" and named `abort` as the only exit — even when the failure was a flake and
the tree was provably untouched. These tests pin the two halves of the fix:

* **Failure-time stamping** (``Orchestrator._assess_shell_failure``) — a shell
  failure whose attempt left the tree provably clean against its ``base_sha``
  records ``failure_kind=side_effect_free_unknown``, so a plain resume re-runs
  it (R7) and a deterministic repeat trips the R5 no-progress guard. A
  side-effecting failure stays terminal exactly as before.
* **Resume-time reclassification** (``RunManager._reclassify_clean_shell_failure``)
  — a FAILED shell record written *before* the stamping existed (``failure_kind``
  None) is assessed at the verb boundary: provably clean → upgraded (audited
  manifest warning) and re-run; dirty → the terminal refusal is unchanged.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import git

from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord

from test_recovery_p5 import _seed


def _shell_pipeline(command: str) -> str:
    return (
        "name: demo\n"
        "version: 1\n"
        "stages:\n"
        "  - id: s\n"
        "    steps:\n"
        f"      - {{id: tests, type: shell, run: {command!r}}}\n"
    )


def test_clean_shell_failure_is_stamped_and_plain_resumable(
    fixture_repo, tmp_path
):
    """A flaky suite (fails once, tree untouched) records the side-effect-free
    kind, and the plain resume the note promises actually re-runs it to DONE."""
    flag = tmp_path / "flake.flag"  # outside the worktree: the tree stays clean
    cmd = f"test -f {flag} && exit 0; touch {flag}; exit 1"
    mgr, man, run_dir = _seed(fixture_repo, _shell_pipeline(cmd))

    assert mgr.resume("demo", use_judge=False) == M.RUN_FAILED
    rec = Manifest.load(run_dir / "manifest.json").record("tests")
    assert rec.status == M.FAILED
    assert rec.failure_kind == M.FAILURE_KIND_SIDE_EFFECT_FREE
    assert "no Git/worktree side effects" in (rec.notes or "")

    assert mgr.resume("demo", use_judge=False) == M.RUN_DONE
    rec = Manifest.load(run_dir / "manifest.json").record("tests")
    assert rec.status == M.DONE


def test_dirty_shell_failure_stays_terminal(fixture_repo):
    """A shell failure that left worktree changes keeps today's behavior:
    terminal, and a plain resume refuses rather than re-running over it."""
    mgr, man, run_dir = _seed(
        fixture_repo, _shell_pipeline("echo dirt > dirt.txt; exit 1")
    )

    assert mgr.resume("demo", use_judge=False) == M.RUN_FAILED
    rec = Manifest.load(run_dir / "manifest.json").record("tests")
    assert rec.failure_kind is None
    assert "left Git/worktree changes" in (rec.notes or "")

    with pytest.raises(ValueError, match="terminal failure"):
        mgr.resume("demo", use_judge=False)


def test_legacy_clean_shell_failure_reclassified_at_resume(fixture_repo):
    """A FAILED shell record with no failure_kind (written before the
    failure-time stamping existed) is assessed at the resume boundary:
    provably clean → upgraded with an audited warning, then re-run to DONE."""
    mgr, man, run_dir = _seed(fixture_repo, _shell_pipeline("exit 0"))
    head = git(fixture_repo, "rev-parse", "HEAD").strip()
    man = Manifest.load(run_dir / "manifest.json")
    man.steps = [StepRecord(
        id="tests", type="shell", status=M.FAILED,
        halt_reason=M.HALT_REASON_ADAPTER_ERROR,
        started="t0", ended="t1", base_sha=head,
        notes="`pnpm test` exited 1",
    )]
    man.status = M.RUN_FAILED
    man.write_atomic(run_dir / "manifest.json")

    assert mgr.resume("demo", use_judge=False) == M.RUN_DONE
    man = Manifest.load(run_dir / "manifest.json")
    assert man.record("tests").status == M.DONE
    assert any(
        "reclassified failed shell step 'tests'" in w for w in man.warnings
    )


def test_legacy_dirty_shell_failure_still_refuses(fixture_repo):
    """The reclassification is evidence-based and fail-closed: a dirty tree
    against the attempt's base_sha leaves the record terminal, and the resume
    refusal is byte-for-byte today's."""
    mgr, man, run_dir = _seed(fixture_repo, _shell_pipeline("exit 0"))
    head = git(fixture_repo, "rev-parse", "HEAD").strip()
    man = Manifest.load(run_dir / "manifest.json")
    man.steps = [StepRecord(
        id="tests", type="shell", status=M.FAILED,
        halt_reason=M.HALT_REASON_ADAPTER_ERROR,
        started="t0", ended="t1", base_sha=head,
        notes="`pnpm test` exited 1",
    )]
    man.status = M.RUN_FAILED
    man.write_atomic(run_dir / "manifest.json")
    (fixture_repo / "partial-edit.txt").write_text("stale partial work\n")

    with pytest.raises(ValueError, match="terminal failure"):
        mgr.resume("demo", use_judge=False)
    man = Manifest.load(run_dir / "manifest.json")
    assert man.record("tests").failure_kind is None
    assert not any("reclassified" in w for w in man.warnings)


# --- #134: a plain resume re-arms an exhausted on_fail route -----------------
def _routed_pipeline(command: str, *, max_retries: int = 0) -> str:
    """`prep` (a shell step that counts its runs) then a failing `tests` step
    routed back to `prep` with the given budget."""
    return (
        "name: demo\n"
        "version: 1\n"
        "stages:\n"
        "  - id: s\n"
        "    steps:\n"
        "      - {id: prep, type: shell, run: 'echo run >> $PREP_LOG'}\n"
        f"      - {{id: tests, type: shell, run: {command!r}, "
        f"on_fail: {{route_to: prep, max_retries: {max_retries}}}}}\n"
    )


def _prep_runs(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def test_plain_resume_rearms_one_route_per_human_action(
    fixture_repo, tmp_path, monkeypatch
):
    log = tmp_path / "prep.log"  # outside the worktree: the tree stays clean
    monkeypatch.setenv("PREP_LOG", str(log))
    mgr, man, run_dir = _seed(fixture_repo, _routed_pipeline("exit 1"))

    # First drive: budget 0 → the first failure (attempts 1 ≤ 0 is false)
    # exhausts at once → FAILED, prep ran once.
    assert mgr.resume("demo", use_judge=False) == M.RUN_FAILED
    rec = Manifest.load(run_dir / "manifest.json").record("tests")
    assert rec.status == M.FAILED and rec.attempts == 1
    assert _prep_runs(log) == 1
    # A plain resume re-arms exactly one more route: prep re-runs, tests fails
    # again, the run surfaces FAILED again — no NoProgressError, no git surgery.
    assert mgr.resume("demo", use_judge=False) == M.RUN_FAILED
    man = Manifest.load(run_dir / "manifest.json")
    assert _prep_runs(log) == 2
    assert man.record("tests").attempts == 2
    rearms = [w for w in man.warnings if w.startswith(mgr.REARM_WARNING_PREFIX)]
    assert len(rearms) == 1 and "re-arm #1" in rearms[0]
    assert "route_to=prep" in rearms[0]
    # Each later plain resume is another human action → another route, k+1.
    assert mgr.resume("demo", use_judge=False) == M.RUN_FAILED
    man = Manifest.load(run_dir / "manifest.json")
    assert _prep_runs(log) == 3
    rearms = [w for w in man.warnings if w.startswith(mgr.REARM_WARNING_PREFIX)]
    assert [("re-arm #1" in w, "re-arm #2" in w) for w in rearms] == [
        (True, False), (False, True)
    ]


def test_rearm_then_success_completes_the_run(fixture_repo, tmp_path, monkeypatch):
    """The re-armed route is a real retry: once the cause is fixed (here, a
    flag the operator sets between resumes) the run completes."""
    log = tmp_path / "prep.log"
    flag = tmp_path / "fixed.flag"
    monkeypatch.setenv("PREP_LOG", str(log))
    mgr, man, run_dir = _seed(fixture_repo, _routed_pipeline(f"test -f {flag}"))
    assert mgr.resume("demo", use_judge=False) == M.RUN_FAILED
    flag.write_text("fixed\n")  # the operator fixes the environment
    assert mgr.resume("demo", use_judge=False) == M.RUN_DONE
    man = Manifest.load(run_dir / "manifest.json")
    assert man.record("tests").status == M.DONE
    assert any(w.startswith(mgr.REARM_WARNING_PREFIX) for w in man.warnings)


def test_no_rearm_inside_budget(fixture_repo, tmp_path, monkeypatch):
    """A step still inside its budget is the orchestrator's to route: the
    verb boundary never re-arms it (no warning), and the budget is honored
    exactly (max_retries=1 → two failures, one route). A step WITHOUT on_fail
    keeps today's refusals (test_dirty_shell_failure_stays_terminal)."""
    log = tmp_path / "prep.log"
    monkeypatch.setenv("PREP_LOG", str(log))
    mgr, man, run_dir = _seed(
        fixture_repo, _routed_pipeline("exit 1", max_retries=1)
    )
    assert mgr.resume("demo", use_judge=False) == M.RUN_FAILED
    man = Manifest.load(run_dir / "manifest.json")
    assert man.record("tests").attempts == 2
    assert _prep_runs(log) == 2
    assert not any(w.startswith(mgr.REARM_WARNING_PREFIX) for w in man.warnings)


def test_standard_pipeline_routes_tests_recheck_back_to_the_cycle():
    """`tests-recheck` now carries an on_fail route to impl-cycle (#134) so a
    post-cycle test failure re-enters the cycle instead of terminating."""
    from pathlib import Path as _P

    from gauntlet.engine.pipeline import load_pipeline

    root = _P(__file__).resolve().parents[2]
    pipeline, _ = load_pipeline(root / "pipelines" / "standard.yaml")
    steps = {s.id: s for s in pipeline.all_steps()}
    assert steps["tests-recheck"].on_fail is not None
    assert steps["tests-recheck"].on_fail.route_to == "impl-cycle"
    assert steps["tests-recheck"].on_fail.max_retries == 1
    assert steps["tests"].on_fail.route_to == "implement"


def test_reset_for_retry_clears_cycle_checkpoints(tmp_path):
    """The route reset (shared by the orchestrator and the re-arm) clears a
    completed cycle's sub-step checkpoints so it re-runs as a fresh round."""
    from gauntlet.engine.orchestrator import Orchestrator
    from gauntlet.engine.pipeline import load_pipeline

    path = tmp_path / "p.yaml"
    path.write_text(
        "name: p\nversion: 1\nstages:\n  - id: s\n    steps:\n"
        "      - {id: cyc, type: shell, run: 'true'}\n"
        "      - {id: tests, type: shell, run: 'true', "
        "on_fail: {route_to: cyc, max_retries: 1}}\n"
    )
    pipeline, _ = load_pipeline(path)
    man = Manifest(
        run_id="r", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        steps=[
            StepRecord(id="cyc", type="adversarial_cycle", status=M.DONE,
                       base_sha="abc", checkpoints=[M.Checkpoint(sub_step="review", round=1, handoff_sha="abc")]),
            StepRecord(id="tests", type="shell", status=M.FAILED, attempts=2),
        ],
    )
    Orchestrator.reset_records_for_retry(man, pipeline.stages[0], "cyc", None)
    assert man.record("cyc").status == M.PENDING
    assert man.record("cyc").checkpoints == []
    assert man.record("cyc").base_sha is None
    assert man.record("tests").status == M.PENDING

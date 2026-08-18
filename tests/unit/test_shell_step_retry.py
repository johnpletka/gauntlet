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
from gauntlet.engine import steptypes
from gauntlet.engine.manifest import Manifest, StepRecord

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
    assert rec.failure_kind == M.FAILURE_KIND_SHELL_EXIT_NONZERO
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


def test_post_command_handler_fault_stays_terminal(
    fixture_repo, tmp_path, monkeypatch
):
    """A successful command followed by a handler fault must not be retried.

    The generic orchestrator catch also uses ``adapter_error``; only the shell
    handler's structured nonzero-exit marker proves the process itself failed.
    """
    marker = tmp_path / "command-ran.txt"
    mgr, _man, run_dir = _seed(
        fixture_repo,
        _shell_pipeline(f"echo ran >> {marker}"),
    )

    def fail_log(*_args, **_kwargs):
        raise OSError("step transcript directory is unwritable")

    monkeypatch.setattr(steptypes, "_write_step_log", fail_log)

    assert mgr.resume("demo", use_judge=False) == M.RUN_FAILED
    rec = Manifest.load(run_dir / "manifest.json").record("tests")
    assert rec.failure_kind is None
    assert "handler error" in (rec.notes or "")
    assert marker.read_text().splitlines() == ["ran"]

    with pytest.raises(ValueError, match="terminal failure"):
        mgr.resume("demo", use_judge=False)
    assert marker.read_text().splitlines() == ["ran"]


def test_legacy_handler_fault_is_not_reclassified(fixture_repo, tmp_path):
    """An old adapter_error without canonical exit evidence fails closed."""
    marker = tmp_path / "must-not-run.txt"
    mgr, man, run_dir = _seed(
        fixture_repo,
        _shell_pipeline(f"touch {marker}"),
    )
    head = git(fixture_repo, "rev-parse", "HEAD").strip()
    man = Manifest.load(run_dir / "manifest.json")
    man.steps = [StepRecord(
        id="tests", type="shell", status=M.FAILED,
        halt_reason=M.HALT_REASON_ADAPTER_ERROR,
        started="t0", ended="t1", base_sha=head,
        notes="handler error: step transcript directory is unwritable",
    )]
    man.status = M.RUN_FAILED
    man.write_atomic(run_dir / "manifest.json")

    with pytest.raises(ValueError, match="terminal failure"):
        mgr.resume("demo", use_judge=False)
    assert not marker.exists()
    man = Manifest.load(run_dir / "manifest.json")
    assert man.record("tests").failure_kind is None
    assert not any("reclassified" in w for w in man.warnings)

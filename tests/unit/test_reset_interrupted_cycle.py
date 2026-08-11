"""Issue #95 — reset/resume of an interrupted adversarial_cycle step.

A `resume --response` driver killed mid-drive (SIGTERM) on a cycle step used
to be unrecoverable through every native verb:

* defect A: `--reset-interrupted` rewound to the RUN BASE — cycle round
  commits (`PRD.n:`/`P<N>.n: Address review`) are not `P<N> wip:` checkpoints,
  so "rewind to the latest committed checkpoint" degenerated to "discard every
  committed round";
* defect B: the reset left the cycle's recorded rewind targets (checkpoint
  handoff/result shas, the response round base) dangling, so the NEXT drive
  burned agent time and died terminal on "rewind target X is not an ancestor";
* defect C: plain `resume` was a 0-exit no-progress loop — the stale base_sha
  read the manifested round commits as dirt, and each re-park's notes embedded
  the moving raw HEAD so the R5 fingerprint never saw the repeat.

These tests pin the fixed behavior: cycle-aware reset targets, the fail-closed
up-front refusal, round-commit adoption at park time, byte-identical re-parks
feeding the R5 guard, and planner/executor target agreement (R4).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from conftest import FakeAdapter, git
from gauntlet.engine import gitops, manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine import recovery_exec as RX
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import (
    HumanResponse,
    Manifest,
    PipelineRef,
    StepRecord,
)
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline, load_pipeline
from gauntlet.engine.recovery import NoProgressError
from gauntlet.engine.run import RunManager

# Reuse the scripted-cycle harness pieces from the sibling cycle test.
from test_cycle import (  # noqa: F401  (cycle_repo is a pytest fixture)
    BASE_CONFIG,
    REVIEW,
    SeqAdapter,
    cycle_repo,
    cycle_step,
)


def _round_commit(repo: Path, n: int, content: str) -> str:
    """One committed cycle fix round (`P5.<n>: Address review — …`)."""
    (repo / "prd.md").write_text(content)
    return gitops.commit_all(
        repo,
        f"P5.{n}: Address review — 1 fixed, 0 declined\n\nRound {n} body.\n",
        identity=gitops.Identity("Gauntlet Builder (claude)", "builder@gauntlet.local"),
    )


def _bookkeeping_commit(repo: Path, subject: str) -> str:
    return gitops.commit_all(
        repo, subject, identity=gitops.ENGINE_IDENTITY, allow_empty=True
    )


def _cycle_orch(repo: Path, man: Manifest, adapters, *, override=None) -> Orchestrator:
    pipeline = Pipeline.model_validate({
        "name": "demo", "version": 1,
        "stages": [{"id": "s", "steps": [cycle_step()]}],
    })
    cfg = RunConfig.model_validate({**BASE_CONFIG, "interrupted_step": "park"})
    orch = Orchestrator(
        repo_root=repo, run_dir=repo / "runs" / "demo" / "run-1",
        artifact_root=repo, config=cfg, pipeline=pipeline, manifest=man,
        adapter_factory=lambda n: adapters[n],
    )
    if override:
        orch.interrupted_override = override
    return orch


def _killed_cycle_manifest(base: str, rounds: list[tuple[str, str]]) -> Manifest:
    """The #95 kill shape: checkpoint-less INTERRUPTED cycle, stale base_sha,
    round commits recorded only in manifest.commits (the `_Resume.invalidate()`
    window of a killed `--response` re-drive)."""
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    man.steps.append(StepRecord(
        id="cycle", type="adversarial_cycle", status=M.INTERRUPTED,
        base_sha=base, halt_reason=M.HALT_REASON_SIGNAL_KILL, started="t0",
    ))
    for phase, sha in rounds:
        man.commits.append(M.CommitRecord(step_id="cycle", phase=phase, sha=sha))
    return man


# --- defect A: reset targets the latest committed round commit -----------------
def test_reset_interrupted_cycle_targets_latest_round_commit(cycle_repo):
    """#95 defect A: `--reset-interrupted` on a cycle step with committed round
    commits rewinds to the LATEST round commit — `P5.1`/`P5.2` stay in history
    (asserted via git ancestry after the reset) — never to the run base."""
    base = gitops.head_sha(cycle_repo)
    r1 = _round_commit(cycle_repo, 1, "ARTIFACT v2 — round 1 fixes\n")
    r2 = _round_commit(cycle_repo, 2, "ARTIFACT v3 — round 2 fixes\n")
    _bookkeeping_commit(cycle_repo, "gauntlet: response cycle-resp-1 pending")
    (cycle_repo / "partial.py").write_text("half written by the killed fixer\n")

    man = _killed_cycle_manifest(base, [("P5.1", r1), ("P5.2", r2)])
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),  # re-run converges with no findings
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    orch = _cycle_orch(cycle_repo, man, adapters, override="reset_to_base")
    assert orch.drive() == M.RUN_DONE

    head = gitops.head_sha(cycle_repo)
    # The committed rounds survived the rewind — the #95 run lost both.
    assert gitops.is_ancestor(cycle_repo, r1, head)
    assert gitops.is_ancestor(cycle_repo, r2, head)
    # The implementation tree was rewound to the LATEST round, not the base.
    assert RX.skip_engine_bookkeeping_commits(cycle_repo, head) == r2
    assert not (cycle_repo / "partial.py").exists()  # partial work discarded…
    refs = gitops._run(cycle_repo, "for-each-ref", "refs/gauntlet/recovery/")
    assert "refs/gauntlet/recovery/" in refs  # …but snapshotted first
    rec = man.record("cycle")
    assert rec.status == M.DONE
    # The audit trail names the round commit the re-run resumed from.
    assert (rec.resumed_from_checkpoint or "").startswith("P5.2: Address review")


# --- defect B: dangling recorded targets refuse the reset up front -------------
def test_reset_refuses_up_front_on_dangling_cycle_rewind_target(cycle_repo):
    """#95 defect B: a reset that would strand a recorded cycle rewind target
    (here: a manifested round commit not reachable from the branch) REFUSES
    before applying — nothing mutated, no snapshot consumed, no agent invoked,
    the run stays parked and drivable — instead of succeeding and letting the
    next drive die terminal mid-cycle."""
    base = gitops.head_sha(cycle_repo)
    r1 = _round_commit(cycle_repo, 1, "ARTIFACT v2 — round 1 fixes\n")
    # A round-2 commit on unrelated history: recorded in the manifest, but not
    # an ancestor of the branch tip (the post-discard #95 shape).
    git(cycle_repo, "checkout", "-qb", "stranded")
    dangling = _round_commit(cycle_repo, 2, "ARTIFACT v3 — stranded round\n")
    git(cycle_repo, "checkout", "-q", "main")
    (cycle_repo / "partial.py").write_text("half written by the killed fixer\n")

    man = _killed_cycle_manifest(base, [("P5.1", r1), ("P5.2", dangling)])
    adapters = {  # empty scripts: ANY agent call raises (must never be reached)
        "reviewer": SeqAdapter(), "triage": SeqAdapter(), "builder": SeqAdapter(),
    }
    orch = _cycle_orch(cycle_repo, man, adapters, override="reset_to_base")
    head_before = gitops.head_sha(cycle_repo)
    assert orch.drive() == M.RUN_PARKED

    rec = man.record("cycle")
    assert rec.status == M.INTERRUPTED
    assert rec.halt_reason == M.HALT_REASON_PRECONDITION
    assert "refused fail-closed" in rec.notes
    assert dangling[:10] in rec.notes  # names the offending sha…
    assert "gauntlet resume demo" in rec.notes  # …and the alternative
    # Nothing was mutated and no snapshot was consumed.
    assert gitops.head_sha(cycle_repo) == head_before
    assert (cycle_repo / "partial.py").exists()
    refs = gitops._run(cycle_repo, "for-each-ref", "refs/gauntlet/recovery/")
    assert refs.strip() == ""
    for adapter in adapters.values():
        assert adapter.calls == []


# --- defect C (adopt-and-proceed): clean tree, manifested rounds ---------------
def test_clean_cycle_resume_adopts_manifested_rounds_and_proceeds(cycle_repo):
    """#95 defect C: a plain resume of the killed-response-drive shape with a
    CLEAN tree adopts the manifested round commits (re-arms the boundary at the
    newest one, loudly) and proceeds to re-drive the cycle — instead of
    insta-re-parking on its own committed work."""
    base = gitops.head_sha(cycle_repo)
    r1 = _round_commit(cycle_repo, 1, "ARTIFACT v2 — round 1 fixes\n")
    r2 = _round_commit(cycle_repo, 2, "ARTIFACT v3 — round 2 fixes\n")
    _bookkeeping_commit(cycle_repo, "gauntlet: response cycle-resp-1 pending")

    man = _killed_cycle_manifest(base, [("P5.1", r1), ("P5.2", r2)])
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),  # the re-drive runs and converges
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    orch = _cycle_orch(cycle_repo, man, adapters)  # policy: park (default)
    assert orch.drive() == M.RUN_DONE
    assert adapters["reviewer"].calls  # re-ran; committed rounds are not dirt
    assert any("adopted committed cycle round" in w for w in man.warnings)
    assert man.record("cycle").status == M.DONE
    head = gitops.head_sha(cycle_repo)
    assert gitops.is_ancestor(cycle_repo, r2, head)  # nothing rewound


# --- defect C (park + R5): the 0-exit no-progress loop is closed ---------------
CYCLE_RUN_CONFIG = """
base_branch: main
run_root: runs
interrupted_step: park
triage_concurrency: 1
agents:
  reviewer: {adapter: codex}
  triage: {adapter: api, model: h}
  builder: {adapter: claude-code}
"""

CYCLE_RUN_PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: cycle, type: adversarial_cycle, mode: artifact, artifact: prd.md,
         phase: PRD, reviewer: reviewer, triager: triage, fixer: builder,
         max_rounds: 2}
"""


def _seed_killed_response_drive(repo: Path):
    """The full #95 run shape under a RunManager: a `--response` driver killed
    mid-drive on a cycle step — pending response, stale base_sha, empty
    checkpoints, committed `PRD.n` rounds + engine bookkeeping on the branch,
    and the fixer's uncommitted partial edit."""
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CYCLE_RUN_CONFIG)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "seed config")
    git(repo, "checkout", "-qb", "gauntlet/demo")

    slug_dir = repo / "runs" / "demo"
    run_dir = slug_dir / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / ".gitignore").write_text("*\n")
    (run_dir / "pipeline.yaml").write_text(CYCLE_RUN_PIPELINE)
    (slug_dir / ".gitignore").write_text(".gitignore\nactive-run.txt\n")
    (slug_dir / "active-run.txt").write_text("run-1")
    (slug_dir / "prd.md").write_text("# PRD\n\nHuman-authored PRD.\n")
    git(repo, "add", "--", "runs/demo/prd.md")
    git(repo, "commit", "-qm", "P0: baseline the PRD")
    base = gitops.head_sha(repo)

    def round_commit(n: int) -> str:
        (slug_dir / "prd.md").write_text(f"# PRD\n\nrevision {n}\n")
        git(repo, "add", "--", "runs/demo/prd.md")
        return gitops.commit_all(
            repo,
            f"PRD.{n}: Address review — {n} fixed, 0 declined\n\nRound {n}.\n",
            identity=gitops.Identity(
                "Gauntlet Builder (claude)", "builder@gauntlet.local"
            ),
        )

    r1 = round_commit(1)
    r2 = round_commit(2)
    _bookkeeping_commit(repo, "gauntlet: response cycle-resp-1 pending")
    (repo / "partial.py").write_text("half written by the killed fixer\n")

    _, phash = load_pipeline(run_dir / "pipeline.yaml")
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash=phash),
        status=M.RUN_RUNNING, current_step="cycle",
        steps=[StepRecord(
            id="cycle", type="adversarial_cycle", status=M.RUNNING,
            base_sha=base, started="t0",
            human_responses=[HumanResponse(
                response_id="cycle-resp-1", response_text="proceed as agreed",
                timestamp="2026-08-09T12:51:15+00:00", user="op@example.com",
                response_attempt=1, state=M.RESPONSE_PENDING,
            )],
        )],
        commits=[
            M.CommitRecord(step_id="cycle", phase="PRD.1", sha=r1),
            M.CommitRecord(step_id="cycle", phase="PRD.2", sha=r2),
        ],
    )
    man.write_atomic(run_dir / "manifest.json")
    return RunManager(repo), run_dir, base, r1, r2


def test_killed_response_drive_park_rearm_and_no_progress_loop(fixture_repo):
    """#95 defect C, end to end: the first plain resume re-arms base_sha to the
    newest manifested round commit and parks on the genuine dirt (progress,
    exit 0); the repeat re-parks BYTE-IDENTICALLY and raises NoProgressError
    (nonzero) instead of exiting 0; a further repeat stamps NO new
    `gauntlet: response … pending` bookkeeping commit."""
    mgr, run_dir, base, r1, r2 = _seed_killed_response_drive(fixture_repo)
    clock = lambda: "2026-08-10T00:00:00+00:00"  # noqa: E731 — deterministic bytes

    adapter = SeqAdapter()  # any agent call raises: the park needs zero work
    first = mgr.resume(
        "demo", use_judge=False, adapter_factory=lambda n: adapter, clock=clock,
    )
    assert first == M.RUN_PARKED
    man = Manifest.load(run_dir / "manifest.json")
    rec = man.record("cycle")
    assert rec.status == M.INTERRUPTED
    assert rec.base_sha == r2  # re-armed at the newest manifested round commit
    assert any("adopted committed cycle round" in w for w in man.warnings)
    # The park verdict is anchored on the re-armed base, and the manifested
    # rounds are no longer called dirt: only the uncommitted edit is named.
    assert "partial.py" in rec.notes
    assert base[:10] not in rec.notes
    notes_after_first = rec.notes

    # The repeat changes nothing → R5 fires (nonzero), never 0-exit re-park.
    with pytest.raises(NoProgressError) as exc:
        mgr.resume(
            "demo", use_judge=False, adapter_factory=lambda n: adapter,
            clock=clock,
        )
    assert "no progress" in str(exc.value)
    man = Manifest.load(run_dir / "manifest.json")
    assert man.record("cycle").notes == notes_after_first  # byte-identical park
    count_after_second = int(
        gitops._run(fixture_repo, "rev-list", "--count", "HEAD").strip()
    )

    # A further repeat raises again AND lands no new bookkeeping commit — the
    # "stamps a fresh `response … pending` commit each time" loop is closed.
    with pytest.raises(NoProgressError):
        mgr.resume(
            "demo", use_judge=False, adapter_factory=lambda n: adapter,
            clock=clock,
        )
    count_after_third = int(
        gitops._run(fixture_repo, "rev-list", "--count", "HEAD").strip()
    )
    assert count_after_third == count_after_second
    assert adapter.calls == []  # no agent work across any of the repeats


# --- defect A/B (R4): the advertised reset target is the executor's ------------
def test_planner_advertises_cycle_reset_target_matching_executor(fixture_repo):
    """#95: `status`/`resume` advertise the SAME reset target the executing
    path computes for a cycle step — the latest committed round commit, not the
    stale rec.base_sha (R4: the read-only view and the mutating path agree)."""
    mgr, run_dir, base, r1, r2 = _seed_killed_response_drive(fixture_repo)
    # The shape an old engine (or the park itself) persists: run parked, step
    # interrupted, boundary still at the stale run base.
    man = Manifest.load(run_dir / "manifest.json")
    man.status = M.RUN_PARKED
    rec = man.record("cycle")
    rec.status = M.INTERRUPTED
    rec.halt_reason = M.HALT_REASON_SIGNAL_KILL
    man.write_atomic(run_dir / "manifest.json")

    liveness = op.driver_liveness(fixture_repo / "runs", "demo")
    assessment = op.compute_status_assessment(
        fixture_repo, man, liveness, run_instance_dir=run_dir
    )
    reset = next(
        a for a in assessment.safe_actions
        if a.kind is RX.RecoveryActionKind.SNAPSHOT_AND_RESTART
    )
    # Honest advertisement: the latest round commit, though base_sha is stale.
    assert reset.target_sha == r2
    assert rec.base_sha == base != r2
    # And it matches what the executor would do (the shared #95 selection).
    assert RX.latest_cycle_round_commit(
        fixture_repo, man, rec, tip=gitops.head_sha(fixture_repo)
    ) == r2


# --- fix 4: the driver SIGTERM flush -------------------------------------------
def test_drive_installs_sigterm_flush_that_persists_and_rethrows(
    fixture_repo, monkeypatch
):
    """#95 fix 4: during a drive SIGTERM is routed through a best-effort
    manifest flush; the handler persists the in-memory manifest, restores the
    DEFAULT disposition and re-raises the signal (never swallowed). Outside
    the drive the handler is uninstalled."""
    import os
    import signal

    seen: dict = {}

    def capture(adapter, prompt, cwd):
        seen["during"] = signal.getsignal(signal.SIGTERM)

    pipeline = Pipeline.model_validate(yaml.safe_load("""
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""))
    cfg = RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "claude-code"}}}
    )
    man = Manifest(run_id="run-1", slug="demo", branch="gauntlet/demo",
                   base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    run_dir = fixture_repo / "runs" / "demo" / "run-1"
    adapter = FakeAdapter(writes={"clean.py": "out\n"}, on_run=capture)
    orch = Orchestrator(
        repo_root=fixture_repo, run_dir=run_dir,
        artifact_root=fixture_repo / "runs" / "demo", config=cfg,
        pipeline=pipeline, manifest=man, adapter_factory=lambda n: adapter,
    )
    assert orch.drive() == M.RUN_DONE

    handler = seen["during"]
    assert callable(handler)
    assert handler not in (signal.SIG_DFL, signal.SIG_IGN)  # installed in-drive
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL  # restored after

    # Invoke the handler with the death intercepted: it must flush the LATEST
    # in-memory manifest, set SIG_DFL, and re-raise SIGTERM at this process.
    kills: list = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    orch.manifest.warnings.append("SIGTERM-FLUSH-SENTINEL")
    handler(signal.SIGTERM, None)
    assert kills == [(os.getpid(), signal.SIGTERM)]  # re-raised, not swallowed
    assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL
    flushed = Manifest.load(run_dir / "manifest.json")
    assert "SIGTERM-FLUSH-SENTINEL" in flushed.warnings  # the delta was flushed

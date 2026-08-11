"""Orchestrator state machine: control flow, budget halt, gates, resume (F-003)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from gauntlet.adapters.base import Usage
from gauntlet.engine import git_snapshot, gitops, manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline

from conftest import FakeAdapter

BUILDER_CFG = {"agents": {"builder": {"adapter": "claude-code"}}}


def _build(
    repo: Path,
    pipeline_text: str,
    *,
    config: dict | None = None,
    adapters: dict | None = None,
    extra_context: dict | None = None,
    interrupted: str = "park",
    manifest: Manifest | None = None,
) -> Orchestrator:
    cfg = RunConfig.model_validate({**(config or BUILDER_CFG), "interrupted_step": interrupted})
    pipeline = Pipeline.model_validate(yaml.safe_load(pipeline_text))
    artifact_root = repo / "runs" / "demo"
    run_dir = artifact_root / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    man = manifest or Manifest(
        run_id="run-1",
        slug="demo",
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )
    adapters = adapters or {}
    return Orchestrator(
        repo_root=repo,
        run_dir=run_dir,
        artifact_root=artifact_root,
        config=cfg,
        pipeline=pipeline,
        manifest=man,
        adapter_factory=(lambda name: adapters[name]) if adapters else None,
        extra_context=extra_context or {},
    )


LINEAR = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, output: out.txt, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, message: "P1: implement phase\\n\\nbody of the commit."}
"""


def test_linear_run_to_commit(fixture_repo):
    adapter = FakeAdapter(writes={"src.py": "print(1)\n"}, text="done")
    orch = _build(fixture_repo, LINEAR, adapters={"builder": adapter})
    status = orch.drive()
    assert status == M.RUN_DONE
    assert orch.manifest.record("implement").status == M.DONE
    assert orch.manifest.record("commit").status == M.DONE
    assert len(orch.manifest.commits) == 1
    assert orch.manifest.commits[0].phase == "P1"
    assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: implement phase"
    # work tree is clean; the run's own out.txt/manifest under runs/ is excluded
    assert gitops.is_clean(fixture_repo, exclude=["runs"])


def test_agent_with_all_judge_calls_denied_fails_instead_of_done(fixture_repo):
    audit = fixture_repo / "runs" / "demo" / "run-1" / "judge-audit.jsonl"

    def write_denials(adapter, prompt, cwd):
        rows = [
            {
                "step_id": "implement", "decision": "deny",
                "source": "fail-closed",
                "rationale": "judge LLM error: unsupported reasoning effort",
            }
            for _ in range(3)
        ]
        audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    pipeline = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""
    orch = _build(
        fixture_repo, pipeline,
        adapters={"builder": FakeAdapter(text="I could not read any files", on_run=write_denials)},
    )
    orch.judge_env = {"GAUNTLET_JUDGE_TOKEN": "tok"}
    assert orch.drive() == M.RUN_FAILED
    rec = orch.manifest.record("implement")
    assert rec.status == M.FAILED
    assert rec.halt_reason == M.HALT_REASON_JUDGE_DENY
    assert rec.judge_tool_calls_allowed == 0
    assert rec.judge_tool_calls_denied == 3
    assert "judge cannot evaluate" in (rec.notes or "")
    assert "unsupported reasoning effort" in (rec.notes or "")


def test_agent_judge_counts_allow_done_when_at_least_one_call_allowed(fixture_repo):
    audit = fixture_repo / "runs" / "demo" / "run-1" / "judge-audit.jsonl"

    def write_mixed(adapter, prompt, cwd):
        rows = [
            {"step_id": "implement", "decision": "deny", "source": "fast-path",
             "rationale": "blocked"},
            {"step_id": "implement", "decision": "allow", "source": "fast-path",
             "rationale": "safe"},
        ]
        audit.write_text("".join(json.dumps(row) + "\n" for row in rows))

    pipeline = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""
    orch = _build(
        fixture_repo, pipeline,
        adapters={"builder": FakeAdapter(text="done", on_run=write_mixed)},
    )
    orch.judge_env = {"GAUNTLET_JUDGE_TOKEN": "tok"}
    assert orch.drive() == M.RUN_DONE
    rec = orch.manifest.record("implement")
    assert rec.status == M.DONE
    assert rec.judge_tool_calls_allowed == 1
    assert rec.judge_tool_calls_denied == 1


def test_when_skips_step(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: maybe, type: shell, run: "false", when: "enabled"}
      - {id: always, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text, extra_context={"enabled": False})
    assert orch.drive() == M.RUN_DONE
    assert orch.manifest.record("maybe").status == M.SKIPPED
    assert orch.manifest.record("always").status == M.DONE


def test_foreach_fans_out(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: s
    foreach: vars.items
    steps:
      - {id: work, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text, extra_context={"items": ["a", "b", "c"]})
    assert orch.drive() == M.RUN_DONE
    assert orch.manifest.record("work", "0").status == M.DONE
    assert orch.manifest.record("work", "2").status == M.DONE


def test_on_fail_routes_back_with_retries(fixture_repo):
    # tests fail until implement has run twice (marker appears on 2nd call).
    state = {"n": 0}

    def on_run(adapter, prompt, cwd):
        state["n"] += 1
        if state["n"] >= 2:
            (Path(cwd) / "marker.txt").write_text("ok")

    adapter = FakeAdapter(on_run=on_run)
    text = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "test -f marker.txt", on_fail: {route_to: implement, max_retries: 2}}
"""
    orch = _build(fixture_repo, text, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_DONE
    assert state["n"] == 2
    assert orch.manifest.record("tests").status == M.DONE


def test_on_fail_exhausted_retries_fails(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: tests, type: shell, run: "false", on_fail: {route_to: tests, max_retries: 1}}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_FAILED
    assert orch.manifest.record("tests").status == M.FAILED
    assert orch.manifest.record("tests").attempts == 2  # initial + 1 retry


def test_budget_guard_halts(fixture_repo):
    adapter = FakeAdapter(usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.5))
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: pricey, type: agent_task, agent: builder, budget_usd: 0.1, prompt_text: go}
"""
    orch = _build(fixture_repo, text, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("pricey").status == M.HALTED


def test_budget_guard_preserves_side_effect_metadata(fixture_repo):
    # F-001: a DONE result that already produced a commit + per-agent usage must
    # keep those fields when the guard converts it to HALTED — otherwise
    # _finalize records the step halted with no commit/usage, breaking FR-3.3
    # checkpointing and FR-9 branch/manifest consistency.
    from gauntlet.engine.execution import DONE, HALTED, StepResult

    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: pricey, type: agent_task, agent: builder, budget_usd: 0.1, prompt_text: go}
"""
    orch = _build(fixture_repo, text)
    step = next(s for s in orch.pipeline.all_steps() if s.id == "pricey")
    rec = M.StepRecord(id="pricey", type="agent_task", agent="builder")
    result = StepResult(
        status=DONE,
        usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.5),
        commit_sha="a" * 40,
        commit_phase="P1",
        commits=[("P1.1", "b" * 40)],
        usage_by_agent={"builder": Usage(cost_usd=0.5)},
        artifact_writes={"findings.json": Path("/tmp/findings.json")},
        notes="converged in round 1",
    )
    guarded = orch._apply_budget_guard(step, rec, result)
    assert guarded.status == HALTED
    assert guarded.commit_sha == "a" * 40
    assert guarded.commits == [("P1.1", "b" * 40)]
    assert "builder" in guarded.usage_by_agent
    assert guarded.artifact_writes  # side-effect metadata not discarded
    assert "converged in round 1" in guarded.notes  # original notes kept
    assert "budget halt" in guarded.notes


def test_human_gate_parks_then_approve_continues(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: a, type: shell, run: "true"}
      - {id: gate, type: human_gate, show: [plan.md]}
      - {id: b, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("gate").status == M.PARKED
    assert orch.manifest.record("b") is None  # not reached
    assert orch.approve_gate("gate", notes="lgtm") == M.RUN_DONE
    assert orch.manifest.record("b").status == M.DONE


def test_reject_gate_fails_run(fixture_repo):
    # #98: a terminal reject (no upstream cycle to iterate) is a run-ending
    # decision, so it requires the explicit allow_terminal (CLI --terminal).
    # The flag-less call refuses drivably; the explicit call fails the run.
    import pytest

    from gauntlet.engine.orchestrator import TerminalRejectRefusedError

    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: gate, type: human_gate}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    with pytest.raises(TerminalRejectRefusedError):
        orch.reject_gate("gate", notes="no")
    assert orch.manifest.record("gate").status == M.PARKED  # nothing persisted
    assert orch.reject_gate("gate", notes="no", allow_terminal=True) == M.RUN_FAILED


# ---- resume transaction boundary (review F-003) ----------------------------
def _seed_running_step(repo, step_id, step_type, base_sha) -> Manifest:
    man = Manifest(
        run_id="run-1",
        slug="demo",
        branch="gauntlet/demo",
        base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )
    man.upsert(
        StepRecord(
            id=step_id,
            type=step_type,
            agent="builder",
            status=M.RUNNING,
            base_sha=base_sha,
            attempts=1,
            started="t0",
        )
    )
    return man


def test_resume_dirty_agent_step_parks_under_park_policy(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")  # killed mid-edit
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""
    adapter = FakeAdapter()
    orch = _build(fixture_repo, text, adapters={"builder": adapter}, manifest=man,
                  interrupted="park")
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("implement").status == M.INTERRUPTED
    assert adapter.calls == []  # never re-ran the agent over a dirty tree


def test_resume_dirty_agent_step_resets_under_reset_policy(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""
    adapter = FakeAdapter(writes={"clean.py": "real output\n"})
    orch = _build(fixture_repo, text, adapters={"builder": adapter}, manifest=man,
                  interrupted="reset_to_base")
    assert orch.drive() == M.RUN_DONE
    assert adapter.calls  # re-ran after reset
    assert not (fixture_repo / "partial.py").exists()  # partial work discarded
    # a complete recovery snapshot preserved the discarded partial work
    # (F-010-style safety; P3: refs/gauntlet/recovery/ via the executor)
    refs = gitops._run(
        fixture_repo, "for-each-ref", "--format=%(refname)",
        "refs/gauntlet/recovery/",
    ).splitlines()
    assert refs
    snapshot = git_snapshot.load_snapshot(fixture_repo, refs[0])
    tree = gitops._run(
        fixture_repo, "ls-tree", "-r", "--name-only", snapshot.worktree_tree
    )
    assert "partial.py" in tree


def test_resume_dirty_artifact_under_runroot_is_detected(fixture_repo):
    # Review F-001: a partial *declared artifact* under runs/<slug> (not just a
    # repo-root file) must still be seen as a mid-edit interruption and parked.
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "runs" / "demo").mkdir(parents=True)
    (fixture_repo / "runs" / "demo" / "plan.md").write_text("half-written plan")
    man = _seed_running_step(fixture_repo, "author", "agent_task", base)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: author, type: agent_task, agent: builder, output: plan.md, prompt_text: go}
"""
    adapter = FakeAdapter()
    orch = _build(fixture_repo, text, adapters={"builder": adapter}, manifest=man,
                  interrupted="park")
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("author").status == M.INTERRUPTED
    assert adapter.calls == []  # not re-run over the partial artifact


_RESUME_PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""


def test_resume_after_engine_bookkeeping_advance_reruns_cleanly(fixture_repo):
    """#62/#65 incident replay: the engine's own bookkeeping commits advance
    HEAD past the recorded base_sha; a plain resume must re-run the step, not
    re-park INTERRUPTED forever on the engine's own commits."""
    base = gitops.head_sha(fixture_repo)
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    # Between the kill and this resume the engine landed a response checkpoint
    # (force-tracked manifest.json under the excluded run dir) — porcelain is
    # clean, but HEAD != base_sha.
    (fixture_repo / "runs" / "demo" / "run-1").mkdir(parents=True)
    (fixture_repo / "runs" / "demo" / "run-1" / "manifest.json").write_text("{}\n")
    bk = gitops.commit_run_bookkeeping(
        fixture_repo, "gauntlet: response implement-resp-1 pending",
        ["runs/demo/run-1/manifest.json"], identity=gitops.ENGINE_IDENTITY,
    )
    adapter = FakeAdapter(writes={"clean.py": "real output\n"})
    orch = _build(fixture_repo, _RESUME_PIPELINE, adapters={"builder": adapter},
                  manifest=man, interrupted="park")
    assert orch.drive() == M.RUN_DONE
    assert adapter.calls  # re-ran; the engine's own commits are not dirt
    # The transaction boundary was re-armed at THIS attempt's entry HEAD (#65),
    # not left at the stale pre-bookkeeping base.
    assert orch.manifest.record("implement").base_sha == bk


def test_resume_with_real_commits_past_base_parks_loudly(fixture_repo):
    """Real (non-engine) commits above the base still park — and the park names
    the offending commit range instead of a bare status (#65)."""
    base = gitops.head_sha(fixture_repo)
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    (fixture_repo / "wip.py").write_text("committed but unmanifested\n")
    gitops.commit_all(
        fixture_repo, "P2 wip: arm the thing\n\nbody",
        identity=gitops.Identity("Builder", "b@g.local"),
    )
    adapter = FakeAdapter()
    orch = _build(fixture_repo, _RESUME_PIPELINE, adapters={"builder": adapter},
                  manifest=man, interrupted="park")
    assert orch.drive() == M.RUN_PARKED
    rec = orch.manifest.record("implement")
    assert rec.status == M.INTERRUPTED
    assert adapter.calls == []  # protection intact: never re-run over real commits
    assert "interrupted mid-edit" in rec.notes
    # The dirty verdict is inspectable from the park message alone.
    assert "P2 wip: arm the thing" in rec.notes
    assert base[:10] in rec.notes


def test_engine_marked_commit_touching_implementation_still_parks(fixture_repo):
    # Fail closed: engine markers alone don't buy tolerance — a commit that
    # moves implementation reads dirty even with ENGINE_IDENTITY + `gauntlet:`.
    base = gitops.head_sha(fixture_repo)
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    (fixture_repo / "impl.py").write_text("smuggled\n")
    gitops.commit_paths(
        fixture_repo, "gauntlet: rewind implementation to abcdef1234 for re-run (x)",
        ["impl.py"], identity=gitops.ENGINE_IDENTITY,
    )
    adapter = FakeAdapter()
    orch = _build(fixture_repo, _RESUME_PIPELINE, adapters={"builder": adapter},
                  manifest=man, interrupted="park")
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("implement").status == M.INTERRUPTED
    assert adapter.calls == []


def test_reset_interrupted_override_forces_reset_under_park_policy(fixture_repo):
    """#72: `resume --reset-interrupted` is a one-shot override — the config
    says park, the override discards the interrupted attempt (backed up) and
    re-runs cleanly. The park state gains a sanctioned exit."""
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    adapter = FakeAdapter(writes={"clean.py": "real output\n"})
    orch = _build(fixture_repo, _RESUME_PIPELINE, adapters={"builder": adapter},
                  manifest=man, interrupted="park")
    orch.interrupted_override = "reset_to_base"
    assert orch.drive() == M.RUN_DONE
    assert adapter.calls  # re-ran after the reset
    assert not (fixture_repo / "partial.py").exists()  # partial work discarded
    refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/recovery/")
    assert "refs/gauntlet/recovery/" in refs  # ...but snapshotted first


def test_reset_interrupted_override_preserves_wip_checkpoints(fixture_repo):
    """#72: the override rewinds to the latest committed `P<N> wip:` milestone
    (FR-11.2), never past it — committed builder work survives the discard."""
    base = gitops.head_sha(fixture_repo)
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    (fixture_repo / "milestone.py").write_text("committed milestone\n")
    gitops.commit_all(
        fixture_repo, "P2 wip: arm the thing\n\nbody",
        identity=gitops.Identity("Builder", "b@g.local"),
    )
    (fixture_repo / "partial.py").write_text("uncommitted partial")
    adapter = FakeAdapter(writes={"clean.py": "real output\n"})
    orch = _build(fixture_repo, _RESUME_PIPELINE, adapters={"builder": adapter},
                  manifest=man, interrupted="park")
    orch.interrupted_override = "reset_to_base"
    assert orch.drive() == M.RUN_DONE
    assert adapter.calls
    assert (fixture_repo / "milestone.py").exists()  # committed wip preserved
    assert not (fixture_repo / "partial.py").exists()  # partial discarded
    assert orch.manifest.record("implement").resumed_from_checkpoint == (
        "P2 wip: arm the thing"
    )


def test_reset_policy_preserves_uncommitted_pr_md_edits(fixture_repo):
    """PR #77 review (blocking): a tracked PR.md with uncommitted human edits
    is invisible to the dirty check AND the backup (policy exclusion), but
    reset --hard is not policy-scoped — the edit must be carried across the
    rewind, byte-for-byte, not silently destroyed."""
    pr = fixture_repo / "runs" / "demo" / "PR.md"
    pr.parent.mkdir(parents=True)
    pr.write_text("PR draft v1\n")
    gitops.commit_all(
        fixture_repo, "P1: track the PR draft\n\nbody",
        identity=gitops.Identity("Human", "h@g.local"),
    )
    base = gitops.head_sha(fixture_repo)
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    (fixture_repo / "partial.py").write_text("half written")  # killed mid-edit
    pr.write_text("PR draft v2 — human edited, uncommitted\n")
    adapter = FakeAdapter(writes={"clean.py": "real output\n"})
    orch = _build(fixture_repo, _RESUME_PIPELINE, adapters={"builder": adapter},
                  manifest=man, interrupted="reset_to_base")
    assert orch.drive() == M.RUN_DONE
    assert not (fixture_repo / "partial.py").exists()  # partial work discarded
    assert pr.read_text() == "PR draft v2 — human edited, uncommitted\n"
    refs = gitops._run(
        fixture_repo, "for-each-ref", "--format=%(refname)",
        "refs/gauntlet/recovery/",
    ).splitlines()
    ref = next(r for r in refs if "/implement-" in r)
    snapshot = git_snapshot.load_snapshot(fixture_repo, ref)
    assert "runs/demo/PR.md" in snapshot.protected_paths
    assert gitops._run(
        fixture_repo, "show", f"{snapshot.worktree_tree}:runs/demo/PR.md"
    ) == "PR draft v2 — human edited, uncommitted\n"


def test_reset_policy_preserves_and_backs_up_pr_md_deletion(fixture_repo):
    """A tracked PR.md deletion is an uncommitted human edit: reset must not
    resurrect it, and the backup ref must durably represent the deletion."""
    pr = fixture_repo / "runs" / "demo" / "PR.md"
    pr.parent.mkdir(parents=True)
    pr.write_text("PR draft to delete\n")
    gitops.commit_all(
        fixture_repo, "P1: track the PR draft\n\nbody",
        identity=gitops.Identity("Human", "h@g.local"),
    )
    base = gitops.head_sha(fixture_repo)
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    (fixture_repo / "partial.py").write_text("half written")
    pr.unlink()
    orch = _build(
        fixture_repo,
        _RESUME_PIPELINE,
        adapters={"builder": FakeAdapter(writes={"clean.py": "real output\n"})},
        manifest=man,
        interrupted="reset_to_base",
    )

    assert orch.drive() == M.RUN_DONE
    assert not pr.exists()
    refs = gitops._run(
        fixture_repo, "for-each-ref", "--format=%(refname)",
        "refs/gauntlet/recovery/",
    ).splitlines()
    ref = next(r for r in refs if "/implement-" in r)
    snapshot = git_snapshot.load_snapshot(fixture_repo, ref)
    # The deletion is durably represented: recorded as a protected deletion,
    # and the path is absent from the snapshot's worktree tree.
    assert "runs/demo/PR.md" in snapshot.protected_deletions
    tree = gitops._run(
        fixture_repo, "ls-tree", "-r", "--name-only", snapshot.worktree_tree
    )
    assert "runs/demo/PR.md" not in tree


def test_interrupted_park_notes_name_the_reset_verb(fixture_repo):
    # The park message must point at a REAL command, not implied git surgery
    # (#72): `gauntlet resume <slug> --reset-interrupted`.
    base = gitops.head_sha(fixture_repo)
    (fixture_repo / "partial.py").write_text("half written")
    man = _seed_running_step(fixture_repo, "implement", "agent_task", base)
    orch = _build(fixture_repo, _RESUME_PIPELINE, adapters={"builder": FakeAdapter()},
                  manifest=man, interrupted="park")
    assert orch.drive() == M.RUN_PARKED
    notes = orch.manifest.record("implement").notes
    assert "gauntlet resume demo --reset-interrupted" in notes


def test_interrupted_step_config_rejects_unknown_value():
    # F-003/#72: previously unvalidated — a typo silently meant `park` and the
    # configured recovery policy just didn't happen. Fail closed at load.
    import pytest

    with pytest.raises(ValueError, match="interrupted_step must be one of"):
        RunConfig.model_validate({**BUILDER_CFG, "interrupted_step": "reset-to-base"})
    cfg = RunConfig.model_validate({**BUILDER_CFG, "interrupted_step": "RESET_TO_BASE"})
    assert cfg.interrupted_step == "reset_to_base"  # case-normalized, valid


def test_reset_for_retry_rearms_transaction_boundary(fixture_repo):
    """#65: base_sha belongs to a step ATTEMPT — an on_fail retry must re-stamp
    it at the retry's own entry HEAD, never keep the first attempt's."""
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: a, type: shell, run: "true"}
      - {id: b, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text)
    orch.manifest.upsert(StepRecord(id="a", type="shell", status=M.DONE,
                                    base_sha="a" * 40))
    orch.manifest.upsert(StepRecord(id="b", type="shell", status=M.FAILED,
                                    base_sha="b" * 40))
    orch._reset_for_retry(orch.pipeline.stages[0], "a", None)
    assert orch.manifest.record("a").status == M.PENDING
    assert orch.manifest.record("a").base_sha is None
    assert orch.manifest.record("b").base_sha is None


def test_step_foreach_skips_completed_iterations_on_resume(fixture_repo):
    # Review F-004: a resumed step-level foreach must not re-run done iterations.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: work, type: agent_task, agent: builder, foreach: vars.items, prompt_text: go}
"""
    adapter = FakeAdapter()
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="x"))
    man.upsert(StepRecord(id="work", type="agent_task", iteration="0", status=M.DONE))
    orch = _build(fixture_repo, text, adapters={"builder": adapter},
                  extra_context={"items": ["a", "b", "c"]}, manifest=man)
    assert orch.drive() == M.RUN_DONE
    # iteration 0 was already done; only 1 and 2 ran
    assert len(adapter.calls) == 2


def test_gate_inside_foreach_is_approvable(fixture_repo):
    # Review F-004: a human_gate parked inside a foreach must be reachable.
    text = """
name: demo
version: 1
stages:
  - id: s
    foreach: vars.items
    steps:
      - {id: gate, type: human_gate}
"""
    orch = _build(fixture_repo, text, extra_context={"items": ["a", "b"]})
    assert orch.drive() == M.RUN_PARKED
    # the first iteration's gate is parked; approve targets it across iterations
    assert orch.approve_gate("gate") in (M.RUN_PARKED, M.RUN_DONE)


def test_shell_timeout_halts(fixture_repo):
    # Review F-006: a shell step exceeding its timeout halts at a checkpoint.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: slow, type: shell, run: "sleep 5", timeout_s: 0.3}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("slow").status == M.HALTED


# ---- conflict-park discriminator (FR-2.1) ----------------------------------
CONFLICT_PIPELINE = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go,
         halt_on: "UPSTREAM CONFLICT"}
"""


def test_upstream_conflict_sets_parked_reason(fixture_repo):
    # An UPSTREAM CONFLICT halt parks AND stamps the discriminator so `--response`
    # scoping can tell a conflict park from every other park. Under FR-7.2 the
    # discriminator is the PRD value `response` (the agent_task type distinguishes
    # it from a cycle escalation), with a null halt_reason (disjoint).
    adapter = FakeAdapter(text="UPSTREAM CONFLICT\nplan contradicts the impl")
    orch = _build(fixture_repo, CONFLICT_PIPELINE, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED
    rec = orch.manifest.record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_RESPONSE
    assert rec.halt_reason is None  # disjoint (FR-7.2)


def test_non_conflict_halt_marker_parks_with_response_reason(fixture_repo):
    # Every halt_on park is a human-decision park, so it stamps the PRD
    # `response` reason (FR-7.2 park invariant) — a custom (non-UPSTREAM CONFLICT)
    # marker included. No park is left with a null parked_reason (which would
    # classify as `unknown`); routing is by step type, not the marker text.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go,
         halt_on: "NEEDS REVIEW"}
"""
    adapter = FakeAdapter(text="NEEDS REVIEW\nplease look at this")
    orch = _build(fixture_repo, text, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED
    rec = orch.manifest.record("implement")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_RESPONSE
    assert rec.halt_reason is None  # disjoint (FR-7.2)


def test_human_gate_park_sets_gate_reason(fixture_repo):
    # FR-7.2: a human_gate park stamps the PRD `gate` reason (never a null
    # parked-reason on a park), with a null halt_reason (disjoint).
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: gate, type: human_gate}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    rec = orch.manifest.record("gate")
    assert rec.parked_reason == M.PARKED_REASON_GATE
    assert rec.halt_reason is None


def test_budget_halt_stamps_budget_reason(fixture_repo):
    # FR-7.2: a budget halt is terminal (HALTED) → stamps halt_reason=budget with
    # a null parked_reason (disjoint), never a parked_reason.
    adapter = FakeAdapter(usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.5))
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: pricey, type: agent_task, agent: builder, budget_usd: 0.1, prompt_text: go}
"""
    orch = _build(fixture_repo, text, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED
    rec = orch.manifest.record("pricey")
    assert rec.status == M.HALTED
    assert rec.halt_reason == M.HALT_REASON_BUDGET
    assert rec.parked_reason is None


def test_parked_reason_cleared_when_conflict_resumes_to_done(fixture_repo):
    # FR-2.1 lifecycle (current-state, not a latch): a step that parks on a
    # conflict and is then resumed to `done` ends with parked_reason unset.
    state = {"n": 0}

    def on_run(adapter, prompt, cwd):
        state["n"] += 1
        adapter.text = (
            "UPSTREAM CONFLICT\nplan contradicts the impl"
            if state["n"] == 1
            else "all good, proceeding"
        )

    adapter = FakeAdapter(on_run=on_run)
    orch = _build(fixture_repo, CONFLICT_PIPELINE, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED
    assert orch.manifest.record("implement").parked_reason == (
        M.PARKED_REASON_RESPONSE
    )
    # resume: the PARKED step re-executes; this run does not signal a conflict
    assert orch.drive() == M.RUN_DONE
    assert orch.manifest.record("implement").parked_reason is None


def test_finalize_is_current_state_not_a_latch(fixture_repo):
    # Drives the clear/re-set rule directly across the outcomes that are awkward
    # to stage end-to-end (failed / non-conflict park): each non-conflict
    # finalize clears a stale upstream_conflict, and only a conflict result
    # re-sets it (FR-2.1).
    from gauntlet.engine.execution import DONE, FAILED, PARKED, StepResult

    orch = _build(fixture_repo, CONFLICT_PIPELINE)
    rec = M.StepRecord(id="implement", type="agent_task")

    for status in (DONE, FAILED, PARKED):
        rec.parked_reason = M.PARKED_REASON_UPSTREAM_CONFLICT
        orch._finalize(rec, StepResult(status=status))  # no conflict reason
        assert rec.parked_reason is None, f"{status} should clear stale reason"

    # only a result carrying the conflict reason re-sets it
    rec.parked_reason = None
    orch._finalize(
        rec,
        StepResult(status=PARKED, parked_reason=M.PARKED_REASON_UPSTREAM_CONFLICT),
    )
    assert rec.parked_reason == M.PARKED_REASON_UPSTREAM_CONFLICT


def test_conflict_parked_agent_task_is_not_approvable_as_a_gate(fixture_repo):
    # F-001: an agent_task halted on an UPSTREAM CONFLICT parks (status PARKED),
    # but it is NOT a human_gate. Approving it would drive the run to `done`
    # while leaving parked_reason="upstream_conflict" live — a false current
    # state (FR-2.1). The gate path must refuse it.
    import pytest

    adapter = FakeAdapter(text="UPSTREAM CONFLICT\nplan contradicts the impl")
    orch = _build(fixture_repo, CONFLICT_PIPELINE, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_PARKED
    with pytest.raises(ValueError, match="not parked at a human gate"):
        orch.approve_gate("implement", notes="ship it")
    with pytest.raises(ValueError, match="not parked at a human gate"):
        orch.reject_gate("implement", notes="nope")
    # the conflict discriminator is untouched by the refused attempts
    assert orch.manifest.record("implement").parked_reason == (
        M.PARKED_REASON_RESPONSE
    )


def test_approve_gate_clears_stale_parked_reason(fixture_repo):
    # F-001: approving a gate is a direct terminal transition; it must clear any
    # parked_reason so a finished step never carries a stale discriminator.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: gate, type: human_gate}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    orch.manifest.record("gate").parked_reason = M.PARKED_REASON_UPSTREAM_CONFLICT
    assert orch.approve_gate("gate") == M.RUN_DONE
    rec = orch.manifest.record("gate")
    assert rec.status == M.DONE
    assert rec.parked_reason is None


def test_reject_gate_clears_stale_parked_reason(fixture_repo):
    # F-001: rejecting a gate must likewise clear parked_reason on the record.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: gate, type: human_gate}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_PARKED
    orch.manifest.record("gate").parked_reason = M.PARKED_REASON_UPSTREAM_CONFLICT
    # #98: terminal path (no upstream cycle) needs the explicit flag.
    assert orch.reject_gate("gate", notes="no", allow_terminal=True) == M.RUN_FAILED
    rec = orch.manifest.record("gate")
    assert rec.status == M.FAILED
    assert rec.parked_reason is None


# --- FR-7.2: halt_reason stamped on every terminal path, disjoint from parked --
def test_shell_no_command_stamps_precondition(fixture_repo):
    # A fail-closed guard that fired before running anything → precondition.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: broken, type: shell}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_FAILED
    rec = orch.manifest.record("broken")
    assert rec.status == M.FAILED
    assert rec.halt_reason == M.HALT_REASON_PRECONDITION
    assert rec.parked_reason is None


def test_shell_nonzero_exit_stamps_adapter_error(fixture_repo):
    # The command ran and reported failure → a terminal execution failure.
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: tests, type: shell, run: "exit 3"}
"""
    orch = _build(fixture_repo, text)
    assert orch.drive() == M.RUN_FAILED
    rec = orch.manifest.record("tests")
    assert rec.status == M.FAILED
    assert rec.halt_reason == M.HALT_REASON_ADAPTER_ERROR
    assert rec.parked_reason is None


def test_finalize_stamps_and_enforces_disjoint_reasons(fixture_repo):
    # The stamping mechanism carries EVERY halt_reason enum member (incl.
    # judge_deny / signal_kill, which have no synthetic producer here) onto the
    # record with a null parked_reason (disjoint), and vice-versa for a park.
    from gauntlet.engine.execution import HALTED, PARKED, StepResult

    orch = _build(fixture_repo, CONFLICT_PIPELINE)
    for halt in sorted(M.HALT_REASONS):
        rec = M.StepRecord(id="x", type="agent_task")
        orch._finalize(rec, StepResult(status=HALTED, halt_reason=halt))
        assert rec.halt_reason == halt
        assert rec.parked_reason is None
        assert M.reason_fields_disjoint(rec.halt_reason, rec.parked_reason)
    # A park carries parked_reason with a null halt_reason.
    rec = M.StepRecord(id="x", type="agent_task")
    orch._finalize(rec, StepResult(status=PARKED,
                                   parked_reason=M.PARKED_REASON_USAGE_LIMIT))
    assert rec.parked_reason == M.PARKED_REASON_USAGE_LIMIT
    assert rec.halt_reason is None


def test_finalize_defaults_mandatory_halt_reason(fixture_repo):
    # A terminal StepResult that carries NO halt_reason is defaulted so the
    # mandatory invariant always holds: a precondition failure (failure_kind set)
    # → precondition; anything else → adapter_error.
    from gauntlet.engine.execution import FAILED, StepResult

    orch = _build(fixture_repo, CONFLICT_PIPELINE)
    rec = M.StepRecord(id="x", type="agent_task")
    orch._finalize(rec, StepResult(status=FAILED))  # no halt_reason
    assert rec.halt_reason == M.HALT_REASON_ADAPTER_ERROR

    rec = M.StepRecord(id="y", type="adversarial_cycle")
    orch._finalize(rec, StepResult(status=FAILED,
                                   failure_kind=M.FAILURE_KIND_CLEAN_HANDOFF))
    assert rec.halt_reason == M.HALT_REASON_PRECONDITION


def test_mark_skipped_clears_parked_reason(fixture_repo):
    # F-001: a skip is a direct terminal transition too — it must not leave a
    # stale conflict discriminator behind (FR-2.1).
    orch = _build(fixture_repo, CONFLICT_PIPELINE)
    orch.manifest.upsert(
        StepRecord(
            id="implement",
            type="agent_task",
            status=M.PARKED,
            parked_reason=M.PARKED_REASON_UPSTREAM_CONFLICT,
        )
    )
    orch._mark_skipped("implement", None)
    rec = orch.manifest.record("implement")
    assert rec.status == M.SKIPPED
    assert rec.parked_reason is None


def test_resume_mid_commit_reconciles_without_double_commit(fixture_repo):
    base = gitops.head_sha(fixture_repo)
    # Simulate: engine recorded base + ran commit, the commit landed, then the
    # process died before recording the SHA. Reproduce the landed commit:
    (fixture_repo / "feature.py").write_text("done\n")
    msg = "P1: implement phase\n\nbody."
    landed = gitops.commit_all(
        fixture_repo, msg, identity=gitops.Identity("Builder", "b@gauntlet.local")
    )
    assert landed != base
    man = _seed_running_step(fixture_repo, "commit", "commit", base)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, message: "P1: implement phase\\n\\nbody."}
"""
    orch = _build(fixture_repo, text, manifest=man)
    assert orch.drive() == M.RUN_DONE
    assert gitops.head_sha(fixture_repo) == landed  # no second commit
    assert len(orch.manifest.commits) == 1
    assert orch.manifest.commits[0].sha == landed


# A gauntlet-phases block whose `goal:` carries an unquoted `schema:` — a
# colon-space mid-scalar that YAML reads as a nested mapping ("mapping values
# are not allowed here"). This is the exact defect that crashed the
# gauntlet-resume-response run's resume.
MALFORMED_PLAN = (
    "# Plan\n\n"
    "```gauntlet-phases\n"
    "- id: P1\n"
    "  title: Broken phase\n"
    "  goal: the implement step has no schema: field and must not change\n"
    "```\n"
)


def test_malformed_plan_phases_parks_instead_of_crashing(fixture_repo):
    text = """
name: demo
version: 1
stages:
  - id: phases
    foreach: plan.phases
    steps:
      - {id: implement, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text)
    (orch.artifact_root / "plan.md").write_text(MALFORMED_PLAN)
    # A malformed block must not escape drive() as an uncaught PlanPhasesError
    # (which would leave the write-ahead RUN_RUNNING persisted); it parks.
    assert orch.drive() == M.RUN_PARKED
    # The persisted manifest reflects the park — never a stale "running" that
    # `gauntlet status` would report as a live run. Since P5 (plan §5.1) the
    # park is a classified artifact_invalid STEP transition (with validator,
    # diagnostic, and content fingerprint) instead of a bare warning + HALTED.
    reloaded = Manifest.load(orch.manifest_path)
    assert reloaded.status == M.RUN_PARKED
    rec = reloaded.record("implement")
    assert rec is not None and rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID
    assert rec.revalidation is not None
    assert rec.revalidation.validator == "plan_phases"
    assert "gauntlet-phases" in (rec.notes or "")


def test_malformed_plan_does_not_block_steps_that_ignore_phases(fixture_repo):
    # `plan.phases` is parsed lazily: a step that never reads it must run even
    # when the gauntlet-phases block is malformed (so the deterministic plan-lint
    # gate, not an eager parse in some earlier step's context, is what reports
    # the defect). The parse is deferred to the foreach, where it fails closed.
    text = """
name: demo
version: 1
stages:
  - id: pre
    steps:
      - {id: noop, type: shell, run: "true"}
  - id: phases
    foreach: plan.phases
    steps:
      - {id: implement, type: shell, run: "true"}
"""
    orch = _build(fixture_repo, text)
    (orch.artifact_root / "plan.md").write_text(MALFORMED_PLAN)
    assert orch.drive() == M.RUN_PARKED
    # The phases-agnostic step ran instead of being pre-empted by the parse...
    assert orch.manifest.record("noop").status == M.DONE
    # ...and the run still failed closed at the foreach, with the reason
    # persisted as a classified artifact_invalid park on the stopped step
    # (P5, plan §5.1) rather than a bare warning.
    reloaded = Manifest.load(orch.manifest_path)
    assert reloaded.status == M.RUN_PARKED
    rec = reloaded.record("implement")
    assert rec is not None and rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID
    assert "gauntlet-phases" in (rec.notes or "")


AGENT_STEP = """
name: demo
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, output: out.txt, prompt_text: go}
"""


def test_step_id_exported_to_agent_even_without_judge(fixture_repo):
    # FR-5.5 / review F-001: GAUNTLET_STEP_ID must reach EVERY in-run agent
    # INDEPENDENT of judge configuration. The prior code set it only when a judge
    # was configured, so under `--no-judge` an in-pipeline agent that shelled out to
    # `gauntlet recover` would NOT be blocked by the operator-only guard. This is an
    # end-to-end proof of propagation: the orchestrator runs with judge_env empty
    # (the no-judge path), and the agent — the FakeAdapter standing in for an
    # in-pipeline agent — actually invokes `RunManager.recover` mid-step and is
    # refused by the operator-only boundary. It also proves the marker is scoped
    # (restored after the step, never leaked into the parent session).
    import os

    from gauntlet.engine.run import RecoverRefused, RunManager

    os.environ.pop("GAUNTLET_STEP_ID", None)
    seen: dict[str, str | None] = {}

    def on_run(adapter, prompt, cwd):
        seen["step_id"] = os.environ.get("GAUNTLET_STEP_ID")
        mgr = RunManager(fixture_repo, config=RunConfig.model_validate(BUILDER_CFG))
        try:
            mgr.recover("demo")  # the guard fires before any run-dir resolution
        except RecoverRefused as exc:
            seen["recover_refused"] = str(exc)

    adapter = FakeAdapter(text="done", on_run=on_run)
    orch = _build(fixture_repo, AGENT_STEP, adapters={"builder": adapter})
    assert orch.judge_env == {}  # the no-judge path
    orch.drive()

    assert seen["step_id"] == "implement"  # propagated under --no-judge
    assert "operator-only" in seen.get("recover_refused", "")  # agent cannot recover
    assert os.environ.get("GAUNTLET_STEP_ID") is None  # scoped — no leak past the step

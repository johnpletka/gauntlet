"""Run lifecycle: new, entry contract, run, gates, rollback (FR-8, FR-10, F-010)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.engine import gitops, manifest as M
from gauntlet.engine.run import EntryContractError, RollbackGuardError, RunManager

from conftest import FakeAdapter, git

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
  triage: {adapter: api, model: haiku}
"""


def _prepare(repo: Path) -> RunManager:
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_YAML)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _write_pipeline(repo: Path, text: str) -> Path:
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / "p.yaml"
    path.write_text(text)
    return path


def _author_prd(mgr: RunManager, slug: str) -> None:
    mgr.new(slug)
    mgr.layout(slug).prd_path.write_text("# Real PRD\n\nA genuine human-authored PRD.\n")


def test_new_scaffolds_stub_and_entry_contract_refuses(fixture_repo):
    mgr = _prepare(fixture_repo)
    mgr.new("demo")
    with pytest.raises(EntryContractError, match="stub"):
        mgr.check_entry_contract("demo")


def test_entry_contract_refuses_when_absent(fixture_repo):
    mgr = _prepare(fixture_repo)
    with pytest.raises(EntryContractError, match="does not exist"):
        mgr.check_entry_contract("demo")


def test_entry_contract_passes_for_real_prd(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    mgr.check_entry_contract("demo")  # no raise


def test_entry_contract_refuses_marker_only_removed(fixture_repo):
    # F-007: deleting only the marker line leaves the scaffold body -> refuse.
    from gauntlet.engine.run import PRD_STUB_MARKER

    mgr = _prepare(fixture_repo)
    mgr.new("demo")
    prd = mgr.layout("demo").prd_path
    stub = prd.read_text()
    prd.write_text("\n".join(l for l in stub.splitlines() if PRD_STUB_MARKER not in l))
    assert PRD_STUB_MARKER not in prd.read_text()
    with pytest.raises(EntryContractError, match="only the marker removed"):
        mgr.check_entry_contract("demo")


GATED_REFUSE = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [prd.md]}
"""


def test_start_refuses_second_run_while_active(fixture_repo):
    # review finding: a second `start()` over a still-live run would overwrite
    # active-run.txt and orphan the first, risking competing agents on one
    # worktree. Refuse unless the active run is terminal (resume/abort instead).
    from gauntlet.engine.run import ActiveRunError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED_REFUSE)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    with pytest.raises(ActiveRunError, match="parked"):
        mgr.start("demo", path, use_judge=False)


def test_start_allowed_after_terminal_run(fixture_repo):
    # once the active run is terminal (here: aborted), a fresh start is fine.
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED_REFUSE)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    mgr.abort("demo")  # terminal
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED  # no raise


LINEAR = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "true"}
      - {id: commit, type: commit, message: "P1: implement\\n\\nthe body."}
"""


def test_run_end_to_end_creates_branch_and_commit(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    adapter = FakeAdapter(writes={"feature.py": "code\n"})
    status = mgr.start("demo", path, use_judge=False,
                       adapter_factory=lambda n: adapter)
    assert status == M.RUN_DONE
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"
    assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: implement"
    man = mgr.status("demo")
    assert man.status == M.RUN_DONE
    assert man.commits[-1].phase == "P1"


GATED = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [prd.md]}
      - {id: after, type: shell, run: "true"}
"""


def test_human_gate_park_then_approve(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    assert mgr.status("demo").record("gate").status == M.PARKED
    assert mgr.approve("demo", notes="ok", use_judge=False) == M.RUN_DONE
    assert mgr.status("demo").record("after").status == M.DONE


TWO_PHASE = """
name: p
version: 1
stages:
  - id: p1
    steps:
      - {id: impl1, type: agent_task, agent: builder, prompt_text: a}
      - {id: c1, type: commit, message: "P1: phase one\\n\\nbody one."}
  - id: p2
    steps:
      - {id: impl2, type: agent_task, agent: builder, prompt_text: b}
      - {id: c2, type: commit, message: "P2: phase two\\n\\nbody two."}
"""


def test_rollback_to_phase_one_rewinds_branch_and_manifest(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, TWO_PHASE)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    p2_sha = gitops.head_sha(fixture_repo)
    assert gitops.commit_subject(fixture_repo, p2_sha) == "P2: phase two"

    target = mgr.rollback("demo", phase=1)
    assert gitops.head_sha(fixture_repo) == target
    assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: phase one"
    man = mgr.status("demo")
    assert [c.phase for c in man.commits] == ["P1"]
    # F-002: ALL phase-2 step records (not just its commit) are rewound to
    # pending, so a resume re-does the work git reset removed.
    assert man.record("impl2").status == M.PENDING
    assert man.record("c2").status == M.PENDING
    assert man.record("impl1").status == M.DONE  # phase 1 kept
    # a backup ref preserved the pre-rollback tip
    refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/backup/")
    assert p2_sha in refs or "refs/gauntlet/backup/" in refs


def test_rollback_refuses_branch_ahead_of_manifest(fixture_repo):
    # F-003: an extra unmanifested commit means branch != manifest tip -> refuse.
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    mgr.start("demo", path, use_judge=False,
              adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))
    (fixture_repo / "extra.py").write_text("out of band\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "-c", "user.name=H", "-c", "user.email=h@h.local",
        "commit", "-qm", "out-of-band commit")
    with pytest.raises(RollbackGuardError, match="diverged"):
        mgr.rollback("demo", phase=1)


def test_rollback_refuses_dirty_worktree(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    adapter = FakeAdapter(writes={"feature.py": "code\n"})
    mgr.start("demo", path, use_judge=False, adapter_factory=lambda n: adapter)
    (fixture_repo / "dirt.py").write_text("uncommitted")
    with pytest.raises(RollbackGuardError, match="dirty"):
        mgr.rollback("demo", phase=1)


def test_rollback_refuses_unknown_phase(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    adapter = FakeAdapter(writes={"feature.py": "code\n"})
    mgr.start("demo", path, use_judge=False, adapter_factory=lambda n: adapter)
    with pytest.raises(RollbackGuardError, match="phase-9"):
        mgr.rollback("demo", phase=9)


# --- F-002: the manifest records every prompt the cycle will load ------------
def test_prompt_hashes_include_cycle_default_templates():
    from gauntlet.engine.config import RunConfig
    from gauntlet.engine.cycle import CYCLE_PROMPT_DEFAULTS
    from gauntlet.engine.pipeline import Pipeline

    repo = Path(__file__).resolve().parents[2]  # the real repo carries prompts/
    mgr = RunManager(repo, config=RunConfig.model_validate({"agents": {}}))
    pipe = Pipeline.model_validate({
        "name": "demo", "version": 1,
        "stages": [{"id": "s", "steps": [
            {"id": "cyc", "type": "adversarial_cycle", "mode": "artifact",
             "artifact": "plan.md", "reviewer": "reviewer", "triager": "triage",
             "fixer": "builder",
             # only review_prompt named explicitly; the rest fall back to defaults
             "review_prompt": "prompts/cycle-review.md"},
        ]}],
    })
    hashes = mgr._prompt_hashes(pipe)
    # the explicit override AND every default template the cycle would load at
    # runtime are recorded, so the manifest pins the full prompt set (FR-5.6).
    for ref in CYCLE_PROMPT_DEFAULTS.values():
        assert ref in hashes, f"default prompt {ref} missing from prompt_hashes"


# --- F-003: judge LLM spend is folded into the manifest ----------------------
def test_merge_judge_usage_folds_audit_into_manifest(fixture_repo):
    mgr = _prepare(fixture_repo)
    layout = mgr.layout("toy")
    run_dir = layout.slug_dir / "run-x"
    run_dir.mkdir(parents=True)
    man = M.Manifest(
        run_id="run-x", slug="toy", branch="gauntlet/toy", base_branch="main",
        pipeline=M.PipelineRef(name="p", version=1, hash="h"),
    )
    man.totals = M.UsageTotals(input_tokens=100, output_tokens=10, cost_usd=1.0)
    audit = run_dir / "judge-audit.jsonl"
    audit.write_text(
        json.dumps({"decision": "allow",
                    "usage": {"input_tokens": 5, "output_tokens": 2,
                              "cost_usd": 0.01}}) + "\n"
        + json.dumps({"decision": "deny", "source": "fast-path",
                      "usage": None}) + "\n"  # fast-path: no usage, skipped
        + json.dumps({"decision": "allow",
                      "usage": {"input_tokens": 3, "output_tokens": 1,
                                "cost_usd": 0.02}}) + "\n"
    )
    mgr._merge_judge_usage(man, run_dir)
    jl = man.agent_usage["judge_llm"]
    assert jl.input_tokens == 8 and jl.output_tokens == 3
    assert jl.cost_usd == pytest.approx(0.03)
    # totals now include judge spend so `gauntlet report` can attribute it (FR-3)
    assert man.totals.cost_usd == pytest.approx(1.03)
    # persisted to disk, not just in memory (data over inference)
    persisted = M.Manifest.load(run_dir / "manifest.json")
    assert persisted.agent_usage["judge_llm"].cost_usd == pytest.approx(0.03)
    # idempotent: re-merging the same audit does not double count (resume safety)
    mgr._merge_judge_usage(man, run_dir)
    assert man.agent_usage["judge_llm"].cost_usd == pytest.approx(0.03)
    assert man.totals.cost_usd == pytest.approx(1.03)


# --- F-005: a failed required PR.md draft is surfaced, not swallowed ----------
def test_pr_draft_failure_is_recorded_and_raised(fixture_repo, monkeypatch):
    import gauntlet.engine.pr as pr

    mgr = _prepare(fixture_repo)
    layout = mgr.layout("toy")
    run_dir = layout.slug_dir / "run-x"
    run_dir.mkdir(parents=True)
    man = M.Manifest(
        run_id="run-x", slug="toy", branch="gauntlet/toy", base_branch="main",
        pipeline=M.PipelineRef(name="p", version=1, hash="h"),
    )

    def boom(*a, **k):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(pr, "write_pr_draft", boom)
    with pytest.raises(RuntimeError, match="render exploded"):
        mgr._maybe_draft_pr(layout, run_dir, man, M.RUN_DONE)
    assert any("PR.md draft failed" in w for w in man.warnings)
    # the warning is persisted, so the missing deliverable is never silent
    persisted = M.Manifest.load(run_dir / "manifest.json")
    assert any("PR.md draft failed" in w for w in persisted.warnings)


def test_pr_draft_not_attempted_when_run_not_done(fixture_repo, monkeypatch):
    import gauntlet.engine.pr as pr

    mgr = _prepare(fixture_repo)
    layout = mgr.layout("toy")
    run_dir = layout.slug_dir / "run-x"
    run_dir.mkdir(parents=True)
    man = M.Manifest(
        run_id="run-x", slug="toy", branch="gauntlet/toy", base_branch="main",
        pipeline=M.PipelineRef(name="p", version=1, hash="h"),
    )
    monkeypatch.setattr(pr, "write_pr_draft",
                        lambda *a, **k: pytest.fail("should not draft when parked"))
    mgr._maybe_draft_pr(layout, run_dir, man, M.RUN_PARKED)  # no raise, no draft
    assert man.warnings == []

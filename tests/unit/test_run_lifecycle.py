"""Run lifecycle: new, entry contract, run, gates, rollback (FR-8, FR-10, F-010)."""

from __future__ import annotations

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
    # a backup ref preserved the pre-rollback tip
    refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/backup/")
    assert p2_sha in refs or "refs/gauntlet/backup/" in refs


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

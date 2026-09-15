"""#164: completion survives stale projections and transfer through Git."""
from pathlib import Path
import os

import pytest

from conftest import git
from gauntlet.engine import journal
from gauntlet.engine.manifest import Manifest, PipelineRef, StepRecord
from gauntlet.web.store import RunNotFound, RunStore
from gauntlet.web.watcher import Watcher
from test_run_branch_lifecycle import (
    CONFIG_YAML, GATED, _author_prd, _prepare, _run_linear, _write_pipeline,
)


def _run(repo: Path):
    rd = repo / "runs/demo/run-2026-01-01T00-00-00"
    rd.mkdir(parents=True)
    (rd / "steps/build").mkdir(parents=True)
    man = Manifest(
        slug="demo", run_id=rd.name, branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="test", version=1, hash="sha256:test"),
        status="running", current_step="build",
        steps=[StepRecord(id="build", type="agent_task", status="running")],
    )
    man.write_atomic(rd / "manifest.json")
    return rd, man, RunStore.from_repo(repo)


@pytest.mark.parametrize("damage", ["stale", "corrupt", "missing", "unjournaled"])
def test_console_reads_completed_journal_without_repairing_projection(tmp_path, damage):
    rd, man, store = _run(tmp_path)
    path = rd / "manifest.json"
    old = path.read_bytes()
    man.status = "done"
    man.current_step = None
    man.steps[0].status = "done"
    man.write_atomic(path)
    if damage == "stale":
        path.write_bytes(old)
    elif damage == "corrupt":
        path.write_text("{")
    elif damage == "missing":
        path.unlink()
    else:
        rogue = man.model_copy(deep=True)
        rogue.status = "parked"
        path.write_text(rogue.model_dump_json())
    before = {p.relative_to(rd): p.read_bytes() for p in rd.rglob("*") if p.is_file()}

    assert store.list_rows()[0].status == "done"
    assert store.run_history("demo")[0]["status"] == "done"
    assert store.manifest("demo").status == "done"
    assert store.step_detail("demo", "build").status == "done"
    events = Watcher(store).poll_once()
    assert len(events) == 1 and events[0].run_status == "done"
    after = {p.relative_to(rd): p.read_bytes() for p in rd.rglob("*") if p.is_file()}
    assert after == before


def test_watcher_observes_journal_append_without_projection_write(tmp_path):
    rd, man, store = _run(tmp_path)
    path = rd / "manifest.json"
    watcher = Watcher(store)
    assert watcher.poll_once()[0].run_status == "running"
    old = path.read_bytes()
    previous_revision = max(x for x in store.state_revision(path) if x is not None)
    man.status = "done"
    man.current_step = None
    man.steps[0].status = "done"
    # Reproduce a driver killed after the authoritative append, before the
    # projection write. Its mtime and bytes never change.
    journal.record_transition(path, man.model_dump_json(indent=2))
    stamp = previous_revision + 1_000_000
    os.utime(rd / "journal", ns=(stamp, stamp))
    events = watcher.poll_once()
    assert len(events) == 1 and events[0].run_status == "done"
    assert watcher.poll_once() == []
    assert path.read_bytes() == old


def test_console_still_reads_legacy_manifest_only_run(tmp_path):
    rd, man, store = _run(tmp_path)
    for p in (rd / "journal").iterdir():
        p.unlink()
    (rd / "journal").rmdir()
    assert store.manifest("demo").status == "running"
    assert not (rd / "journal").exists()


def test_console_refuses_journal_symlink_outside_run_root(tmp_path):
    repo = tmp_path / "repo"
    rd, man, store = _run(repo)
    (rd / "journal").rename(tmp_path / "outside-journal")
    (rd / "journal").symlink_to(tmp_path / "outside-journal")
    with pytest.raises(RunNotFound, match="path escapes"):
        store.manifest("demo")


@pytest.mark.parametrize("mode", ["same_tree", "dedicated"])
@pytest.mark.parametrize("gate_only", [False, True])
def test_completion_is_committed_and_visible_in_fresh_worktree(fixture_repo, tmp_path, mode, gate_only):
    mgr = _prepare(fixture_repo, CONFIG_YAML + f"worktree:\n  mode: {mode}\n")
    if gate_only:
        _author_prd(mgr, "demo")
        pipeline = _write_pipeline(fixture_repo, GATED)
        assert mgr.start("demo", pipeline, use_judge=False) == "parked"
        assert mgr.approve("demo", use_judge=False) == "done"
    else:
        assert _run_linear(mgr, fixture_repo, "demo") == "done"
    man = mgr.status("demo")
    relative = f"runs/demo/{man.run_id}/completion.json"
    committed = Manifest.model_validate_json(git(fixture_repo, "show", f"gauntlet/demo:{relative}"))
    assert committed.status == "done"
    assert committed.current_step is None
    assert all(s.status in ("done", "skipped") for s in committed.steps)
    files = git(fixture_repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "gauntlet/demo").splitlines()
    assert set(files) == {relative}
    assert git(fixture_repo, "log", "-1", "--format=%an", "gauntlet/demo").strip() == "Gauntlet Engine"
    fresh = tmp_path / "fresh-view"
    git(fixture_repo, "worktree", "add", "--detach", str(fresh), "gauntlet/demo")
    assert not (fresh / relative).parent.joinpath("journal").exists()
    assert RunStore.from_repo(fresh).list_rows()[0].status == "done"


def test_failed_completion_export_retries_without_rerunning_agents(fixture_repo, monkeypatch):
    from gauntlet.engine import gitops

    mgr = _prepare(fixture_repo)
    real_commit = gitops.commit_run_bookkeeping

    def fail_final(repo, message, paths, **kwargs):
        if message == "gauntlet: complete demo":
            raise gitops.GitError(["commit"], 1, "simulated final export failure")
        return real_commit(repo, message, paths, **kwargs)

    monkeypatch.setattr(gitops, "commit_run_bookkeeping", fail_final)
    with pytest.raises(gitops.GitError, match="simulated final export failure"):
        _run_linear(mgr, fixture_repo, "demo")
    man = mgr.status("demo")
    assert man.status == "done"
    assert all(s.status in ("done", "skipped") for s in man.steps)
    monkeypatch.setattr(gitops, "commit_run_bookkeeping", real_commit)

    def no_agent(name):
        pytest.fail("completed work must not run again to publish the snapshot")

    assert mgr.resume("demo", use_judge=False, adapter_factory=no_agent) == "done"
    relative = f"runs/demo/{man.run_id}/completion.json"
    committed = Manifest.model_validate_json(git(fixture_repo, "show", f"gauntlet/demo:{relative}"))
    assert committed.status == "done"
    run_tip = git(fixture_repo, "rev-parse", "gauntlet/demo")
    assert mgr.resume("demo", use_judge=False, adapter_factory=no_agent) == "done"
    assert git(fixture_repo, "rev-parse", "gauntlet/demo") == run_tip


@pytest.mark.parametrize("with_manifest", [True, False])
def test_portable_completion_bootstraps_same_state_for_cli_and_mutations(fixture_repo, with_manifest):
    from gauntlet.engine import manifest as M, operator

    mgr = _prepare(fixture_repo)
    rd, man, store = _run(fixture_repo)
    for path in (rd / "journal").iterdir():
        path.unlink()
    (rd / "journal").rmdir()
    stale = (rd / "manifest.json").read_bytes()
    man.status = "done"
    man.current_step = None
    man.steps[0].status = "done"
    (rd / "completion.json").write_text(man.model_dump_json(indent=2))
    if not with_manifest:
        (rd / "manifest.json").unlink()
    assert store.list_rows()[0].status == "done"
    assert operator.load_projection_view(fixture_repo, rd).manifest.status == "done"
    assert not (rd / "journal").exists()  # reads have no migration side effects

    mgr._reconcile_projection(rd, "demo")
    assert Manifest.load(rd / "manifest.json").status == "done"
    genesis = journal.read_events(rd)[0]
    assert genesis["payload"]["migrated_from"] == "completion.json"
    assert journal.projection_status(rd, validate=M.validate_projection_text).health == journal.HEALTH_OK
    if with_manifest:
        assert any(path.read_bytes() == stale for path in rd.glob("manifest.*json") if path.name != "manifest.json")

    # A later sanctioned rollback belongs to the local journal and must beat
    # the old imported completion record for both console and CLI readers.
    man.status = "running"
    man.current_step = "build"
    man.steps[0].status = "pending"
    man.write_atomic(rd / "manifest.json")
    assert store.manifest("demo").status == "running"
    assert operator.load_projection_view(fixture_repo, rd).manifest.status == "running"


@pytest.mark.parametrize("invalid", ["slug", "run_id", "pipeline", "status", "corrupt"])
def test_invalid_completion_cannot_override_legacy_state(tmp_path, invalid):
    rd, man, store = _run(tmp_path)
    for path in (rd / "journal").iterdir():
        path.unlink()
    (rd / "journal").rmdir()
    man.status = "done"
    man.current_step = None
    if invalid == "pipeline":
        man.pipeline.hash = "different"
    elif invalid in ("slug", "run_id"):
        setattr(man, invalid, "different")
    elif invalid == "status":
        man.status = "running"
    (rd / "completion.json").write_text("{" if invalid == "corrupt" else man.model_dump_json())
    assert store.manifest("demo").status == "running"
    assert not (rd / "journal").exists()


def test_watcher_observes_imported_completion_without_manifest_change(tmp_path):
    rd, man, store = _run(tmp_path)
    for path in (rd / "journal").iterdir():
        path.unlink()
    (rd / "journal").rmdir()
    watcher = Watcher(store)
    assert watcher.poll_once()[0].run_status == "running"
    man.status = "done"
    man.current_step = None
    (rd / "completion.json").write_text(man.model_dump_json())
    assert watcher.poll_once()[0].run_status == "done"
    assert watcher.poll_once() == []


def test_console_refuses_completion_symlink_outside_run_root(tmp_path):
    rd, man, store = _run(tmp_path / "repo")
    outside = tmp_path / "completion.json"
    outside.write_text(man.model_dump_json())
    (rd / "completion.json").symlink_to(outside)
    with pytest.raises(RunNotFound, match="path escapes"):
        store.manifest("demo")


@pytest.mark.parametrize("tamper", [False, True])
def test_completed_run_recreates_missing_worktree_without_replaying_agents(fixture_repo, tamper):
    import shutil
    from gauntlet.engine import worktree as WT

    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == "done"
    man = mgr.status("demo")
    state = WT.describe(fixture_repo, mode=WT.MODE_DEDICATED, branch=man.branch)
    tip = git(fixture_repo, "rev-parse", man.branch)
    if tamper:
        (state.path / "unrecorded.txt").write_text("unrecorded branch change")
        git(state.path, "add", "unrecorded.txt")
        git(state.path, "commit", "-qm", "unrecorded human change")
    shutil.rmtree(state.path)

    def no_agent(name):
        pytest.fail("recreating a completed tree must not replay agents")

    if tamper:
        from gauntlet.engine.run import WorktreeUnavailableError

        with pytest.raises(WorktreeUnavailableError, match="journal disagree"):
            mgr.resume("demo", use_judge=False, adapter_factory=no_agent)
        return
    assert mgr.resume("demo", use_judge=False, adapter_factory=no_agent) == "done"
    from gauntlet.engine import gitops
    from gauntlet.engine.execution import engine_bookkeeping_candidates

    # The missing-tree diagnostic can add a warning to the final snapshot;
    # any new commit must still be metadata alone, with every prior commit kept.
    restored = WT.describe(fixture_repo, mode=WT.MODE_DEDICATED, branch=man.branch)
    assert gitops.advance_is_engine_bookkeeping(
        fixture_repo, tip.strip(), tip=gitops.head_sha(restored.path),
        bookkeeping=engine_bookkeeping_candidates(
            restored.path, restored.path / "runs/demo" / man.run_id,
        ),
    )
    assert RunStore.from_repo(fixture_repo).manifest("demo").status == "done"



def test_completion_retries_a_failed_recovery_audit_without_new_commit(fixture_repo, monkeypatch):
    mgr = _prepare(fixture_repo)
    real_append = journal.append_audit

    def fail_completion(run_dir, kind, *args, **kwargs):
        if kind == "CompletionExported":
            return None
        return real_append(run_dir, kind, *args, **kwargs)

    monkeypatch.setattr(journal, "append_audit", fail_completion)
    with pytest.raises(journal.JournalError, match="recovery record"):
        _run_linear(mgr, fixture_repo, "demo")
    assert mgr.status("demo").status == "done"
    tip = git(fixture_repo, "rev-parse", "gauntlet/demo")
    monkeypatch.setattr(journal, "append_audit", real_append)

    def no_agent(name):
        pytest.fail("an audit retry must not replay agents")

    assert mgr.resume("demo", use_judge=False, adapter_factory=no_agent) == "done"
    assert git(fixture_repo, "rev-parse", "gauntlet/demo") == tip
    rd = mgr.layout("demo").active_run_dir()
    assert mgr._recorded_branch_sha(rd, mgr.status("demo"), "gauntlet/demo") == tip.strip()

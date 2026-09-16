"""#164: completion survives stale projections and transfer through Git."""
from pathlib import Path
import os

import pytest

from conftest import git
from gauntlet.engine import journal
from gauntlet.engine.config import RunConfig
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


# --- review fixes (PR #165) ---------------------------------------------------
#
# Each test below pins one finding from the PR review; the name says which.


def _done_copy(man: Manifest) -> Manifest:
    done = man.model_copy(deep=True)
    done.status = "done"
    done.current_step = None
    for step in done.steps:
        step.status = "done"
    return done


def _drop_journal(rd: Path) -> None:
    for path in (rd / "journal").iterdir():
        path.unlink()
    (rd / "journal").rmdir()


def _bump(path: Path, seconds: int = 2) -> None:
    """Advance a path's mtime deterministically (no reliance on fs granularity)."""
    st = path.stat()
    stamp = st.st_mtime_ns + seconds * 1_000_000_000
    os.utime(path, ns=(stamp, stamp))


def test_same_tree_completion_never_moves_operator_checkout(fixture_repo):
    """F-1: a same-tree run completed by ``approve`` from another branch lands
    its export on the run branch through the object database; the operator's
    branch, index and working tree — including uncommitted edits that would
    make a ``git checkout`` refuse — are byte-identical before and after."""
    from conftest import _operator_fingerprint
    from gauntlet.engine import gitops

    mgr = _prepare(fixture_repo, CONFIG_YAML + "worktree:\n  mode: same_tree\n")
    _author_prd(mgr, "demo")
    pipeline = _write_pipeline(fixture_repo, GATED)
    assert mgr.start("demo", pipeline, use_judge=False) == "parked"
    git(fixture_repo, "checkout", "-q", "main")
    (fixture_repo / "README.md").write_text("operator edit in progress\n")
    before = _operator_fingerprint(fixture_repo)

    assert mgr.approve("demo", use_judge=False) == "done"

    assert gitops.current_branch(fixture_repo) == "main"
    assert _operator_fingerprint(fixture_repo) == before
    assert (fixture_repo / "README.md").read_text() == "operator edit in progress\n"
    man = mgr.status("demo")
    relative = f"runs/demo/{man.run_id}/completion.json"
    committed = Manifest.model_validate_json(git(fixture_repo, "show", f"gauntlet/demo:{relative}"))
    assert committed.status == "done"
    files = git(fixture_repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "gauntlet/demo").splitlines()
    assert set(files) == {relative}
    assert git(fixture_repo, "log", "-1", "--format=%an", "gauntlet/demo").strip() == "Gauntlet Engine"
    # The export is journaled in same-tree mode too, at the branch tip.
    rd = mgr.layout("demo").active_run_dir()
    tip = git(fixture_repo, "rev-parse", "gauntlet/demo").strip()
    assert mgr._recorded_branch_sha(rd, man, "gauntlet/demo") == tip


def test_commit_file_to_branch_is_idempotent_and_refuses_checked_out_branch(fixture_repo, tmp_path):
    from gauntlet.engine import gitops

    git(fixture_repo, "branch", "target")
    first = gitops.commit_file_to_branch(
        fixture_repo, "target", "runs/x/completion.json", "{}\n", "gauntlet: complete x",
        identity=gitops.ENGINE_IDENTITY,
    )
    assert first == git(fixture_repo, "rev-parse", "target").strip()
    assert git(fixture_repo, "show", "target:runs/x/completion.json") == "{}\n"
    assert gitops.commit_file_to_branch(
        fixture_repo, "target", "runs/x/completion.json", "{}\n", "gauntlet: complete x",
        identity=gitops.ENGINE_IDENTITY,
    ) is None
    assert git(fixture_repo, "rev-parse", "target").strip() == first
    assert gitops.current_branch(fixture_repo) == "main"
    assert git(fixture_repo, "status", "--porcelain").strip() == ""
    git(fixture_repo, "worktree", "add", "-q", str(tmp_path / "held"), "target")
    with pytest.raises(gitops.GitError, match="checked out"):
        gitops.commit_file_to_branch(
            fixture_repo, "target", "runs/x/completion.json", "{2}\n", "gauntlet: complete x",
            identity=gitops.ENGINE_IDENTITY,
        )


def test_failed_completion_audit_then_missing_tree_still_resumes(fixture_repo, monkeypatch):
    """F-2: the advertised "resume to retry" works even when the dedicated tree
    vanished between the completion commit and the retry: a branch tip that is
    the recorded SHA plus engine bookkeeping only is accepted by the recreate."""
    import shutil
    from gauntlet.engine import gitops, worktree as WT
    from gauntlet.engine.execution import engine_bookkeeping_candidates

    mgr = _prepare(fixture_repo)
    real_append = journal.append_audit

    def fail_completion(run_dir, kind, *args, **kwargs):
        if kind == "CompletionExported":
            return None
        return real_append(run_dir, kind, *args, **kwargs)

    monkeypatch.setattr(journal, "append_audit", fail_completion)
    with pytest.raises(journal.JournalError, match="recovery record"):
        _run_linear(mgr, fixture_repo, "demo")
    monkeypatch.setattr(journal, "append_audit", real_append)
    man = mgr.status("demo")
    tip = git(fixture_repo, "rev-parse", man.branch).strip()
    state = WT.describe(fixture_repo, mode=WT.MODE_DEDICATED, branch=man.branch)
    shutil.rmtree(state.path)

    def no_agent(name):
        pytest.fail("an audit retry must not replay agents")

    assert mgr.resume("demo", use_judge=False, adapter_factory=no_agent) == "done"
    new_tip = git(fixture_repo, "rev-parse", man.branch).strip()
    restored = WT.describe(fixture_repo, mode=WT.MODE_DEDICATED, branch=man.branch)
    assert gitops.advance_is_engine_bookkeeping(
        fixture_repo, tip, tip=new_tip,
        bookkeeping=engine_bookkeeping_candidates(
            restored.path, restored.path / "runs/demo" / man.run_id,
        ),
    )
    rd = mgr.layout("demo").active_run_dir()
    assert mgr._recorded_branch_sha(rd, mgr.status("demo"), man.branch) == new_tip
    # A genuinely foreign tip is still refused.
    (restored.path / "unrecorded.txt").write_text("unrecorded branch change")
    git(restored.path, "add", "unrecorded.txt")
    git(restored.path, "commit", "-qm", "unrecorded human change")
    shutil.rmtree(restored.path)
    from gauntlet.engine.run import WorktreeUnavailableError

    with pytest.raises(WorktreeUnavailableError, match="journal disagree"):
        mgr.resume("demo", use_judge=False, adapter_factory=no_agent)


@pytest.mark.parametrize("bad", ["{", "[]", "null", "\"text\""])
def test_corrupt_legacy_manifest_cannot_veto_valid_completion(fixture_repo, bad):
    """F-3: an unparseable / non-object manifest.json beside a valid
    completion.json (a torn or conflict-marked checkpoint in a fresh checkout)
    skips the identity comparison instead of discarding the snapshot."""
    from gauntlet.engine import operator

    mgr = _prepare(fixture_repo)
    rd, man, store = _run(fixture_repo)
    _drop_journal(rd)
    (rd / "completion.json").write_text(_done_copy(man).model_dump_json(indent=2) + "\n")
    (rd / "manifest.json").write_text(bad)
    assert store.manifest("demo").status == "done"
    assert operator.load_projection_view(fixture_repo, rd).manifest.status == "done"
    assert not (rd / "journal").exists()
    mgr._reconcile_projection(rd, "demo")
    assert journal.read_events(rd)[0]["payload"]["migrated_from"] == "completion.json"
    assert Manifest.load(rd / "manifest.json").status == "done"
    # The corrupt bytes were preserved, never silently discarded.
    assert any(
        p.read_text() == bad for p in rd.glob("manifest.*json") if p.name != "manifest.json"
    )


def test_completion_snapshot_accepts_pipeline_ref_with_extra_defaulted_field(tmp_path):
    """F-3 (cont.): pipeline identity is the hash the engine itself checks, so
    a serialization difference in the ref cannot reject a snapshot."""
    import json

    rd, man, store = _run(tmp_path)
    _drop_journal(rd)
    snapshot = json.loads(_done_copy(man).model_dump_json())
    snapshot["pipeline"] = {**snapshot["pipeline"], "hash": man.pipeline.hash}
    legacy = json.loads((rd / "manifest.json").read_text())
    legacy["pipeline"] = {"name": man.pipeline.name, "hash": man.pipeline.hash}
    (rd / "manifest.json").write_text(json.dumps(legacy))
    (rd / "completion.json").write_text(json.dumps(snapshot))
    assert store.manifest("demo").status == "done"


def test_completion_import_over_checkpoint_manifest_adds_no_warning_or_commit(fixture_repo):
    """F-4: importing the snapshot over the branch's checkpoint manifest is a
    deliberate supersession, not an out-of-band edit: no "[projection]"
    warning is stamped into the manifest, so a later resume of the done run
    re-exports identical bytes and mints no new commit."""
    import shutil

    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == "done"
    man = mgr.status("demo")
    rd = mgr.layout("demo").active_run_dir()
    tip = git(fixture_repo, "rev-parse", man.branch).strip()
    completion = git(fixture_repo, "show", f"{man.branch}:runs/demo/{man.run_id}/completion.json")
    # A fresh checkout: no journal; the branch's last checkpoint (running)
    # projection beside the committed terminal snapshot.
    shutil.rmtree(rd / "journal")
    stale = man.model_copy(deep=True)
    stale.status = "running"
    stale.current_step = stale.steps[0].id
    stale.steps[0].status = "running"
    (rd / "manifest.json").write_text(stale.model_dump_json(indent=2))
    (rd / "completion.json").write_text(completion)

    def no_agent(name):
        pytest.fail("importing a completion must not replay agents")

    assert mgr.resume("demo", use_judge=False, adapter_factory=no_agent) == "done"
    after = Manifest.load(rd / "manifest.json")
    assert not any(w.startswith("[projection]") for w in after.warnings)
    assert git(fixture_repo, "rev-parse", man.branch).strip() == tip
    events = journal.read_events(rd)
    assert events[0]["kind"] == "JournalGenesis"
    assert events[0]["payload"]["migrated_from"] == "completion.json"
    preserved = [p for p in rd.glob("manifest.unjournaled-*.json")]
    assert preserved and Manifest.model_validate_json(preserved[0].read_text()).status == "running"
    assert mgr.resume("demo", use_judge=False, adapter_factory=no_agent) == "done"
    assert git(fixture_repo, "rev-parse", man.branch).strip() == tip


def test_console_gate_diff_and_supervisor_read_authoritative_state(fixture_repo):
    """F-5: the gate/diff resolvers and the job supervisor classify from the
    same journal-aware state as the run list — a stale parked projection over
    a journal-done run offers no gate, and a completion-only checkout does not
    500."""
    from fastapi.testclient import TestClient

    from gauntlet.web.service import TOKEN_HEADER, create_app
    from gauntlet.web.supervisor import JobSupervisor

    rd, man, store = _run(fixture_repo)
    path = rd / "manifest.json"
    parked = man.model_copy(deep=True)
    parked.status = "parked"
    parked.current_step = "gate"
    parked.steps = [
        StepRecord(id="build", type="agent_task", status="done"),
        StepRecord(id="gate", type="human_gate", status="parked", notes="awaiting human"),
    ]
    parked.write_atomic(path)
    parked_bytes = path.read_bytes()
    done = _done_copy(parked)
    done.write_atomic(path)
    path.write_bytes(parked_bytes)  # kill window: journal says done, file says parked

    headers = {TOKEN_HEADER: "t"}
    client = TestClient(create_app(store, token="t"), raise_server_exceptions=False)
    assert client.get("/api/runs", headers=headers).json()[0]["status"] == "done"
    assert client.get("/api/runs/demo/gate", headers=headers).status_code == 404
    assert client.get("/runs/demo/diff", headers=headers).status_code != 500
    sup = JobSupervisor(fixture_repo, RunConfig())
    assert sup._load_manifest(rd).status == "done"

    # A fresh checkout carrying only the terminal snapshot.
    _drop_journal(rd)
    path.unlink()
    (rd / "completion.json").write_text(done.model_dump_json(indent=2) + "\n")
    fresh = RunStore.from_repo(fixture_repo)
    client = TestClient(create_app(fresh, token="t"), raise_server_exceptions=False)
    assert client.get("/api/runs", headers=headers).json()[0]["status"] == "done"
    assert client.get("/runs/demo/diff", headers=headers).status_code != 500
    assert client.get("/api/runs/demo/gate", headers=headers).status_code == 404
    assert sup._load_manifest(rd).status == "done"


def test_bare_journal_dir_is_not_a_run(tmp_path):
    """F-6: an empty (or quarantine-only) journal/ dir — the engine creates it
    before the first event — never selects an unreadable newest run dir and
    hides the slug's readable history behind it."""
    rd, man, store = _run(tmp_path)
    newer = rd.parent / "run-2026-01-02T00-00-00"
    (newer / "journal").mkdir(parents=True)
    assert [r.run_id for r in store.list_rows()] == [rd.name]
    assert store.manifest("demo").run_id == rd.name
    assert [e["run_id"] for e in store.run_history("demo")] == [rd.name]
    (newer / "journal" / "evt-00000001-JournalGenesis-abcdefabcdef.json.torn").write_text("{")
    assert store.manifest("demo").run_id == rd.name
    with pytest.raises(RunNotFound):
        store.manifest("demo", newer.name)
    # An explicit pointer at the empty dir cannot resurrect it either.
    (rd.parent / "active-run.txt").write_text(newer.name)
    assert store.manifest("demo").run_id == rd.name


def test_superseded_completion_import_follows_upstream_rollback(fixture_repo):
    """F-7: a checkout that only IMPORTED a completion (no local transition on
    top of the genesis) stops trusting that import once Git no longer carries
    the snapshot — an upstream rollback rewinds the branch and drops
    completion.json — and bootstraps again from what the branch holds now."""
    from gauntlet.engine import manifest as M, operator

    mgr = _prepare(fixture_repo)
    rd, man, store = _run(fixture_repo)
    _drop_journal(rd)
    checkpoint = (rd / "manifest.json").read_text()  # the branch's tracked checkpoint
    (rd / "completion.json").write_text(_done_copy(man).model_dump_json(indent=2) + "\n")
    mgr._reconcile_projection(rd, "demo")
    assert store.manifest("demo").status == "done"
    assert journal.read_events(rd)[0]["payload"]["migrated_from"] == "completion.json"

    # Upstream rolled back: `git reset --hard` to the rewound tip restores the
    # checkpoint manifest and removes the snapshot.
    (rd / "completion.json").unlink()
    (rd / "manifest.json").write_text(checkpoint)
    _bump(rd / "manifest.json")
    assert store.manifest("demo").status == "running"
    view = operator.load_projection_view(fixture_repo, rd)
    assert view.manifest.status == "running"
    assert view.health == journal.HEALTH_NO_JOURNAL
    mgr._reconcile_projection(rd, "demo")
    events = journal.read_events(rd)
    assert events[-1]["kind"] == "JournalGenesis"
    assert events[-1]["payload"]["migrated_from"] == "manifest.json"
    assert Manifest.load(rd / "manifest.json").status == "running"
    assert journal.projection_status(rd, validate=M.validate_projection_text).health == journal.HEALTH_OK
    # ... and it is idempotent: a second contact seeds nothing more.
    mgr._reconcile_projection(rd, "demo")
    assert len(journal.read_events(rd)) == len(events)


def test_superseded_import_with_no_other_source_reports_no_state(fixture_repo):
    """F-7 (cont.): when the rewound branch carries no manifest either, the
    projection this checkout wrote from the superseded import is not a
    bootstrap source; the run reads as unavailable rather than done."""
    from gauntlet.engine import operator

    mgr = _prepare(fixture_repo)
    rd, man, store = _run(fixture_repo)
    _drop_journal(rd)
    (rd / "manifest.json").unlink()
    (rd / "completion.json").write_text(_done_copy(man).model_dump_json(indent=2) + "\n")
    mgr._reconcile_projection(rd, "demo")
    assert store.manifest("demo").status == "done"
    (rd / "completion.json").unlink()
    assert operator.load_projection_view(fixture_repo, rd).manifest is None
    with pytest.raises(RunNotFound):
        store.manifest("demo")


def test_local_transition_after_import_keeps_journal_authority(fixture_repo):
    """F-7 (cont.): once this checkout journals its own transition on top of
    the import, the local journal is authoritative for good — with or without
    the snapshot on disk."""
    mgr = _prepare(fixture_repo)
    rd, man, store = _run(fixture_repo)
    _drop_journal(rd)
    (rd / "completion.json").write_text(_done_copy(man).model_dump_json(indent=2) + "\n")
    mgr._reconcile_projection(rd, "demo")
    rolled = man.model_copy(deep=True)
    rolled.status = "running"
    rolled.write_atomic(rd / "manifest.json")
    (rd / "completion.json").unlink()
    assert store.manifest("demo").status == "running"
    (rd / "completion.json").write_text(_done_copy(man).model_dump_json(indent=2) + "\n")
    assert store.manifest("demo").status == "running"


def test_run_manager_status_reads_journal_head_not_stale_projection(fixture_repo):
    """F-8: ``RunManager.status`` (``gauntlet report`` and the CLI echoes) can
    never disagree with ``gauntlet status``: a projection left one journaled
    state behind is resolved from the head."""
    from gauntlet.engine.report import render_report

    mgr = _prepare(fixture_repo)
    assert _run_linear(mgr, fixture_repo, "demo") == "done"
    rd = mgr.layout("demo").active_run_dir()
    older = next(
        e["state_json"] for e in journal.read_events(rd)
        if e.get("state_json") and '"status": "running"' in e["state_json"]
    )
    (rd / "manifest.json").write_text(older)
    assert Manifest.load(rd / "manifest.json").status == "running"
    assert mgr.status("demo").status == "done"
    assert "[done]" in render_report(mgr.status("demo"))


def test_watcher_emits_one_transition_across_the_two_step_persist(tmp_path):
    """F-9: the engine's persist is journal append THEN projection replace. A
    poll landing between the two, and the next poll after the replace, must
    observe ONE transition (FR-8.1: exactly once); an audit-only append is no
    transition at all."""
    from gauntlet.engine.manifest import _replace_atomic

    rd, man, store = _run(tmp_path)
    path = rd / "manifest.json"
    watcher = Watcher(store)
    assert watcher.poll_once()[0].run_status == "running"
    man.status = "parked"
    man.steps[0].status = "parked"
    payload = man.model_dump_json(indent=2)
    journal.record_transition(path, payload)
    _bump(rd / "journal")
    mid = watcher.poll_once()
    assert len(mid) == 1 and mid[0].run_status == "parked"
    _replace_atomic(path, payload)
    _bump(path)
    assert watcher.poll_once() == []
    journal.append_audit(
        rd, "WorktreeAdopted", {"branch": man.branch, "branch_sha": "abc"},
        run_id=man.run_id, idempotency_key="audit-only",
    )
    _bump(rd / "journal", 4)
    assert watcher.poll_once() == []
    # A genuine re-persist of the same semantic state is still a new identity.
    man.totals.input_tokens += 7
    man.write_atomic(path)
    _bump(rd / "journal", 6)
    again = watcher.poll_once()
    assert len(again) == 1 and again[0].revision > mid[0].revision


def test_console_memoises_projection_view_on_state_revision(tmp_path, monkeypatch):
    """F-10: the read-only resolver replays the whole journal per call; the
    console resolves each run once per state revision, not once per row,
    history entry, manifest fetch and watcher tick."""
    from gauntlet.engine import operator

    rd, man, store = _run(tmp_path)
    calls: list[int] = []
    real = operator.load_projection_view

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(operator, "load_projection_view", counting)
    store.list_rows()
    store.run_history("demo")
    store.manifest("demo")
    store.step_detail("demo", "build")
    Watcher(store).poll_once()
    assert len(calls) == 1
    man.status = "done"
    man.current_step = None
    man.steps[0].status = "done"
    man.write_atomic(rd / "manifest.json")
    _bump(rd / "manifest.json")
    assert store.manifest("demo").status == "done"
    assert len(calls) == 2
    store.list_rows()
    assert len(calls) == 2


def test_console_still_refuses_journal_event_symlink_outside_run_root(tmp_path):
    rd, man, store = _run(tmp_path / "repo")
    outside = tmp_path / "evt-00000099-JournalGenesis-abcdefabcdef.json"
    outside.write_text("{}")
    (rd / "journal" / outside.name).symlink_to(outside)
    with pytest.raises(RunNotFound, match="path escapes"):
        store.manifest("demo")


def test_row_updated_follows_the_newest_state_source(tmp_path):
    """A row resolved from a journal append the projection never caught up
    with sorts by that append, not by the stale projection's mtime."""
    rd, man, store = _run(tmp_path)
    before = store.list_rows()[0].updated
    done = _done_copy(man)
    journal.record_transition(rd / "manifest.json", done.model_dump_json(indent=2))
    _bump(rd / "journal", 30)
    row = store.list_rows()[0]
    assert row.status == "done"
    assert row.updated > before
    assert store.run_history("demo")[0]["updated"] == row.updated

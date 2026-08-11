"""The #97 three-way governed-artifact publish, driven through real verbs.

The bug, stated plainly: every root resolution republished the OPERATOR
CHECKOUT's `prd.md`/`plan.md` into the run tree unconditionally. Mid-phase fix
rounds legitimately AMEND the governed artifact on the run branch
(amendments-ledger entries, FR-10.4 upstream fixes), after which the checkout
copy lags the branch — and the next `resume` overwrote the run tree with the
stale bytes, a git-visible pure deletion of ratified amendments that failed the
FR-9.3 clean-handoff guard on every subsequent resume (issue #97). The same
pre-guard publish re-dirtied the tree on every `rollback` invocation (#99).

The fix records what the engine last published (``govsync``) and compares three
states — checkout bytes, last-published record, run-branch committed bytes —
before publishing anything. These tests reproduce the issue's exact repro
shape and pin every arm of the compare through the production resolution path
(:meth:`RunManager._run_paths`, the same context manager `start`/`resume`/
`approve`/`rollback` enter).
"""

from __future__ import annotations

import json

import pytest

from gauntlet.engine import gitops
from gauntlet.engine import govsync as GS
from gauntlet.engine import manifest as M
from gauntlet.engine import worktree as WT
from gauntlet.engine.run import RunManager

from conftest import FakeAdapter, git

CONFIG_DEDICATED = """
base_branch: main
run_root: runs
worktree:
  mode: dedicated
agents:
  builder: {adapter: claude-code}
"""

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

TWO_PHASE = """
name: p
version: 1
stages:
  - id: p1
    steps:
      - {id: a, type: agent_task, agent: builder, prompt_text: go}
      - {id: c1, type: commit, message: "P1: phase one\\n\\nbody one."}
  - id: p2
    steps:
      - {id: b, type: agent_task, agent: builder, prompt_text: go}
      - {id: c2, type: commit, message: "P2: phase two\\n\\nbody two."}
"""

AUTHORED = b"# Real PRD\n\nA genuine PRD.\n"
AMENDED = (
    b"# Real PRD\n\nA genuine PRD.\n\n## Amendments ledger\n\n"
    b"- A-001: ratified by the human at the P1 response gate.\n"
)


def _prepare(repo) -> RunManager:
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_DEDICATED)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _pipeline(repo, text: str):
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / "p.yaml"
    path.write_text(text)
    git(repo, "add", "pipelines")
    git(repo, "commit", "-qm", "add pipeline")
    return path


def _author_prd(mgr: RunManager, slug: str) -> None:
    mgr.new(slug)
    mgr.layout(slug).prd_path.write_bytes(AUTHORED)


def _run_worktree(mgr: RunManager, branch: str):
    return WT.observe(
        mgr.operator_root, branch, main_root=mgr._main_worktree_root()
    )


def _completed_run(fixture_repo, pipeline_text: str = LINEAR):
    """A DONE dedicated run whose branch committed the published prd.md."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _pipeline(fixture_repo, pipeline_text)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=factory) == M.RUN_DONE
    entry = _run_worktree(mgr, "gauntlet/demo")
    assert entry is not None
    assert gitops.file_bytes_at_commit(
        entry.path, "HEAD", "runs/demo/prd.md"
    ) == AUTHORED, "precondition: the run branch committed the published prd"
    return mgr, entry


def _resolve_roots(mgr: RunManager, slug: str = "demo"):
    """Enter the production root resolution, exactly as resume/rollback do."""
    man = mgr.status(slug)
    layout = mgr.layout(slug)
    run_dir = layout.run_dir(man.run_id)
    return mgr._run_paths(
        layout, run_dir, man, slug=slug, run_id=man.run_id,
        branch=man.branch, mode=mgr._effective_worktree_mode(man),
    )


def _amend_on_branch(entry_path, data: bytes = AMENDED) -> None:
    """A ratified amendment committed on the run branch, checkout NOT synced."""
    (entry_path / "runs" / "demo" / "prd.md").write_bytes(data)
    git(entry_path, "add", "runs/demo/prd.md")
    git(entry_path, "commit", "-qm", "P1.x: record amendments-ledger entry")


def _record_file(mgr: RunManager, slug: str = "demo"):
    man = mgr.status(slug)
    return mgr.layout(slug).run_dir(man.run_id) / GS.STATE_FILENAME


# --- the issue's repro shape: stale checkout must NOT clobber amendments -----


def test_republish_is_a_noop_when_only_the_branch_moved(fixture_repo):
    """Issue #97 steps (1)-(3): amendment on the branch, checkout stale.

    The next resolution must NOT republish the stale checkout bytes (the
    git-visible "pure deletion of ratified amendments" that failed FR-9.3),
    and the checkout must catch up from the run tree instead — the same
    back-sync the engine already performs at gates via ``adopt_artifact``.
    """
    mgr, entry = _completed_run(fixture_repo)
    _amend_on_branch(entry.path)
    checkout = fixture_repo / "runs" / "demo" / "prd.md"
    assert checkout.read_bytes() == AUTHORED, "precondition: checkout lags"

    with _resolve_roots(mgr):
        pass

    in_tree = entry.path / "runs" / "demo" / "prd.md"
    assert in_tree.read_bytes() == AMENDED, (
        "the publish must be a NO-OP: republishing the stale checkout bytes "
        "is exactly the #97 clobber"
    )
    assert gitops.status_porcelain(
        entry.path, untracked_all=True, paths=["runs/demo/prd.md"]
    ) == "", "and the run tree stays clean — nothing for FR-9.3 to refuse"
    assert checkout.read_bytes() == AMENDED, (
        "the operator's authoring surface catches up from the run branch"
    )

    # And the state is a fixpoint: the next resolution changes nothing.
    with _resolve_roots(mgr):
        pass
    assert in_tree.read_bytes() == AMENDED
    assert checkout.read_bytes() == AMENDED


def test_a_real_operator_edit_still_publishes(fixture_repo):
    """The authoring-surface contract survives: checkout edits reach the tree."""
    mgr, entry = _completed_run(fixture_repo)
    edited = AUTHORED + b"\nA sanctioned hand-edit by the operator.\n"
    checkout = fixture_repo / "runs" / "demo" / "prd.md"
    checkout.write_bytes(edited)

    with _resolve_roots(mgr):
        pass

    assert (entry.path / "runs" / "demo" / "prd.md").read_bytes() == edited, (
        "an operator edit over an unmoved branch publishes, as before"
    )
    assert checkout.read_bytes() == edited, "the checkout copy is untouched"

    # Repeated resolutions BEFORE the branch commits the edit must not revert
    # it (the published-but-uncommitted state is not a branch move).
    with _resolve_roots(mgr):
        pass
    assert (entry.path / "runs" / "demo" / "prd.md").read_bytes() == edited
    assert checkout.read_bytes() == edited


def test_three_way_divergence_refuses_loudly_and_mutates_nothing(fixture_repo):
    """Both sides moved: refuse with all three states named, leave everything."""
    mgr, entry = _completed_run(fixture_repo)
    _amend_on_branch(entry.path)
    edited = AUTHORED + b"\nA conflicting operator edit.\n"
    checkout = fixture_repo / "runs" / "demo" / "prd.md"
    checkout.write_bytes(edited)
    record_before = _record_file(mgr).read_text()

    with pytest.raises(GS.GovernedArtifactDivergence) as exc:
        with _resolve_roots(mgr):
            pass

    message = str(exc.value)
    assert "prd.md" in message
    assert str(checkout) in message, "the checkout path is named"
    assert str(entry.path / "runs" / "demo" / "prd.md") in message, (
        "the run-tree path is named"
    )
    assert GS.digest(edited) in message, "the checkout state is named"
    assert GS.digest(AMENDED) in message, "the branch state is named"
    assert GS.digest(AUTHORED) in message, "the last-published state is named"
    assert "adopt ONE side" in message, (
        "the refusal must tell the operator exactly how to resolve"
    )

    assert checkout.read_bytes() == edited, "the checkout copy is untouched"
    assert (entry.path / "runs" / "demo" / "prd.md").read_bytes() == AMENDED, (
        "the branch's amendments are untouched"
    )
    assert _record_file(mgr).read_text() == record_before, (
        "no state mutation: the recorded baseline did not move"
    )

    # The run stays drivable: adopting one side (branch wins) unblocks it.
    checkout.write_bytes(AMENDED)
    with _resolve_roots(mgr):
        pass
    assert (entry.path / "runs" / "demo" / "prd.md").read_bytes() == AMENDED


def test_first_contact_without_a_baseline_adopts_the_run_tree(fixture_repo):
    """A run predating the record must not resurrect the pre-fix clobber.

    With no recorded baseline the engine cannot prove the checkout was never
    edited, so the run tree is the baseline — never the checkout — and the
    divergent checkout bytes are preserved in a backup rather than silently
    overwritten.
    """
    mgr, entry = _completed_run(fixture_repo)
    _amend_on_branch(entry.path)
    record = _record_file(mgr)
    record.unlink()  # simulate the pre-#97 run: no baseline was ever written
    checkout = fixture_repo / "runs" / "demo" / "prd.md"
    assert checkout.read_bytes() == AUTHORED

    with _resolve_roots(mgr):
        pass

    assert (entry.path / "runs" / "demo" / "prd.md").read_bytes() == AMENDED, (
        "first contact must NOT assume operator authority"
    )
    assert checkout.read_bytes() == AMENDED, "the checkout catches up"
    assert record.exists(), "and the baseline now exists"
    man = mgr.status("demo")
    backups = list(
        (mgr.layout("demo").run_dir(man.run_id) / GS.BACKUP_DIRNAME).iterdir()
    )
    assert len(backups) == 1 and backups[0].read_bytes() == AUTHORED, (
        "the replaced checkout bytes are preserved, never destroyed"
    )
    assert any("prd.md" in w and "preserved" in w for w in man.warnings), (
        "the back-sync is named durably, not performed silently"
    )


def test_stale_publish_dirt_over_amendments_is_healed(fixture_repo):
    """Issue #97 step (3)'s live state: the pre-fix publish already clobbered.

    The tree copy sits uncommitted at exactly the stale checkout bytes over the
    committed amendments — the state that previously required the operator to
    re-sync the checkout AND restore the worktree file by hand. The resolution
    recognises the residue (byte-identical to the last-published state) and
    restores the committed bytes, leaving the tree clean.
    """
    mgr, entry = _completed_run(fixture_repo)
    _amend_on_branch(entry.path)
    in_tree = entry.path / "runs" / "demo" / "prd.md"
    in_tree.write_bytes(AUTHORED)  # what the pre-fix publish-back left behind
    assert gitops.status_porcelain(
        entry.path, untracked_all=True, paths=["runs/demo/prd.md"]
    ) != "", "precondition: the clobber is git-visible dirt"

    with _resolve_roots(mgr):
        pass

    assert in_tree.read_bytes() == AMENDED, "the committed amendments win"
    assert gitops.status_porcelain(
        entry.path, untracked_all=True, paths=["runs/demo/prd.md"]
    ) == "", "and the tree is clean again — no manual restore needed"
    assert (fixture_repo / "runs" / "demo" / "prd.md").read_bytes() == AMENDED


def test_start_publishes_the_checkout_over_a_stale_tracked_copy(fixture_repo):
    """First publish on `start` is the legitimate authoring flow (unchanged).

    Even when the base branch tracks a STALE copy of the artifact — so the
    fresh run worktree materializes with old committed bytes — `start` must
    publish what the human just authored: the run branch was created this
    instant and there are no amendments to protect.
    """
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    # A stale tracked copy on the base branch, older than the authored bytes.
    git(fixture_repo, "add", "runs/demo/prd.md")
    git(fixture_repo, "commit", "-qm", "seed: stale prd on base")
    mgr.layout("demo").prd_path.write_bytes(AUTHORED + b"\nRevised for v2.\n")
    path = _pipeline(fixture_repo, LINEAR)

    assert mgr.start("demo", path, use_judge=False,
                     adapter_factory=lambda n: FakeAdapter(
                         writes={"f.py": "x\n"})) == M.RUN_DONE

    entry = _run_worktree(mgr, "gauntlet/demo")
    assert gitops.file_bytes_at_commit(
        entry.path, "HEAD", "runs/demo/prd.md"
    ) == AUTHORED + b"\nRevised for v2.\n", (
        "the run committed what the human actually wrote, not the stale base"
    )


# --- #99: the rollback dirty-guard stops false-firing on a stale checkout ----


def test_rollback_is_not_blocked_by_a_stale_checkout(fixture_repo):
    """The #99 papercut: publish-back runs before the rollback guards.

    Before the three-way no-op, a checkout lagging the branch re-dirtied the
    run tree on every rollback invocation, inside the very context manager the
    guard then evaluated. With the no-op in place the tree stays exactly as
    committed and the rollback proceeds.
    """
    mgr, entry = _completed_run(fixture_repo, TWO_PHASE)
    man = mgr.status("demo")
    assert [c.phase for c in man.commits] == ["P1", "P2"]
    # The run branch amends prd.md after P2; the checkout is not re-synced.
    _amend_on_branch(entry.path)
    assert (fixture_repo / "runs" / "demo" / "prd.md").read_bytes() == AUTHORED

    target = mgr.rollback("demo", 1)  # must not raise RollbackGuardError

    assert target
    after = mgr.status("demo")
    assert [c.phase for c in after.commits] == ["P1"]
    in_tree = entry.path / "runs" / "demo" / "prd.md"
    assert in_tree.read_bytes() == AUTHORED, (
        "the rewound tree holds P1's committed artifact, no stale republish"
    )


# --- record coherence: every writer moves the baseline with the bytes --------


def test_publish_and_adopt_advance_the_recorded_baseline(tmp_path):
    """`publish_artifact`/`adopt_artifact` must move the #97 record.

    If they did not, the next root resolution would misread the agreement they
    just created — a mid-drive publish as an operator edit over a moved branch,
    a gate-time adopt as a branch move over an operator edit — and land in the
    divergence refusal. Uses the same two-root harness as
    ``test_adopt_artifact_carries_cycle_fixes_back_to_the_authority``.
    """
    from gauntlet.engine.execution import StepContext

    operator = tmp_path / "operator"
    work = tmp_path / "work"
    (operator / "runs" / "slug").mkdir(parents=True)
    (work / "runs" / "slug").mkdir(parents=True)

    ctx = StepContext.__new__(StepContext)
    ctx.repo_root = operator
    ctx.work_root = work
    ctx.run_dir = operator / "runs" / "slug" / "run-1"
    ctx.artifact_root = operator / "runs" / "slug"
    ctx.state_outside_worktree = False
    assert ctx.paths.dedicated_worktree

    authored = b"# Plan\n\nauthored in the checkout\n"
    (operator / "runs" / "slug" / "plan.md").write_bytes(authored)
    ctx.publish_artifact("plan.md")
    record = GS.load_published(ctx.run_dir)
    assert record["plan.md"]["published"] == GS.digest(authored)

    fixed = b"# Plan\n\nreviewed and committed by the cycle\n"
    (work / "runs" / "slug" / "plan.md").write_bytes(fixed)
    ctx.adopt_artifact("plan.md")
    record = GS.load_published(ctx.run_dir)
    assert record["plan.md"]["published"] == GS.digest(fixed), (
        "the adopt back-sync must advance the baseline too, or the next "
        "resume misreads the adopt as an operator edit"
    )

    # Non-governed outputs never enter the three-way compare: not recorded.
    (operator / "runs" / "slug" / "notes.md").write_bytes(b"notes\n")
    ctx.publish_artifact("notes.md")
    assert "notes.md" not in GS.load_published(ctx.run_dir)


def test_record_file_is_valid_json_and_atomic_shape(tmp_path):
    """The durable record round-trips and tolerates garbage fail-open to {}."""
    GS.record_published(tmp_path, "plan.md", published="a" * 64, branch=None)
    GS.record_published(tmp_path, "prd.md", published="b" * 64, branch="c" * 64)
    raw = json.loads((tmp_path / GS.STATE_FILENAME).read_text())
    assert raw == {
        "plan.md": {"published": "a" * 64, "branch": None},
        "prd.md": {"published": "b" * 64, "branch": "c" * 64},
    }
    (tmp_path / GS.STATE_FILENAME).write_text("not json")
    assert GS.load_published(tmp_path) == {}

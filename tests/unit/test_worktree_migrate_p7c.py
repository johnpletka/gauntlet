"""`gauntlet migrate-worktree`: the migration ACTION (P7c-2, spike §10).

P7c-1 shipped the migration DECISION — a run that predates the dedicated layout
keeps driving `same_tree`, and nothing in the engine may move it implicitly.
This file covers the only thing that may move it, and the matrix of cases where
it must not.

The claim these tests exist to defend is the last row of §10's table, and it is
the point of the whole design: **a run that cannot migrate is never wedged by
that.** Every refusal here is followed by an assertion that the run is still
exactly where it was and still drivable in `same_tree` mode. A migration
feature that can leave a run stuck would be worse than no migration feature.

Nothing is mocked that git can answer for real: the E2-A refusal, the worktree
registration, the lock marker and the teardown are all measured against a real
repository, because every claim §10 makes is a claim about what git does.
"""

from __future__ import annotations

import ast
import os
import socket
from pathlib import Path

import pytest

from gauntlet.engine import gitops
from gauntlet.engine import locking
from gauntlet.engine import manifest as M
from gauntlet.engine import operator as op
from gauntlet.engine import worktree as WT
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.run import (
    DRIVING_LOCK_NAME,
    MigrateWorktreeRefused,
    RunManager,
    WorktreeLockError,
    _LockRecord,
)
from gauntlet.procident import read_process_identity

from conftest import FakeAdapter, git

DEAD_PID = 2_000_000_000  # never live: the kill -9'd / power-loss driver
THIS_HOST = socket.gethostname()

# Deliberately PINNED, not inherited (P7g). Through P7f this relied on
# `same_tree` being the shipped default; P7g flips that, and a default-dedicated
# fixture has nothing to migrate — every test in this file would pass vacuously
# by asserting on a run that was already in the destination state.
#
# The mode is pinned rather than the file deleted because migration's population
# did not go away: it is every run born before `dedicated` existed, plus every
# adopter whose layout cannot host a worktree and who sets `same_tree`
# deliberately (spike §16 keeps it as the documented fallback). The path still
# exists and still needs this coverage; it just stops being the default.
CONFIG_SAME_TREE = """
base_branch: main
run_root: runs
worktree:
  mode: same_tree
agents:
  builder: {adapter: claude-code}
"""

# Two gates, so the run is still NON-TERMINAL after being driven once in its
# migrated tree — which is what the round trip needs in order to roll back
# afterwards. A single-gate pipeline runs to `done`, and a `done` run is
# correctly refused by both verbs.
TWO_GATES = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [prd.md]}
      - {id: work, type: shell, run: "true"}
      - {id: gate2, type: human_gate, show: [prd.md]}
"""


# --- fixtures ----------------------------------------------------------------


def _prepare(repo: Path) -> RunManager:
    (repo / ".gauntlet").mkdir(exist_ok=True)
    (repo / ".gauntlet" / "config.yaml").write_text(CONFIG_SAME_TREE)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _pipeline(repo: Path, text: str = TWO_GATES) -> Path:
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / "p.yaml"
    path.write_text(text)
    git(repo, "add", "pipelines")
    git(repo, "commit", "-qm", "add pipeline")
    return path


def _parked_same_tree_run(repo: Path, slug: str = "demo") -> RunManager:
    """A real `same_tree` run, parked at its first gate — the §10 row-2 shape."""
    mgr = _prepare(repo)
    path = _pipeline(repo)
    mgr.new(slug)
    mgr.layout(slug).prd_path.write_text("# Real PRD\n\nA genuine PRD.\n")
    status = mgr.start(
        slug, path, use_judge=False, adapter_factory=lambda n: FakeAdapter()
    )
    assert status == M.RUN_PARKED
    return mgr


def _step_off_the_run_branch(repo: Path) -> None:
    """What §10 step 2 requires of the operator, and why it is not automatic.

    A `same_tree` run leaves its branch checked out in the operator's tree, and
    git refuses a second worktree for a checked-out branch (E2-A). Migration
    will NOT resolve that by checking out something else: touching the
    operator's checkout is the exact thing P7 exists to stop. The human steps
    off; the engine reads.
    """
    git(repo, "checkout", "-q", "main")


def _ident(pid: int) -> dict | None:
    i = read_process_identity(pid)
    return i.to_dict() if i else None


def _write_run_lock(
    mgr: RunManager, slug: str, *, pid: int, identity: dict | None
) -> Path:
    """Make `driver_liveness` resolve to a chosen state for THIS run (P7b path).

    alive → live pid + matching identity + this host; indeterminate → live pid
    + null identity; orphaned → dead pid. `none` is simply no lock at all.
    """
    run_dir = mgr.layout(slug).active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    rec = _LockRecord(
        nonce="nonce-1",
        slug=slug,
        run_id=man.run_id,
        pid=pid,
        pgid=pid,
        started_at="2026-08-05T10-00-00",
        host=THIS_HOST,
        proc_identity=identity,
    )
    path = run_dir / DRIVING_LOCK_NAME
    path.write_text(rec.to_json())
    return path


def _manifest(slug="demo", run_id="run-1", branch=None, mode=None,
              status=M.RUN_PARKED) -> Manifest:
    return Manifest(
        run_id=run_id,
        slug=slug,
        branch=branch or f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="p", version="1", hash="h"),
        worktree_mode=mode,
        status=status,
    )


def _still_fully_resumable(mgr: RunManager, slug: str, repo: Path) -> None:
    """The R1 obligation, asserted rather than asserted-about.

    §10's last row is not advice: "stays fully resumable in `same_tree` mode;
    the refusal names the blocker … the run is never wedged by the migration
    being impossible." So every refusal test ends here — the run still resolves
    `same_tree`, still has no worktree, and its state is untouched.
    """
    run_dir = mgr.layout(slug).active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    assert mgr._effective_worktree_mode(man) == WT.MODE_SAME_TREE
    assert WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    ) is None
    assert not WT.worktrees_root(mgr._main_worktree_root()).exists() or not list(
        WT.worktrees_root(mgr._main_worktree_root()).glob(f"{slug}/*")
    )
    assert man.status == M.RUN_PARKED
    # And the branch is still where the run left it, in the operator's repo.
    assert gitops.branch_exists(repo, man.branch)


# --- the refusal matrix (§10) -------------------------------------------------


def test_refused_under_a_live_driver_and_the_run_stays_resumable(fixture_repo):
    """§10 row 3: `alive` → refuse, fail closed, change nothing.

    Moving the tree under a running agent would pull the ground out from
    underneath it mid-step. There is no version of that which is recoverable,
    so this is the one refusal that is not merely conservative.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    _write_run_lock(mgr, "demo", pid=os.getpid(), identity=_ident(os.getpid()))
    assert op.driver_liveness(
        mgr._run_root_dir(), "demo",
        run_instance_dir=mgr.layout("demo").active_run_dir(),
    ) == op.LIVENESS_ALIVE

    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.migrate_worktree("demo")
    assert "LIVE" in str(exc.value)
    assert "fully drivable in `same_tree` mode" in str(exc.value)
    _still_fully_resumable(mgr, "demo", fixture_repo)


def test_refused_under_an_indeterminate_driver(fixture_repo):
    """§10 row 3, the half that is easy to get wrong.

    `indeterminate` means the driver can be proven neither alive nor dead. The
    table's asymmetry treats it as live, never as gone — the same fail-closed
    direction `recover` and `_reap_orphaned_judge` take. A migration that
    treated "cannot tell" as "nobody home" would move the tree out from under a
    driver exactly when the machine is already in a state we cannot read.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    _write_run_lock(mgr, "demo", pid=os.getpid(), identity=None)
    assert op.driver_liveness(
        mgr._run_root_dir(), "demo",
        run_instance_dir=mgr.layout("demo").active_run_dir(),
    ) == op.LIVENESS_INDETERMINATE

    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.migrate_worktree("demo")
    assert "indeterminate" in str(exc.value)
    _still_fully_resumable(mgr, "demo", fixture_repo)


def test_an_orphaned_driver_does_not_block_migration(fixture_repo):
    """The other side of the same gate: `orphaned` is PROVEN dead, so it passes.

    Without this the two refusals above would be satisfiable by a predicate
    that simply always refuses, which would wedge every run whose driver was
    killed — the exact population §10 row 4 says must be reconciled and then
    offered migration.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    _write_run_lock(mgr, "demo", pid=DEAD_PID, identity=_ident(os.getpid()))
    run_dir = mgr.layout("demo").active_run_dir()
    assert op.driver_liveness(
        mgr._run_root_dir(), "demo", run_instance_dir=run_dir
    ) == op.LIVENESS_ORPHANED

    man = Manifest.load(run_dir / "manifest.json")
    assert mgr.migration_blocker(man, liveness=op.LIVENESS_ORPHANED) is None


@pytest.mark.parametrize(
    "status", [M.RUN_DONE, M.RUN_ABORTED, M.RUN_FAILED]
)
def test_a_terminal_run_is_never_migrated(fixture_repo, status):
    """§10 row 1: terminal runs have no live tree to isolate.

    Refusing costs the operator nothing — a terminal run is not driving — and
    it keeps the population that CAN be migrated to exactly the population that
    has something to gain.
    """
    mgr = _prepare(fixture_repo)
    man = _manifest(status=status)
    blocker = mgr.migration_blocker(man, liveness=op.LIVENESS_NONE)
    assert blocker is not None and status in blocker


def test_a_branch_checked_out_in_the_operator_tree_refuses_actionably(
    fixture_repo,
):
    """§10 step 2 / E2-A: the NORMAL state of a same_tree run, and the answer.

    A `same_tree` run's branch is checked out in the operator's own tree, so
    git refuses the `worktree add`. That refusal is correct and deliberate —
    this verb will not check out, reset or move a branch in the human's tree
    (spike §9.4 / acceptance A1) — so the only thing owed to the operator is a
    message that names what THEY must do. The run stays fully resumable
    meanwhile, which is what makes "just refuse" an acceptable answer at all.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"

    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.migrate_worktree("demo")
    msg = str(exc.value)
    assert "is currently checked out" in msg
    assert f"git -C {fixture_repo} checkout main" in msg
    _still_fully_resumable(mgr, "demo", fixture_repo)
    # A1 in its rawest form: the refusal did not move the human off the branch.
    assert gitops.current_branch(fixture_repo) == "gauntlet/demo"


def test_the_branch_holder_is_a_precondition_checked_before_any_mutation(
    fixture_repo,
):
    """Rewritten at P7c-2.1 (review F-006): the refusal moved EARLIER, on purpose.

    P7c-2 let this case reach `worktree add` and turned git's E2-A error into
    the message. That worked, but it meant `status` — which cannot see a git
    error that has not happened yet — advertised the migration as executable in
    precisely the state where it was certain to fail. Making the branch holder a
    PRECONDITION lets both surfaces consult the same answer, so the offer and
    the refusal agree (R4), and the refusal now happens before the engine
    touches git at all.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")

    blocker = mgr.migration_blocker(man, liveness=op.LIVENESS_NONE)
    assert blocker is not None and "is currently checked out" in blocker

    _step_off_the_run_branch(fixture_repo)
    assert mgr.migration_blocker(man, liveness=op.LIVENESS_NONE) is None


def test_a_dirty_operator_tree_blocks_migration(fixture_repo):
    """Spike §10's "dirty operator tree" cannot-migrate case (review F-005).

    A `same_tree` run's work-in-progress lives in the operator's checkout.
    Migration builds the new tree from the committed branch tip, so uncommitted
    work would be stranded here — silently, and on whatever branch the operator
    stepped onto, since git carries compatible edits across a checkout. The
    spike lists this as a refusal; P7c-2 checked only mode, terminality and
    liveness and would have proceeded.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    (fixture_repo / "wip.py").write_text("half a feature\n")
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")

    blocker = mgr.migration_blocker(man, liveness=op.LIVENESS_NONE)
    assert blocker is not None and "wip.py" in blocker

    with pytest.raises(MigrateWorktreeRefused):
        mgr.migrate_worktree("demo")
    _still_fully_resumable(mgr, "demo", fixture_repo)

    # Committed → no longer stranded, so no longer a blocker.
    git(fixture_repo, "add", "wip.py")
    git(fixture_repo, "commit", "-qm", "wip")
    assert mgr.migration_blocker(man, liveness=op.LIVENESS_NONE) is None


def test_the_governed_artifact_does_not_read_as_operator_dirt(fixture_repo):
    """The other half of F-005: the dirt check must not block every migration.

    The operator's `prd.md` is the authoring surface and the sync republishes it
    into the run tree, so it is never stranded by a migration. Counting it as
    dirt would refuse every run whose artifact is not yet committed — which is
    the normal state before the first commit step — and trade a data-loss bug
    for a wedge.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    prd = mgr.layout("demo").prd_path
    assert prd.exists() and "?? runs/demo/prd.md" in gitops.status_porcelain(
        fixture_repo, untracked_all=True
    )
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    assert mgr.migration_blocker(man, liveness=op.LIVENESS_NONE) is None


def test_a_blocked_migration_leaves_the_run_drivable(fixture_repo):
    """The R1 clause, end to end: refuse, then actually drive the run.

    The other refusal tests assert the run *looks* untouched. This one proves
    the consequence that matters — after a refused migration the run still
    approves and advances, in the operator's checkout, exactly as if the
    migration had never been attempted.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    with pytest.raises(MigrateWorktreeRefused):
        mgr.migrate_worktree("demo")  # branch is checked out here → refused

    status = mgr.approve("demo", use_judge=False,
                         adapter_factory=lambda n: FakeAdapter())
    assert status == M.RUN_PARKED  # advanced to the SECOND gate
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    assert mgr._effective_worktree_mode(man) == WT.MODE_SAME_TREE
    parked = [s for s in man.steps if s.status == M.PARKED]
    assert [s.id for s in parked] == ["gate2"]


# --- eligibility is the NEGATION of the mode resolver, not a second rule ------


@pytest.mark.parametrize(
    "recorded_mode,registered,adopted",
    [
        (None, False, False),                 # a pre-P7c run — the population
        (WT.MODE_SAME_TREE, False, False),    # born same_tree
        (WT.MODE_DEDICATED, False, False),    # born dedicated (rule 3)
        (None, True, False),                  # a tree is registered (rule 1)
        (None, False, True),                  # adopted, tree gone (rule 2)
        (WT.MODE_SAME_TREE, True, False),     # evidence beats the record
    ],
)
def test_eligibility_agrees_with_the_mode_resolver_on_every_evidence_shape(
    fixture_repo, monkeypatch, recorded_mode, registered, adopted
):
    """`migration_blocker` says "migratable" iff the resolver says `same_tree`.

    The hazard this closes is drift, and drift here is not cosmetic: an
    eligibility rule that answered `same_tree` where the resolver answers
    `dedicated` would migrate a run that already has a tree, and the converse
    would refuse a run that should move. Holding the terminal and liveness legs
    at their eligible values isolates the tree leg, so what is compared is
    exactly the one shared question.
    """
    mgr = RunManager(fixture_repo, RunConfig())
    man = _manifest(mode=recorded_mode)
    monkeypatch.setattr(
        WT, "observe",
        lambda *a, **k: (object() if registered else None),
    )
    monkeypatch.setattr(
        RunManager, "_journal_says_adopted", lambda self, m: adopted
    )

    resolved = mgr._effective_worktree_mode(man)
    eligible = mgr.migration_blocker(man, liveness=op.LIVENESS_NONE) is None
    assert eligible == (resolved == WT.MODE_SAME_TREE)


def test_eligibility_follows_the_resolver_even_when_the_resolver_is_replaced(
    fixture_repo, monkeypatch
):
    """The derivation, proven behaviourally: swap the rule, the answer follows.

    The parametrized test above could in principle pass against a faithful but
    *independent* re-implementation. This one cannot: it replaces
    `_effective_worktree_mode` outright and asserts eligibility flips with it,
    which is only true if the predicate CALLS it.
    """
    mgr = RunManager(fixture_repo, RunConfig())
    man = _manifest()
    monkeypatch.setattr(
        RunManager, "_effective_worktree_mode",
        lambda self, m: WT.MODE_DEDICATED,
    )
    assert mgr.migration_blocker(man, liveness=op.LIVENESS_NONE) is not None
    monkeypatch.setattr(
        RunManager, "_effective_worktree_mode",
        lambda self, m: WT.MODE_SAME_TREE,
    )
    assert mgr.migration_blocker(man, liveness=op.LIVENESS_NONE) is None


def test_the_eligibility_predicate_reads_no_worktree_evidence_of_its_own():
    """A static guard, for the same reason `test_config_is_read_in_exactly_one_place`
    is static: a second reader is invisible at runtime while it happens to agree.

    Two rules that agree today drift the first time one of them is edited, and
    the edit that breaks them will look local and safe. So the predicate is
    pinned to a single source of the tree question: it may call
    `_effective_worktree_mode` and nothing else that observes a worktree.
    """
    src = Path(RunManager.__module__.replace(".", "/") + ".py")
    src = Path(__file__).resolve().parents[2] / "src" / src
    tree = ast.parse(src.read_text())

    def names_in(fn_name: str) -> set[str]:
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == fn_name
        )
        return {
            (n.attr if isinstance(n, ast.Attribute) else n.id)
            for n in ast.walk(fn)
            if isinstance(n, (ast.Attribute, ast.Name))
        }

    names = names_in("migration_blocker")
    assert "_effective_worktree_mode" in names, (
        "migration eligibility must be derived from the single mode-resolution "
        "rule, not computed independently"
    )
    banned = {"observe", "_journal_says_adopted", "worktree_mode", "describe"}
    assert not (names & banned), (
        f"`migration_blocker` reads worktree evidence directly ({names & banned}); "
        "that is a SECOND resolution rule, and it will drift from "
        "`_effective_worktree_mode`. Ask the resolver instead."
    )
    # Extended at P7c-2.1. The eligibility path grew git PRECONDITION legs
    # (branch holder, dirty operator tree) which legitimately observe git —
    # they answer "would the operation succeed?", not "what mode is this run
    # in?" So `observe`-family calls are allowed there, but the MODE-deriving
    # names stay banned across the whole path: one authority for the mode, no
    # matter how many helpers the path grows.
    mode_only = {"_journal_says_adopted", "worktree_mode"}
    for helper in (
        "_migration_precondition_blocker", "_dirty_operator_tree_blocker"
    ):
        leaked = names_in(helper) & mode_only
        assert not leaked, (
            f"`{helper}` re-derives the worktree MODE ({leaked}). Preconditions "
            "may observe git; they may not answer the mode question a second "
            "time."
        )


# --- the round trip: migrate → drive → roll back ------------------------------


def test_migrate_then_drive_then_rollback_round_trip(fixture_repo):
    """The whole capability, in one run's life.

    Asserted at every hinge rather than at the ends, because the interesting
    failures are in the middle: a migration that registers a tree but does not
    lock it, a drive that silently keeps using the operator's checkout, a
    rollback that removes the tree but leaves the journal claiming an adoption
    (which would make the NEXT resume rebuild it).
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    run_dir = mgr.layout("demo").active_run_dir()
    before_head = gitops.head_sha(fixture_repo)

    out = mgr.migrate_worktree("demo")
    assert "migrated 'demo'" in out

    # 1. the tree exists, is registered under the ENGINE's derived root, and
    #    carries this run's anti-prune marker (§8.3 — without it any other
    #    run's prune can delete it mid-drive).
    man = Manifest.load(run_dir / "manifest.json")
    entry = WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    )
    assert entry is not None
    assert entry.path == WT.run_worktree_path(
        mgr._main_worktree_root(), "demo", man.run_id
    )
    assert WT.parse_lock_reason(entry.locked) == ("demo", man.run_id)

    # 2. the resolver now answers `dedicated` — from EVIDENCE. The manifest's
    #    BIRTH mode is deliberately left alone (here `same_tree`, what this run
    #    was actually born as), which is what makes the rollback exact: stamping
    #    it `dedicated` would make rule 3 keep answering `dedicated` after the
    #    tree was removed, and the rollback would be a lie.
    assert mgr._effective_worktree_mode(man) == WT.MODE_DEDICATED
    assert man.worktree_mode != WT.MODE_DEDICATED

    # 3. the journal records the transition, and says it was a migration.
    adopted = [
        e for e in _events(run_dir) if e.get("kind") == "WorktreeAdopted"
    ]
    assert len(adopted) == 1
    assert adopted[0]["payload"]["migrated"] is True
    assert adopted[0]["payload"]["path"] == str(entry.path)

    # 4. the export landed where the path builders mirror to (§9.3).
    assert (entry.path / "runs" / "demo" / man.run_id / "manifest.json").exists()

    # 5. the operator's checkout was not touched by any of it.
    assert gitops.current_branch(fixture_repo) == "main"
    assert gitops.head_sha(fixture_repo) == before_head

    # 6. the run then DRIVES in its new tree — the migration is not cosmetic.
    status = mgr.approve("demo", use_judge=False,
                         adapter_factory=lambda n: FakeAdapter())
    assert status == M.RUN_PARKED
    assert gitops.current_branch(fixture_repo) == "main"

    # 7. rollback returns it to same_tree, with the journal intact.
    out = mgr.rollback_worktree_migration("demo")
    assert "rolled back 'demo'" in out
    man = Manifest.load(run_dir / "manifest.json")
    assert WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    ) is None
    assert mgr._effective_worktree_mode(man) == WT.MODE_SAME_TREE
    kinds = [
        e["kind"] for e in _events(run_dir)
        if e.get("kind", "").startswith("Worktree")
    ]
    assert kinds == ["WorktreeAdopted", "WorktreeReleased"]

    # 8. and what survives is everything §4.4 said would: the branch, its
    #    commits, the journal and the run dir.
    assert gitops.branch_exists(fixture_repo, man.branch)
    assert (run_dir / "manifest.json").exists()
    assert len(_events(run_dir)) >= len(kinds)
    # 9. the rolled-back run still drives, in the operator's checkout again.
    assert mgr.approve("demo", use_judge=False,
                       adapter_factory=lambda n: FakeAdapter()) == M.RUN_DONE


def _events(run_dir: Path) -> list[dict]:
    from gauntlet.engine import journal as J

    return J.read_events(run_dir)


def test_rollback_refuses_a_run_that_was_born_dedicated(fixture_repo):
    """There is no migration to undo, and saying otherwise would be a lie.

    A run born `dedicated` resolves that way from its manifest (rule 3), so
    removing its tree would not return it to `same_tree` — the next drive would
    simply rebuild it. Reporting "rolled back" for that is worse than refusing:
    the operator would believe the run had moved.
    """
    mgr = _prepare(fixture_repo)
    man = _manifest(mode=WT.MODE_DEDICATED)
    blocker = mgr._migration_rollback_blocker(man, liveness=op.LIVENESS_NONE)
    assert blocker is not None and "BORN dedicated" in blocker


def test_rollback_closes_an_open_adoption_when_the_tree_is_already_gone(
    fixture_repo,
):
    """§11 row 2 reached through rollback, and why it is not "nothing to do".

    With the tree swept but the journal's adoption still open, the resolver
    answers `dedicated` and the next resume REBUILDS the tree. An operator
    rolling back in that state wants the opposite, so the rollback closes the
    adoption — the only thing left to roll back.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    entry = WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    )
    # The tree vanishes (reboot / tmp sweep / rm -rf) but stays REGISTERED, so
    # `observe` still finds it — clear the registration too, which is the shape
    # where only the journal remembers.
    gitops.unlock_worktree(mgr.operator_root, entry.path)
    gitops.remove_worktree(mgr.operator_root, entry.path)
    gitops.prune_worktrees(mgr.operator_root, expire="now")
    assert mgr._effective_worktree_mode(man) == WT.MODE_DEDICATED  # rule 2

    out = mgr.rollback_worktree_migration("demo")
    assert "already gone" in out
    assert mgr._effective_worktree_mode(man) == WT.MODE_SAME_TREE


def test_the_engines_own_export_never_blocks_a_rollback(fixture_repo):
    """The migration writes the export; the rollback must not trip over it.

    A dirt check that is uniquely stricter than every other engine surface —
    which all exclude the engine's own bookkeeping — would make the immediate
    undo of a migration impossible, which is the one moment an operator is
    most likely to want it. The export is write-only with zero readers and is
    regenerated on the next drive: destroying it destroys nothing.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    entry = WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    )
    assert gitops.status_porcelain(entry.path, untracked_all=True), (
        "precondition: the export leaves the fresh tree untracked-dirty"
    )

    assert "rolled back" in mgr.rollback_worktree_migration("demo")
    _still_fully_resumable(mgr, "demo", fixture_repo)


def test_rollback_refuses_rather_than_sweeping_away_uncommitted_work(
    fixture_repo,
):
    """F-011's lesson, applied to the verb that destroys a tree on purpose.

    A snapshot would preserve the bytes, but only as a recovery ref the
    operator never asked for and would not know to look for. The engine's own
    bookkeeping export is excluded — it is write-only and regenerated — so what
    blocks a rollback is a builder's work and nothing else.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    entry = WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    )

    (entry.path / "builder-wip.py").write_text("half a feature\n")

    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.rollback_worktree_migration("demo")
    assert "builder-wip.py" in str(exc.value)
    assert str(entry.path) in str(exc.value)
    # Still migrated, still drivable — the refusal changed nothing.
    assert WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    ) is not None


def test_a_failed_export_removes_the_worktree_and_leaves_the_run_same_tree(
    fixture_repo, monkeypatch
):
    """§10 step 4: "a failure here aborts the migration with the worktree removed".

    This is the one step that can fail AFTER a healthy `worktree add`, so it is
    the only place a partial migration could survive. Leaving the tree behind
    would be bad; leaving it behind having already journalled `WorktreeAdopted`
    would be worse — the run would resolve `dedicated` into a tree its own
    bookkeeping cannot be committed in.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    run_dir = mgr.layout("demo").active_run_dir()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(WT, "write_bookkeeping_export", boom)
    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.migrate_worktree("demo")
    assert "disk full" in str(exc.value)
    # The same_tree clause is EARNED here — the unwind ran and was verified
    # before the message was composed (F-004) — so asserting it is asserting
    # the observation, not the wording.
    assert "fully drivable in `same_tree` mode" in str(exc.value)

    man = Manifest.load(run_dir / "manifest.json")
    assert WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    ) is None
    assert mgr._effective_worktree_mode(man) == WT.MODE_SAME_TREE
    assert not [
        e for e in _events(run_dir) if e.get("kind") == "WorktreeAdopted"
    ]
    _still_fully_resumable(mgr, "demo", fixture_repo)


def test_an_interrupt_mid_migration_stays_an_interrupt(fixture_repo, monkeypatch):
    """Ctrl-C during a migration is not a migration refusal.

    The unwind must still run — the half-created tree is removed either way,
    which is the part that matters — but laundering a `KeyboardInterrupt` into
    `MigrateWorktreeRefused` would report a decision the engine never made, and
    would swallow an interrupt the operator is entitled to have honoured.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)

    def interrupt(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(WT, "write_bookkeeping_export", interrupt)
    with pytest.raises(KeyboardInterrupt):
        mgr.migrate_worktree("demo")
    # …and the tree it had created is gone all the same.
    _still_fully_resumable(mgr, "demo", fixture_repo)


# --- review P7c-2.1: the lifecycle is durable, repeatable and honest ---------


def test_migration_fails_closed_when_the_adoption_cannot_be_journalled(
    fixture_repo, monkeypatch
):
    """F-001: `append_audit` is best-effort BY CONTRACT, so success must be proven.

    It swallows I/O errors and returns False for both "already recorded" and
    "could not record" — opposite outcomes. A migration that ignored that could
    report success while leaving no record of the adoption, and if the tree
    later vanished together with its registration the run would silently fall
    back to driving the operator's checkout: resolver rule 2 is the backstop,
    and it would be missing.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    monkeypatch.setattr(
        "gauntlet.engine.journal.append_audit", lambda *a, **k: False
    )
    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.migrate_worktree("demo")
    assert "journal" in str(exc.value)
    _still_fully_resumable(mgr, "demo", fixture_repo)


def test_rollback_fails_closed_before_removing_anything(fixture_repo, monkeypatch):
    """F-001, the direction that matters more: record BEFORE you destroy.

    Removing the tree and then failing to journal the release leaves no tree and
    an OPEN adoption, so the resolver answers `dedicated`, the next resume
    rebuilds the tree the operator just removed — and the verb has already said
    it succeeded. Recording first means the failure leaves the tree intact and
    the run coherent, and a retry is safe.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")
    monkeypatch.setattr(
        "gauntlet.engine.journal.append_audit", lambda *a, **k: False
    )
    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.rollback_worktree_migration("demo")
    assert "journal" in str(exc.value)

    monkeypatch.undo()
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    entry = WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    )
    assert entry is not None, "the tree must survive a rollback that failed closed"
    assert mgr._effective_worktree_mode(man) == WT.MODE_DEDICATED
    # …and the retry works.
    assert "rolled back" in mgr.rollback_worktree_migration("demo")


def test_a_second_migrate_rollback_cycle_is_recorded_distinctly(fixture_repo):
    """F-002: the lifecycle is a CYCLE, so its keys must carry a generation.

    Before the fix an adoption key was a function of (run, path, head,
    transition) and a release key of (run, path) alone — all of which repeat.
    So the second migration at an unchanged head was deduplicated away, and the
    second rollback ALWAYS was. Either one leaves the journal's last word
    disagreeing with reality, which is exactly what resolver rule 2 reads.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    run_dir = mgr.layout("demo").active_run_dir()

    mgr.migrate_worktree("demo")
    mgr.rollback_worktree_migration("demo")
    mgr.migrate_worktree("demo")          # same HEAD as the first migration
    man = Manifest.load(run_dir / "manifest.json")
    kinds = [
        e["kind"] for e in _events(run_dir)
        if e.get("kind", "").startswith("Worktree")
    ]
    assert kinds == ["WorktreeAdopted", "WorktreeReleased", "WorktreeAdopted"], (
        "the second adoption was deduplicated against the first"
    )
    # The journal's last word is an OPEN adoption, so a tree that vanishes with
    # its registration still resolves `dedicated` and is recreated (rule 2).
    assert mgr._journal_says_adopted(man)

    mgr.rollback_worktree_migration("demo")
    kinds = [
        e["kind"] for e in _events(run_dir)
        if e.get("kind", "").startswith("Worktree")
    ]
    assert kinds[-1] == "WorktreeReleased", (
        "the second release was deduplicated against the first, leaving an open "
        "adoption that would rebuild the tree the operator just removed"
    )
    assert not mgr._journal_says_adopted(man)
    assert mgr._effective_worktree_mode(man) == WT.MODE_SAME_TREE


def test_rollback_refuses_a_governed_artifact_edited_in_the_run_tree(
    fixture_repo,
):
    """F-003: "it is only a synced copy" was an assumption, and it deleted bytes.

    The exclusion feeds `WT.release`'s snapshot decision as well as the
    refusal, so an artifact edited inside the run worktree was invisible to
    BOTH protections and destroyed. The playbook saying not to edit it there is
    not proof that nobody did. Now each artifact earns its exclusion by
    byte-comparison against the authoritative copy.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    entry = WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    )
    tree_prd = entry.path / "runs" / "demo" / "prd.md"
    tree_prd.parent.mkdir(parents=True, exist_ok=True)
    tree_prd.write_text("# Real PRD\n\nEDITED IN THE RUN TREE.\n")

    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.rollback_worktree_migration("demo")
    assert "prd.md" in str(exc.value)
    # Still there — refused, not swept into a recovery ref.
    assert "EDITED IN THE RUN TREE" in tree_prd.read_text()


def test_an_identical_synced_artifact_still_does_not_block_a_rollback(
    fixture_repo,
):
    """The other half of F-003: proof, not paranoia.

    The common case — a synced copy byte-identical to the operator's file — must
    still be excluded, or every dedicated run becomes un-rollbackable and the
    fix has traded one wedge for another.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    entry = WT.observe(
        mgr.operator_root, man.branch, main_root=mgr._main_worktree_root()
    )
    tree_prd = entry.path / "runs" / "demo" / "prd.md"
    tree_prd.parent.mkdir(parents=True, exist_ok=True)
    tree_prd.write_text(mgr.layout("demo").prd_path.read_text())  # exact copy

    assert "rolled back" in mgr.rollback_worktree_migration("demo")


def test_a_rollback_refusal_never_claims_the_run_is_same_tree(fixture_repo):
    """F-007: a rollback refusal is reached only after the run resolved dedicated.

    Telling that operator the run "remains fully drivable in `same_tree` mode"
    is not a harmless simplification — it is a false statement about which tree
    their agents edit next, handed to them at the moment they are trying to
    establish exactly that.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")
    run_dir = mgr.layout("demo").active_run_dir()
    _write_run_lock(mgr, "demo", pid=os.getpid(), identity=_ident(os.getpid()))

    with pytest.raises(MigrateWorktreeRefused) as exc:
        mgr.rollback_worktree_migration("demo")
    msg = str(exc.value)
    assert "same_tree" not in msg, msg
    assert "own worktree at" in msg


# --- A2 in the mixed population is not weakened by migration ------------------


def test_a_migrated_run_drives_on_the_per_run_lock_alone(
    fixture_repo, monkeypatch
):
    """P7h: the migrated run demotes to the per-run lock, like a born one.

    REWRITTEN AT P7g/P7h (was
    `test_a_migrated_run_still_writes_the_worktree_global_tree_guard`). The
    old claim was P7c-1's Problem A resolution, and its own docstring named the
    precondition it rested on: "P7c-1 kept the worktree-global tree guard
    **because `same_tree` is still the default**". P7g flips that default and
    P7h retires the guard from drives, so the assertion encoded a `same_tree`
    truth — the *wrong* class, not the vacuous one, because the path still runs
    and still needs a claim.

    The concern behind the old claim does not survive contact with P7g. It was
    that a dedicated run which stopped writing the guard would stop excluding a
    concurrent `same_tree` run of another slug **from the operator's
    checkout**. A dedicated drive does not touch the operator's checkout, so
    that exclusion was protecting a tree the drive never edits: over-exclusion,
    which is exactly what P7h retired. What still needs cross-slug exclusion —
    `finish` and `clean` — are operator-tree verbs and keep both scopes.

    Migration is nonetheless its own birth path into `dedicated`, which is why
    this file asserts it rather than leaning on
    `test_a_dedicated_drive_holds_only_the_per_run_lock`'s born-dedicated run.
    Asserted as the per-run lock GENUINELY being held — right slug, right run
    id, this pid — rather than merely as the tree guard being absent, because
    "no lock at all" would satisfy the weaker form.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")

    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")
    tree_guard = mgr._run_root_dir() / DRIVING_LOCK_NAME
    seen: list[dict] = []
    original_ensure = WT.ensure

    def spy(*a, **k):
        # Sampled at the moment the run's tree is resolved — i.e. INSIDE the
        # driving verb, which is the only place the two scopes are
        # distinguishable. Unchanged from the original test: the sampling point
        # was never what was wrong with it.
        seen.append({
            "tree": locking.read_record(tree_guard),
            "run": locking.read_record(run_dir / DRIVING_LOCK_NAME),
        })
        return original_ensure(*a, **k)

    monkeypatch.setattr(WT, "ensure", spy)
    mgr.approve("demo", use_judge=False,
                adapter_factory=lambda n: FakeAdapter())

    assert seen, "the drive never resolved its tree; the spy proves nothing"
    for sample in seen:
        run, tree = sample["run"], sample["tree"]
        assert run is not None, sample      # the drive's own exclusion IS held
        assert run.pid == os.getpid()
        assert run.slug == "demo" and run.run_id == man.run_id
        assert tree is None, (              # ...and the repo-wide one is not
            "a migrated (dedicated) run still holds the worktree-global tree "
            "guard for its drive; P7h demotes every dedicated run to the "
            "per-run lock, however it became dedicated"
        )
    # Released, and nothing left behind at either scope.
    assert not tree_guard.exists()
    assert not (run_dir / DRIVING_LOCK_NAME).exists()


def test_a_live_tree_guard_still_refuses_a_migrated_drive(fixture_repo):
    """The half of the old claim that DOES survive: the READ direction.

    P7h retired the tree guard from dedicated drives to read-only, not to
    ignored. A holder is a driver that believes it owns the operator's shared
    checkout — a legacy `same_tree` run of another slug, or a `finish`/`clean`
    in flight — and a migrated run lands in exactly that mixed population, so
    it must still refuse while one is live.

    Added at P7g/P7h alongside the rewrite above so the mixed-population
    concern the original test existed for keeps a test, rather than being
    dropped along with the assertion that had encoded it.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    mgr.migrate_worktree("demo")
    run_dir = mgr.layout("demo").active_run_dir()

    # A live legacy holder, named for another slug — the half-migrated shape.
    tree_guard = mgr._run_root_dir() / DRIVING_LOCK_NAME
    ident = read_process_identity(os.getpid())
    tree_guard.write_text(_LockRecord(
        nonce="nonce-legacy",
        slug="legacy-other",
        run_id="run-legacy",
        pid=os.getpid(),
        pgid=os.getpid(),
        started_at="2026-08-05T10-00-00",
        host=THIS_HOST,
        proc_identity=ident.to_dict() if ident else None,
    ).to_json())

    with pytest.raises(WorktreeLockError, match="being driven by legacy-other"):
        mgr.approve("demo", use_judge=False,
                    adapter_factory=lambda n: FakeAdapter())
    # Refused without writing anything: the guard still carries the legacy
    # nonce and this run took no per-run lock.
    assert locking.read_record(tree_guard).nonce == "nonce-legacy"
    assert not (run_dir / DRIVING_LOCK_NAME).exists()
    # R1: refusing must never wedge the run. Asserted directly rather than
    # through `_still_fully_resumable`, whose claim is that a run which FAILED
    # to migrate is still `same_tree` — this one migrated, so `dedicated` is
    # the correct state and that helper would assert the opposite.
    tree_guard.unlink()
    mgr.approve("demo", use_judge=False, adapter_factory=lambda n: FakeAdapter())
    man = Manifest.load(run_dir / "manifest.json")
    assert mgr._effective_worktree_mode(man) == WT.MODE_DEDICATED
    assert man.status != M.RUN_FAILED, man.model_dump()


# --- the operator surface -----------------------------------------------------


def test_status_offers_migration_only_to_an_eligible_run(fixture_repo):
    """§10 row 2: `status` surfaces an OPTIONAL `gauntlet migrate-worktree`.

    Offered from the same predicate the verb refuses on, so the read-only
    surface can never advertise a migration the mutating path would reject —
    the R4 discipline the projection-rebuild action already follows.
    """
    from gauntlet.cli import _append_migration_action

    mgr = _parked_same_tree_run(fixture_repo)
    run_dir = mgr.layout("demo").active_run_dir()
    man = Manifest.load(run_dir / "manifest.json")

    # Rewritten at P7c-2.1 (review F-006). P7c-2 asserted the offer appears in
    # the state this fixture builds — but that state has the run branch checked
    # out in the operator's tree, where git is CERTAIN to refuse the migration.
    # Advertising `executable: true` there is the R4 disagreement this file
    # otherwise exists to prevent, so the offer is now withheld until the
    # migration would actually succeed.
    rstate0 = op.compute_run_state(man, op.LIVENESS_NONE)
    n0 = len(rstate0.next_actions)
    _append_migration_action(mgr, man, op.LIVENESS_NONE, rstate0, "demo")
    assert len(rstate0.next_actions) == n0, (
        "migration was offered while the run branch was checked out in the "
        "operator's tree, where the verb refuses"
    )

    _step_off_the_run_branch(fixture_repo)
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    before = len(rstate.next_actions)

    _append_migration_action(mgr, man, op.LIVENESS_NONE, rstate, "demo")
    assert len(rstate.next_actions) == before + 1
    action = rstate.next_actions[-1]
    assert action.argv == ["gauntlet", "migrate-worktree", "demo"]
    assert action.kind == "control"
    assert action.executable is True
    # Appended, never inserted: it must not displace the action that moves the
    # run forward.
    assert rstate.next_actions[0] is not action

    # A live driver makes it ineligible, and the offer disappears with it.
    rstate2 = op.compute_run_state(man, op.LIVENESS_ALIVE)
    n2 = len(rstate2.next_actions)
    _append_migration_action(mgr, man, op.LIVENESS_ALIVE, rstate2, "demo")
    assert len(rstate2.next_actions) == n2


def test_status_json_carries_the_migration_action_and_stays_schema_valid(
    fixture_repo, monkeypatch
):
    """End to end through the real CLI, because that is the contract consumers read.

    The unit test above proves the action is appended; this proves it survives
    the §6.1 serializer and its emission-time validation. No schema field was
    added for this (`proposals/P7c-split-seam.md` §5 designed the `worktree`
    object to make that unnecessary) — so if the payload stopped validating,
    the additive-at-`schema_version: 1` claim would be false.
    """
    import json

    from typer.testing import CliRunner

    from gauntlet.adapters._structured import validate_schema
    from gauntlet.cli import app

    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "schemas" / "status.json")
        .read_text()
    )
    _parked_same_tree_run(fixture_repo)
    # The offer is real only once it would actually run (review F-006), so the
    # operator steps off the run branch exactly as the playbook says.
    _step_off_the_run_branch(fixture_repo)
    monkeypatch.chdir(fixture_repo)
    result = CliRunner().invoke(app, ["status", "demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)

    assert payload["schema_version"] == 1
    assert payload["worktree"]["mode"] == WT.MODE_SAME_TREE
    migrate = [
        a for a in payload["next_actions"]
        if a["argv"] == ["gauntlet", "migrate-worktree", "demo"]
    ]
    assert len(migrate) == 1
    assert migrate[0]["consequence"] and "Optional" in migrate[0]["consequence"]
    # `executable: true` is now a claim the engine can stand behind: the same
    # predicate that produced this offer is what the verb consults.
    assert migrate[0]["executable"] is True
    assert migrate[0]["required_inputs"] == []
    validate_schema(payload, schema)


@pytest.mark.parametrize("rollback", [False, True])
def test_both_directions_are_operator_only(fixture_repo, monkeypatch, rollback):
    """Same boundary as `recover`, for the same reason (FR-5.5).

    An in-pipeline agent would be refused for its own run anyway — its driver
    is alive — but nothing stops it reaching another slug's, and relocating the
    tree another run is driving in is not a builder's or reviewer's business.
    Refused before any read or mutation, so it cannot be half-applied.
    """
    mgr = _parked_same_tree_run(fixture_repo)
    monkeypatch.setenv("GAUNTLET_STEP_ID", "phase.implement")
    verb = (
        mgr.rollback_worktree_migration if rollback else mgr.migrate_worktree
    )
    with pytest.raises(MigrateWorktreeRefused) as exc:
        verb("demo")
    assert "pipeline-agent context" in str(exc.value)
    _still_fully_resumable(mgr, "demo", fixture_repo)


def _cli_app():
    from gauntlet.cli import app

    return app


def test_the_cli_refuses_a_migration_in_one_line_not_a_traceback(
    fixture_repo, monkeypatch
):
    """A refusal is an operational condition, so it prints one line and exits 1.

    That is what putting `MigrateWorktreeRefused` in the CLI's known-user-errors
    tuple buys. A traceback would bury the clause the operator most needs to
    read — that they are not wedged and the run still drives.

    Deliberately a SEPARATE test from the success path: the refusal needs the
    run branch checked out here, the success path needs it not to be, and
    switching between them inside one test would move the operator's branch
    after the engine had already attempted a tree — which the autouse A1
    property (correctly) reads as a violation.
    """
    from typer.testing import CliRunner

    _parked_same_tree_run(fixture_repo)
    monkeypatch.chdir(fixture_repo)
    result = CliRunner().invoke(_cli_app(), ["migrate-worktree", "demo"])
    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "error:" in result.output
    assert "fully drivable in `same_tree` mode" in result.output


def test_the_cli_migrates_and_rolls_back(fixture_repo, monkeypatch):
    """The success path and its undo, through the shipped argv."""
    from typer.testing import CliRunner

    runner = CliRunner()
    _parked_same_tree_run(fixture_repo)
    _step_off_the_run_branch(fixture_repo)
    monkeypatch.chdir(fixture_repo)

    ok = runner.invoke(_cli_app(), ["migrate-worktree", "demo"])
    assert ok.exit_code == 0, ok.output
    assert "migrated 'demo'" in ok.stdout

    back = runner.invoke(_cli_app(), ["migrate-worktree", "demo", "--rollback"])
    assert back.exit_code == 0, back.output
    assert "rolled back 'demo'" in back.stdout


def test_the_offer_is_withheld_when_the_tree_question_cannot_be_answered(
    fixture_repo, monkeypatch
):
    """Fail-soft in the SAFE direction: withhold the recommendation, not the view.

    `status` is what an operator reaches for when things are already wrong, so
    an unreadable worktree list must not fail the whole command — but it must
    also not produce a confident "you can migrate this" the verb would then
    refuse. The `worktree` block still reports the observation honestly; only
    the advice is withheld.
    """
    from gauntlet.cli import _append_migration_action

    mgr = _parked_same_tree_run(fixture_repo)
    man = Manifest.load(
        mgr.layout("demo").active_run_dir() / "manifest.json"
    )
    rstate = op.compute_run_state(man, op.LIVENESS_NONE)
    before = len(rstate.next_actions)
    monkeypatch.setattr(
        RunManager, "migration_blocker",
        lambda self, m, **k: (_ for _ in ()).throw(RuntimeError("git is gone")),
    )
    _append_migration_action(mgr, man, op.LIVENESS_NONE, rstate, "demo")
    assert len(rstate.next_actions) == before

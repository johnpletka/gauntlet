"""Run lifecycle: new, entry contract, run, gates, rollback (FR-8, FR-10, F-010)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gauntlet.engine import git_snapshot, gitops, manifest as M
from gauntlet.engine.run import EntryContractError, RollbackGuardError, RunManager

from conftest import FakeAdapter, git, run_work_tree

CONFIG_YAML = """
base_branch: main
run_root: runs
agents:
  builder: {adapter: claude-code}
  triage: {adapter: api, model: haiku}
"""


# P7g. The human-owned `PR.md` is drafted into the OPERATOR's slug dir by
# design (FR-9.8, PRD §2.2): it is addressed to the human, and a copy in the run
# worktree would be destroyed by the `finish`/`clean` teardown that follows. The
# rollback tests below defend a hazard that needs PR.md and the rewound tree to
# be the SAME tree — `reset --hard` is not policy-scoped, so an uncommitted edit
# to a path the dirty check excludes would be destroyed unbacked-up. Under the
# P7g default rollback rewinds the run's tree, where PR.md is not, so the hazard
# is structurally absent there. Pinned rather than deleted: it is still live for
# every legacy run and every adopter on the documented `same_tree` fallback.
CONFIG_SAME_TREE = CONFIG_YAML + "worktree:\n  mode: same_tree\n"


def _prepare(repo: Path, config: str = CONFIG_YAML) -> RunManager:
    (repo / ".gauntlet").mkdir()
    (repo / ".gauntlet" / "config.yaml").write_text(config)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "add config")
    return RunManager(repo)


def _write_pipeline(repo: Path, text: str) -> Path:
    (repo / "pipelines").mkdir(exist_ok=True)
    path = repo / "pipelines" / "p.yaml"
    path.write_text(text)
    # the start() preflight (#61) refuses uncommitted files outside the run
    # root, so fixtures commit the pipeline like a real adopter would
    git(repo, "add", "pipelines")
    git(repo, "commit", "-qm", "add pipeline")
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


# --- P2: structured stub is the §6 manifest skeleton (FR-2.1, FR-2.2) --------

def _guidance_comment_after(content: str, heading: str) -> str:
    """The first non-blank line after ``heading`` (its one-line guidance comment)."""
    lines = content.splitlines()
    idx = lines.index(heading)
    for ln in lines[idx + 1:]:
        if ln.strip():
            return ln.strip()
    return ""


def test_new_scaffolds_full_section_skeleton(fixture_repo):
    # FR-2.1 / review F-003, F-006: a freshly scaffolded prd.md contains EVERY §6
    # manifest header — both mandatory AND scale-with-size — each with a one-line
    # guidance comment, and exactly one marker. Asserting only the mandatory
    # subset would let an implementation drop every scale-with-size section, and
    # asserting only section names would let it drop the guidance comments.
    from gauntlet.engine import prd_stub as PS
    from gauntlet.engine.run import PRD_STUB_MARKER

    mgr = _prepare(fixture_repo)
    prd = mgr.new("demo")
    content = prd.read_text()
    assert content.count(PRD_STUB_MARKER) == 1
    manifest = PS.resolve_manifest(fixture_repo, ".")
    # the scaffolded structure equals the full parsed manifest, in order
    assert PS.stub_section_names(content) == [e.name for e in manifest]
    # both classes are represented (not mandatory-only)
    assert {e.cls for e in manifest} == {PS.MANDATORY, PS.SCALE}
    # F-006: EVERY '##' section heading is immediately followed by a one-line
    # HTML guidance comment (not just present in the marker count).
    h2_headings = [ln for ln in content.splitlines() if ln.startswith("## ")]
    assert len(h2_headings) >= 1
    for heading in h2_headings:
        guidance = _guidance_comment_after(content, heading)
        assert guidance.startswith("<!--") and guidance.endswith("-->"), (
            f"section {heading!r} lacks its one-line guidance comment; got {guidance!r}"
        )
    # the header-block (not a heading) also carries a guidance comment of its own
    assert any(
        ln.strip().startswith("<!--") and "Header block" in ln
        for ln in content.splitlines()
    ), "header block lacks its one-line guidance comment"


def test_scaffold_and_entry_contract_read_the_same_resolved_source(fixture_repo):
    # FR-2.2: `new` (which writes the scaffold) and `check_entry_contract` (which
    # decides "still a stub") resolve the SAME template bytes — no second copy.
    from gauntlet.engine import prd_stub as PS

    mgr = _prepare(fixture_repo)
    written = mgr.new("demo").read_text()
    template, _ = PS.resolve_stub_template(fixture_repo, ".")
    assert written == template


def test_drift_guard_trips_when_playbook_section_changes(fixture_repo):
    # FR-2.2 / review F-006: the drift test is driven off the PARSED playbook, so
    # adding, renaming, OR removing a heading of EITHER class (mandatory or scale-
    # with-size) trips it — all six class/mutation combinations, not a subset.
    from gauntlet.engine import prd_stub as PS

    mgr = _prepare(fixture_repo)
    stub = mgr.new("demo").read_text()
    playbook = PS.resolve_playbook_text(fixture_repo, ".")
    base = PS.parse_manifest(playbook)
    assert PS.stub_section_names(stub) == [e.name for e in base]  # aligned today

    mutations = {
        # add — for each class
        "add-mandatory": playbook.replace(
            "**§11 Open Questions**",
            "**§11.5 Brand New** *(mandatory)*\n\n**§11 Open Questions**",
        ),
        "add-scale": playbook.replace(
            "**§11 Open Questions**",
            "**§11.6 Extra Notes** *(scale-with-size)*\n\n**§11 Open Questions**",
        ),
        # rename — for each class
        "rename-mandatory": playbook.replace(
            "**§5 Functional Requirements**", "**§5 Core Requirements**"
        ),
        "rename-scale": playbook.replace(
            "**§3 Users and Personas**", "**§3 Stakeholders**"
        ),
        # remove — for each class (drop the whole bold-paragraph entry)
        "remove-mandatory": playbook.replace(
            "**§9 Success Metrics** *(mandatory)*", "removed line"
        ),
        "remove-scale": playbook.replace(
            "**§10 Risks & Mitigations** *(scale-with-size)*", "removed line"
        ),
    }
    for label, mutated in mutations.items():
        assert mutated != playbook, f"{label}: mutation did not change the playbook"
        mutated_manifest = PS.parse_manifest(mutated)
        assert PS.stub_section_names(stub) != [e.name for e in mutated_manifest], label


# --- P2: §4.4 header-block invariant (review F-006) --------------------------

def test_header_block_invariant_requires_each_label_exactly_once(fixture_repo):
    # review F-006: the synthetic header-block entry is validated by metadata
    # labels, not a heading. A stub MISSING a required label, or with a
    # DUPLICATED label, fails §4.4 even when every section header is present.
    from gauntlet.engine import prd_stub as PS

    template, _ = PS.resolve_stub_template(fixture_repo, ".")
    manifest = PS.resolve_manifest(fixture_repo, ".")
    PS.validate_template(template, manifest)  # the shipped template is valid

    missing = template.replace("**Author:** <you>\n", "")
    with pytest.raises(PS.StubTemplateError, match="Author"):
        PS.validate_template(missing, manifest)

    duplicated = template.replace(
        "**Status:** Draft v0.1\n", "**Status:** Draft v0.1\n**Status:** again\n"
    )
    with pytest.raises(PS.StubTemplateError, match="Status"):
        PS.validate_template(duplicated, manifest)


# --- P2: FR-2.4 deterministic authored-content predicate ---------------------

def _fresh_authored(fixture_repo, mgr):
    """A scaffolded prd.md path plus its template, for the FR-2.4 matrix."""
    from gauntlet.engine import prd_stub as PS

    prd = mgr.new("demo")
    template, _ = PS.resolve_stub_template(fixture_repo, ".")
    return prd, template


def test_authored_content_matrix(fixture_repo):
    # FR-2.4 acceptance matrix: whitespace-/comment-/heading-only edits and a
    # present/duplicated marker all reject; substantive body prose accepts.
    from gauntlet.engine import prd_stub as PS
    from gauntlet.engine.run import PRD_STUB_MARKER

    mgr = _prepare(fixture_repo)
    _, template = _fresh_authored(fixture_repo, mgr)
    no_marker = template.replace(PRD_STUB_MARKER, "", 1)

    # whitespace-only change → reject
    assert not PS.has_authored_content(no_marker + "\n\n   \n", template)
    # comment-only edit (add a guidance comment) → reject
    assert not PS.has_authored_content(no_marker + "\n<!-- a new note -->\n", template)
    # heading-only edit (add/rename a heading, no body) → reject
    assert not PS.has_authored_content(no_marker + "\n## §12 Extra\n", template)
    # marker present → reject
    assert not PS.has_authored_content(template, template)
    # marker DUPLICATED → reject (marker still present)
    assert not PS.has_authored_content(template + "\n" + PRD_STUB_MARKER + "\n", template)
    # substantive body prose, marker removed → accept
    authored = no_marker + "\nFR-1: the step halts on timeout. Acceptance: a test asserts it.\n"
    assert PS.has_authored_content(authored, template)


def test_entry_contract_accepts_authored_and_rejects_trivial_edits(fixture_repo):
    # End-to-end through check_entry_contract for the boundary FR-2.4 cases.
    from gauntlet.engine.run import PRD_STUB_MARKER

    mgr = _prepare(fixture_repo)
    prd = mgr.new("demo")
    template = prd.read_text()

    # heading-only edit (marker removed) → still refused
    prd.write_text(template.replace(PRD_STUB_MARKER, "", 1) + "\n## §12 Extra\n")
    with pytest.raises(EntryContractError, match="no authored content"):
        mgr.check_entry_contract("demo")

    # substantive body authored → passes
    prd.write_text(
        template.replace(PRD_STUB_MARKER, "", 1)
        + "\nFR-1: the run halts on a backbone failure. Acceptance: covered by a test.\n"
    )
    mgr.check_entry_contract("demo")  # no raise


# --- P2: FR-3.3 fail-closed on a malformed installed stub template -----------

def test_fail_closed_on_malformed_installed_stub(fixture_repo):
    # FR-3.3: an installed <asset_root>/prd-stub.md whose marker is deleted,
    # duplicated, or that drops a mandatory header makes BOTH `gauntlet new` and
    # `check_entry_contract` raise — a broken gate-input template cannot disable
    # the FR-10.1 human-author gate.
    from gauntlet.engine import prd_stub as PS
    from gauntlet.engine.run import PRD_STUB_MARKER

    mgr = _prepare(fixture_repo)
    template, _ = PS.resolve_stub_template(fixture_repo, ".")
    repo_stub = fixture_repo / "prd-stub.md"  # asset_root "." → repo root

    cases = {
        "marker deleted": template.replace(PRD_STUB_MARKER + "\n", "", 1),
        "marker duplicated": template + "\n" + PRD_STUB_MARKER + "\n",
        "mandatory header removed": "\n".join(
            l for l in template.splitlines() if not l.startswith("## §5 ")
        ),
    }
    for i, (label, broken) in enumerate(cases.items()):
        repo_stub.write_text(broken)
        slug = f"broken{i}"
        with pytest.raises(PS.StubTemplateError):
            mgr.new(slug)  # refuses to scaffold from a broken template
        # author a real prd.md, then prove the gate still fails closed on the
        # malformed *template* even though the candidate would otherwise pass.
        layout = mgr.layout(slug)
        layout.slug_dir.mkdir(parents=True, exist_ok=True)
        layout.prd_path.write_text("# Real\n\nGenuine authored content here.\n")
        with pytest.raises(PS.StubTemplateError):
            mgr.check_entry_contract(slug)


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
    # P7g: `abort` keeps the aborted run's worktree on purpose ("abort while
    # retaining all snapshots and evidence", spike §11), and git will not
    # recreate a branch a worktree holds (E2-E). So under the dedicated default
    # the abort-then-re-run sequence goes through `clean`, which is the verb
    # that removes the tree — in the E2-D-safe order, snapshotting first.
    # The refusal in between is actionable rather than a bare git fatal, and
    # that is asserted by its own test below.
    mgr.clean("demo", force=True)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED  # no raise


def test_start_after_abort_names_clean_rather_than_a_bare_git_fatal(fixture_repo):
    """P7g: the abort-then-re-run path fails closed *actionably*.

    `abort` retains the run worktree as evidence, so the next `start` of that
    slug meets a spent branch that a live worktree still holds. Git's own
    refusal ("cannot force update the branch ... used by worktree at ...") is
    correct and useless: it names no verb. The engine must name `gauntlet clean`
    and say that nothing was changed.
    """
    from gauntlet.engine.run import StaleRunBranchError

    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, GATED_REFUSE)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    mgr.abort("demo")
    # merge the spent branch into base so the branch reads as spent, not stale
    git(fixture_repo, "merge", "-q", "--no-ff", "-m", "land", "gauntlet/demo")

    with pytest.raises(StaleRunBranchError, match="gauntlet clean demo"):
        mgr.start("demo", path, use_judge=False)
    # ...and the refusal changed nothing: the tree and its branch are both there
    assert gitops.branch_exists(fixture_repo, "gauntlet/demo")


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
    # P7g: the run branch is checked out in the RUN's tree and carries the
    # phase commit; the operator's checkout never moved (acceptance A1, which
    # the autouse property asserts for this test too). Naming the branch rather
    # than `HEAD` says the same thing from either vantage — refs are shared
    # across worktrees (spike E1).
    assert gitops.current_branch(run_work_tree(fixture_repo)) == "gauntlet/demo"
    assert gitops.commit_subject(fixture_repo, "gauntlet/demo") == "P1: implement"
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
    # P7g: the phase commits are on the run branch, in the RUN's tree; the
    # operator's HEAD never moved (acceptance A1).
    work = run_work_tree(fixture_repo)
    p2_sha = gitops.head_sha(work)
    assert gitops.commit_subject(fixture_repo, p2_sha) == "P2: phase two"

    target = mgr.rollback("demo", phase=1)
    assert gitops.head_sha(work) == target
    assert gitops.commit_subject(fixture_repo, "gauntlet/demo") == "P1: phase one"
    man = mgr.status("demo")
    assert [c.phase for c in man.commits] == ["P1"]
    # F-002: ALL phase-2 step records (not just its commit) are rewound to
    # pending, so a resume re-does the work git reset removed.
    assert man.record("impl2").status == M.PENDING
    assert man.record("c2").status == M.PENDING
    assert man.record("impl1").status == M.DONE  # phase 1 kept
    # a recovery snapshot preserved the pre-rollback tip (P3: the executor's
    # durable snapshot anchors the discarded tip through its parent chain)
    refs = gitops._run(
        fixture_repo, "for-each-ref", "--format=%(refname)",
        "refs/gauntlet/recovery/",
    ).splitlines()
    assert refs
    snapshot = git_snapshot.load_snapshot(fixture_repo, refs[-1])
    assert snapshot.run_branch_sha == p2_sha
    assert gitops.is_ancestor(fixture_repo, p2_sha, snapshot.snapshot_commit)


def test_rollback_reverses_auto_approval_and_flips_policy(fixture_repo):
    # pipeline-effectiveness FR-4.2 / P8-A5: a `gauntlet rollback` past a phase
    # boundary IS the human reversal of any auto-approved gate at or beyond it —
    # it stamps the reversal on the record and flips the run's effective policy
    # to `always` for the remainder (auto_approval_disabled), the deterministic
    # in-run circuit breaker.
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, TWO_PHASE)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    # Simulate an auto-approved P2 gate having occurred: inject the record on disk.
    layout = mgr.layout("demo")
    run_dir = layout.active_run_dir()
    man = M.Manifest.load(run_dir / "manifest.json")
    man.auto_approvals.append(M.AutoApproval(
        gate_id="p2-gate", phase="P2", evidence={"verifier": "clean", "rounds": 1},
        at="2026-07-07T00:00:00Z",
    ))
    man.write_atomic(run_dir / "manifest.json")

    mgr.rollback("demo", phase=1)
    rolled = M.Manifest.load(run_dir / "manifest.json")
    assert rolled.auto_approval_disabled is True
    assert rolled.auto_approvals[0].reversed_at is not None
    assert rolled.auto_approvals[0].reversed_by == "operator"


def test_rollback_tolerates_engine_bookkeeping_above_last_recorded(fixture_repo):
    """#62: engine bookkeeping commits (response checkpoints) are never appended
    to man.commits, so Guard 2's strict HEAD == last-recorded made every run
    that ever took a checkpoint permanently un-rollback-able. Bookkeeping-only
    advance is not divergence."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, TWO_PHASE)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    # P7g: a bookkeeping checkpoint commits the run's own EXPORT, which under
    # `dedicated` is the two-file copy the engine writes inside the RUN's tree
    # (spike §4.4 — the authoritative journal and projection stay in the
    # operator's checkout and are never committed, so the export is the only
    # in-tree bookkeeping path a commit can name). Materialised here with the
    # engine's own writer, which is what a real checkpoint does immediately
    # before staging (`StepContext.refresh_bookkeeping_export`).
    from gauntlet.engine import worktree as WT

    work = run_work_tree(fixture_repo)
    run_dir = mgr.layout("demo").active_run_dir()
    WT.write_bookkeeping_export(
        work, run_dir, mgr.config.run_root, "demo", run_dir.name
    )
    rel = (Path(mgr.config.run_root) / "demo" / run_dir.name).as_posix()
    bk = gitops.commit_run_bookkeeping(
        work, "gauntlet: response impl2-resp-1 pending",
        [f"{rel}/manifest.json"], identity=gitops.ENGINE_IDENTITY,
    )
    assert bk is not None  # the run branch is now ahead of its last recorded commit

    target = mgr.rollback("demo", phase=1)
    assert gitops.head_sha(work) == target
    assert gitops.commit_subject(fixture_repo, "gauntlet/demo") == "P1: phase one"
    man = mgr.status("demo")
    assert [c.phase for c in man.commits] == ["P1"]
    assert man.record("impl2").status == M.PENDING
    # The pre-rollback tip (incl. the bookkeeping commit) is preserved in the
    # recovery snapshot's parent chain.
    refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/recovery/")
    assert "refs/gauntlet/recovery/" in refs


def test_rollback_engine_shaped_pr_md_commit_is_not_bookkeeping(fixture_repo):
    """PR #76 review F-001 (updated for #72 absorb): PR.md is hidden from dirty
    checks (human-owned) but the engine never commits it, so an engine-MARKED
    commit touching it must NOT take the silent bookkeeping fast path. Under
    the #72 absorb tier it is handled like any real unmanifested descendant:
    backed up and absorbed LOUDLY — a recorded warning + backup ref, never a
    silent discard."""
    mgr = _prepare(fixture_repo, CONFIG_SAME_TREE)  # see CONFIG_SAME_TREE
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, TWO_PHASE)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    (fixture_repo / "runs" / "demo" / "PR.md").write_text("human-owned draft\n")
    gitops.commit_run_bookkeeping(
        fixture_repo, "gauntlet: response impl2-resp-1 pending",
        ["runs/demo/PR.md"], identity=gitops.ENGINE_IDENTITY,
    )
    ahead = gitops.head_sha(fixture_repo)

    mgr.rollback("demo", phase=1)
    man = mgr.status("demo")
    # The absorb audit trail proves the commit was NOT classified bookkeeping
    # (the bookkeeping fast path records no warning and needs no absorption).
    assert any("absorbed 1 unmanifested commit" in w for w in man.warnings)
    refs = gitops._run(
        fixture_repo, "for-each-ref", "--format=%(refname)",
        "refs/gauntlet/recovery/",
    ).splitlines()
    snapshot = git_snapshot.load_snapshot(fixture_repo, refs[-1])
    # the discarded state is reachable, by construction
    assert gitops.is_ancestor(fixture_repo, ahead, snapshot.snapshot_commit)


def test_rollback_absorbs_strictly_ahead_commits_with_backup(fixture_repo):
    """#72: an unmanifested descendant commit (a builder killed after
    committing wip but before a manifest flush, then `recover`ed) used to
    deadlock rollback behind the FR-9.9 guard with no native way out. It is
    now backed up, absorbed to the phase boundary, and recorded loudly."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, TWO_PHASE)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    # P7g: an unmanifested descendant commit lands on the RUN branch, in the
    # RUN's tree — that is where a builder killed after committing left it.
    work = run_work_tree(fixture_repo)
    (work / "wip.py").write_text("committed but unmanifested\n")
    git(work, "add", "-A")
    git(work, "-c", "user.name=B", "-c", "user.email=b@g.local",
        "commit", "-qm", "P2 wip: arm the thing")
    ahead = gitops.head_sha(work)

    target = mgr.rollback("demo", phase=1)
    assert gitops.head_sha(work) == target
    assert gitops.commit_subject(fixture_repo, "gauntlet/demo") == "P1: phase one"
    man = mgr.status("demo")
    assert any(
        "absorbed 1 unmanifested commit" in w and "P2 wip: arm the thing" in w
        for w in man.warnings
    )
    refs = gitops._run(
        fixture_repo, "for-each-ref", "--format=%(refname)",
        "refs/gauntlet/recovery/",
    ).splitlines()
    snapshot = git_snapshot.load_snapshot(fixture_repo, refs[-1])
    assert gitops.is_ancestor(fixture_repo, ahead, snapshot.snapshot_commit)


def test_rollback_rewinds_the_run_branch_not_the_checkout(fixture_repo):
    """PR #77 review (blocking): with the descendant-absorb tier, a checked-out
    merged base branch IS a descendant of the last recorded commit — reading
    bare HEAD would hard-reset MAIN while the run branch stood still. Rollback
    must resolve and check out man.branch explicitly."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, TWO_PHASE)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    work = run_work_tree(fixture_repo)
    run_tip = gitops.head_sha(work)
    # The operator merged the run branch to main and main moved on — a strict
    # descendant of the last recorded commit, checked out at rollback time.
    # P7g: their untracked authoring copy of prd.md would block the merge (the
    # state `gauntlet finish` resolves for them); clear it as finish would.
    git(fixture_repo, "checkout", "-q", "main")
    local_prd = mgr.layout("demo").prd_path
    if local_prd.exists() and "prd.md" not in git(fixture_repo, "ls-files", "runs"):
        local_prd.unlink()
    git(fixture_repo, "merge", "-q", "--ff-only", "gauntlet/demo")
    (fixture_repo / "post-merge.py").write_text("main moved on\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "-c", "user.name=H", "-c", "user.email=h@h.local",
        "commit", "-qm", "post-merge work on main")
    main_tip = gitops.head_sha(fixture_repo)

    target = mgr.rollback("demo", phase=1)
    # main is untouched; the RUN branch was rewound. P7g: "checked out and
    # rewound" happens in the run's own tree — the operator stays on `main`,
    # which is the stronger form of the property this test defends (reading
    # bare HEAD would have hard-reset `main`).
    assert gitops.rev_parse(fixture_repo, "main") == main_tip
    assert gitops.rev_parse(fixture_repo, "gauntlet/demo") == target
    assert gitops.current_branch(work) == "gauntlet/demo"
    assert gitops.current_branch(fixture_repo) == "main"
    assert gitops.rev_parse(fixture_repo, "gauntlet/demo") != run_tip


def test_rollback_refuses_missing_run_branch(fixture_repo):
    # PR #77 review: a deleted run branch must refuse loudly, never fall back
    # to rewinding whatever is checked out.
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    mgr.start("demo", path, use_judge=False,
              adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))
    # P7g: the run's own worktree holds the branch, and the engine's anti-prune
    # `git worktree lock` blocks even `remove --force` (spike E6-B), so taking
    # a run branch away is unlock → remove → `branch -D`. That is the §11 row-3
    # sequence, and it leaves exactly the state under test: the branch gone
    # while the run's manifest still records commits on it.
    work = run_work_tree(fixture_repo)
    git(fixture_repo, "checkout", "-q", "main")
    if work != fixture_repo:
        git(fixture_repo, "worktree", "unlock", str(work))
        git(fixture_repo, "worktree", "remove", "--force", str(work))
    git(fixture_repo, "branch", "-qD", "gauntlet/demo")
    with pytest.raises(RollbackGuardError, match="missing"):
        mgr.rollback("demo", phase=1)


def test_rollback_refuses_cross_branch_checkout_with_pr_md_edits(fixture_repo):
    """Excluded human edits must be handled before switching to the run branch;
    fail with an actionable guard instead of leaking a checkout failure."""
    mgr = _prepare(fixture_repo, CONFIG_SAME_TREE)  # see CONFIG_SAME_TREE
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    mgr.start("demo", path, use_judge=False,
              adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))
    git(fixture_repo, "checkout", "-q", "main")
    pr = fixture_repo / "runs" / "demo" / "PR.md"
    pr.parent.mkdir(parents=True, exist_ok=True)
    pr.write_text("uncommitted human draft\n")

    with pytest.raises(RollbackGuardError, match="human-owned PR.md"):
        mgr.rollback("demo", phase=1)
    assert gitops.current_branch(fixture_repo) == "main"
    assert pr.read_text() == "uncommitted human draft\n"


def test_rollback_preserves_uncommitted_pr_md_edits(fixture_repo):
    """PR #77 review (blocking): PR.md is excluded from the dirty check and
    the backup by policy, but reset --hard is not policy-scoped — the human's
    uncommitted edit must be carried across the rewind, not destroyed."""
    mgr = _prepare(fixture_repo, CONFIG_SAME_TREE)  # see CONFIG_SAME_TREE
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, TWO_PHASE)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    # The human tracked the drafted PR.md (a real descendant commit — absorbed
    # by the #72 tier), then kept editing without committing.
    pr = fixture_repo / "runs" / "demo" / "PR.md"
    pr.write_text("PR draft v1\n")
    git(fixture_repo, "add", "runs/demo/PR.md")
    git(fixture_repo, "-c", "user.name=H", "-c", "user.email=h@h.local",
        "commit", "-qm", "chore: track the PR draft")
    pr.write_text("PR draft v2 — human edited, uncommitted\n")

    target = mgr.rollback("demo", phase=1)
    assert gitops.head_sha(fixture_repo) == target
    # The tracked version was absorbed (backed up); the uncommitted edit is
    # back in the worktree byte-for-byte.
    assert pr.read_text() == "PR draft v2 — human edited, uncommitted\n"
    refs = gitops._run(
        fixture_repo, "for-each-ref", "--format=%(refname)",
        "refs/gauntlet/recovery/",
    ).splitlines()
    snapshot = git_snapshot.load_snapshot(fixture_repo, refs[-1])
    assert "runs/demo/PR.md" in snapshot.protected_paths
    assert gitops._run(
        fixture_repo, "show", f"{snapshot.worktree_tree}:runs/demo/PR.md"
    ) == "PR draft v2 — human edited, uncommitted\n"


def test_rollback_preserves_and_backs_up_pr_md_deletion(fixture_repo):
    """Rollback restores a tracked PR.md deletion after reset and represents
    that deletion durably in its backup ref."""
    mgr = _prepare(fixture_repo, CONFIG_SAME_TREE)  # see CONFIG_SAME_TREE
    _author_prd(mgr, "demo")
    pr = fixture_repo / "runs" / "demo" / "PR.md"
    pr.write_text("draft to delete\n")
    git(fixture_repo, "add", "runs/demo/prd.md", "runs/demo/PR.md")
    git(fixture_repo, "commit", "-qm", "track run inputs")
    path = _write_pipeline(fixture_repo, TWO_PHASE)
    calls = {"n": 0}

    def factory(name):
        calls["n"] += 1
        return FakeAdapter(writes={f"f{calls['n']}.py": "x\n"})

    assert mgr.start("demo", path, use_judge=False, adapter_factory=factory) == M.RUN_DONE
    pr.unlink()

    mgr.rollback("demo", phase=1)
    assert not pr.exists()
    refs = gitops._run(
        fixture_repo, "for-each-ref", "--format=%(refname)",
        "refs/gauntlet/recovery/",
    ).splitlines()
    snapshot = git_snapshot.load_snapshot(fixture_repo, refs[-1])
    # The deletion is durably represented in the snapshot record itself.
    assert "runs/demo/PR.md" in snapshot.protected_deletions
    tree = gitops._run(
        fixture_repo, "ls-tree", "-r", "--name-only", snapshot.worktree_tree
    )
    assert "runs/demo/PR.md" not in tree


def test_rollback_refuses_branch_forked_from_manifest(fixture_repo):
    # F-003 protection, retained under the #72 absorb tier: a tip that is NOT a
    # descendant of the last recorded commit (genuine fork — recorded commits
    # missing from the branch) still refuses. Only strictly-ahead descendants
    # are absorbed; a fork cannot be represented as a linear backup range.
    # (Updated with #72: the prior descendant-commit variant of this test now
    # absorbs by design — see test_rollback_absorbs_strictly_ahead_commits.)
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    mgr.start("demo", path, use_judge=False,
              adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))
    # P7g: the fork has to happen where the run branch lives — its own tree.
    # Git hard-refuses a `reset`/`branch -f` of a branch checked out elsewhere
    # (spike E2-E), and the guard under test reads the RUN branch, so forking
    # the operator's `main` would prove nothing about it.
    work = run_work_tree(fixture_repo)
    last_recorded = gitops.head_sha(work)
    gitops.reset_hard(work, f"{last_recorded}~1")
    (work / "extra.py").write_text("forked line of history\n")
    git(work, "add", "-A")
    git(work, "-c", "user.name=H", "-c", "user.email=h@h.local",
        "commit", "-qm", "fork commit")
    with pytest.raises(RollbackGuardError, match="diverged"):
        mgr.rollback("demo", phase=1)


def test_rollback_refuses_dirty_worktree(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    adapter = FakeAdapter(writes={"feature.py": "code\n"})
    mgr.start("demo", path, use_judge=False, adapter_factory=lambda n: adapter)
    # P7g: rollback inspects the tree it is about to rewind — the RUN's tree
    # under the default. `run.py` already says so ("WORK tree: rollback rewinds
    # the RUN's branch and tree; it must never reach into the operator's
    # checkout"), and the refusal message names the tree it inspected. Dirtying
    # the operator's checkout would assert the opposite of acceptance A1.
    (run_work_tree(fixture_repo) / "dirt.py").write_text("uncommitted")
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


def test_rollback_prevalidation_failure_leaves_checkout_unchanged(fixture_repo):
    """post-177d721 F-004 regression (structural, P3): every rollback guard and
    the target resolution run BEFORE any checkout, so a refused rollback is
    observational — the operator's checked-out branch and HEAD are untouched.
    Previously the checkout to the run branch happened before the phase-target
    resolution, so an unknown phase mutated the checkout and then refused."""
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    path = _write_pipeline(fixture_repo, LINEAR)
    adapter = FakeAdapter(writes={"feature.py": "code\n"})
    mgr.start("demo", path, use_judge=False, adapter_factory=lambda n: adapter)
    git(fixture_repo, "checkout", "-q", "main")
    original_branch = gitops.current_branch(fixture_repo)
    original_head = gitops.head_sha(fixture_repo)

    with pytest.raises(RollbackGuardError, match="phase-9"):
        mgr.rollback("demo", phase=9)

    assert gitops.current_branch(fixture_repo) == original_branch
    assert gitops.head_sha(fixture_repo) == original_head


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

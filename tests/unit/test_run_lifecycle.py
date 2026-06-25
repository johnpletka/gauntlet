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


# --- P2: structured stub is the §6 manifest skeleton (FR-2.1, FR-2.2) --------

def test_new_scaffolds_full_section_skeleton(fixture_repo):
    # FR-2.1 / review F-003: a freshly scaffolded prd.md contains EVERY §6
    # manifest header — both mandatory AND scale-with-size — each with a one-line
    # guidance comment, and exactly one marker. Asserting only the mandatory
    # subset would let an implementation drop every scale-with-size section.
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


def test_scaffold_and_entry_contract_read_the_same_resolved_source(fixture_repo):
    # FR-2.2: `new` (which writes the scaffold) and `check_entry_contract` (which
    # decides "still a stub") resolve the SAME template bytes — no second copy.
    from gauntlet.engine import prd_stub as PS

    mgr = _prepare(fixture_repo)
    written = mgr.new("demo").read_text()
    template, _ = PS.resolve_stub_template(fixture_repo, ".")
    assert written == template


def test_drift_guard_trips_when_playbook_section_changes(fixture_repo):
    # FR-2.2: the drift test is driven off the PARSED playbook, so adding,
    # renaming, or removing a heading of EITHER class (mandatory or scale-with-
    # size) in the playbook trips it (the stub no longer mirrors the manifest).
    from gauntlet.engine import prd_stub as PS

    mgr = _prepare(fixture_repo)
    stub = mgr.new("demo").read_text()
    playbook = PS.resolve_playbook_text(fixture_repo, ".")
    base = PS.parse_manifest(playbook)
    assert PS.stub_section_names(stub) == [e.name for e in base]  # aligned today

    # add (mandatory), rename (scale-with-size), remove (mandatory) — each breaks
    add = playbook.replace(
        "**§11 Open Questions**", "**§11.5 Brand New** *(mandatory)*\n\n**§11 Open Questions**"
    )
    rename = playbook.replace("**§3 Users and Personas**", "**§3 Stakeholders**")
    remove = playbook.replace("**§9 Success Metrics** *(mandatory)*", "removed line")
    for mutated in (add, rename, remove):
        mutated_manifest = PS.parse_manifest(mutated)
        assert PS.stub_section_names(stub) != [e.name for e in mutated_manifest]


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

"""P3 — deferral reconciliation + phase-size lint (FR-3.3, FR-3.4).

The remaining two #54 guardrails: a deferral that points to a nonexistent phase
parks (FR-3.3), a valid open deferral round-trips verbatim into the target
phase's implement prompt (FR-3.3), and an oversized phase (> max_frs_per_phase
distinct FR refs) fires the size lint — warn by default, park when configured
(FR-3.4).

Each ``test_*`` here is a cited node id in this phase's acceptance map.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gauntlet.engine.config import RunConfig
from gauntlet.engine.deferrals import (
    Deferral,
    deferrals_from_map,
    distinct_fr_refs,
    open_deferrals_for,
    parse_body_deferrals,
    phantom_deferrals,
)
from gauntlet.engine.execution import DONE, HALTED, StepContext
from gauntlet.engine.manifest import (
    HALT_REASON_PRECONDITION,
    CommitRecord,
    Manifest,
    PipelineRef,
    StepRecord,
)
from gauntlet.engine.pipeline import Pipeline, Step
from gauntlet.engine.steptypes import (
    DeferralCollectionError,
    _collect_run_deferrals,
    _acceptance_map_relpath,
    _render_prompt,
    handle_acceptance_gate,
    handle_phase_lint,
)
from gauntlet.logging.redact import RedactingWriter

REPO = Path(__file__).resolve().parents[2]


# --- context builder ---------------------------------------------------------
def _ctx(repo: Path, *, iteration_item=None, commits=None) -> StepContext:
    """A StepContext with the real acceptance-map schema copied into the tmp repo."""
    artifact_root = repo / "runs" / "demo"
    artifact_root.mkdir(parents=True, exist_ok=True)
    schemas = repo / "schemas"
    schemas.mkdir(exist_ok=True)
    (schemas / "acceptance-map.json").write_text(
        (REPO / "schemas" / "acceptance-map.json").read_text()
    )
    cfg = RunConfig.model_validate({"agents": {"builder": {"adapter": "claude-code"}}})
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"),
                   commits=commits or [])
    return StepContext(
        repo_root=repo, run_dir=artifact_root / "run-1", artifact_root=artifact_root,
        config=cfg, pipeline=Pipeline.model_validate(
            {"name": "demo", "version": 1, "stages": []}),
        manifest=man, record=StepRecord(id="s", type="agent_task"),
        writer=RedactingWriter(),
        iteration_item=iteration_item,
        judge_env={"GAUNTLET_JUDGE_TOKEN": "tok", "GAUNTLET_JUDGE_MODE": "unattended"},
    )


def _phase(pid, clause_ids):
    return {
        "id": pid, "title": "t", "goal": "g",
        "acceptance": [{"id": cid, "clause": f"clause {cid}"} for cid in clause_ids],
    }


def _write_plan(ctx: StepContext, phase_ids) -> None:
    """A minimal plan.md whose gauntlet-phases block declares ``phase_ids``."""
    blocks = "\n".join(
        f"- id: {pid}\n  title: t\n  goal: g\n"
        f"  acceptance:\n    - id: {pid}-A1\n      clause: it works"
        for pid in phase_ids
    )
    heads = "\n\n".join(f"## {pid} — {pid} title\nprose" for pid in phase_ids)
    text = (
        f"# Plan\n\n{heads}\n\n## Machine-readable phase list\n\n"
        f"```gauntlet-phases\n{blocks}\n```\n"
    )
    (ctx.artifact_root / "plan.md").write_text(text)


def _write_map(ctx: StepContext, phase, clauses, deferrals=None) -> None:
    mapping = {
        "phase": phase,
        "clauses": [
            {"id": cid, "text": f"clause {cid}", "evidence": ev}
            for cid, ev in clauses.items()
        ],
    }
    if deferrals is not None:
        mapping["deferrals"] = deferrals
    d = ctx.artifact_root / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "acceptance-map.json").write_text(json.dumps(mapping))


def _gate_step():
    return Step.model_validate({"id": "acceptance-gate", "type": "acceptance_gate",
                                "collector": "pytest"})


def _git(repo: Path, *args, message: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=message,
                          capture_output=True, text=True, check=True).stdout


# --- deferral parsing (pure) -------------------------------------------------
def test_parse_body_deferrals_extracts_phase_and_verbatim_text():
    body = (
        "P2: Do the thing\n\nWhat changed and why.\n"
        "Deferred to P5: windows path handling\n"
        "not a deferral line\n"
    )
    out = parse_body_deferrals(body, source="commit:abc")
    assert out == [Deferral(to_phase="P5", text="windows path handling",
                            source="commit:abc")]


def test_parse_body_deferrals_handles_no_colon_and_multiple():
    body = "Deferred to P3 retry logic\nDeferred to P4: later work"
    out = parse_body_deferrals(body, source="c")
    assert [(d.to_phase, d.text) for d in out] == [
        ("P3", "retry logic"), ("P4", "later work")]


def test_parse_body_deferrals_empty_when_none():
    assert parse_body_deferrals("no deferrals here", source="c") == []


def test_deferrals_from_map_reads_structured_entries():
    mapping = {"phase": "P2", "clauses": [],
               "deferrals": [{"text": "windows paths", "to_phase": "P5"},
                             {"text": "ignored", "to_phase": "P6"}]}
    out = deferrals_from_map(mapping, source="acceptance-map:P2")
    assert [(d.to_phase, d.text) for d in out] == [
        ("P5", "windows paths"), ("P6", "ignored")]


def test_deferrals_from_map_empty_on_missing_list():
    assert deferrals_from_map({"phase": "P2", "clauses": []}, source="m") == []


def test_phantom_deferrals_flags_only_unknown_targets():
    ds = [Deferral("P5", "a", "m"), Deferral("P99", "b", "m")]
    phantom = phantom_deferrals(ds, {"P1", "P2", "P5"})
    assert [d.to_phase for d in phantom] == ["P99"]


def test_phantom_deferrals_exempts_out_of_run_targets():
    """A deferral to a non-phase target (post-v1 / FUTURE.md — the CLAUDE.md §7
    out-of-run convention) is intentional, not a phantom phase, and never parks
    (FR-3.3 reconciles only 'Deferred to P<N>'-style targets)."""
    ds = [Deferral("post-v1", "a", "m"), Deferral("FUTURE.md", "b", "m")]
    assert phantom_deferrals(ds, {"P1", "P2"}) == []


def test_open_deferrals_for_filters_and_dedups():
    ds = [
        Deferral("P3", "retry logic", "commit:a"),
        Deferral("P3", "retry logic", "acceptance-map@a"),  # dupe text -> collapsed
        Deferral("P3", "another item", "commit:b"),
        Deferral("P4", "other phase", "commit:c"),          # different target
    ]
    out = open_deferrals_for("P3", ds)
    assert [d.text for d in out] == ["retry logic", "another item"]
    assert out[0].source == "commit:a"  # first-seen source wins


# --- FR-3.4 distinct FR references (pure) ------------------------------------
def test_distinct_fr_refs_counts_dotted_tokens():
    text = "Deliverables (FR-1.1, FR-1.2, FR-1.3): implements FR-1.1 again."
    assert distinct_fr_refs(text) == {"FR-1.1", "FR-1.2", "FR-1.3"}


def test_distinct_fr_refs_word_bounded():
    # FR-34 is one token, not FR-3 + 4; bare FR-6 and FR-6.1 are distinct.
    assert distinct_fr_refs("FR-34 and FR-6 and FR-6.1") == {
        "FR-34", "FR-6", "FR-6.1"}


# --- FR-3.3: reconciliation parks a phantom deferral (P3-A1) -----------------
def test_gate_parks_on_phantom_deferral_in_map(fixture_repo):
    """P3-A1: a deferral (in the acceptance map) to a nonexistent phase parks."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P2", ["P2-A1"]))
    _write_plan(ctx, ["P1", "P2"])  # the plan has no P99
    _write_map(ctx, "P2", {"P2-A1": [{"kind": "pytest", "id": "tests/x.py::t"}]},
               deferrals=[{"text": "later work", "to_phase": "P99"}])
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert result.halt_reason == HALT_REASON_PRECONDITION
    assert "P99" in result.notes and "nonexistent" in result.notes


def test_gate_parks_on_phantom_deferral_in_commit_body(fixture_repo):
    """A phantom deferral recorded only in this phase's commit body also parks."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P2", ["P2-A1"]))
    _write_plan(ctx, ["P1", "P2"])
    _write_map(ctx, "P2", {"P2-A1": [{"kind": "pytest", "id": "tests/x.py::t"}]})
    _git(fixture_repo, "commit", "--allow-empty", "-q",
         "-m", "P2: work\n\nBody.\nDeferred to P42: phantom target")
    sha = _git(fixture_repo, "rev-parse", "HEAD").strip()
    ctx.manifest.commits.append(CommitRecord(step_id="phase-commit", phase="P2", sha=sha))
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert "P42" in result.notes and "nonexistent" in result.notes


def _stub_gate_enumeration(monkeypatch, ids):
    """Stub the enumeration seams (engine-subprocess-in-copy posture, PR #59 F3)
    so the deferral-reconciliation path is exercised without a real worktree copy
    or pytest run: a no-op copy plus a fixed enumeration result."""
    from pathlib import Path

    from gauntlet.engine import collectors, verify

    copy = verify.DisposableCopy(path=Path("/copy-path"), root=Path("/copy-path"))
    monkeypatch.setattr(verify, "make_disposable_copy", lambda repo, **k: copy)
    monkeypatch.setattr(verify, "discard_disposable_copy", lambda repo, c: None)
    monkeypatch.setattr(collectors.Collector, "enumerate",
                        lambda self, **k: set(ids))


def test_gate_passes_on_valid_deferral(fixture_repo, monkeypatch):
    """A deferral to a REAL later phase reconciles cleanly (the gate proceeds)."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P2", ["P2-A1"]))
    _write_plan(ctx, ["P1", "P2", "P5"])  # P5 exists
    _write_map(ctx, "P2", {"P2-A1": [{"kind": "pytest", "id": "tests/x.py::t"}]},
               deferrals=[{"text": "windows paths", "to_phase": "P5"}])
    _stub_gate_enumeration(monkeypatch, {"tests/x.py::t"})
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == DONE


def test_gate_passes_on_out_of_run_deferral(fixture_repo, monkeypatch):
    """An out-of-run deferral (to_phase 'post-v1' — the CLAUDE.md §7 / FUTURE.md
    convention, as P2's own shipped map uses) is NOT a phantom phase and clears
    the gate, even though 'post-v1' is not a plan phase."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P2", ["P2-A1"]))
    _write_plan(ctx, ["P1", "P2"])
    _write_map(ctx, "P2", {"P2-A1": [{"kind": "pytest", "id": "tests/x.py::t"}]},
               deferrals=[{"text": "non-pytest collectors", "to_phase": "post-v1"}])
    _stub_gate_enumeration(monkeypatch, {"tests/x.py::t"})
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == DONE


def test_gate_no_plan_when_no_deferrals(fixture_repo, monkeypatch):
    """A phase with no deferral never loads (or fails on) the plan list — the
    plain P2 gate path is unchanged when nothing needs reconciling."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P2", ["P2-A1"]))
    # deliberately NO plan.md written; no deferrals in the map
    _write_map(ctx, "P2", {"P2-A1": [{"kind": "pytest", "id": "tests/x.py::t"}]})
    _stub_gate_enumeration(monkeypatch, {"tests/x.py::t"})
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == DONE


# --- FR-3.3: valid open deferral injected verbatim (P3-A2) -------------------
def test_open_deferral_appears_verbatim_in_implement_prompt(fixture_repo):
    """P3-A2: a deferral a prior phase pushed to P3 (recorded in a commit body)
    appears verbatim in P3's rendered implement prompt."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P3", ["P3-A1"]))
    _git(fixture_repo, "commit", "--allow-empty", "-q",
         "-m", "P2: earlier phase\n\nBody.\nDeferred to P3: retry logic on ApiAdapter")
    sha = _git(fixture_repo, "rev-parse", "HEAD").strip()
    ctx.manifest.commits.append(CommitRecord(step_id="phase-commit", phase="P2", sha=sha))
    step = Step.model_validate(
        {"id": "implement", "type": "agent_task", "agent": "builder",
         "prompt_text": "Implement the current phase."})
    rendered = _render_prompt(step, ctx)
    assert "retry logic on ApiAdapter" in rendered  # verbatim
    assert "open deferrals targeting P3" in rendered


def test_open_deferral_from_committed_map_injected(fixture_repo):
    """A deferral in a prior phase's COMMITTED acceptance-map (read out of history)
    is injected into the target phase's prompt too (the mapping-artifact source)."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P5", ["P5-A1"]))
    # commit an acceptance-map.json (phase P2) deferring work to P5
    map_dir = fixture_repo / "runs" / "demo" / "artifacts"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "acceptance-map.json").write_text(json.dumps({
        "phase": "P2", "clauses": [{"id": "P2-A1", "text": "c",
                                    "evidence": [{"kind": "pytest", "id": "t::t"}]}],
        "deferrals": [{"text": "sandbox network egress", "to_phase": "P5"}]}))
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-q", "-m", "P2: map\n\nbody")
    sha = _git(fixture_repo, "rev-parse", "HEAD").strip()
    ctx.manifest.commits.append(CommitRecord(step_id="phase-commit", phase="P2", sha=sha))
    step = Step.model_validate(
        {"id": "implement", "type": "agent_task", "agent": "builder",
         "prompt_text": "Implement."})
    rendered = _render_prompt(step, ctx)
    assert "sandbox network egress" in rendered
    assert "open deferrals targeting P5" in rendered


def test_no_deferral_block_when_none_target_phase(fixture_repo):
    """A phase nobody deferred work to gets no injected block (the ordinary case)."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P3", ["P3-A1"]))
    _git(fixture_repo, "commit", "--allow-empty", "-q",
         "-m", "P2: earlier\n\nBody.\nDeferred to P4: unrelated work")
    sha = _git(fixture_repo, "rev-parse", "HEAD").strip()
    ctx.manifest.commits.append(CommitRecord(step_id="phase-commit", phase="P2", sha=sha))
    step = Step.model_validate(
        {"id": "implement", "type": "agent_task", "agent": "builder",
         "prompt_text": "Implement."})
    rendered = _render_prompt(step, ctx)
    assert "open deferrals" not in rendered


# --- FR-3.3: committed-map recovery fails closed (review F-001) --------------
def _commit_map_content(fixture_repo: Path, content: str) -> str:
    """Commit ``content`` as the run's acceptance-map.json; return the commit sha."""
    map_dir = fixture_repo / "runs" / "demo" / "artifacts"
    map_dir.mkdir(parents=True, exist_ok=True)
    (map_dir / "acceptance-map.json").write_text(content)
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-q", "-m", "P2: map\n\nbody")
    return _git(fixture_repo, "rev-parse", "HEAD").strip()


def test_collect_run_deferrals_fails_closed_on_unparseable_committed_map(fixture_repo):
    """A commit that TRACKS the acceptance map but whose committed content is not
    valid JSON raises instead of degrading to 'no deferrals' — recovering the
    committed deferrals[] must fail closed, never silently drop the obligation."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P5", ["P5-A1"]))
    sha = _commit_map_content(fixture_repo, "{not valid json")
    ctx.manifest.commits.append(CommitRecord(step_id="phase-commit", phase="P2", sha=sha))
    with pytest.raises(DeferralCollectionError) as exc:
        _collect_run_deferrals(ctx, map_relpath=_acceptance_map_relpath(ctx))
    assert sha[:10] in str(exc.value)


def test_render_prompt_halts_on_unparseable_committed_map(fixture_repo):
    """The target phase's prompt render halts (raises) rather than omitting the
    open-deferral block when a prior committed map cannot be parsed — the omitted
    block would be indistinguishable from 'no deferral' (review F-001)."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P5", ["P5-A1"]))
    sha = _commit_map_content(fixture_repo, "{not valid json")
    ctx.manifest.commits.append(CommitRecord(step_id="phase-commit", phase="P2", sha=sha))
    step = Step.model_validate(
        {"id": "implement", "type": "agent_task", "agent": "builder",
         "prompt_text": "Implement."})
    with pytest.raises(DeferralCollectionError):
        _render_prompt(step, ctx)


def test_collect_run_deferrals_skips_commit_without_map(fixture_repo):
    """A commit that does not carry the acceptance map (the ordinary non-phase
    commit) is skipped, not failed — only a body deferral is collected there."""
    ctx = _ctx(fixture_repo, iteration_item=_phase("P3", ["P3-A1"]))
    _git(fixture_repo, "commit", "--allow-empty", "-q",
         "-m", "P2: earlier\n\nBody.\nDeferred to P3: retry logic")
    sha = _git(fixture_repo, "rev-parse", "HEAD").strip()
    ctx.manifest.commits.append(CommitRecord(step_id="phase-commit", phase="P2", sha=sha))
    out = _collect_run_deferrals(ctx, map_relpath=_acceptance_map_relpath(ctx))
    assert [(d.to_phase, d.text) for d in out] == [("P3", "retry logic")]


# --- FR-3.4: phase-size lint at the boundary (P3-A3) -------------------------
def _plan_with_frs(refs: list[str]) -> str:
    deliverables = ", ".join(refs)
    return (
        "# Plan\n\n"
        f"## P1 — Big phase\nDeliverables ({deliverables}): stuff.\n\n"
        "## Machine-readable phase list\n\n"
        "```gauntlet-phases\n"
        "- id: P1\n  title: Big phase\n  goal: do it\n"
        "  acceptance:\n    - id: P1-A1\n      clause: it works\n```\n"
    )


def _lint_step(**kw):
    return Step.model_validate({"id": "plan-lint", "type": "phase_lint",
                                "artifact": "plan.md", **kw})


def test_size_lint_passes_at_bound(fixture_repo):
    """Exactly max_frs_per_phase (3) distinct FR refs is NOT oversized."""
    ctx = _ctx(fixture_repo)
    (ctx.artifact_root / "plan.md").write_text(
        _plan_with_frs(["FR-1.1", "FR-1.2", "FR-1.3"]))
    result = handle_phase_lint(_lint_step(), ctx)
    assert result.status == DONE
    assert "WARNING" not in result.notes


def test_size_lint_warns_above_bound(fixture_repo):
    """P3-A3: > max_frs_per_phase distinct FR refs warns (default mode) — DONE
    with the finding surfaced in notes, not a park."""
    ctx = _ctx(fixture_repo)
    (ctx.artifact_root / "plan.md").write_text(
        _plan_with_frs(["FR-1.1", "FR-1.2", "FR-1.3", "FR-1.4"]))
    result = handle_phase_lint(_lint_step(), ctx)
    assert result.status == DONE
    assert "WARNING" in result.notes and "P1 carries 4" in result.notes


def test_size_lint_parks_above_bound_in_park_mode(fixture_repo):
    """P3-A3: in park mode, an oversized phase fails closed at the plan gate."""
    ctx = _ctx(fixture_repo)
    (ctx.artifact_root / "plan.md").write_text(
        _plan_with_frs(["FR-1.1", "FR-1.2", "FR-1.3", "FR-1.4"]))
    result = handle_phase_lint(_lint_step(size_lint="park"), ctx)
    assert result.status == HALTED
    assert result.halt_reason == HALT_REASON_PRECONDITION
    assert "max_frs_per_phase=3" in result.notes and "P1 carries 4" in result.notes


def test_size_lint_respects_configured_bound(fixture_repo):
    """The bound is config-driven: max_frs_per_phase=5 admits a 4-FR phase."""
    ctx = _ctx(fixture_repo)
    ctx.config.max_frs_per_phase = 5
    (ctx.artifact_root / "plan.md").write_text(
        _plan_with_frs(["FR-1.1", "FR-1.2", "FR-1.3", "FR-1.4"]))
    result = handle_phase_lint(_lint_step(size_lint="park"), ctx)
    assert result.status == DONE


def test_size_lint_unknown_mode_fails_closed(fixture_repo):
    """An unrecognized size_lint mode is a fail-closed misconfiguration (FR-3.4)."""
    ctx = _ctx(fixture_repo)
    (ctx.artifact_root / "plan.md").write_text(
        _plan_with_frs(["FR-1.1", "FR-1.2"]))
    result = handle_phase_lint(_lint_step(size_lint="explode"), ctx)
    assert result.status == HALTED
    assert "unknown size_lint mode" in result.notes

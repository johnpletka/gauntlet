"""P2 — acceptance mapping + acceptance_gate (FR-3.1, FR-3.2).

Covers the deterministic completeness gate that structurally closes the #54
class (silent partial delivery): a required per-phase ``acceptance:`` list
(FR-3.1), an ``acceptance_gate`` that proves every clause maps to a real,
collector-enumerated test id (FR-3.2), the closed collector-kind enum, and the
P2-P4 fail-closed interim enumeration posture (review F-002).

Each ``test_*`` here is a cited node id in this phase's acceptance map.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from gauntlet.engine import collectors, verify
from gauntlet.engine.collectors import (
    CollectorEnumerationError,
    _parse_pytest,
    run_bounded_enumeration,
)
from gauntlet.engine.config import RunConfig
from gauntlet.engine.execution import DONE, HALTED, PARKED, StepContext
from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import (
    HALT_REASON_PRECONDITION,
    Manifest,
    PipelineRef,
    StepRecord,
)
from gauntlet.engine.pipeline import Pipeline, Step
from gauntlet.engine.planphases import (
    PlanPhasesError,
    acceptance_clause_errors,
    extract_phases,
)
from gauntlet.engine.steptypes import handle_acceptance_gate, handle_phase_lint
from gauntlet.engine.validate import PipelineValidationError, validate_pipeline
from gauntlet.logging.redact import RedactingWriter

REPO = Path(__file__).resolve().parents[2]


# --- test context builder ----------------------------------------------------
def _ctx(repo: Path, *, iteration_item=None) -> StepContext:
    """A StepContext for the acceptance_gate handler, with the real schema copied
    into the tmp repo so schema validation exercises the shipped closed enum."""
    artifact_root = repo / "runs" / "demo"
    artifact_root.mkdir(parents=True, exist_ok=True)
    # copy the shipped acceptance-map schema into the tmp repo (asset_root ".")
    schemas = repo / "schemas"
    schemas.mkdir(exist_ok=True)
    (schemas / "acceptance-map.json").write_text(
        (REPO / "schemas" / "acceptance-map.json").read_text()
    )
    cfg = RunConfig.model_validate({"agents": {"builder": {"adapter": "claude-code"}}})
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    return StepContext(
        repo_root=repo, run_dir=artifact_root / "run-1", artifact_root=artifact_root,
        config=cfg, pipeline=Pipeline.model_validate(
            {"name": "demo", "version": 1, "stages": []}),
        manifest=man, record=StepRecord(id="acceptance-gate", type="acceptance_gate"),
        writer=RedactingWriter(),
        iteration_item=iteration_item,
        judge_env={"GAUNTLET_JUDGE_TOKEN": "tok", "GAUNTLET_JUDGE_MODE": "unattended"},
    )


def _phase(clause_ids):
    return {
        "id": "P2", "title": "t", "goal": "g",
        "acceptance": [{"id": cid, "clause": f"clause {cid}"} for cid in clause_ids],
    }


def _write_map(ctx: StepContext, clauses: dict[str, list[dict]]) -> None:
    """Write artifacts/acceptance-map.json mapping clause id -> evidence list."""
    mapping = {
        "phase": "P2",
        "clauses": [
            {"id": cid, "text": f"clause {cid}", "evidence": ev}
            for cid, ev in clauses.items()
        ],
    }
    d = ctx.artifact_root / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "acceptance-map.json").write_text(json.dumps(mapping))


def _gate_step(**kw):
    return Step.model_validate({"id": "acceptance-gate", "type": "acceptance_gate",
                                "collector": "pytest", **kw})


# Enumeration runs as an engine subprocess in a disposable copy (PR #59 F3/F7).
# These helpers let the gate's enumeration path be exercised without a real git
# worktree copy or a real pytest run: the copy machinery is stubbed and
# `Collector.enumerate` returns the scripted id set (its own subprocess behavior
# has dedicated tests below and in test_deferrals).
def _stub_backend(monkeypatch):
    copy = verify.DisposableCopy(path=Path("/nonexistent-copy"),
                                 root=Path("/nonexistent-copy"))
    monkeypatch.setattr(verify, "make_disposable_copy", lambda repo, **k: copy)
    monkeypatch.setattr(verify, "discard_disposable_copy", lambda repo, c: None)


def _stub_enumeration(monkeypatch, ids):
    _stub_backend(monkeypatch)
    from gauntlet.engine.collectors import Collector

    monkeypatch.setattr(Collector, "enumerate",
                        lambda self, **k: set(ids), raising=True)


# --- FR-3.1: phase_lint requires a well-formed acceptance list (P2-A1) --------
_PLAN_NO_ACCEPTANCE = (
    "# Plan\n\n## P1 — Build it\nDo it.\n\n"
    "```gauntlet-phases\n- id: P1\n  title: Build it\n  goal: Do it.\n```\n"
)
_PLAN_WITH_ACCEPTANCE = (
    "# Plan\n\n## P1 — Build it\nDo it.\n\n"
    "```gauntlet-phases\n- id: P1\n  title: Build it\n  goal: Do it.\n"
    "  acceptance:\n    - id: P1-A1\n      clause: It is built.\n```\n"
)


def test_phase_lint_parks_on_clause_less_phase(fixture_repo):
    """P2-A1: phase_lint fails closed on a plan phase carrying no acceptance list."""
    ctx = _ctx(fixture_repo)
    (ctx.artifact_root / "plan.md").write_text(_PLAN_NO_ACCEPTANCE)
    step = Step.model_validate({"id": "plan-lint", "type": "phase_lint",
                                "artifact": "plan.md"})
    result = handle_phase_lint(step, ctx)
    # P5 (plan §5.1, issue #64): an artifact defect parks artifact_invalid
    # (validator + diagnostic + content fingerprint) instead of a bare HALTED.
    assert result.status == PARKED
    assert result.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID
    assert result.revalidation is not None
    assert result.revalidation.validator == "phase_lint"
    assert "acceptance" in result.notes and "P1" in result.notes


def test_phase_lint_passes_when_acceptance_present(fixture_repo):
    """A well-formed acceptance list clears the lint (the complement of P2-A1)."""
    ctx = _ctx(fixture_repo)
    (ctx.artifact_root / "plan.md").write_text(_PLAN_WITH_ACCEPTANCE)
    step = Step.model_validate({"id": "plan-lint", "type": "phase_lint",
                                "artifact": "plan.md"})
    assert handle_phase_lint(step, ctx).status == DONE


def test_acceptance_clause_errors_flags_missing_and_malformed():
    """The shared FR-3.1 check reports both missing and malformed acceptance lists."""
    errs = acceptance_clause_errors([
        {"id": "P1"},                                   # missing
        {"id": "P2", "acceptance": []},                 # empty
        {"id": "P3", "acceptance": [{"id": "x"}]},      # clause without text
        {"id": "P4", "acceptance": [{"id": "P4-A1", "clause": "ok"}]},  # good
    ])
    joined = " | ".join(errs)
    assert "P1" in joined and "P2" in joined and "P3" in joined
    assert "P4" not in joined  # the well-formed phase is not flagged


def test_extract_phases_stays_lenient_when_acceptance_absent():
    """extract_phases does NOT require acceptance — a pre-FR-3 approved plan still
    loads for foreach/rollback; presence is required only at the gate (FR-3.1)."""
    phases = extract_phases(_PLAN_NO_ACCEPTANCE)
    assert [p["id"] for p in phases] == ["P1"]


def test_extract_phases_rejects_malformed_acceptance_when_present():
    """When present, a malformed acceptance list fails closed at extraction."""
    bad = (
        "```gauntlet-phases\n- id: P1\n  title: t\n  goal: g\n"
        "  acceptance:\n    - id: P1-A1\n```\n"  # clause missing 'clause' text
    )
    with pytest.raises(PlanPhasesError, match="P1-A1"):
        extract_phases(bad)


# --- FR-3.2: the acceptance_gate (P2-A2, P2-A3, P2-A4) ------------------------
def test_gate_parks_on_unmapped_clause(fixture_repo, monkeypatch):
    """P2-A2: acceptance_gate parks when a clause is unmapped, naming it in notes."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1", "P2-A2"]))
    # only P2-A1 mapped; P2-A2 has no evidence entry in the map
    _write_map(ctx, {"P2-A1": [{"kind": "pytest", "id": "tests/unit/x.py::t"}]})
    # enumeration is never reached (mapping check fails first); guard anyway
    monkeypatch.setattr(collectors, "run_bounded_enumeration",
                        lambda *a, **k: "tests/unit/x.py::t\n")
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert result.halt_reason == HALT_REASON_PRECONDITION
    assert "P2-A2" in result.notes and "unmapped" in result.notes
    assert "P2-A1" not in result.notes  # only the offending clause is named


def test_gate_parks_on_nonexistent_node_id(fixture_repo, monkeypatch):
    """P2-A3: acceptance_gate parks when a cited pytest node id is absent from the
    side-effect-free collector enumeration."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    _write_map(ctx, {"P2-A1": [{"kind": "pytest", "id": "tests/unit/x.py::missing"}]})
    _stub_enumeration(monkeypatch, {"tests/unit/x.py::present"})
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert "tests/unit/x.py::missing" in result.notes
    assert "absent" in result.notes


def test_gate_passes_on_complete_mapping(fixture_repo, monkeypatch):
    """P2-A4: a complete pytest mapping clears the gate (citation + existence
    proven; sufficiency not asserted)."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1", "P2-A2"]))
    _write_map(ctx, {
        "P2-A1": [{"kind": "pytest", "id": "tests/unit/x.py::a"}],
        "P2-A2": [{"kind": "pytest", "id": "tests/unit/x.py::b"}],
    })
    _stub_enumeration(monkeypatch, {"tests/unit/x.py::a", "tests/unit/x.py::b"})
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == DONE
    assert "citation + existence" in result.notes


def test_gate_parks_on_wrong_phase_map(fixture_repo, monkeypatch):
    """Review F-001: a map declaring a different phase (a stale/wrong-phase
    acceptance-map.json) is rejected even when it reuses this phase's clause ids —
    the map must declare it covers THIS phase (fail closed)."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    # a P1 map that happens to reuse the clause id P2-A1
    d = ctx.artifact_root / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "acceptance-map.json").write_text(json.dumps({
        "phase": "P1",
        "clauses": [{"id": "P2-A1", "text": "c",
                     "evidence": [{"kind": "pytest", "id": "tests/unit/x.py::t"}]}],
    }))
    # enumeration must never run — the phase-scope check fails first
    monkeypatch.setattr(collectors, "run_bounded_enumeration",
                        lambda *a, **k: pytest.fail("enumeration must not run"))
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert result.halt_reason == HALT_REASON_PRECONDITION
    assert "'P1'" in result.notes and "P2" in result.notes


def test_gate_parks_on_extra_unplanned_clause(fixture_repo, monkeypatch):
    """Review F-002: a map carrying a clause id that is not in the current plan
    phase is rejected — the artifact must be an exact map of the phase's
    acceptance list, not a superset carrying stale evidence."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    # every plan clause is mapped, but the map also carries an unplanned P2-A9
    _write_map(ctx, {
        "P2-A1": [{"kind": "pytest", "id": "tests/unit/x.py::a"}],
        "P2-A9": [{"kind": "pytest", "id": "tests/unit/x.py::stale"}],
    })
    monkeypatch.setattr(collectors, "run_bounded_enumeration",
                        lambda *a, **k: pytest.fail("enumeration must not run"))
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert result.halt_reason == HALT_REASON_PRECONDITION
    assert "P2-A9" in result.notes and "not in the plan phase" in result.notes
    assert "P2-A1" not in result.notes  # only the unplanned id is named


def test_gate_parks_on_missing_map(fixture_repo):
    """Fail closed: an absent acceptance map is never 'all clauses mapped'."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert "no acceptance map" in result.notes


def test_gate_parks_on_schema_invalid_kind(fixture_repo, monkeypatch):
    """A map whose evidence declares an unregistered collector kind is rejected at
    map load (schema-invalid, closed enum) — never run through enumeration then
    parked (FR-3.2 / P2-A5, the artifact-evidence facet)."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    # write the map by hand with kind: shell (schema enum is pytest-only)
    d = ctx.artifact_root / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    (d / "acceptance-map.json").write_text(json.dumps({
        "phase": "P2",
        "clauses": [{"id": "P2-A1", "text": "c",
                     "evidence": [{"kind": "shell", "id": "make check"}]}],
    }))
    # if the gate ever reached enumeration this would blow up loudly
    monkeypatch.setattr(collectors, "run_bounded_enumeration",
                        lambda *a, **k: pytest.fail("enumeration must not run"))
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert "schema-invalid" in result.notes and "rejected at load" in result.notes


# --- FR-3.2 collector-execution safety, interim posture (P2-A6) ---------------
def test_enumeration_failure_parks_closed(fixture_repo, monkeypatch):
    """P2-A6: a collector enumeration that exits non-zero parks the gate closed —
    an absent/failed enumeration is never treated as 'all mapped'."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    _write_map(ctx, {"P2-A1": [{"kind": "pytest", "id": "tests/unit/x.py::t"}]})
    _stub_backend(monkeypatch)

    def _boom(self, **k):
        raise CollectorEnumerationError("collector enumeration exited 2 (fail closed)")

    monkeypatch.setattr(collectors.Collector, "enumerate", _boom)
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert "enumeration failed" in result.notes and "fail closed" in result.notes


def test_run_bounded_enumeration_nonzero_exit_fails_closed(monkeypatch, tmp_path):
    """A non-zero collector exit raises CollectorEnumerationError (fail closed)."""
    class _Proc:
        returncode = 5
        stdout = ""
        stderr = "no tests ran"

    monkeypatch.setattr(collectors.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(CollectorEnumerationError, match="exited 5"):
        run_bounded_enumeration([sys.executable, "-c", "pass"], worktree=tmp_path,
                                judge_env={})


def test_run_bounded_enumeration_timeout_fails_closed(monkeypatch, tmp_path):
    """A timed-out enumeration raises CollectorEnumerationError (fail closed)."""
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

    monkeypatch.setattr(collectors.subprocess, "run", _timeout)
    with pytest.raises(CollectorEnumerationError, match="timed out"):
        run_bounded_enumeration([sys.executable, "-c", "pass"], worktree=tmp_path,
                                judge_env={}, timeout_s=1)


def test_enumeration_runs_as_bounded_stripped_subprocess(monkeypatch, tmp_path):
    """PR #59 F3: enumeration is spawned as a bounded engine subprocess with the
    verifier's STRIPPED env (no secrets, no judge vars — branch conftest code
    executes at import time), cwd scoped to the caller-provided (disposable-copy)
    worktree, with a resource limit applied."""
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "tests/unit/x.py::t\n"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(collectors.subprocess, "run", _fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-conftest")
    judge = {"GAUNTLET_JUDGE_TOKEN": "tok", "GAUNTLET_JUDGE_MODE": "unattended"}
    out = run_bounded_enumeration([sys.executable, "-m", "pytest"], worktree=tmp_path,
                                  judge_env=judge, timeout_s=90.0)
    assert out == "tests/unit/x.py::t\n"
    # working directory scoped to the (disposable-copy) worktree
    assert captured["cwd"] == str(tmp_path)
    # a wall-clock bound is armed
    assert captured["timeout"] == 90.0
    # STRIPPED env: no secrets, and no judge vars either — the child is an
    # engine subprocess, not an agent; there is no hook to authenticate
    # (previously the whole engine os.environ + judge env was forwarded,
    # handing credential-shaped vars to branch import-time code)
    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert "GAUNTLET_JUDGE_TOKEN" not in captured["env"]
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    # a process resource limit is applied on POSIX (preexec_fn set)
    if collectors._POSIX:
        assert captured["preexec_fn"] is not None


def test_pytest_collector_parses_real_node_ids(tmp_path):
    """The pytest collector's real command + parse enumerate genuine node ids from
    a side-effect-free `pytest --collect-only` (end-to-end, no monkeypatch)."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    (tmp_path / "test_sample.py").write_text(
        "def test_one():\n    assert True\n\n"
        "def test_two():\n    assert True\n"
    )
    ids = collectors.get_collector("pytest").enumerate(worktree=tmp_path, judge_env={})
    assert "test_sample.py::test_one" in ids
    assert "test_sample.py::test_two" in ids


def test_parse_pytest_ignores_summary_and_blanks():
    """The node-id parse keeps node ids and drops the summary/blank/warning lines."""
    out = (
        "tests/unit/test_a.py::test_x\n"
        "tests/unit/test_a.py::TestC::test_y\n"
        "\n"
        "3 tests collected in 0.02s\n"
    )
    assert _parse_pytest(out) == {
        "tests/unit/test_a.py::test_x",
        "tests/unit/test_a.py::TestC::test_y",
    }


def test_empty_enumeration_fails_closed(monkeypatch, tmp_path):
    """A clean exit that yields no parseable ids fails closed — 'unparseable' is
    never read as 'no ids to check' (P2-A6)."""
    class _Proc:
        returncode = 0
        stdout = "no node ids here\n"  # nothing matches the node-id shape
        stderr = ""

    monkeypatch.setattr(collectors.subprocess, "run", lambda *a, **k: _Proc())
    with pytest.raises(CollectorEnumerationError, match="no parseable ids"):
        collectors.get_collector("pytest").enumerate(worktree=tmp_path, judge_env={})


# --- PR #59 F3/F4/F7: deterministic, project-resolved enumeration in a copy ---
def test_gate_enumerates_in_a_disposable_copy(fixture_repo, monkeypatch):
    """PR #59 F3: the gate creates a disposable copy, enumerates THERE (branch
    conftest import-time effects land in a discarded tree), and always discards
    the copy — no agent, no judge, fully deterministic."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    _write_map(ctx, {"P2-A1": [{"kind": "pytest", "id": "tests/unit/x.py::t"}]})
    copy = verify.DisposableCopy(path=Path("/copy-path"), root=Path("/copy-path"))
    events = []
    monkeypatch.setattr(verify, "make_disposable_copy",
                        lambda repo, **k: events.append("made") or copy)
    monkeypatch.setattr(verify, "discard_disposable_copy",
                        lambda repo, c: events.append("discarded"))
    seen = {}

    def _enum(self, *, worktree, judge_env, command=None, **k):
        seen.update(worktree=worktree, judge_env=judge_env, command=command)
        return {"tests/unit/x.py::t"}

    monkeypatch.setattr(collectors.Collector, "enumerate", _enum)
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == DONE
    assert seen["worktree"] == copy.path          # enumerated IN the copy
    assert seen["judge_env"] == {}                # engine subprocess: no judge env
    assert seen["command"]                        # project-resolved command passed
    assert events == ["made", "discarded"]        # copy always torn down


def test_gate_parks_when_copy_cannot_be_created(fixture_repo, monkeypatch):
    """Fail closed: an absent disposable copy parks the gate — enumeration never
    runs against the real worktree as a fallback."""
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    _write_map(ctx, {"P2-A1": [{"kind": "pytest", "id": "tests/unit/x.py::t"}]})

    def _no_copy(repo, **k):
        raise verify.CopyCreationError("mkdtemp failed")

    monkeypatch.setattr(verify, "make_disposable_copy", _no_copy)
    monkeypatch.setattr(
        collectors.Collector, "enumerate",
        lambda self, **k: pytest.fail("enumeration must not run without a copy"),
    )
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert result.halt_reason == HALT_REASON_PRECONDITION
    assert "disposable" in result.notes and "fail closed" in result.notes


def test_gate_discards_copy_even_when_enumeration_fails(fixture_repo, monkeypatch):
    ctx = _ctx(fixture_repo, iteration_item=_phase(["P2-A1"]))
    _write_map(ctx, {"P2-A1": [{"kind": "pytest", "id": "tests/unit/x.py::t"}]})
    copy = verify.DisposableCopy(path=Path("/copy-path"), root=Path("/copy-path"))
    discarded = {}
    monkeypatch.setattr(verify, "make_disposable_copy", lambda repo, **k: copy)
    monkeypatch.setattr(verify, "discard_disposable_copy",
                        lambda repo, c: discarded.setdefault("done", True))

    def _boom(self, **k):
        raise CollectorEnumerationError("collection blew up (fail closed)")

    monkeypatch.setattr(collectors.Collector, "enumerate", _boom)
    result = handle_acceptance_gate(_gate_step(), ctx)
    assert result.status == HALTED
    assert discarded.get("done") is True


# --- PR #59 F4: the enumeration command is resolved per project ---------------
def _cfg(**kw):
    from gauntlet.engine.config import RunConfig

    return RunConfig.model_validate(kw)


def test_resolve_command_prefers_config_override():
    collector = collectors.get_collector("pytest")
    cfg = _cfg(collectors={"pytest": {"command": "hatch run pytest --collect-only -q"}})
    assert collectors.resolve_command(collector, cfg) == (
        "hatch", "run", "pytest", "--collect-only", "-q")
    cfg_list = _cfg(collectors={"pytest": {"command": ["tox", "-e", "collect"]}})
    assert collectors.resolve_command(collector, cfg_list) == ("tox", "-e", "collect")


def test_resolve_command_derives_from_pytest_shaped_test_command():
    """PR #59 F4: gauntlet's runtime deps do not include pytest, so the previous
    hard-coded `sys.executable -m pytest` parked every pipx/uv-tool install and
    every adopter repo. A pytest-shaped test_command supplies the project's OWN
    test environment."""
    collector = collectors.get_collector("pytest")
    cfg = _cfg(test_command="uv run pytest")
    assert collectors.resolve_command(collector, cfg) == (
        "uv", "run", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider")


@pytest.mark.parametrize(
    "test_command",
    [
        "uv run pytest -q",
        "uv run pytest --quiet --collect-only",
        "uv run pytest -p no:cacheprovider -q",
    ],
)
def test_resolve_command_normalizes_collector_owned_pytest_flags(test_command):
    """Collection owns output-shaping flags and emits each exactly once.

    Pytest interprets two quiet flags as ``-qq`` and suppresses node ids, so
    blindly appending ``-q`` to a project command can make a valid suite fail
    closed as unparseable.
    """
    collector = collectors.get_collector("pytest")
    command = collectors.resolve_command(collector, _cfg(test_command=test_command))
    assert command == (
        "uv", "run", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider")


def test_resolve_command_preserves_project_environment_arguments():
    collector = collectors.get_collector("pytest")
    command = collectors.resolve_command(
        collector,
        _cfg(test_command="uv run --with pytest pytest -q"),
    )
    assert command == (
        "uv", "run", "--with", "pytest", "pytest",
        "--collect-only", "-q", "-p", "no:cacheprovider",
    )


def test_resolve_command_falls_back_when_test_command_is_not_pytest():
    collector = collectors.get_collector("pytest")
    cfg = _cfg(test_command="make test")
    assert collectors.resolve_command(collector, cfg) == collector.command
    assert collectors.resolve_command(collector, _cfg()) != collector.command  # default derives


# --- FR-3.2: unregistered collector rejected at pipeline LOAD (P2-A5) ---------
def _pipeline_with_gate(collector: str) -> Pipeline:
    text = f"""
name: demo
version: 1
stages:
  - id: phases
    foreach: plan.phases
    steps:
      - {{id: acceptance-gate, type: acceptance_gate, collector: {collector},
         map: artifacts/acceptance-map.json}}
"""
    return Pipeline.model_validate(yaml.safe_load(text))


_CFG = RunConfig.model_validate({"agents": {"builder": {"adapter": "claude-code"}}})


def test_pipeline_load_rejects_unregistered_collector():
    """P2-A5: a phase whose acceptance evidence declares a collector kind other
    than the v1 pytest collector is rejected at pipeline load, not at runtime — the
    acceptance_gate names its collector, and an unregistered kind aborts the load."""
    with pytest.raises(PipelineValidationError) as exc:
        validate_pipeline(_pipeline_with_gate("shell"), _CFG)
    assert "shell" in str(exc.value)
    assert "no registered collector" in str(exc.value)


def test_pipeline_load_accepts_pytest_collector():
    """The v1 pytest collector passes pipeline load (complement of P2-A5)."""
    report = validate_pipeline(_pipeline_with_gate("pytest"), _CFG)
    assert report.ok()


def test_pipeline_load_rejects_gate_without_collector():
    """An acceptance_gate with no collector is a load-time misconfiguration."""
    pipeline = Pipeline.model_validate(yaml.safe_load(
        "name: d\nversion: 1\nstages:\n  - id: s\n    steps:\n"
        "      - {id: g, type: acceptance_gate}\n"))
    with pytest.raises(PipelineValidationError, match="no `collector:`"):
        validate_pipeline(pipeline, _CFG)


# --- schema: closed kind enum -------------------------------------------------
def test_acceptance_map_schema_closed_kind_enum():
    """The shipped acceptance-map schema's `kind` is a closed enum (pytest only)."""
    schema = json.loads((REPO / "schemas" / "acceptance-map.json").read_text())
    kind = schema["properties"]["clauses"]["items"]["properties"]["evidence"][
        "items"]["properties"]["kind"]
    assert kind["enum"] == ["pytest"]

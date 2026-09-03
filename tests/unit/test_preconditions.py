"""Plan-declared environmental preconditions (#134, rec. 7).

The parser fails closed on a malformed item, the resolver is pure over an
injected environment and command runner, the plan gate refuses approval while
any item is unmet (`--skip-preflight` audits instead), and a step declaring
`preconditions_from: plan` fails as a re-runnable precondition before its
handler runs — nothing invoked — until the operator satisfies the item.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import run_work_tree

from gauntlet.engine import manifest as M
from gauntlet.engine import preconditions as PC
from gauntlet.engine.planphases import PlanPhasesError, extract_plan, extract_phases

from test_run_lifecycle import _author_prd, _prepare, _write_pipeline


# --- parsing -------------------------------------------------------------------
def test_parse_each_kind_and_labels():
    items = PC.parse_preconditions([
        {"path": "data/bundle.parquet", "description": "staged by ops"},
        {"env": "OPENAI_API_KEY"},
        {"command": "true", "timeout_s": 5},
    ], scope="P2")
    assert [i.kind for i in items] == ["path", "env", "command"]
    assert items[0].label() == "path data/bundle.parquet [P2] — staged by ops"
    assert items[2].effective_timeout_s == 5
    assert items[1].effective_timeout_s == PC.DEFAULT_COMMAND_TIMEOUT_S


@pytest.mark.parametrize("raw, needle", [
    ("nope", "must be a list"),
    (["x"], "not a mapping"),
    ([{"description": "d"}], "exactly one of"),
    ([{"path": "a", "env": "B"}], "exactly one of"),
    ([{"path": "a", "bogus": 1}], "unknown key"),
    ([{"env": "not a name"}], "environment variable NAME"),
    ([{"path": "a", "timeout_s": 3}], "unknown key"),
    ([{"command": "true", "timeout_s": 0}], "positive integer"),
    ([{"path": ""}], "non-empty string"),
])
def test_parse_fails_closed(raw, needle):
    with pytest.raises(PC.PreconditionSpecError, match=needle):
        PC.parse_preconditions(raw, scope=PC.PLAN_SCOPE)


# --- resolution ----------------------------------------------------------------------
def test_resolve_path_env_and_injected_command(tmp_path):
    (tmp_path / "present.txt").write_text("x")
    items = PC.parse_preconditions([
        {"path": "present.txt"}, {"path": "missing.txt"},
        {"env": "PC_SET"}, {"env": "PC_EMPTY"}, {"env": "PC_UNSET"},
        {"command": "ok"}, {"command": "bad"}, {"command": "slow", "timeout_s": 1},
    ], scope="P1")
    env = {"PC_SET": "s3cr3t-value", "PC_EMPTY": "  "}
    ran = []

    def runner(item, cwd, env_):
        ran.append(item.target)
        if item.target == "ok":
            return PC.CommandOutcome(0)
        if item.target == "bad":
            return PC.CommandOutcome(2, "boom | line two", evidence="/log/cmd-2.txt")
        return PC.CommandOutcome(None, "", timed_out=True)

    unmet = PC.resolve_preconditions(items, cwd=tmp_path, env=env, run_command=runner)
    rendered = [u.render() for u in unmet]
    assert len(unmet) == 5 and ran == ["ok", "bad", "slow"]  # every item checked
    assert any("missing.txt" in r and "missing:" in r for r in rendered)
    assert any("PC_EMPTY is set but empty" in r for r in rendered)
    assert any("PC_UNSET is not set" in r for r in rendered)
    assert any("exited 2; output tail: boom | line two (full output: /log/cmd-2.txt)" in r
               for r in rendered)
    assert any("timed out after 1s" in r for r in rendered)
    assert not any("s3cr3t-value" in r for r in rendered)  # values never leak
    # read-only mode: commands are neither run nor reported
    ran.clear()
    ro = PC.resolve_preconditions(items, cwd=tmp_path, env=env, run_command=None)
    assert ran == [] and len(ro) == 3
    assert [i.target for i in PC.command_items(items)] == ["ok", "bad", "slow"]


def test_real_command_runner_persists_output(tmp_path):
    class W:
        def write_text(self, path, text):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(text)

    runner = PC.command_runner(tmp_path / "pre", W())
    item = PC.parse_preconditions([{"command": "echo hello; echo oops >&2; exit 3"}], scope="P1")[0]
    out = runner(item, tmp_path, dict(os.environ))
    assert out.returncode == 3 and "hello" in out.output_tail and "oops" in out.output_tail
    assert out.evidence and Path(out.evidence).read_text().startswith("$ echo hello")
    checklist = PC.render_checklist([item], [PC.Unmet(item, "exited 3")])
    assert checklist.startswith("[UNMET] command")


# --- the plan block ----------------------------------------------------------------------
PLAN_MAPPING = """# Plan

```gauntlet-phases
preconditions:
  - {env: PLAN_TOKEN, description: whole-plan}
phases:
  - id: P1
    title: One
    goal: g
    preconditions:
      - {path: data/one.parquet}
  - id: P2
    title: Two
    goal: g
```
"""


def test_extract_plan_mapping_form_and_per_phase_items():
    spec = extract_plan(PLAN_MAPPING)
    assert [p["id"] for p in spec.phases] == ["P1", "P2"]
    assert [i.target for i in spec.preconditions] == ["PLAN_TOKEN"]
    assert [i.target for i in spec.preconditions_for("P1")] == ["PLAN_TOKEN", "data/one.parquet"]
    assert [i.target for i in spec.preconditions_for("P2")] == ["PLAN_TOKEN"]
    assert [i.scope for i in spec.all_preconditions()] == ["plan", "P1"]
    assert extract_phases(PLAN_MAPPING) == spec.phases  # classic API unchanged
    with pytest.raises(PlanPhasesError, match="not declared"):
        spec.preconditions_for("P9")


@pytest.mark.parametrize("block, needle", [
    ("{preconditions: [], bogus: 1, phases: [{id: P1, title: t, goal: g}]}", "unknown top-level key"),
    ("{preconditions: []}", "missing 'phases'"),
    ("{preconditions: [{path: a, x: 1}], phases: [{id: P1, title: t, goal: g}]}", "unknown key"),
    ("- {id: P1, title: t, goal: g, preconditions: [{env: 'bad name'}]}", "environment variable NAME"),
])
def test_extract_plan_fails_closed_on_malformed_preconditions(block, needle):
    text = f"# Plan\n\n```gauntlet-phases\n{block}\n```\n"
    with pytest.raises(PlanPhasesError, match=needle):
        extract_plan(text)


# --- pipeline validation ---------------------------------------------------------------
def test_pipeline_options_validate_at_run_start(fixture_repo):
    from gauntlet.engine.config import RunConfig
    from gauntlet.engine.pipeline import load_pipeline
    from gauntlet.engine.validate import PipelineValidationError, validate_pipeline

    from test_pipeline_loader import CONFIG

    def _validate(text):
        path = fixture_repo / "p.yaml"
        path.write_text(text)
        pipeline, _ = load_pipeline(path)
        return validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))

    head = "name: p\nversion: 1\nstages:\n  - id: s\n    steps:\n"
    assert _validate(
        head + "      - {id: g, type: human_gate, preflight: plan_preconditions}\n"
        "      - {id: t, type: shell, run: 'true', preconditions_from: plan}\n"
    ).ok()
    for bad, needle in [
        ("      - {id: t, type: shell, run: 'true', preflight: plan_preconditions}\n", "human_gate steps only"),
        ("      - {id: g, type: human_gate, preflight: nope}\n", "unknown preflight"),
        ("      - {id: t, type: shell, run: 'true', preconditions_from: elsewhere}\n", "unknown preconditions_from"),
    ]:
        with pytest.raises(PipelineValidationError, match=needle):
            _validate(head + bad)


# --- the plan gate: approve refuses, --skip-preflight audits --------------------------------
GATED = """
name: p
version: 1
stages:
  - id: phase
    steps:
      - {id: gate, type: human_gate, show: [plan.md], preflight: plan_preconditions}
      - {id: after, type: shell, run: "true"}
"""


def _plan_with(items: str) -> str:
    return (
        "# Plan\n\n```gauntlet-phases\n"
        "- id: P1\n  title: One\n  goal: g\n"
        f"  preconditions:\n{items}"
        "```\n"
    )


def test_approve_refuses_until_preconditions_are_met(fixture_repo, tmp_path, monkeypatch):
    bundle = tmp_path / "bundle.parquet"
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    (mgr.layout("demo").slug_dir / "plan.md").write_text(_plan_with(
        f"    - {{path: {bundle}, description: staged by ops}}\n"
        "    - {env: DEMO_PRE_TOKEN}\n"
        "    - {command: 'test -f " + str(bundle) + "'}\n"
    ))
    path = _write_pipeline(fixture_repo, GATED)
    monkeypatch.delenv("DEMO_PRE_TOKEN", raising=False)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    with pytest.raises(ValueError, match="3 unmet precondition") as ei:
        mgr.approve("demo", use_judge=False)
    msg = str(ei.value)
    assert "staged by ops" in msg and "DEMO_PRE_TOKEN is not set" in msg and "exited 1" in msg
    assert "--skip-preflight" in msg
    assert mgr.status("demo").record("gate").status == M.PARKED  # nothing stamped
    run_dir = mgr.layout("demo").active_run_dir()
    assert (run_dir / "preflight" / "preflight.txt").read_text().count("[UNMET]") == 3
    # Satisfy everything → approval lands and the run completes.
    bundle.write_text("data")
    monkeypatch.setenv("DEMO_PRE_TOKEN", "t")
    assert mgr.approve("demo", use_judge=False) == M.RUN_DONE
    assert not any("skip-preflight" in w for w in mgr.status("demo").warnings)


def test_skip_preflight_approves_with_audit_warning(fixture_repo, monkeypatch):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    (mgr.layout("demo").slug_dir / "plan.md").write_text(_plan_with("    - {env: DEMO_PRE_TOKEN}\n"))
    path = _write_pipeline(fixture_repo, GATED)
    monkeypatch.delenv("DEMO_PRE_TOKEN", raising=False)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    assert mgr.approve("demo", use_judge=False, skip_preflight=True) == M.RUN_DONE
    warnings = [w for w in mgr.status("demo").warnings if "skip-preflight" in w]
    assert len(warnings) == 1 and "DEMO_PRE_TOKEN is not set" in warnings[0]


def test_gate_without_preflight_or_plan_items_is_unchanged(fixture_repo):
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    (mgr.layout("demo").slug_dir / "plan.md").write_text(
        "# Plan\n\n```gauntlet-phases\n- id: P1\n  title: One\n  goal: g\n```\n"
    )
    path = _write_pipeline(fixture_repo, GATED)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    assert mgr.approve("demo", use_judge=False) == M.RUN_DONE


# --- before a phase launches: FAILED precondition, nothing invoked, re-runnable ------------
PHASED = """
name: p
version: 1
stages:
  - id: phases
    foreach: plan.phases
    steps:
      - {id: work, type: shell, run: "echo ran >> $PRE_LOG", preconditions_from: plan}
"""


def test_phase_precondition_blocks_before_the_handler_then_resumes(
    fixture_repo, tmp_path, monkeypatch
):
    log = tmp_path / "work.log"
    monkeypatch.setenv("PRE_LOG", str(log))
    monkeypatch.delenv("PHASE_TOKEN", raising=False)
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    (mgr.layout("demo").slug_dir / "plan.md").write_text(
        "# Plan\n\n```gauntlet-phases\n"
        "preconditions:\n  - {env: PLAN_WIDE}\n"
        "phases:\n"
        "  - id: P1\n    title: One\n    goal: g\n"
        "  - id: P2\n    title: Two\n    goal: g\n    preconditions: [{env: PHASE_TOKEN}]\n"
        "```\n"
    )
    monkeypatch.setenv("PLAN_WIDE", "1")
    path = _write_pipeline(fixture_repo, PHASED)
    # P1 has only the plan-wide item (met) → runs; P2 needs PHASE_TOKEN → blocked.
    assert mgr.start("demo", path, use_judge=False) == M.RUN_FAILED
    assert log.read_text().count("ran") == 1
    man = mgr.status("demo")
    rec = next(r for r in man.steps if r.id == "work" and r.status == M.FAILED)
    assert rec.halt_reason == M.HALT_REASON_PRECONDITION
    assert rec.failure_kind == M.FAILURE_KIND_CLEAN_HANDOFF
    assert "PHASE_TOKEN is not set" in (rec.notes or "") and "nothing invoked" in rec.notes
    work = run_work_tree(fixture_repo)
    steps_dir = mgr.layout("demo").active_run_dir() / "steps"
    assert any(p.name == "preflight.txt" for p in steps_dir.rglob("preflight.txt"))
    # Satisfy it → a plain resume re-checks and the phase runs.
    monkeypatch.setenv("PHASE_TOKEN", "x")
    assert mgr.resume("demo", use_judge=False) == M.RUN_DONE
    assert log.read_text().count("ran") == 2


# --- status advisory on a parked preflight gate (read-only) --------------------------
def test_status_lists_unmet_preconditions_read_only(fixture_repo, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import gauntlet.cli as cli

    bundle = tmp_path / "bundle.parquet"
    mgr = _prepare(fixture_repo)
    _author_prd(mgr, "demo")
    (mgr.layout("demo").slug_dir / "plan.md").write_text(_plan_with(
        f"    - {{path: {bundle}}}\n"
        "    - {env: DEMO_PRE_TOKEN}\n"
        "    - {command: 'exit 1'}\n"
    ))
    path = _write_pipeline(fixture_repo, GATED)
    monkeypatch.delenv("DEMO_PRE_TOKEN", raising=False)
    assert mgr.start("demo", path, use_judge=False) == M.RUN_PARKED
    monkeypatch.chdir(fixture_repo)
    r = CliRunner().invoke(cli.app, ["status", "demo"])
    assert r.exit_code == 0, r.output
    assert "plan preflight: 2 unmet precondition(s)" in r.output
    assert "DEMO_PRE_TOKEN is not set" in r.output and str(bundle) in r.output
    assert "1 command precondition(s) are run at approve time" in r.output
    # read-only: the command was NOT executed by status (no preflight dir yet)
    assert not (mgr.layout("demo").active_run_dir() / "preflight").exists()

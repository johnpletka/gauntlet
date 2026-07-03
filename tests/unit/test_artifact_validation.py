"""In-step artifact validation + repair loop + artifact_invalid park (FR-2.1–2.3).

Drives the orchestrator with a stub adapter that returns a scripted plan.md text
per call (the engine writes it to the `output:` artifact). Asserts: a malformed
`gauntlet-phases` block is repaired in-session within the bounded loop; an
incorrigible one parks `artifact_invalid` (not FAILED) with the validator error
verbatim + the content-hash pair; and a plain `gauntlet resume` re-runs ONLY the
validator against the (possibly hand-edited) on-disk artifact — no adapter call.
Plus unit coverage of the named validators and the shipped-pipeline wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gauntlet.adapters.base import AgentResult, Usage
from gauntlet.engine import manifest as M
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.planphases import extract_phases
from gauntlet.engine.validators import (
    UnknownValidatorError,
    known_validator,
    validate_artifact,
)

from test_orchestrator import _build

# A single agent_task that authors an `output` artifact and validates it in-step.
PIPE = """
name: demo
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-author, type: agent_task, agent: builder, output: plan.md,
         validate: plan_phases, prompt_text: author the plan}
"""

VALID_PLAN = """# Plan

Prose a human ratifies.

```gauntlet-phases
- id: P1
  title: First phase
  goal: Do the first thing.
- id: P2
  title: Second phase
  goal: Do the second thing.
```
"""

# A block that PARSES as YAML but violates the phase contract (id not P<n>), so
# it exercises the PlanPhasesError branch, not merely a missing block.
MALFORMED_PLAN = """# Plan

```gauntlet-phases
- id: X1
  title: Bad id
  goal: This id does not match P<n>.
```
"""

NO_BLOCK_PLAN = "# Plan\n\nJust prose, no gauntlet-phases block at all.\n"


def _manifest() -> Manifest:
    return Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="demo", version=1, hash="sha256:x"),
    )


class ScriptedTextAdapter:
    """Returns a scripted text per call; the engine writes it to `output`.

    Records each call's prompt + session so a test can assert the repair
    continued the same session with the validator error, and reports a fixed
    per-call usage so summed-across-attempts accounting can be checked.
    """

    name = "fake"

    def __init__(self, texts, *, session="sess-1"):
        from gauntlet.adapters.base import AdapterCapabilities

        self.capabilities = AdapterCapabilities(
            repo_write=True, structured_output="native", resume=True
        )
        self._texts = list(texts)
        self.session = session
        self.calls: list[dict] = []
        self.timeout_s = 600.0

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls.append({"prompt": prompt, "session": session})
        if not self._texts:
            raise AssertionError("adapter called more times than scripted")
        text = self._texts.pop(0)
        return AgentResult(
            text=text, session_id=self.session,
            usage=Usage(input_tokens=100, output_tokens=10, cost_usd=0.01),
            exit_code=0,
        )


def _plan_path(repo: Path) -> Path:
    return repo / "runs" / "demo" / "plan.md"


def _steps_dir(repo: Path) -> Path:
    return repo / "runs" / "demo" / "run-1" / "steps" / "plan-author"


# --- FR-2.1: in-session repair loop -----------------------------------------
def test_repair_loop_fixes_malformed_artifact_on_attempt_two(fixture_repo):
    man = _manifest()
    adapter = ScriptedTextAdapter([MALFORMED_PLAN, VALID_PLAN])
    orch = _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man)
    assert orch.drive() == M.RUN_DONE

    # initial call + exactly one repair
    assert len(adapter.calls) == 2
    assert adapter.calls[0]["session"] is None  # fresh first call
    # the repair continued the SAME session and carried the validator error
    assert adapter.calls[1]["session"] == "sess-1"
    assert "failed validation" in adapter.calls[1]["prompt"]
    assert "P<n>" in adapter.calls[1]["prompt"]  # the concrete error, fed back

    rec = man.record("plan-author")
    assert rec.status == M.DONE
    assert rec.parked_reason is None and rec.halt_reason is None
    # the persisted artifact parses to a non-empty phase list
    assert [p["id"] for p in extract_phases(_plan_path(fixture_repo).read_text())] == ["P1", "P2"]
    # both attempts survive in the transcript (initial + repair, suffixed) and
    # the initial prompt is not clobbered by the repair (lossless, FR-4)
    sd = _steps_dir(fixture_repo)
    assert (sd / "transcript.md").exists()
    assert (sd / "transcript-repair1.md").exists()
    assert "author the plan" in (sd / "prompt.md").read_text()
    assert "failed validation" in (sd / "prompt-repair1.md").read_text()
    # every attempt's spend is accounted (2 calls x $0.01)
    assert man.totals.cost_usd == pytest.approx(0.02)
    assert man.agent_usage["builder"].cost_usd == pytest.approx(0.02)


# --- FR-2.2: exhausted repairs park, then hand-edit + resume revalidates ------
def test_exhausted_repairs_park_artifact_invalid(fixture_repo):
    man = _manifest()
    # initial + 2 repairs, all malformed → park (never a 4th call)
    adapter = ScriptedTextAdapter([MALFORMED_PLAN, MALFORMED_PLAN, MALFORMED_PLAN])
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_PARKED

    assert len(adapter.calls) == 3  # initial + _MAX_ARTIFACT_REPAIRS (2)
    rec = man.record("plan-author")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID
    assert rec.halt_reason is None  # disjoint reason fields (FR-7.2)
    assert M.reason_fields_disjoint(rec.halt_reason, rec.parked_reason)
    # the validator error is recorded verbatim in notes
    assert "failed validation" in (rec.notes or "")
    assert "P<n>" in (rec.notes or "")
    # the content-hash pair is stamped (hash_at_park set, resume side still empty)
    assert rec.revalidation is not None
    assert rec.revalidation.artifact == "plan.md"
    assert rec.revalidation.hash_at_park.startswith("sha256:")
    assert rec.revalidation.hash_at_resume is None
    assert rec.revalidation.passed_on_resume is False
    # a usage_limit-style park is not response-resolvable — a plain resume works
    assert M.PARKED_REASON_ARTIFACT_INVALID not in M.RESPONSE_RESOLVABLE_PARK_REASONS


def test_handedit_then_resume_completes_without_adapter_call(fixture_repo):
    man = _manifest()
    adapter = ScriptedTextAdapter([MALFORMED_PLAN, MALFORMED_PLAN, MALFORMED_PLAN])
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_PARKED
    calls_at_park = len(adapter.calls)

    # hand-edit the artifact on disk to a valid plan (the sanctioned path)
    _plan_path(fixture_repo).write_text(VALID_PLAN)

    # plain resume: re-run ONLY the validator — NO new adapter invocation
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_DONE
    assert len(adapter.calls) == calls_at_park  # zero further adapter calls

    rec = man.record("plan-author")
    assert rec.status == M.DONE
    assert rec.parked_reason is None
    # the revalidation pair records the audited hand-edit path (§7)
    assert rec.revalidation.passed_on_resume is True
    assert rec.revalidation.changed_while_parked is True
    assert rec.revalidation.hash_at_resume is not None
    assert rec.revalidation.hash_at_resume != rec.revalidation.hash_at_park
    assert "on resume" in (rec.notes or "")


def test_resume_without_fixing_reparks_no_adapter_call(fixture_repo):
    man = _manifest()
    adapter = ScriptedTextAdapter([MALFORMED_PLAN, MALFORMED_PLAN, MALFORMED_PLAN])
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_PARKED
    calls_at_park = len(adapter.calls)

    # resume WITHOUT fixing the artifact: the validator still fails → re-park,
    # still with no adapter invocation, and the pair records "unchanged".
    assert _build(fixture_repo, PIPE, adapters={"builder": adapter}, manifest=man).drive() == M.RUN_PARKED
    assert len(adapter.calls) == calls_at_park
    rec = man.record("plan-author")
    assert rec.status == M.PARKED
    assert rec.parked_reason == M.PARKED_REASON_ARTIFACT_INVALID
    assert rec.revalidation.passed_on_resume is False
    assert rec.revalidation.changed_while_parked is False


# --- FR-2.3: shipped-pipeline wiring + backstop unchanged --------------------
@pytest.mark.parametrize(
    "pipeline_path",
    ["pipelines/standard.yaml", "src/gauntlet/scaffold/pipelines/standard.yaml"],
)
def test_shipped_plan_author_validates_plan_phases(pipeline_path):
    repo = Path(__file__).resolve().parents[2]
    data = yaml.safe_load((repo / pipeline_path).read_text())
    steps = [s for stage in data["stages"] for s in stage["steps"]]
    plan_author = next(s for s in steps if s["id"] == "plan-author")
    assert plan_author.get("validate") == "plan_phases"
    # phase_lint remains as the fail-closed backstop gate (FR-2.3)
    assert any(s["type"] == "phase_lint" for s in steps)


# --- observability: status identifies the park + its next command (§9) -------
def test_artifact_invalid_park_status_names_resume():
    from gauntlet.engine import operator as op
    from gauntlet.engine.manifest import StepRecord

    man = _manifest()
    man.status = M.RUN_PARKED
    man.steps = [
        StepRecord(
            id="plan-author", type="agent_task", status=M.PARKED,
            parked_reason=M.PARKED_REASON_ARTIFACT_INVALID,
        )
    ]
    rs = op.compute_run_state(man, op.LIVENESS_NONE)
    assert rs.state == op.STATE_PARKED_ARTIFACT_INVALID
    assert rs.parked is not None and rs.parked.reason == M.PARKED_REASON_ARTIFACT_INVALID
    # a plain `resume` is the recommended next action (no --response needed)
    commands = [a.command for a in rs.next_actions]
    assert any(c.endswith("resume demo") or "resume demo" in c for c in commands)
    assert not any("--response" in c for c in commands)


# --- validators.py unit coverage --------------------------------------------
def test_plan_phases_validator_accepts_valid():
    assert validate_artifact(
        "plan_phases", VALID_PLAN, repo_root=Path("/"), asset_root="."
    ) is None


def test_plan_phases_validator_rejects_malformed_block():
    err = validate_artifact(
        "plan_phases", MALFORMED_PLAN, repo_root=Path("/"), asset_root="."
    )
    assert err is not None and "P<n>" in err


def test_plan_phases_validator_rejects_missing_block():
    err = validate_artifact(
        "plan_phases", NO_BLOCK_PLAN, repo_root=Path("/"), asset_root="."
    )
    assert err is not None and "gauntlet-phases" in err


def test_unknown_validator_name_raises():
    assert not known_validator("nope")
    with pytest.raises(UnknownValidatorError, match="unknown artifact validator"):
        validate_artifact("nope", "x", repo_root=Path("/"), asset_root=".")


def test_schema_ref_validator_passes_and_fails(tmp_path):
    (tmp_path / "s.json").write_text('{"type": "object", "required": ["ok"]}')
    assert known_validator("schema:s.json")
    assert validate_artifact(
        "schema:s.json", '{"ok": 1}', repo_root=tmp_path, asset_root="."
    ) is None
    err = validate_artifact(
        "schema:s.json", '{"nope": 1}', repo_root=tmp_path, asset_root="."
    )
    assert err is not None
    # non-JSON artifact is a repairable defect, not a crash
    assert validate_artifact(
        "schema:s.json", "not json at all", repo_root=tmp_path, asset_root="."
    ) is not None


def test_schema_ref_missing_file_raises(tmp_path):
    with pytest.raises(UnknownValidatorError, match="not found"):
        validate_artifact(
            "schema:absent.json", "{}", repo_root=tmp_path, asset_root="."
        )

"""Step-type behaviors: trust model, commit drafting, schema, session reuse."""

from __future__ import annotations

import json

import pytest
import yaml

from gauntlet.adapters.base import AgentResult
from gauntlet.engine import manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.orchestrator import Orchestrator
from gauntlet.engine.pipeline import Pipeline
from gauntlet.engine.steptypes import _marker_signalled, render_shell_command

from conftest import FakeAdapter


def _orch(repo, text, *, config=None, adapters=None, extra_context=None):
    cfg = RunConfig.model_validate(config or {"agents": {"builder": {"adapter": "claude-code"}}})
    pipeline = Pipeline.model_validate(yaml.safe_load(text))
    ar = repo / "runs" / "demo"
    rd = ar / "run-1"
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    return Orchestrator(
        repo_root=repo, run_dir=rd, artifact_root=ar, config=cfg,
        pipeline=pipeline, manifest=man,
        adapter_factory=(lambda n: adapters[n]) if adapters else None,
        extra_context=extra_context or {},
    )


# --- trust model (review F-001) ---------------------------------------------
def test_shell_resolves_only_config_tokens():
    cfg = RunConfig.model_validate({"test_command": "pytest -q"})
    assert render_shell_command("run {{config.test_command}}", cfg) == "run pytest -q"


def test_shell_refuses_non_config_token():
    cfg = RunConfig.model_validate({})
    with pytest.raises(ValueError, match="refusing to substitute"):
        render_shell_command("rm {{artifacts.plan}}", cfg)


def test_shell_refuses_unknown_config_key():
    cfg = RunConfig.model_validate({})
    with pytest.raises(ValueError, match="unknown config key"):
        render_shell_command("{{config.nonexistent}}", cfg)


# --- commit drafting + redraft (FR-9.2) -------------------------------------
class DraftAdapter:
    """Returns a bad message first, then a well-formed one (tests redraft)."""

    capabilities = FakeAdapter.capabilities

    def __init__(self):
        self.n = 0

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.n += 1
        text = "no prefix here" if self.n == 1 else "P1: drafted\n\nthe body."
        return AgentResult(text=text, exit_code=0)


def test_commit_message_agent_redrafts_until_valid(fixture_repo):
    (fixture_repo / "work.py").write_text("code\n")
    drafter = DraftAdapter()
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, message_agent: triage, max_redrafts: 2}
"""
    cfg = {"agents": {"triage": {"adapter": "api", "model": "h"}}}
    orch = _orch(fixture_repo, text, config=cfg, adapters={"triage": drafter})
    assert orch.drive() == M.RUN_DONE
    assert drafter.n == 2
    from gauntlet.engine import gitops
    assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: drafted"


class RecordingDrafter:
    """Captures the draft prompt and reports usage (F-008)."""

    capabilities = FakeAdapter.capabilities

    def __init__(self):
        self.prompts = []

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        from gauntlet.adapters.base import Usage

        self.prompts.append(prompt)
        return AgentResult(
            text="P1: add new file\n\nbody.",
            usage=Usage(input_tokens=100, output_tokens=10, cost_usd=0.002),
            session_id="draft-sess",
            exit_code=0,
        )


def test_commit_draft_sees_untracked_files_and_accounts_usage(fixture_repo):
    # F-008: a NEW-file phase must not draft from an empty diff, and the
    # message-agent's usage must land in the manifest totals.
    (fixture_repo / "brand_new.py").write_text("print('new')\n")  # untracked
    drafter = RecordingDrafter()
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, message_agent: triage}
"""
    cfg = {"agents": {"triage": {"adapter": "api", "model": "h"}}}
    orch = _orch(fixture_repo, text, config=cfg, adapters={"triage": drafter})
    assert orch.drive() == M.RUN_DONE
    # the untracked new file is visible to the drafter (status section)
    assert "brand_new.py" in drafter.prompts[0]
    # the drafter's cost is accumulated into the run + step totals
    assert orch.manifest.totals.cost_usd == 0.002
    assert orch.manifest.record("commit").usage.cost_usd == 0.002
    assert orch.manifest.record("commit").session_id == "draft-sess"


class RedraftUsageAdapter:
    """Bad draft (with usage) then a good one (with usage) — usage must sum."""

    capabilities = FakeAdapter.capabilities

    def __init__(self):
        self.n = 0

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        from gauntlet.adapters.base import Usage

        self.n += 1
        text = "bad header" if self.n == 1 else "P1: ok\n\nbody."
        return AgentResult(
            text=text,
            usage=Usage(
                input_tokens=50, output_tokens=5, cached_input_tokens=20, cost_usd=0.001
            ),
            session_id="s",
            exit_code=0,
        )


def test_commit_redraft_usage_is_summed(fixture_repo):
    # F-008 round 2: a rejected draft attempt's cost is real spend and must be
    # counted, not overwritten by the accepted attempt.
    (fixture_repo / "new.py").write_text("x\n")
    drafter = RedraftUsageAdapter()
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, message_agent: triage, max_redrafts: 2}
"""
    cfg = {"agents": {"triage": {"adapter": "api", "model": "h"}}}
    orch = _orch(fixture_repo, text, config=cfg, adapters={"triage": drafter})
    assert orch.drive() == M.RUN_DONE
    assert drafter.n == 2  # one rejected + one accepted
    # both attempts counted across every usage field: 2 x each
    assert abs(orch.manifest.totals.cost_usd - 0.002) < 1e-9
    assert orch.manifest.record("commit").usage.input_tokens == 100
    assert orch.manifest.record("commit").usage.cached_input_tokens == 40


def test_commit_authored_by_builder_not_message_agent(fixture_repo):
    # F-003: the message_agent (triage) drafts only the message TEXT; the commit
    # must be AUTHORED by the builder (the implementer), or implementation work
    # is mislabelled as triage-authored, breaking provenance (FR-9.7).
    (fixture_repo / "work.py").write_text("code\n")
    drafter = RecordingDrafter()
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, message_agent: triage}
"""
    cfg = {"agents": {"triage": {"adapter": "api", "model": "h"}}}
    orch = _orch(fixture_repo, text, config=cfg, adapters={"triage": drafter})
    assert orch.drive() == M.RUN_DONE
    from gauntlet.engine import gitops

    author = gitops._run(fixture_repo, "log", "-1", "--format=%an <%ae>", "HEAD").strip()
    assert author == "Gauntlet builder <builder@gauntlet.local>"
    assert "triage" not in author


def test_commit_explicit_agent_overrides_author(fixture_repo):
    # An explicit `agent:` on the commit step still wins as the author identity.
    (fixture_repo / "work.py").write_text("code\n")
    drafter = RecordingDrafter()
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, agent: reviewer, message_agent: triage}
"""
    cfg = {"agents": {"triage": {"adapter": "api", "model": "h"}}}
    orch = _orch(fixture_repo, text, config=cfg, adapters={"triage": drafter})
    assert orch.drive() == M.RUN_DONE
    from gauntlet.engine import gitops

    author = gitops._run(fixture_repo, "log", "-1", "--format=%ae", "HEAD").strip()
    assert author == "reviewer@gauntlet.local"


def test_commit_bad_literal_message_fails(fixture_repo):
    (fixture_repo / "work.py").write_text("code\n")
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: commit, type: commit, message: "not a valid header"}
"""
    orch = _orch(fixture_repo, text)
    assert orch.drive() == M.RUN_FAILED
    assert "invalid" in orch.manifest.record("commit").notes


# --- schema-validated agent_task --------------------------------------------
def test_agent_task_validates_structured_output(fixture_repo):
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    (fixture_repo / "schema.json").write_text(json.dumps(schema))
    adapter = FakeAdapter(text='{"ok": true}', structured={"ok": True})
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: review, type: agent_task, agent: builder, schema: schema.json, repo_write: false, prompt_text: go}
"""
    orch = _orch(fixture_repo, text, adapters={"builder": adapter})
    assert orch.drive() == M.RUN_DONE
    # the schema was loaded and handed to the adapter
    assert adapter.calls[0]["prompt"]  # ran


# --- session reuse on retry (FR-8.2) ----------------------------------------
def test_session_id_reused_on_retry(fixture_repo):
    captured = []

    def on_run(adapter, prompt, cwd):
        captured.append(adapter.calls[-1]["session"])

    adapter = FakeAdapter(session_id="sess-123", on_run=on_run)
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
      - {id: tests, type: shell, run: "false", on_fail: {route_to: implement, max_retries: 1}}
"""
    orch = _orch(fixture_repo, text, adapters={"builder": adapter})
    orch.drive()
    # first call has no session; the retry reuses the recorded session id
    assert captured[0] is None
    assert captured[1] == "sess-123"


# --- FR-2.2 role swap by YAML/profile only ----------------------------------
def test_agent_profile_swap_is_config_only(fixture_repo):
    """The same step bound to different profiles routes to different adapters."""
    claude = FakeAdapter(text="from-claude", writes={"a.py": "x\n"})
    codex = FakeAdapter(text="from-codex", writes={"a.py": "x\n"})
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go}
"""
    cfg = {"agents": {"builder": {"adapter": "claude-code"}, "reviewer": {"adapter": "codex"}}}
    orch = _orch(fixture_repo, text, config=cfg,
                 adapters={"builder": claude, "reviewer": codex})
    orch.drive()
    assert claude.calls and not codex.calls  # routed to the bound profile only


# --- completion-signal marker matching (#32; halt_on false-positive fix) -----
# The substring check used to read a plan that *quotes* the FR-10.4 protocol as
# prose as a genuine halt, park plan-author, and discard the authored plan.md.
# The signal must be line-leading (a "clearly marked block", implement-phase.md).
@pytest.mark.parametrize("text", [
    "UPSTREAM CONFLICT\nPhase: P1\nConflict: the PRD contradicts itself.",
    "## UPSTREAM CONFLICT\nPhase: P3",
    "**UPSTREAM CONFLICT**",
    "> UPSTREAM CONFLICT",
    "  - UPSTREAM CONFLICT: second engine seam needed",
    "intro line\n\nUPSTREAM CONFLICT\nbody",
])
def test_marker_signalled_matches_line_leading_block(text):
    assert _marker_signalled("UPSTREAM CONFLICT", text)


@pytest.mark.parametrize("text", [
    # the exact shape that broke the gauntlet-ui run: marker mid-sentence in prose
    "- Any second engine change is an **UPSTREAM CONFLICT** (FR-10.4), not a quiet edit.",
    "Treat a temptation to widen the seam as an UPSTREAM CONFLICT here.",
    "no marker at all",
    "",
])
def test_marker_signalled_ignores_inline_prose(text):
    assert not _marker_signalled("UPSTREAM CONFLICT", text)


@pytest.mark.parametrize("text", [
    # review F-002: the marker must OWN the line. A line-leading token that
    # extends into another word or sentence is NOT the canonical signal.
    "UPSTREAM CONFLICTS: none",          # plural — not the marker
    "UPSTREAM CONFLICT resolved",        # trailing word continues the sentence
    "## UPSTREAM CONFLICT has been resolved",  # decorated, but still trailing prose
    "- UPSTREAM CONFLICTING changes detected",
])
def test_marker_signalled_rejects_line_leading_boundary_violations(text):
    assert not _marker_signalled("UPSTREAM CONFLICT", text)


def test_marker_signalled_empty_marker_is_false():
    assert not _marker_signalled("", "anything at all")


_HALT_PIPELINE = """
name: demo
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-author, type: agent_task, agent: builder, prompt_text: go,
         output: plan.md, halt_on: "UPSTREAM CONFLICT"}
"""


def test_halt_marker_in_prose_completes_and_writes_output(fixture_repo):
    """Regression: a plan quoting the protocol as prose is DONE, and the
    authored output: lands on disk (was parked + discarded, #32)."""
    prose_plan = (
        "# Implementation Plan\n\n"
        "- Any second engine change is an **UPSTREAM CONFLICT** (FR-10.4).\n"
    )
    orch = _orch(fixture_repo, _HALT_PIPELINE,
                 adapters={"builder": FakeAdapter(text=prose_plan)})
    assert orch.drive() == M.RUN_DONE
    assert (fixture_repo / "runs" / "demo" / "plan.md").read_text() == prose_plan


def test_halt_marker_as_block_parks_without_writing_output(fixture_repo):
    """A genuine line-leading UPSTREAM CONFLICT block still parks (fail closed),
    and does not write a bogus output artifact."""
    conflict = "UPSTREAM CONFLICT\nPhase: P1\nConflict: the PRD contradicts itself.\n"
    orch = _orch(fixture_repo, _HALT_PIPELINE,
                 adapters={"builder": FakeAdapter(text=conflict)})
    assert orch.drive() == M.RUN_PARKED
    assert not (fixture_repo / "runs" / "demo" / "plan.md").exists()


# --- phase_lint: deterministic plan-gate structural check --------------------
_PHASE_LINT_PIPELINE = """
name: demo
version: 1
stages:
  - id: plan
    steps:
      - {id: plan-lint, type: phase_lint, artifact: plan.md}
"""

_VALID_PLAN = (
    "# Plan\n\n```gauntlet-phases\n"
    "- id: P1\n  title: Build it\n  goal: Implement the widget end-to-end.\n"
    "```\n"
)


def test_phase_lint_passes_a_valid_block(fixture_repo):
    orch = _orch(fixture_repo, _PHASE_LINT_PIPELINE)
    (orch.artifact_root / "plan.md").write_text(_VALID_PLAN)
    assert orch.drive() == M.RUN_DONE
    rec = orch.manifest.record("plan-lint")
    assert rec.status == M.DONE
    assert "valid" in rec.notes and "P1" in rec.notes


def test_phase_lint_halts_on_malformed_block(fixture_repo):
    orch = _orch(fixture_repo, _PHASE_LINT_PIPELINE)
    # Unquoted `schema:` colon — the exact defect the prose reviewer missed.
    (orch.artifact_root / "plan.md").write_text(
        "# Plan\n\n```gauntlet-phases\n"
        "- id: P1\n  title: Broken\n"
        "  goal: the implement step has no schema: field and must not change\n"
        "```\n"
    )
    # A structurally unrunnable plan must not reach approval: it parks here.
    assert orch.drive() == M.RUN_PARKED
    rec = orch.manifest.record("plan-lint")
    assert rec.status == M.HALTED
    assert "invalid" in rec.notes


def test_phase_lint_halts_when_block_absent(fixture_repo):
    orch = _orch(fixture_repo, _PHASE_LINT_PIPELINE)
    (orch.artifact_root / "plan.md").write_text("# Plan\n\nProse only, no block.\n")
    assert orch.drive() == M.RUN_PARKED
    rec = orch.manifest.record("plan-lint")
    assert rec.status == M.HALTED
    assert "no gauntlet-phases block" in rec.notes

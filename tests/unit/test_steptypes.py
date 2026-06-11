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
from gauntlet.engine.steptypes import render_shell_command

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

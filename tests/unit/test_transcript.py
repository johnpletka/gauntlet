"""Transcript logger (FR-4): layout, faithful rendering, redaction, RUN.md."""

from __future__ import annotations

import json
import re

from gauntlet.adapters.base import AgentResult, Usage
from gauntlet.engine.manifest import (
    CommitRecord,
    Manifest,
    PipelineRef,
    StepRecord,
    UsageTotals,
)
from gauntlet.logging.redact import (
    RedactingWriter,
    RedactionSettings,
    Redactor,
    build_redactor,
)
from gauntlet.logging.transcript import (
    GITIGNORE_GUIDANCE,
    StepLogger,
    render_transcript,
    write_run_index,
)

CLAUDE_EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "opus"},
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "tu-1", "name": "Bash",
                 "input": {"command": "ls"}},
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "tu-1",
                 "content": [{"type": "text", "text": "README.md"}]},
            ]
        },
    },
    {"type": "result", "subtype": "success", "result": "done",
     "total_cost_usd": 0.01},
]

CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "th-9"},
    {"type": "item.completed",
     "item": {"type": "command_execution", "command": "pytest",
              "exit_code": 0, "aggregated_output": "ok"}},
    {"type": "item.completed",
     "item": {"type": "agent_message", "text": "All good."}},
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
]


# --- FR-4.2: nothing summarized away -----------------------------------------
def test_render_claude_events_keeps_tool_calls_and_results():
    md = render_transcript(CLAUDE_EVENTS, final_text="done")
    assert "Let me check." in md
    assert "**tool call** `Bash`" in md and '"command": "ls"' in md
    assert "**tool result**" in md and "README.md" in md
    assert "## Final output" in md and md.rstrip().endswith("done")


def test_render_codex_events_keeps_commands():
    md = render_transcript(CODEX_EVENTS)
    assert "$ pytest" in md and "exit: 0" in md
    assert "All good." in md
    assert "th-9" in md


def test_render_flags_codex_sandbox_refusal():
    # BOOTSTRAP-NOTES #11: the errno in agent text is the only refusal signal.
    events = [{"type": "item.completed",
               "item": {"type": "agent_message",
                        "text": "zsh:1: operation not permitted: /etc/x"}}]
    md = render_transcript(events)
    assert "sandbox-refusal" in md and "operation not permitted" in md


def test_render_unknown_event_preserved_verbatim():
    md = render_transcript([{"type": "wat.new", "payload": {"a": 1}}])
    assert "wat.new" in md and '"a": 1' in md


# --- FR-4.1 layout ------------------------------------------------------------
def test_step_logger_writes_full_file_set(tmp_path):
    logger = StepLogger(RedactingWriter(), tmp_path / "010-review-r1")
    logger.log_prompt("the exact prompt")
    result = AgentResult(
        text="{}", structured={"findings": []}, raw_events=CODEX_EVENTS, exit_code=0
    )
    logger.log_result(result, structured_name="findings.json")
    d = tmp_path / "010-review-r1"
    assert (d / "prompt.md").read_text() == "the exact prompt"
    assert "$ pytest" in (d / "transcript.md").read_text()
    lines = (d / "events.jsonl").read_text().strip().splitlines()
    assert [json.loads(l)["type"] for l in lines] == [e["type"] for e in CODEX_EVENTS]
    assert json.loads((d / "findings.json").read_text()) == {"findings": []}


# --- FR-4.4: redaction before any write, configurable -------------------------
def test_step_logger_redacts_before_write(tmp_path):
    redactor = Redactor(env={"MY_API_KEY": "supersecretvalue123"})
    logger = StepLogger(RedactingWriter(redactor), tmp_path / "s")
    logger.log_prompt("key is supersecretvalue123")
    result = AgentResult(
        text="echoing supersecretvalue123",
        raw_events=[{"type": "x", "v": "supersecretvalue123"}],
        exit_code=0,
    )
    logger.log_result(result)
    for name in ("prompt.md", "transcript.md", "events.jsonl"):
        content = (tmp_path / "s" / name).read_text()
        assert "supersecretvalue123" not in content
        assert "[REDACTED:env:MY_API_KEY]" in content


def test_build_redactor_extra_env_and_patterns():
    settings = RedactionSettings(
        extra_env_vars=["WEIRD_NAME"],
        extra_patterns=[{"name": "internal-id", "regex": r"\bINT-[0-9]{8,}\b"}],
    )
    redactor = build_redactor(settings, env={"WEIRD_NAME": "longhiddenvalue42"})
    text, hits = redactor.redact("v=longhiddenvalue42 id=INT-12345678")
    assert "longhiddenvalue42" not in text and "INT-12345678" not in text
    assert {h.pattern for h in hits} == {"env:WEIRD_NAME", "internal-id"}


def test_default_redaction_still_applies_with_empty_settings():
    redactor = build_redactor(RedactionSettings(), env={})
    text, hits = redactor.redact("sk-ant-abcdefghijklmnopqrstuv")
    assert "[REDACTED:anthropic-key]" in text


# --- FR-4.3: RUN.md ------------------------------------------------------------
def _manifest():
    man = Manifest(
        run_id="run-1", slug="demo", branch="gauntlet/demo", base_branch="main",
        pipeline=PipelineRef(name="std", version=1, hash="sha256:" + "0" * 64),
    )
    man.steps.append(StepRecord(
        id="review", type="adversarial_cycle", status="done",
        started="2026-06-11T01:00:00+00:00", ended="2026-06-11T01:02:05+00:00",
        usage=UsageTotals(input_tokens=1000, output_tokens=50, cost_usd=0.12),
        notes="converged in 1 round",
    ))
    man.commits.append(CommitRecord(step_id="commit", phase="P5", sha="a" * 40))
    man.totals = UsageTotals(input_tokens=1000, output_tokens=50, cost_usd=0.12)
    return man


def test_run_index_lists_steps_durations_costs_commits(tmp_path):
    write_run_index(tmp_path, _manifest(), RedactingWriter())
    md = (tmp_path / "RUN.md").read_text()
    assert "`demo`" in md and "gauntlet/demo" in md
    assert "| review | adversarial_cycle | done | 125s | 1000in/50out $0.1200" in md
    assert "converged in 1 round" in md
    assert f"`{'a' * 10}` P5" in md


def test_run_index_links_transcripts_when_present(tmp_path):
    (tmp_path / "steps" / "review").mkdir(parents=True)
    (tmp_path / "steps" / "review" / "transcript.md").write_text("t")
    write_run_index(tmp_path, _manifest(), RedactingWriter())
    assert "[review](steps/review/transcript.md)" in (tmp_path / "RUN.md").read_text()


def test_gitignore_guidance_mentions_events_exclusion():
    # FR-4.5: transcripts committable; events.jsonl exclusion is the documented
    # opt-out, commented out by default.
    assert "events.jsonl" in GITIGNORE_GUIDANCE
    assert re.search(r"^# runs/.*events\.jsonl", GITIGNORE_GUIDANCE, re.M)

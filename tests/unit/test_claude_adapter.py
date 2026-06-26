"""ClaudeCodeAdapter parsing against recorded/fake subprocess output."""

import json

import pytest

from gauntlet.adapters.base import (
    AgentFailedError,
    AgentTimeoutError,
    MalformedOutputError,
)
from gauntlet.adapters.claude_code import ClaudeCodeAdapter
from gauntlet.adapters.process import ProcessOutput


def fake_output(stdout, *, exit_code=0, stderr="", timed_out=False):
    return ProcessOutput(
        argv=["claude"],
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_s=0.1,
        timed_out=timed_out,
    )


RESULT_EVENT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "duration_ms": 4200,
    "num_turns": 1,
    "result": "GAUNTLET_PONG",
    "session_id": "11111111-2222-3333-4444-555555555555",
    "total_cost_usd": 0.0123,
    "usage": {
        "input_tokens": 14,
        "output_tokens": 5,
        "cache_read_input_tokens": 3000,
    },
}


def patch_run(monkeypatch, out):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return out

    monkeypatch.setattr(
        "gauntlet.adapters.claude_code.run_with_timeout", fake_run
    )
    return calls


def test_parses_json_result(monkeypatch):
    calls = patch_run(monkeypatch, fake_output(json.dumps(RESULT_EVENT)))
    result = ClaudeCodeAdapter(model="haiku").run("ping")
    assert result.text == "GAUNTLET_PONG"
    assert result.session_id == "11111111-2222-3333-4444-555555555555"
    assert result.usage.input_tokens == 14
    assert result.usage.output_tokens == 5
    assert result.usage.cached_input_tokens == 3000
    assert result.usage.cost_usd == 0.0123
    assert result.exit_code == 0
    assert result.raw_events == [RESULT_EVENT]
    argv, kwargs = calls[0]
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert ["--model", "haiku"] == argv[4:6]
    assert kwargs["stdin_text"] == "ping"


def test_tokens_only_degraded_path(monkeypatch):
    event = {k: v for k, v in RESULT_EVENT.items() if k != "total_cost_usd"}
    patch_run(monkeypatch, fake_output(json.dumps(event)))
    result = ClaudeCodeAdapter().run("ping")
    assert result.usage.input_tokens == 14
    assert result.usage.cost_usd is None  # PRD §12 Q3: tokens always, cost when derivable


def test_parses_stream_json(monkeypatch):
    lines = [
        {"type": "system", "subtype": "init", "session_id": "abc-123"},
        {"type": "assistant", "message": {"content": "thinking"}},
        RESULT_EVENT,
    ]
    patch_run(
        monkeypatch,
        fake_output("\n".join(json.dumps(line) for line in lines)),
    )
    adapter = ClaudeCodeAdapter(output_format="stream-json")
    result = adapter.run("ping")
    assert result.text == "GAUNTLET_PONG"
    assert len(result.raw_events) == 3
    assert result.session_id == RESULT_EVENT["session_id"]


def test_stream_json_argv_includes_verbose():
    argv = ClaudeCodeAdapter(output_format="stream-json")._build_argv(
        "x", session=None, schema=None
    )
    assert "--verbose" in argv


def test_resume_flag(monkeypatch):
    calls = patch_run(monkeypatch, fake_output(json.dumps(RESULT_EVENT)))
    ClaudeCodeAdapter().run("again", session="sess-1")
    argv, _ = calls[0]
    assert ["--resume", "sess-1"] == argv[argv.index("--resume") : argv.index("--resume") + 2]


def test_tools_disabled_argv():
    argv = ClaudeCodeAdapter(tools=[])._build_argv("x", session=None, schema=None)
    idx = argv.index("--tools")
    assert argv[idx + 1] == ""


def test_effort_argv():
    argv = ClaudeCodeAdapter(effort="high")._build_argv("x", session=None, schema=None)
    idx = argv.index("--effort")
    assert argv[idx + 1] == "high"


def test_no_effort_argv():
    argv = ClaudeCodeAdapter()._build_argv("x", session=None, schema=None)
    assert "--effort" not in argv


def test_malformed_output_raises_with_partial(monkeypatch):
    patch_run(
        monkeypatch,
        fake_output("Execution error", exit_code=1, stderr="boom"),
    )
    with pytest.raises(MalformedOutputError) as excinfo:
        ClaudeCodeAdapter().run("ping")
    partial = excinfo.value.partial
    assert partial is not None
    assert partial.exit_code == 1
    assert any("boom" in str(e) for e in partial.raw_events)


# F-001 (P1 review round 1): parseable output must not mask a failed call.
def test_is_error_result_raises(monkeypatch):
    event = dict(RESULT_EVENT)
    event["is_error"] = True
    event["subtype"] = "error_during_execution"
    patch_run(monkeypatch, fake_output(json.dumps(event)))
    with pytest.raises(AgentFailedError, match="is_error") as excinfo:
        ClaudeCodeAdapter().run("ping")
    partial = excinfo.value.partial
    assert partial.session_id == RESULT_EVENT["session_id"]
    assert partial.text == "GAUNTLET_PONG"  # evidence preserved


def test_error_subtype_raises(monkeypatch):
    event = dict(RESULT_EVENT)
    event["subtype"] = "error_max_turns"
    patch_run(monkeypatch, fake_output(json.dumps(event)))
    with pytest.raises(AgentFailedError, match="error_max_turns"):
        ClaudeCodeAdapter().run("ping")


def test_nonzero_exit_with_parseable_output_raises(monkeypatch):
    patch_run(monkeypatch, fake_output(json.dumps(RESULT_EVENT), exit_code=1))
    with pytest.raises(AgentFailedError, match="exit code 1") as excinfo:
        ClaudeCodeAdapter().run("ping")
    assert excinfo.value.partial.usage.input_tokens == 14


# F-002: stream-json is a JSONL contract; a non-JSON stdout line fails closed.
def test_stream_json_garbage_line_fails_closed(monkeypatch):
    stdout = "\n".join(
        [json.dumps({"type": "system", "session_id": "abc"}), "garbage here"]
    )
    patch_run(monkeypatch, fake_output(stdout))
    with pytest.raises(MalformedOutputError, match="non-JSON line"):
        ClaudeCodeAdapter(output_format="stream-json").run("ping")


def test_missing_result_event_raises(monkeypatch):
    patch_run(
        monkeypatch,
        fake_output(json.dumps({"type": "system", "subtype": "init"})),
    )
    with pytest.raises(MalformedOutputError):
        ClaudeCodeAdapter().run("ping")


def test_timeout_raises_checkpointable(monkeypatch):
    patch_run(monkeypatch, fake_output("", exit_code=-9, timed_out=True))
    adapter = ClaudeCodeAdapter(timeout_s=5)
    with pytest.raises(AgentTimeoutError) as excinfo:
        adapter.run("ping")
    assert excinfo.value.partial is not None
    assert excinfo.value.partial.exit_code == -9


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_structured_output_field(monkeypatch):
    event = dict(RESULT_EVENT)
    event["structured_output"] = {"answer": "pong"}
    calls = patch_run(monkeypatch, fake_output(json.dumps(event)))
    result = ClaudeCodeAdapter().run("ping", schema=SCHEMA)
    assert result.structured == {"answer": "pong"}
    argv, _ = calls[0]
    assert "--json-schema" in argv


def test_structured_fallback_parses_text(monkeypatch):
    event = dict(RESULT_EVENT)
    event["result"] = '{"answer": "pong"}'
    patch_run(monkeypatch, fake_output(json.dumps(event)))
    result = ClaudeCodeAdapter().run("ping", schema=SCHEMA)
    assert result.structured == {"answer": "pong"}


def test_structured_schema_violation_raises(monkeypatch):
    event = dict(RESULT_EVENT)
    event["structured_output"] = {"answer": 42}
    patch_run(monkeypatch, fake_output(json.dumps(event)))
    with pytest.raises(MalformedOutputError):
        ClaudeCodeAdapter().run("ping", schema=SCHEMA)


def test_structured_unparseable_text_raises(monkeypatch):
    patch_run(monkeypatch, fake_output(json.dumps(RESULT_EVENT)))
    with pytest.raises(MalformedOutputError):
        ClaudeCodeAdapter().run("ping", schema=SCHEMA)

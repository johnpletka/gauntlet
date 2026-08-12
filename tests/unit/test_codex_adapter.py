"""CodexAdapter parsing against the event shapes recorded from codex 0.139.0.

The fixture events mirror runs/gauntlet-bootstrap/manual/plan-cycle-r1/
confirm-events.jsonl — a real capture from the installed CLI.
"""

import json
from pathlib import Path

import pytest

from gauntlet.adapters.base import (
    AgentFailedError,
    AgentTimeoutError,
    MalformedOutputError,
)
from gauntlet.adapters.codex import CodexAdapter
from gauntlet.adapters.process import ProcessOutput

THREAD_ID = "019eb429-a100-7fb2-8e40-ce79398c24c8"


def make_events(text='{"answer": "pong"}'):
    return [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": text},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 21497,
                "cached_input_tokens": 3456,
                "output_tokens": 1181,
                "reasoning_output_tokens": 516,
            },
        },
    ]


def fake_output(events, *, exit_code=0, stderr="", timed_out=False):
    stdout = "\n".join(json.dumps(e) for e in events)
    return ProcessOutput(
        argv=["codex"],
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_s=0.1,
        timed_out=timed_out,
    )


def patch_run(monkeypatch, out, *, write_last_message=None):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if write_last_message is not None:
            idx = argv.index("--output-last-message")
            Path(argv[idx + 1]).write_text(write_last_message)
        return out

    monkeypatch.setattr("gauntlet.adapters.codex.run_with_timeout", fake_run)
    return calls


def test_parses_events_and_usage(monkeypatch):
    calls = patch_run(monkeypatch, fake_output(make_events("hello there")))
    result = CodexAdapter().run("hi", cwd=Path("."))
    assert result.text == "hello there"
    assert result.session_id == THREAD_ID
    assert result.usage.input_tokens == 21497
    assert result.usage.cached_input_tokens == 3456
    assert result.usage.output_tokens == 1181
    assert result.usage.cost_usd is None  # codex reports tokens, not cost
    assert result.exit_code == 0
    argv, kwargs = calls[0]
    assert argv[:3] == ["codex", "exec", "--json"]
    assert ["--sandbox", "read-only"] == argv[argv.index("--sandbox") : argv.index("--sandbox") + 2]
    assert argv[-1] == "-"  # prompt over stdin
    assert kwargs["stdin_text"] == "hi"


def test_prefers_output_last_message_file(monkeypatch):
    patch_run(
        monkeypatch,
        fake_output(make_events("from events")),
        write_last_message="from -o file",
    )
    result = CodexAdapter().run("hi")
    assert result.text == "from -o file"


SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def test_schema_writes_file_and_parses_structured(monkeypatch):
    calls = patch_run(monkeypatch, fake_output(make_events()))
    captured_schema = {}

    def fake_run(argv, **kwargs):
        idx = argv.index("--output-schema")
        captured_schema.update(json.loads(Path(argv[idx + 1]).read_text()))
        return fake_output(make_events())

    monkeypatch.setattr("gauntlet.adapters.codex.run_with_timeout", fake_run)
    result = CodexAdapter().run("hi", schema=SCHEMA)
    assert result.structured == {"answer": "pong"}
    assert captured_schema == SCHEMA
    assert calls == []  # earlier patch replaced


def test_schema_violation_raises_with_partial(monkeypatch):
    patch_run(monkeypatch, fake_output(make_events('{"answer": 7}')))
    with pytest.raises(MalformedOutputError) as excinfo:
        CodexAdapter().run("hi", schema=SCHEMA)
    assert excinfo.value.partial.session_id == THREAD_ID


def test_no_agent_message_raises(monkeypatch):
    # exit 0 so the F-001 failure check doesn't fire first; a clean exit
    # with no agent message is a malformed-output case
    events = [{"type": "thread.started", "thread_id": THREAD_ID}]
    patch_run(monkeypatch, fake_output(events, exit_code=0))
    with pytest.raises(MalformedOutputError) as excinfo:
        CodexAdapter().run("hi")
    assert excinfo.value.partial.exit_code == 0


def test_no_agent_message_with_nonzero_exit_raises_agent_failed(monkeypatch):
    # F-001 ratified: nonzero exit takes precedence over the missing message
    events = [{"type": "thread.started", "thread_id": THREAD_ID}]
    patch_run(monkeypatch, fake_output(events, exit_code=1, stderr="auth error"))
    with pytest.raises(AgentFailedError, match="exit code 1") as excinfo:
        CodexAdapter().run("hi")
    assert excinfo.value.partial.exit_code == 1


# Behavior ratified in P1 review round 1 (F-002): non-JSON stdout lines were
# previously tolerated; with --json the event contract puts logs on stderr,
# so they now fail closed.
def test_unparsed_lines_fail_closed(monkeypatch):
    out = fake_output(make_events("ok"))
    noisy = ProcessOutput(
        argv=out.argv,
        stdout="WARN: something\n" + out.stdout,
        stderr="",
        exit_code=0,
        duration_s=0.1,
        timed_out=False,
    )
    patch_run(monkeypatch, noisy)
    with pytest.raises(MalformedOutputError) as excinfo:
        CodexAdapter().run("hi")
    # the checkpointable partial preserves the offending line and the events
    partial = excinfo.value.partial
    assert partial is not None
    assert any(
        e.get("type") == "gauntlet.unparsed_line" and "WARN" in e.get("line", "")
        for e in partial.raw_events
    )
    assert partial.session_id == THREAD_ID


def test_turn_failed_event_raises(monkeypatch):
    events = [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.failed", "error": {"message": "rate limited"}},
    ]
    patch_run(monkeypatch, fake_output(events, exit_code=0))
    with pytest.raises(AgentFailedError, match="turn.failed") as excinfo:
        CodexAdapter().run("hi")
    assert excinfo.value.partial.session_id == THREAD_ID


def test_nonzero_exit_with_parseable_output_raises(monkeypatch):
    # F-001: a parseable stream must not mask a failed invocation
    patch_run(monkeypatch, fake_output(make_events("looks fine"), exit_code=2))
    with pytest.raises(AgentFailedError, match="exit code 2") as excinfo:
        CodexAdapter().run("hi")
    partial = excinfo.value.partial
    assert partial.text == "looks fine"  # evidence preserved for checkpointing
    assert partial.usage.input_tokens == 21497


def test_resume_argv(monkeypatch):
    calls = patch_run(monkeypatch, fake_output(make_events("resumed")))
    CodexAdapter().run("again", session=THREAD_ID)
    argv, _ = calls[0]
    assert argv[1:4] == ["exec", "resume", THREAD_ID]
    # exec resume has no --sandbox flag (verified 0.139.0); -c override instead
    assert "--sandbox" not in argv
    idx = argv.index("-c")
    assert argv[idx + 1] == 'sandbox_mode="read-only"'


def test_reasoning_effort_argv(monkeypatch):
    calls = patch_run(monkeypatch, fake_output(make_events()))
    CodexAdapter(reasoning_effort="xhigh").run("x")
    argv, _ = calls[0]
    idx = argv.index("-c")
    assert argv[idx + 1] == 'model_reasoning_effort="xhigh"'


def test_no_reasoning_effort_argv(monkeypatch):
    calls = patch_run(monkeypatch, fake_output(make_events()))
    CodexAdapter().run("x")
    argv, _ = calls[0]
    c_args = [argv[i + 1] for i, v in enumerate(argv[:-1]) if v == "-c"]
    assert not any("model_reasoning_effort" in a for a in c_args)


def test_timeout_raises_checkpointable(monkeypatch):
    partial_events = make_events("partial answer")[:3]  # no turn.completed
    patch_run(
        monkeypatch,
        fake_output(partial_events, exit_code=-9, timed_out=True),
    )
    with pytest.raises(AgentTimeoutError) as excinfo:
        CodexAdapter(timeout_s=5).run("hi")
    partial = excinfo.value.partial
    assert partial.session_id == THREAD_ID
    assert partial.text == "partial answer"


# --- FR-3.1 failure classification on AgentFailedError ------------------------
def _failed_events(error):
    return [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        {"type": "turn.failed", "error": error},
    ]


def test_usage_limit_message_classified_transient(monkeypatch):
    events = _failed_events(
        {"message": "You've hit your usage limit. Try again at 2:57 AM."}
    )
    patch_run(monkeypatch, fake_output(events))
    with pytest.raises(AgentFailedError) as excinfo:
        CodexAdapter().run("hi")
    info = excinfo.value.failure_info
    assert info is not None and info.kind == "transient_usage_limit"
    assert info.retry_after_s is None  # "2:57 AM" is prose, never scraped (§7)


def test_overload_message_classified_transient(monkeypatch):
    events = _failed_events({"message": "stream error: the model is Overloaded"})
    patch_run(monkeypatch, fake_output(events))
    with pytest.raises(AgentFailedError) as excinfo:
        CodexAdapter().run("hi")
    assert excinfo.value.failure_info.kind == "transient_overload"


def test_transport_503_agent_error_event_classified_transient(monkeypatch):
    # Issue #96 live shape: codex's websocket retry loop gave up against an
    # upstream 503 and surfaced it as an agent error EVENT carrying a bare
    # `message` (no `error` sub-object). That is provider unavailability —
    # transient_dependency, routed to the persisted retry budget and the R7
    # provider_unavailable park — never terminal/unmatched.
    events = [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        {"type": "error",
         "message": "Reconnecting... 2/5 (unexpected status 503 "
                    "Service Unavailable)"},
    ]
    patch_run(monkeypatch, fake_output(events))
    with pytest.raises(AgentFailedError) as excinfo:
        CodexAdapter().run("hi")
    info = excinfo.value.failure_info
    assert info is not None and info.kind == "transient_dependency"
    assert info.marker == "codex_provider_unavailable_message"


def test_final_capacity_failure_after_reconnect_classified_transient(monkeypatch):
    # Issue #119: the final turn.failed verdict, not the first stream diagnostic,
    # determines retryability.
    events = [
        {"type": "thread.started", "thread_id": THREAD_ID},
        {"type": "turn.started"},
        {"type": "error", "message": "stream disconnected"},
        {"type": "error", "message": "Reconnecting... 2/5"},
        {"type": "turn.failed", "error": {
            "message": "Selected model is at capacity. Please try a different model."
        }},
    ]
    patch_run(monkeypatch, fake_output(events, exit_code=1))
    with pytest.raises(AgentFailedError) as excinfo:
        CodexAdapter().run("hi")
    info = excinfo.value.failure_info
    assert info.kind == "transient_overload"
    assert info.marker == "codex_capacity_message"


def test_models_cache_startup_fatal_classified_transient(monkeypatch):
    stderr = (
        "ERROR codex_models_manager::cache: failed to load models cache: "
        "missing field `base_instructions` at line 94 column 5"
    )
    patch_run(monkeypatch, fake_output([], exit_code=1, stderr=stderr))
    with pytest.raises(AgentFailedError) as excinfo:
        CodexAdapter().run("hi")
    info = excinfo.value.failure_info
    assert info.kind == "transient_dependency"
    assert info.marker == "codex_models_cache_schema_startup"


def test_connection_reset_classified_transient(monkeypatch):
    events = _failed_events(
        {"message": "stream disconnected: connection reset by peer"}
    )
    patch_run(monkeypatch, fake_output(events))
    with pytest.raises(AgentFailedError) as excinfo:
        CodexAdapter().run("hi")
    assert excinfo.value.failure_info.kind == "transient_dependency"


def test_unlisted_error_classified_terminal(monkeypatch):
    events = _failed_events({"message": "disk full writing scratch file"})
    patch_run(monkeypatch, fake_output(events))
    with pytest.raises(AgentFailedError) as excinfo:
        CodexAdapter().run("hi")
    assert excinfo.value.failure_info.kind == "terminal"


def test_non_object_json_line_fails_closed(monkeypatch):
    # A valid-JSON-but-not-object stdout line ("oops", 42, null) breaks the
    # event contract exactly like a non-JSON line: strict decode must refuse
    # with MalformedOutputError — never surface as an AttributeError from a
    # downstream `.get()` consumer.
    events = ["not an event object"] + _failed_events({"message": "x"})
    patch_run(monkeypatch, fake_output(events))
    with pytest.raises(MalformedOutputError):
        CodexAdapter().run("hi")


def test_classifier_tolerates_non_dict_events():
    # classify_codex_failure is a public entry point that can be handed a
    # malformed list directly; non-dict elements are skipped so it fails
    # closed to `terminal` instead of crashing (§7: classifier never raises
    # on malformed input).
    from gauntlet.adapters.failure_markers import classify_codex_failure

    events = ["bare string", 42, None,
              {"type": "turn.failed", "error": {"message": "disk full"}}]
    info = classify_codex_failure(events, 1)
    assert info.kind == "terminal"


def test_session_not_found_on_resume(monkeypatch):
    from gauntlet.adapters.base import SessionNotFoundError

    events = _failed_events({"message": "session th_gone not found"})
    patch_run(monkeypatch, fake_output(events, exit_code=1))
    with pytest.raises(SessionNotFoundError):
        CodexAdapter().run("continue", session="th_gone")

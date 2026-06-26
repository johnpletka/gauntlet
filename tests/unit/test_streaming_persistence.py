"""Live, redacted persistence for both CLI adapters (P2, FR-2 / FR-6 / FR-7).

Validates that the fail-closed redaction invariant survives streaming —
including the cross-event value-containment the per-line unit depends on — and
that the authoritative result stays byte-identical to the buffered path, on both
the Claude (``stream-json``) and Codex (``--json``) adapters.

Three layers are exercised:

* **Process + persistence** — a real subprocess fed through ``run_with_timeout``
  with a live ``StepLogger`` stream sink, proving the actual byte path: live
  growth, no raw secret at rest at any read, stderr never persisted, partial
  trailing lines withheld, and non-JSON lines captured verbatim without a parse
  gate.
* **Adapter wiring + parity** — a ``run_with_timeout`` double that streams each
  NDJSON line, proving each CLI adapter threads the sink only when qualified
  (FR-2.7/FR-2.8), value-containment holds per adapter, and the returned
  ``AgentResult`` is identical across modes (FR-2.3/FR-7.2).
* **Config gate + engine wiring** — the default-off flag round-trips, the engine
  opens a stream only for a qualified adapter, and a sink fault fails the step.
"""

from __future__ import annotations

import json
import sys

import pytest
import yaml

from gauntlet.adapters.base import AgentResult
from gauntlet.adapters.claude_code import ClaudeCodeAdapter
from gauntlet.adapters.codex import CodexAdapter
from gauntlet.adapters.process import ProcessOutput, run_with_timeout
from gauntlet.engine.config import RunConfig
from gauntlet.engine.steptypes import open_step_stream
from gauntlet.logging.redact import RedactingWriter, Redactor
from gauntlet.logging.transcript import StepLogger


def _child(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _events_text(step_dir) -> str:
    p = step_dir / "events.jsonl"
    return p.read_text() if p.exists() else ""


# ==========================================================================
# Layer 1 — process + persistence (real subprocess + live StepLogger stream)
# ==========================================================================


def test_events_jsonl_grows_incrementally_during_step(tmp_path):
    # FR-2.1: events.jsonl is non-empty and growing before the step completes.
    # The child emits N lines with delays; a snooping wrapper snapshots the file
    # after each append, so an early snapshot is genuinely taken mid-run.
    logger = StepLogger(RedactingWriter(), tmp_path / "s")
    stream = logger.open_stream()
    events_path = tmp_path / "s" / "events.jsonl"
    snapshots: list[str] = []

    def snooping_sink(line: str) -> None:
        stream.append_line(line)
        snapshots.append(events_path.read_text())

    script = (
        "import sys, time, json\n"
        "for i in range(4):\n"
        "    sys.stdout.write(json.dumps({'type': 'ev', 'i': i}) + '\\n')\n"
        "    sys.stdout.flush(); time.sleep(0.05)\n"
    )
    out = run_with_timeout(_child(script), timeout_s=30, sink=snooping_sink)
    stream.close()

    assert not out.timed_out
    # ≥1 event present at the first mid-run read, and the file grew monotonically.
    assert snapshots[0].strip() != ""
    assert snapshots[0].count("\n") == 1
    assert snapshots[-1].count("\n") == 4
    assert snapshots == sorted(snapshots, key=len)  # only ever grows


def test_no_raw_secret_on_disk_during_streaming(tmp_path):
    # FR-2.2: a known secret in a streamed line is never present on disk raw,
    # even transiently — polled after every append throughout the window.
    secret = "supersecretvalue123456"
    redactor = Redactor(env={"X_API_KEY": secret})
    logger = StepLogger(RedactingWriter(redactor), tmp_path / "s")
    stream = logger.open_stream()
    events_path = tmp_path / "s" / "events.jsonl"
    snapshots: list[str] = []

    def snooping_sink(line: str) -> None:
        stream.append_line(line)
        snapshots.append(events_path.read_text())

    script = (
        "import sys, time, json\n"
        f"lines = [json.dumps({{'type': 'system'}}), "
        f"json.dumps({{'type': 'result', 'result': {secret!r}}}), "
        f"json.dumps({{'type': 'done'}})]\n"
        "for ln in lines:\n"
        "    sys.stdout.write(ln + '\\n'); sys.stdout.flush(); time.sleep(0.05)\n"
    )
    out = run_with_timeout(_child(script), timeout_s=30, sink=snooping_sink)
    stream.close()

    assert not out.timed_out
    assert snapshots, "sink was never invoked"
    for snap in snapshots:  # the raw secret never appears at ANY read
        assert secret not in snap
    final = events_path.read_text()
    assert "[REDACTED:env:X_API_KEY]" in final  # the event still landed, redacted


def test_stderr_never_live_persisted(tmp_path):
    # FR-2.6: only stdout NDJSON lines reach events.jsonl. stderr (diagnostic AND
    # secret-bearing) is drained for deadlock-safety only and kept in
    # ProcessOutput.stderr for unchanged end-of-step handling — never live.
    secret = "stderrsecretvalue99999"
    redactor = Redactor(env={"Y_TOKEN": secret})
    logger = StepLogger(RedactingWriter(redactor), tmp_path / "s")
    stream = logger.open_stream()
    script = (
        "import sys, json\n"
        f"sys.stderr.write('diagnostic ' + {secret!r} + '\\n'); sys.stderr.flush()\n"
        "sys.stdout.write(json.dumps({'type': 'result', 'result': 'ok'}) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    buffered = run_with_timeout(_child(script), timeout_s=30)
    streamed = run_with_timeout(_child(script), timeout_s=30, sink=stream.append_line)
    stream.close()

    events = _events_text(tmp_path / "s")
    assert "diagnostic" not in events  # no stderr line in the live file
    assert secret not in events
    assert "[REDACTED" not in events  # the only secret was on stderr; events has none
    assert '"result": "ok"' in events  # the stdout event did land
    # ProcessOutput.stderr matches the buffered path at step end (raw, as before).
    assert streamed.stderr == buffered.stderr
    assert secret in streamed.stderr  # end-of-step stderr handling is unchanged


def test_partial_trailing_line_not_written_live(tmp_path):
    # FR-2.4: a trailing un-terminated line is never written to the live file; it
    # is captured in ProcessOutput.stdout for the end-of-step partial.
    logger = StepLogger(RedactingWriter(), tmp_path / "s")
    stream = logger.open_stream()
    script = (
        "import sys, json\n"
        "sys.stdout.write(json.dumps({'type': 'a'}) + '\\n')\n"
        "sys.stdout.write('{\"type\": \"partial-no-newline\"}')\n"
        "sys.stdout.flush()\n"
    )
    out = run_with_timeout(_child(script), timeout_s=30, sink=stream.append_line)
    stream.close()

    events = _events_text(tmp_path / "s")
    assert '"type": "a"' in events
    assert "partial-no-newline" not in events  # withheld from the live file
    assert "partial-no-newline" in out.stdout  # but in the end-of-step capture


def test_non_json_line_captured_verbatim_without_parse_gate(tmp_path):
    # FR-2.5: the live writer frames on the newline and appends the raw redacted
    # line without parsing-to-validate; a non-JSON line is captured verbatim and
    # the stream does not error. (Strict end-of-step parsing fails closed
    # elsewhere — see test_claude_adapter::test_stream_json_garbage_line.)
    logger = StepLogger(RedactingWriter(), tmp_path / "s")
    stream = logger.open_stream()
    script = (
        "import sys, json\n"
        "sys.stdout.write('this is not json\\n')\n"
        "sys.stdout.write(json.dumps({'type': 'result', 'result': 'ok'}) + '\\n')\n"
        "sys.stdout.flush()\n"
    )
    out = run_with_timeout(_child(script), timeout_s=30, sink=stream.append_line)
    stream.close()

    lines = _events_text(tmp_path / "s").splitlines()
    assert lines[0] == "this is not json"  # no parse gate on persistence
    assert not out.timed_out


def test_sink_redaction_error_fails_closed(tmp_path):
    # FR-6.2 (process layer): a sink that raises (disk/redaction error) does not
    # silently drop output — the reader kills + drains + re-raises as a
    # StreamSinkError, which the engine records as a failed step.
    from gauntlet.adapters.process import StreamSinkError

    def boom(_line: str) -> None:
        raise RuntimeError("disk full")

    script = (
        "import sys, time, json\n"
        "sys.stdout.write(json.dumps({'type': 'ev'}) + '\\n'); sys.stdout.flush()\n"
        "time.sleep(30)\n"
    )
    with pytest.raises(StreamSinkError):
        run_with_timeout(_child(script), timeout_s=30, sink=boom)


# ==========================================================================
# Layer 2 — adapter wiring + parity (run_with_timeout double)
# ==========================================================================


def _streaming_run_double(events, *, exit_code=0, stderr="", timed_out=False):
    """A ``run_with_timeout`` double: in streaming mode (sink given) it frames
    each NDJSON line and feeds it to the sink — exactly as the real reader does —
    before returning a ProcessOutput that is identical regardless of mode (so
    parity assertions are exact)."""
    stdout = "".join(json.dumps(e) + "\n" for e in events)

    def fake_run(argv, *, timeout_s, stdin_text=None, cwd=None, env=None, sink=None):
        if sink is not None:
            for line in stdout.splitlines(keepends=True):
                sink(line)
        return ProcessOutput(
            argv=list(argv), stdout=stdout, stderr=stderr,
            exit_code=exit_code, duration_s=0.1, timed_out=timed_out,
        )

    return fake_run


CLAUDE_RESULT = {
    "type": "result", "subtype": "success", "is_error": False,
    "result": "GAUNTLET_PONG", "session_id": "sess-1", "total_cost_usd": 0.01,
    "usage": {"input_tokens": 14, "output_tokens": 5},
}
CLAUDE_EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "sess-1"},
    {"type": "assistant", "message": {"content": "thinking"}},
    CLAUDE_RESULT,
]

CODEX_THREAD = "th-1"


def _codex_events(text="codex-pong"):
    return [
        {"type": "thread.started", "thread_id": CODEX_THREAD},
        {"type": "item.completed",
         "item": {"id": "i0", "type": "agent_message", "text": text}},
        {"type": "turn.completed",
         "usage": {"input_tokens": 21, "output_tokens": 3}},
    ]


def test_claude_adapter_streams_through_sink(monkeypatch):
    # FR-7.1: the Claude stream-json adapter threads each NDJSON line to the sink.
    monkeypatch.setattr(
        "gauntlet.adapters.claude_code.run_with_timeout",
        _streaming_run_double(CLAUDE_EVENTS),
    )
    received: list[str] = []
    adapter = ClaudeCodeAdapter(output_format="stream-json")
    # Simulate a fixture-qualified adapter: the real default is False until the
    # live-CLI containment fixture qualifies it (F-001); this test exercises the
    # streaming *mechanism* given qualification, not the shipped default.
    adapter.supports_line_streaming = True
    result = adapter.run("ping", sink=received.append)
    assert result.text == "GAUNTLET_PONG"
    assert [json.loads(line)["type"] for line in received] == ["system", "assistant", "result"]


def test_codex_adapter_streams_through_sink(monkeypatch):
    # FR-7.1: codex exec --json adapter threads each NDJSON line to the sink.
    monkeypatch.setattr(
        "gauntlet.adapters.codex.run_with_timeout",
        _streaming_run_double(_codex_events()),
    )
    received: list[str] = []
    adapter = CodexAdapter()
    # Simulate a fixture-qualified adapter (real default is False, F-001).
    adapter.supports_line_streaming = True
    result = adapter.run("ping", sink=received.append)
    assert result.text == "codex-pong"
    assert [json.loads(line)["type"] for line in received] == [
        "thread.started", "item.completed", "turn.completed"
    ]


def test_claude_buffered_format_never_streams(monkeypatch):
    # The legacy `json` output mode is not message-granular NDJSON; even with a
    # sink it must run buffered (no lines delivered).
    captured: dict = {}

    def wrap(argv, **kw):
        captured["sink"] = kw.get("sink")
        return _streaming_run_double([CLAUDE_RESULT])(argv, **kw)

    monkeypatch.setattr("gauntlet.adapters.claude_code.run_with_timeout", wrap)
    received: list[str] = []
    adapter = ClaudeCodeAdapter(output_format="json")
    # Even a (hypothetically) qualified adapter must not stream the legacy `json`
    # format — prove the format gate blocks it independently of qualification.
    adapter.supports_line_streaming = True
    assert adapter.streams_to_sink() is False
    adapter.run("ping", sink=received.append)
    assert captured["sink"] is None  # the adapter passed None to run_with_timeout
    assert received == []


@pytest.mark.parametrize(
    "make_adapter",
    [
        lambda: ClaudeCodeAdapter(output_format="stream-json"),
        lambda: CodexAdapter(),
    ],
    ids=["claude", "codex"],
)
def test_unqualified_adapter_runs_buffered(monkeypatch, make_adapter):
    # FR-2.8: an adapter NOT declared supports_line_streaming (its containment
    # fixture would not hold) passes None and runs buffered — never streaming a
    # value that might be split across events. This is a static qualification
    # gate, not a mid-stream split detector.
    adapter = make_adapter()
    adapter.supports_line_streaming = False
    module = (
        "gauntlet.adapters.claude_code"
        if isinstance(adapter, ClaudeCodeAdapter)
        else "gauntlet.adapters.codex"
    )
    events = CLAUDE_EVENTS if isinstance(adapter, ClaudeCodeAdapter) else _codex_events()
    captured: dict = {}

    def wrap(argv, **kw):
        captured["sink"] = kw.get("sink")
        return _streaming_run_double(events)(argv, **kw)

    monkeypatch.setattr(f"{module}.run_with_timeout", wrap)
    received: list[str] = []
    assert adapter.streams_to_sink() is False
    adapter.run("ping", sink=received.append)
    assert captured["sink"] is None
    assert received == []


@pytest.mark.parametrize(
    "make_adapter",
    [
        lambda: ClaudeCodeAdapter(output_format="stream-json"),
        lambda: CodexAdapter(),
    ],
    ids=["claude", "codex"],
)
def test_real_adapters_ship_unqualified_and_run_buffered(monkeypatch, make_adapter):
    # FR-2.8 / F-001: the shipped real adapters are NOT yet qualified — no
    # fixture exercises their live NDJSON framing with a planted + split secret —
    # so supports_line_streaming defaults False, streams_to_sink() is False, and
    # even with a sink and stream-json they pass None and run buffered. Flipping
    # the class default back to True without that fixture is a fail-open
    # regression for the secret-splitting case the carryover redactor would catch.
    adapter = make_adapter()
    assert adapter.supports_line_streaming is False
    assert adapter.streams_to_sink() is False

    module = (
        "gauntlet.adapters.claude_code"
        if isinstance(adapter, ClaudeCodeAdapter)
        else "gauntlet.adapters.codex"
    )
    events = CLAUDE_EVENTS if isinstance(adapter, ClaudeCodeAdapter) else _codex_events()
    captured: dict = {}

    def wrap(argv, **kw):
        captured["sink"] = kw.get("sink")
        return _streaming_run_double(events)(argv, **kw)

    monkeypatch.setattr(f"{module}.run_with_timeout", wrap)
    received: list[str] = []
    adapter.run("ping", sink=received.append)
    assert captured["sink"] is None  # buffered path: no sink threaded
    assert received == []


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_secret_value_contained_in_single_event_line(monkeypatch, tmp_path, cli):
    # FR-2.7: a known secret in the agent's streamed output appears within a
    # SINGLE redacted event line, never split across two adjacent events —
    # asserted for both Claude stream-json and Codex --json.
    secret = "containmentsecret12345"
    if cli == "claude":
        events = [
            {"type": "system", "session_id": "sess-1"},
            {**CLAUDE_RESULT, "result": f"here is {secret} done"},
        ]
        monkeypatch.setattr(
            "gauntlet.adapters.claude_code.run_with_timeout",
            _streaming_run_double(events),
        )
        adapter = ClaudeCodeAdapter(output_format="stream-json")
    else:
        events = _codex_events(text=f"here is {secret} done")
        monkeypatch.setattr(
            "gauntlet.adapters.codex.run_with_timeout",
            _streaming_run_double(events),
        )
        adapter = CodexAdapter()

    # Simulate a fixture-qualified adapter (real default is False, F-001); this
    # exercises the containment mechanism given qualification.
    adapter.supports_line_streaming = True
    redactor = Redactor(env={"K_API_KEY": secret})
    logger = StepLogger(RedactingWriter(redactor), tmp_path / "s")
    stream = logger.open_stream()
    adapter.run("ping", sink=stream.append_line)
    stream.close()

    lines = _events_text(tmp_path / "s").splitlines()
    full = "\n".join(lines)
    assert secret not in full  # never on disk raw (FR-2.2)
    # the value was wholly contained in exactly one line — never split (FR-2.7)
    marker_lines = [ln for ln in lines if "[REDACTED:env:K_API_KEY]" in ln]
    assert len(marker_lines) == 1


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_agent_result_parity_buffered_vs_streamed(monkeypatch, cli):
    # FR-2.3 / FR-7.2: the returned AgentResult (text, structured, usage) is
    # identical across buffered and streamed modes — streaming touches only WHEN
    # bytes land, never the authoritative result.
    schema = {
        "type": "object", "properties": {"answer": {"type": "string"}},
        "required": ["answer"], "additionalProperties": False,
    }
    structured_text = '{"answer": "pong"}'
    if cli == "claude":
        events = [
            {"type": "system", "session_id": "sess-1"},
            {**CLAUDE_RESULT, "result": structured_text,
             "structured_output": {"answer": "pong"}},
        ]
        monkeypatch.setattr(
            "gauntlet.adapters.claude_code.run_with_timeout",
            _streaming_run_double(events),
        )
        adapter = ClaudeCodeAdapter(output_format="stream-json")
        adapter.supports_line_streaming = True  # qualify (real default False, F-001)
        buffered = adapter.run("ping", schema=schema)
        streamed = adapter.run("ping", schema=schema, sink=lambda _l: None)
    else:
        # Codex reads its authoritative text from --output-last-message; the
        # double does not write that file, so structured comes from events text.
        events = _codex_events(text=structured_text)
        monkeypatch.setattr(
            "gauntlet.adapters.codex.run_with_timeout",
            _streaming_run_double(events),
        )
        adapter = CodexAdapter()
        adapter.supports_line_streaming = True  # qualify (real default False, F-001)
        buffered = adapter.run("ping", schema=schema)
        streamed = adapter.run("ping", schema=schema, sink=lambda _l: None)

    assert streamed.text == buffered.text
    assert streamed.structured == buffered.structured == {"answer": "pong"}
    assert streamed.usage == buffered.usage
    assert streamed.session_id == buffered.session_id
    assert streamed.raw_events == buffered.raw_events


def test_events_jsonl_final_render_identical_across_modes(monkeypatch, tmp_path):
    # The persisted events.jsonl ends byte-identical across modes: log_result
    # truncates and rewrites the authoritative events regardless of any live
    # content streamed during the step.
    monkeypatch.setattr(
        "gauntlet.adapters.claude_code.run_with_timeout",
        _streaming_run_double(CLAUDE_EVENTS),
    )
    adapter = ClaudeCodeAdapter(output_format="stream-json")
    adapter.supports_line_streaming = True  # qualify (real default False, F-001)

    buf_logger = StepLogger(RedactingWriter(), tmp_path / "buffered")
    buf_logger.log_result(adapter.run("ping"))
    buffered_jsonl = (tmp_path / "buffered" / "events.jsonl").read_text()

    str_logger = StepLogger(RedactingWriter(), tmp_path / "streamed")
    stream = str_logger.open_stream()
    streamed_result = adapter.run("ping", sink=stream.append_line)
    stream.close()
    assert (tmp_path / "streamed" / "events.jsonl").read_text() != ""  # grew live
    str_logger.log_result(streamed_result)  # end-of-step authoritative render
    streamed_jsonl = (tmp_path / "streamed" / "events.jsonl").read_text()

    assert streamed_jsonl == buffered_jsonl


# ==========================================================================
# Layer 3 — config gate + engine wiring
# ==========================================================================


def test_stream_step_output_defaults_false_and_round_trips(tmp_path):
    # FR-6.1: the flag is default-off, an omitted field deserializes to False,
    # and `true` round-trips through the YAML load/dump path.
    assert RunConfig.model_validate({}).stream_step_output is False
    assert RunConfig.model_validate(
        {"reviewer_mutation": "commit"}
    ).stream_step_output is False

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"stream_step_output": True}))
    loaded = RunConfig.load(path)
    assert loaded.stream_step_output is True
    # dump → reload preserves the value
    reloaded = RunConfig.model_validate(yaml.safe_load(yaml.safe_dump(loaded.model_dump())))
    assert reloaded.stream_step_output is True


class _StreamingFakeAdapter:
    """A line-streamable fake: declares streams_to_sink and feeds the sink."""

    def __init__(self, *, lines, supports=True):
        self._lines = lines
        self.supports_line_streaming = supports
        self.calls: list[dict] = []

    def streams_to_sink(self) -> bool:
        return self.supports_line_streaming

    def run(self, prompt, *, session=None, schema=None, cwd=None,
            extra_flags=None, sink=None):
        self.calls.append({"sink": sink})
        if sink is not None:
            for line in self._lines:
                sink(line)
        return AgentResult(text="ok", session_id="s", exit_code=0)


def test_engine_opens_stream_only_for_qualified_adapter(tmp_path):
    # FR-2.1 / FR-6.1 (engine): open_step_stream returns a stream only when the
    # flag is on AND the adapter declares itself line-streamable.
    logger = StepLogger(RedactingWriter(), tmp_path / "s")
    streamable = _StreamingFakeAdapter(lines=['{"type": "ev"}\n'])

    off = RunConfig.model_validate({"stream_step_output": False})
    on = RunConfig.model_validate({"stream_step_output": True})

    class _Ctx:
        def __init__(self, config):
            self.config = config

    assert open_step_stream(_Ctx(off), streamable, logger) is None  # flag off
    # API adapter (no streams_to_sink) → never opens a stream, even flag-on.
    from gauntlet.adapters.api import ApiAdapter
    assert open_step_stream(_Ctx(on), ApiAdapter(model="m"), logger) is None
    # unqualified CLI adapter (FR-2.8) → no stream
    unq = _StreamingFakeAdapter(lines=[], supports=False)
    assert open_step_stream(_Ctx(on), unq, logger) is None
    # qualified + flag on → a stream
    stream = open_step_stream(_Ctx(on), streamable, logger)
    assert stream is not None
    stream.close()


def test_handle_agent_task_streams_when_enabled(fixture_repo):
    # FR-2.1 / FR-6.1 (engine end-to-end): with the flag on and a line-streamable
    # adapter, handle_agent_task threads the sink and events.jsonl grows live.
    import yaml as _yaml

    from gauntlet.engine import manifest as M
    from gauntlet.engine.manifest import Manifest, PipelineRef
    from gauntlet.engine.orchestrator import Orchestrator
    from gauntlet.engine.pipeline import Pipeline

    adapter = _StreamingFakeAdapter(
        lines=['{"type": "system"}\n', '{"type": "ev", "i": 1}\n']
    )
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go, repo_write: false}
"""
    cfg = RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "claude-code"}}, "stream_step_output": True}
    )
    pipeline = Pipeline.model_validate(_yaml.safe_load(text))
    ar = fixture_repo / "runs" / "demo"
    rd = ar / "run-1"
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    orch = Orchestrator(
        repo_root=fixture_repo, run_dir=rd, artifact_root=ar, config=cfg,
        pipeline=pipeline, manifest=man, adapter_factory=lambda n: adapter,
    )
    assert orch.drive() == M.RUN_DONE
    assert adapter.calls[0]["sink"] is not None  # the sink was threaded
    # the authoritative end-of-step render is on disk (one event: text fake → 0
    # raw_events, so events.jsonl is empty; the streamed sink proves wiring).
    assert (rd / "steps" / "implement" / "events.jsonl").exists()


def test_flag_off_runs_buffered_no_sink(fixture_repo):
    # FR-6.1: with the flag off, no sink is threaded — the buffered call shape is
    # exactly today's, and existing (non-sink) adapters are untouched.
    from conftest import FakeAdapter

    import yaml as _yaml

    from gauntlet.engine import manifest as M
    from gauntlet.engine.manifest import Manifest, PipelineRef
    from gauntlet.engine.orchestrator import Orchestrator
    from gauntlet.engine.pipeline import Pipeline

    adapter = FakeAdapter(text="ok")  # run() has no `sink` kwarg
    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go, repo_write: false}
"""
    cfg = RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "claude-code"}}, "stream_step_output": False}
    )
    pipeline = Pipeline.model_validate(_yaml.safe_load(text))
    ar = fixture_repo / "runs" / "demo"
    rd = ar / "run-1"
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    orch = Orchestrator(
        repo_root=fixture_repo, run_dir=rd, artifact_root=ar, config=cfg,
        pipeline=pipeline, manifest=man, adapter_factory=lambda n: adapter,
    )
    # No TypeError despite FakeAdapter.run not accepting `sink` — proof the
    # buffered call passes no sink kwarg.
    assert orch.drive() == M.RUN_DONE
    assert adapter.calls  # ran


def test_handle_agent_task_sink_fault_fails_step_closed(fixture_repo):
    # FR-6.2 (engine): a streaming sink fault surfaces as a failed step, never a
    # silent success. A fake adapter raises StreamSinkError (as the real reader
    # would on a redaction/disk error during streaming).
    import yaml as _yaml

    from gauntlet.adapters.process import StreamSinkError
    from gauntlet.engine import manifest as M
    from gauntlet.engine.manifest import Manifest, PipelineRef
    from gauntlet.engine.orchestrator import Orchestrator
    from gauntlet.engine.pipeline import Pipeline

    class _FaultingAdapter:
        supports_line_streaming = True

        def streams_to_sink(self):
            return True

        def run(self, prompt, *, session=None, schema=None, cwd=None,
                extra_flags=None, sink=None):
            raise StreamSinkError("streaming sink failed while persisting a line")

    text = """
name: demo
version: 1
stages:
  - id: s
    steps:
      - {id: implement, type: agent_task, agent: builder, prompt_text: go, repo_write: false}
"""
    cfg = RunConfig.model_validate(
        {"agents": {"builder": {"adapter": "claude-code"}}, "stream_step_output": True}
    )
    pipeline = Pipeline.model_validate(_yaml.safe_load(text))
    ar = fixture_repo / "runs" / "demo"
    rd = ar / "run-1"
    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
                   pipeline=PipelineRef(name="demo", version=1, hash="h"))
    orch = Orchestrator(
        repo_root=fixture_repo, run_dir=rd, artifact_root=ar, config=cfg,
        pipeline=pipeline, manifest=man, adapter_factory=lambda n: _FaultingAdapter(),
    )
    assert orch.drive() == M.RUN_FAILED
    assert "handler error" in orch.manifest.record("implement").notes

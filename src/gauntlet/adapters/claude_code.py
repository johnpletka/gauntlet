"""ClaudeCodeAdapter: drives `claude -p` headlessly (PRD §4.1).

Verified against claude 2.1.190 (see the doctor pin file): `--output-format
json|stream-json`, `--resume <session>`, `--model`, `--effort`,
`--append-system-prompt`, `--allowedTools`/`--disallowedTools`, `--tools`
(empty string disables all tools), `--permission-mode`, and native structured
output via `--json-schema`. Permission-bypass flags are rejected by the §8
lint, never merely avoided.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gauntlet.adapters._structured import extract_json, validate_schema
from gauntlet.adapters.base import (
    AdapterCapabilities,
    AdapterError,
    AgentFailedError,
    AgentResult,
    AgentTimeoutError,
    MalformedOutputError,
    SessionNotFoundError,
    Usage,
)
from gauntlet.adapters.failure_markers import (
    classify_claude_failure,
    looks_like_session_not_found,
)
from gauntlet.adapters.process import ProcessOutput, run_with_timeout
from gauntlet.config import lint_flags

DEFAULT_TIMEOUT_S = 600.0


class ClaudeCodeAdapter:
    name = "claude-code"
    # Live-streaming qualification (live-run-observability FR-2.7/FR-2.8). claude
    # `--output-format stream-json` is *expected* to emit one whole JSON event per
    # line (message-granular), which would make any sensitive value land wholly
    # inside a single newline-framed line (the per-line redaction unit). But the
    # qualification gate is fail-closed: this flag may be flipped to True ONLY
    # after a fixture-based containment test (FR-2.7) exercises this adapter's
    # *real* NDJSON framing — pinned captured raw streams from the verified CLI
    # version — with a planted secret AND a split-secret negative case (FR-2.8).
    # The current P2 suite proves only the synthetic streaming double, not the
    # live `claude --output-format stream-json` contract, so this adapter is NOT
    # yet qualified and must stay False: streams_to_sink() returns False and the
    # buffered path is used, closing the fail-open secret-splitting case the
    # carryover redactor (deferred) would otherwise be needed to catch.
    supports_line_streaming = False
    capabilities = AdapterCapabilities(
        repo_write=True,
        # claude 2.1.172 grew a native --json-schema flag; the PRD assumed
        # best-effort. Verified by the contract suite and pinned.
        structured_output="native",
        resume=True,
        reads_repo=True,  # FR-1.3: the CLI runs in the repo and can Read files
    )

    # P7f: this CLI applies its OWN permission rules after Gauntlet's PreToolUse
    # hook, and refuses writes to any path carrying a literal `.git` segment —
    # non-uniformly across write mechanisms, so the failure is model-dependent
    # and invisible to the judge audit (`proposals/P7d-gate-blocker.md` §2).
    # This is the adapter the writability preflight exists for.
    probes_writability = True

    @staticmethod
    def writability_flags(tools: tuple[str, ...]) -> list[str]:
        """Narrow the tool set for one probe turn, so the model has no choice.

        The probe's determinism rests first on the prompt naming one mechanism
        and forbidding the others; this is the enforcement behind it, and it is
        adapter-specific CLI knowledge, which is why it lives here rather than
        in the probe.
        """
        return ["--allowedTools", ",".join(tools)]

    def __init__(
        self,
        *,
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        tools: list[str] | None = None,
        append_system_prompt: str | None = None,
        output_format: str = "json",
        executable: str = "claude",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        base_flags: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if output_format not in ("json", "stream-json"):
            raise ValueError(f"unsupported output_format {output_format!r}")
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        self.tools = tools
        self.append_system_prompt = append_system_prompt
        self.output_format = output_format
        self.executable = executable
        self.timeout_s = timeout_s
        self.base_flags = list(base_flags or [])
        # A fully-rebuilt child environment (pipeline-effectiveness FR-2.5, P5): the
        # behavioral verifier spawns claude from an explicit allowlist with every
        # secret/token/credential var stripped by construction (plus the run's judge
        # env so the PreToolUse hook fires). ``None`` (every non-verifier claude
        # step) inherits the parent environment exactly as before — additive knob.
        self.env = dict(env) if env is not None else None
        # Optional child preexec hook (PR #59 §7 item 5): the verifier posture
        # (verify.configure_claude_verifier) sets best-effort rlimit caps here;
        # ``None`` (every non-verifier claude step) spawns exactly as before.
        self.spawn_preexec = None
        lint_flags(self._build_argv("", session=None, schema=None))

    def streams_to_sink(self) -> bool:
        """Whether a provided line ``sink`` is used (live-run-observability
        FR-2.7/FR-2.8). Only ``stream-json`` is message-granular NDJSON; the
        legacy ``json`` mode emits one large object, not lines, so it is never
        streamed. Gated on :attr:`supports_line_streaming` so an unqualified
        adapter falls back to the buffered path rather than risk a value split
        across events (FR-2.8)."""
        return self.output_format == "stream-json" and self.supports_line_streaming

    def run(
        self,
        prompt: str,
        *,
        session: str | None = None,
        schema: dict | None = None,
        cwd: Path | None = None,
        extra_flags: list[str] | None = None,
        sink: Callable[[str], None] | None = None,
    ) -> AgentResult:
        argv = self._build_argv(prompt, session=session, schema=schema)
        argv += extra_flags or []
        lint_flags(argv)
        # Stream only when a sink is provided AND this adapter is qualified for
        # per-line streaming in its current output mode (FR-2.7/FR-2.8);
        # otherwise pass None and the buffered communicate() path runs unchanged.
        effective_sink = sink if (sink is not None and self.streams_to_sink()) else None
        out = run_with_timeout(
            argv, timeout_s=self.timeout_s, stdin_text=prompt, cwd=cwd,
            sink=effective_sink, env=self.env, preexec_fn=self.spawn_preexec,
        )
        if out.timed_out:
            raise AgentTimeoutError(
                f"claude killed after {self.timeout_s}s timeout",
                partial=self._partial_result(out),
            )
        result = self._parse(out, schema=schema, session=session)
        return result

    # -- command construction -------------------------------------------------

    def _build_argv(
        self, prompt: str, *, session: str | None, schema: dict | None
    ) -> list[str]:
        argv = [self.executable, "-p", "--output-format", self.output_format]
        if self.output_format == "stream-json":
            argv.append("--verbose")  # required by claude for stream-json in -p mode
        if self.model:
            argv += ["--model", self.model]
        if self.effort:
            argv += ["--effort", self.effort]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        if self.allowed_tools is not None:
            argv += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.disallowed_tools is not None:
            argv += ["--disallowedTools", ",".join(self.disallowed_tools)]
        if self.tools is not None:
            argv += ["--tools", ",".join(self.tools)]  # "" disables all tools
        if self.append_system_prompt:
            argv += ["--append-system-prompt", self.append_system_prompt]
        if session:
            argv += ["--resume", session]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]
        argv += self.base_flags
        return argv

    # -- output parsing --------------------------------------------------------

    def _parse(
        self, out: ProcessOutput, *, schema: dict | None, session: str | None = None
    ) -> AgentResult:
        events = self._decode_events(out, strict=True)
        result_event = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        partial = AgentResult(
            text=(result_event or {}).get("result") or "",
            session_id=(result_event or {}).get("session_id")
            or next((e["session_id"] for e in events if e.get("session_id")), None),
            usage=self._extract_usage(result_event or {}),
            raw_events=events,
            exit_code=out.exit_code,
        )
        # Fail closed on reported failure, even when output parses (F-001).
        failure = self._failure_marker(out, result_event)
        if failure:
            # FR-3.1: classify transient-vs-terminal from the structured result
            # event so the engine can park-and-resume a usage-limit/overload hit
            # instead of failing the step. FR-3.3: a resume whose session the CLI
            # no longer knows falls back to a full re-run — raise the dedicated
            # error so the handler can retry with no session (best-effort, only
            # when a session was requested and the failure is not itself transient).
            failure_info = classify_claude_failure(result_event, out.exit_code)
            if not failure_info.is_transient and session and looks_like_session_not_found(
                (result_event or {}).get("result")
            ):
                raise SessionNotFoundError(
                    f"claude could not resume session {session}: {failure}",
                    partial=partial,
                )
            raise AgentFailedError(
                f"claude reported failure: {failure}; stderr: {out.stderr[:500]}",
                partial=partial,
                failure_info=failure_info,
            )
        if result_event is None:
            raise MalformedOutputError(
                f"no result event in claude output (exit {out.exit_code}); "
                f"stderr: {out.stderr[:500]}",
                partial=partial,
            )
        text = result_event.get("result") or ""
        structured = self._extract_structured(result_event, text, schema)
        return partial.model_copy(update={"text": text, "structured": structured})

    @staticmethod
    def _failure_marker(out: ProcessOutput, result_event: dict | None) -> str | None:
        if out.exit_code != 0:
            return f"exit code {out.exit_code}"
        if result_event is not None:
            if result_event.get("is_error"):
                return f"is_error=true (subtype: {result_event.get('subtype')!r})"
            subtype = result_event.get("subtype") or ""
            if subtype.startswith("error"):
                return f"subtype {subtype!r}"
        return None

    def _decode_events(
        self, out: ProcessOutput, *, strict: bool
    ) -> list[dict[str, Any]]:
        if self.output_format == "json":
            try:
                obj = json.loads(out.stdout)
            except json.JSONDecodeError as exc:
                raise MalformedOutputError(
                    f"claude --output-format json did not return JSON: {exc}; "
                    f"stdout head: {out.stdout[:500]!r}",
                    partial=self._raw_partial(out),
                ) from exc
            return obj if isinstance(obj, list) else [obj]
        events: list[dict[str, Any]] = []
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                # Fail closed (F-002): with stream-json, logs go to stderr, so
                # a non-JSON stdout line means the output contract broke.
                # Lenient mode is only for building checkpointable partials.
                if strict:
                    raise MalformedOutputError(
                        f"non-JSON line in claude stream-json output: {line[:200]!r}",
                        partial=self._raw_partial(out),
                    ) from exc
                events.append({"type": "gauntlet.unparsed_line", "line": line})
        return events

    def _extract_structured(
        self, result_event: dict, text: str, schema: dict | None
    ) -> Any | None:
        if schema is None:
            return None
        # claude 2.1.x surfaces --json-schema output as `structured_output`
        # on the result event; fall back to parsing the result text.
        structured = result_event.get("structured_output")
        if structured is None:
            try:
                structured = extract_json(text)
            except ValueError as exc:
                raise MalformedOutputError(
                    f"schema requested but result is not parseable JSON: {exc}",
                    partial=self._partial_from_event(result_event),
                ) from exc
        try:
            validate_schema(structured, schema)
        except ValueError as exc:
            raise MalformedOutputError(
                str(exc), partial=self._partial_from_event(result_event)
            ) from exc
        return structured

    @staticmethod
    def _extract_usage(result_event: dict) -> Usage | None:
        usage = result_event.get("usage") or {}
        cost = result_event.get("total_cost_usd")
        if not usage and cost is None:
            return None
        cached = usage.get("cache_read_input_tokens")
        return Usage(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cached_input_tokens=cached,
            cost_usd=cost,  # None => tokens-only degraded path (PRD §12 Q3)
        )

    def _partial_result(
        self, out: ProcessOutput, events: list[dict[str, Any]] | None = None
    ) -> AgentResult:
        if events is None:
            try:
                events = self._decode_events(out, strict=False)
            except AdapterError:
                events = [{"type": "gauntlet.raw_stdout", "stdout": out.stdout}]
        return AgentResult(
            text="",
            raw_events=events
            + [{"type": "gauntlet.stderr", "stderr": out.stderr}],
            exit_code=out.exit_code if out.exit_code is not None else -1,
        )

    @staticmethod
    def _raw_partial(out: ProcessOutput) -> AgentResult:
        return AgentResult(
            text="",
            raw_events=[
                {"type": "gauntlet.raw_stdout", "stdout": out.stdout},
                {"type": "gauntlet.stderr", "stderr": out.stderr},
            ],
            exit_code=out.exit_code if out.exit_code is not None else -1,
        )

    @staticmethod
    def _partial_from_event(result_event: dict) -> AgentResult:
        return AgentResult(
            text=result_event.get("result") or "",
            session_id=result_event.get("session_id"),
            raw_events=[result_event],
            exit_code=0,
        )

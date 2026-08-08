"""CodexAdapter: drives `codex exec` non-interactively (PRD §4.1).

Verified against codex-cli 0.139.0 (see the doctor pin file): `--json` JSONL
events, `--output-schema <file>`, `-o/--output-last-message`, `-s/--sandbox
read-only|workspace-write`, prompt via stdin with `-`, and `codex exec resume
<session>`. Note: 0.139.0 has no `--full-auto` on `exec` (the PRD/plan mention
it); exec mode is already non-interactive and the sandbox flag governs write
access — recorded in the pin file and BOOTSTRAP-NOTES.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gauntlet.adapters._structured import validate_schema
from gauntlet.adapters.base import (
    AdapterCapabilities,
    AgentFailedError,
    AgentResult,
    AgentTimeoutError,
    MalformedOutputError,
    SessionNotFoundError,
    Usage,
)
from gauntlet.adapters.failure_markers import (
    classify_codex_failure,
    looks_like_session_not_found,
)
from gauntlet.adapters.process import ProcessOutput, run_with_timeout
from gauntlet.config import lint_flags

DEFAULT_TIMEOUT_S = 600.0


class CodexAdapter:
    name = "codex"
    # Live-streaming qualification (live-run-observability FR-2.7/FR-2.8). `codex
    # exec --json` is *expected* to be message-granular NDJSON — one whole JSON
    # event per line — which would make a sensitive value land wholly inside a
    # single newline-framed line (the per-line redaction unit). But the
    # qualification gate is fail-closed: this flag may be flipped to True ONLY
    # after a fixture-based containment test (FR-2.7) exercises this adapter's
    # *real* `codex exec --json` framing — pinned captured raw streams from the
    # verified CLI version — with a planted secret AND a split-secret negative
    # case (FR-2.8). The current P2 suite proves only the synthetic streaming
    # double, not the live CLI contract, so this adapter is NOT yet qualified and
    # must stay False: streams_to_sink() returns False and the buffered path is
    # used, closing the fail-open secret-splitting case the carryover redactor
    # (deferred) would otherwise be needed to catch.
    supports_line_streaming = False
    capabilities = AdapterCapabilities(
        repo_write=True, structured_output="native", resume=True,
        reads_repo=True,  # FR-1.3: `codex exec` runs in the repo and can read files
        # The codex app-server rejects any `turn/start` over 1 MiB of input
        # wholesale (`input_too_large`, max_chars=1048576 — observed live on the
        # clerk-auth P3 review). Declared so prompt builders can hand oversize
        # payloads by reference instead of losing the whole invocation.
        max_input_chars=1_048_576,
    )

    # P7f: probed, and probed SEPARATELY. `codex exec --sandbox workspace-write`
    # was measured NOT to be subject to the `.git`-segment guard that blocks
    # `claude` (§2.5 probe 2) — which is exactly why the preflight must never
    # generalize one adapter's verdict to another. A codex-only check would have
    # called the P7d dogfood's tree healthy while every claude builder and fixer
    # step was silently refused, which is the shape that failure actually had.
    probes_writability = True

    @staticmethod
    def writability_flags(tools: tuple[str, ...]) -> list[str]:
        """No per-turn tool narrowing: `codex exec`'s sandbox flags already bound
        what it may touch, and it has no `--allowedTools` equivalent. The probe's
        determinism here rests on the prompt naming one mechanism and on the
        verdict being the specific file that mechanism was told to produce."""
        return []

    def __init__(
        self,
        *,
        model: str | None = None,
        reasoning_effort: str | None = None,
        sandbox: str = "read-only",
        skip_git_repo_check: bool = False,
        executable: str = "codex",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        base_flags: list[str] | None = None,
    ) -> None:
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.sandbox = sandbox
        self.skip_git_repo_check = skip_git_repo_check
        self.executable = executable
        self.timeout_s = timeout_s
        self.base_flags = list(base_flags or [])
        lint_flags(self._build_argv(session=None, schema_path=None, output_path=None))

    def streams_to_sink(self) -> bool:
        """Whether a provided line ``sink`` is used (live-run-observability
        FR-2.7/FR-2.8). `codex exec --json` is always message-granular NDJSON, so
        the only gate is :attr:`supports_line_streaming` — an unqualified adapter
        falls back to the buffered path rather than risk a value split across
        events (FR-2.8)."""
        return self.supports_line_streaming

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
        with tempfile.TemporaryDirectory(prefix="gauntlet-codex-") as tmp:
            schema_path: Path | None = None
            if schema is not None:
                schema_path = Path(tmp) / "output-schema.json"
                schema_path.write_text(json.dumps(schema))
            output_path = Path(tmp) / "last-message.txt"
            argv = self._build_argv(
                session=session, schema_path=schema_path, output_path=output_path
            )
            argv += extra_flags or []
            argv.append("-")  # prompt on stdin
            lint_flags(argv)
            # Stream only when a sink is provided AND this adapter is qualified
            # (FR-2.7/FR-2.8); otherwise pass None and the buffered path runs.
            effective_sink = (
                sink if (sink is not None and self.streams_to_sink()) else None
            )
            out = run_with_timeout(
                argv, timeout_s=self.timeout_s, stdin_text=prompt, cwd=cwd,
                sink=effective_sink,
            )
            last_message = (
                output_path.read_text() if output_path.exists() else None
            )
        if out.timed_out:
            raise AgentTimeoutError(
                f"codex killed after {self.timeout_s}s timeout",
                partial=self._partial_result(out),
            )
        return self._parse(
            out, schema=schema, last_message=last_message, session=session
        )

    # -- command construction -------------------------------------------------

    def _build_argv(
        self,
        *,
        session: str | None,
        schema_path: Path | None,
        output_path: Path | None,
    ) -> list[str]:
        argv = [self.executable, "exec"]
        if session:
            argv += ["resume", session]
        argv.append("--json")
        if self.model:
            argv += ["--model", self.model]
        if self.reasoning_effort:
            argv += ["-c", f'model_reasoning_effort="{self.reasoning_effort}"']
        if session:
            # `exec resume` accepts no --sandbox flag (verified on 0.139.0);
            # the config-override spelling keeps the sandbox pinned on resume.
            argv += ["-c", f'sandbox_mode="{self.sandbox}"']
        else:
            argv += ["--sandbox", self.sandbox]
        if self.skip_git_repo_check:
            argv.append("--skip-git-repo-check")
        if schema_path is not None:
            argv += ["--output-schema", str(schema_path)]
        if output_path is not None:
            argv += ["--output-last-message", str(output_path)]
        argv += self.base_flags
        return argv

    # -- output parsing --------------------------------------------------------

    def _parse(
        self,
        out: ProcessOutput,
        *,
        schema: dict | None,
        last_message: str | None,
        session: str | None = None,
    ) -> AgentResult:
        events = self._decode_events(out.stdout, strict=True, out=out)
        # Fail closed on reported failure, even when output parses (F-001).
        failure = self._failure_marker(out, events)
        if failure:
            # FR-3.1 classification + FR-3.3 session-not-found fallback, mirroring
            # the claude adapter: a transient usage-limit/overload parks-and-
            # resumes; a resume against a session codex no longer knows falls back
            # to a full re-run (only when a session was requested and the failure
            # is not itself transient).
            failure_info = classify_codex_failure(events, out.exit_code)
            failed_event = next(
                (e for e in events if e.get("type") in ("turn.failed", "error")), None
            )
            err = (failed_event or {}).get("error")
            err_msg = err.get("message") if isinstance(err, dict) else (
                err if isinstance(err, str) else (failed_event or {}).get("message")
            )
            if not failure_info.is_transient and session and looks_like_session_not_found(err_msg):
                raise SessionNotFoundError(
                    f"codex could not resume session {session}: {failure}",
                    partial=self._partial_result(out),
                )
            raise AgentFailedError(
                f"codex reported failure: {failure}; stderr: {out.stderr[:500]}",
                partial=self._partial_result(out),
                failure_info=failure_info,
            )
        text = last_message if last_message is not None else self._last_agent_message(events)
        if text is None:
            raise MalformedOutputError(
                f"no agent message in codex output (exit {out.exit_code}); "
                f"stderr: {out.stderr[:500]}",
                partial=self._partial_result(out),
            )
        structured: Any | None = None
        if schema is not None:
            try:
                structured = json.loads(text)
                validate_schema(structured, schema)
            except (json.JSONDecodeError, ValueError) as exc:
                raise MalformedOutputError(
                    f"codex --output-schema response failed to parse/validate: {exc}",
                    partial=AgentResult(
                        text=text,
                        session_id=self._thread_id(events),
                        raw_events=events,
                        exit_code=out.exit_code,
                    ),
                ) from exc
        return AgentResult(
            text=text,
            structured=structured,
            session_id=self._thread_id(events),
            usage=self._extract_usage(events),
            raw_events=events,
            exit_code=out.exit_code,
        )

    def _decode_events(
        self, stdout: str, *, strict: bool, out: ProcessOutput | None = None
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                # Fail closed (F-002): with --json, codex logs go to stderr,
                # so a non-JSON stdout line means the event contract broke.
                # Lenient mode is only for building checkpointable partials.
                if strict:
                    raise MalformedOutputError(
                        f"non-JSON line in codex --json output: {line[:200]!r}",
                        partial=self._partial_result(out)
                        if out is not None
                        else None,
                    ) from exc
                events.append({"type": "gauntlet.unparsed_line", "line": line})
                continue
            if not isinstance(obj, dict):
                # Valid JSON but not an event object (a bare string/number/null
                # line): the event contract is just as broken as non-JSON, and
                # downstream consumers (.get() everywhere) require dicts.
                if strict:
                    raise MalformedOutputError(
                        f"non-object JSON line in codex --json output: "
                        f"{line[:200]!r}",
                        partial=self._partial_result(out)
                        if out is not None
                        else None,
                    )
                events.append({"type": "gauntlet.unparsed_line", "line": line})
                continue
            events.append(obj)
        return events

    @staticmethod
    def _failure_marker(
        out: ProcessOutput, events: list[dict[str, Any]]
    ) -> str | None:
        if out.exit_code != 0:
            return f"exit code {out.exit_code}"
        for event in events:
            if event.get("type") in ("turn.failed", "error"):
                detail = event.get("error") or event.get("message") or event
                return f"{event['type']} event: {str(detail)[:300]}"
        return None

    @staticmethod
    def _thread_id(events: list[dict[str, Any]]) -> str | None:
        for event in events:
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return event["thread_id"]
        return None

    @staticmethod
    def _last_agent_message(events: list[dict[str, Any]]) -> str | None:
        for event in reversed(events):
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    return item["text"]
        return None

    @staticmethod
    def _extract_usage(events: list[dict[str, Any]]) -> Usage | None:
        totals: dict[str, int] = {}
        for event in events:
            if event.get("type") == "turn.completed" and event.get("usage"):
                for key, value in event["usage"].items():
                    if isinstance(value, int):
                        totals[key] = totals.get(key, 0) + value
        if not totals:
            return None
        # codex reports tokens but no cost: degraded path per PRD §12 Q3.
        return Usage(
            input_tokens=totals.get("input_tokens"),
            output_tokens=totals.get("output_tokens"),
            cached_input_tokens=totals.get("cached_input_tokens"),
            cost_usd=None,
        )

    def _partial_result(self, out: ProcessOutput) -> AgentResult:
        events = self._decode_events(out.stdout, strict=False)
        return AgentResult(
            text=self._last_agent_message(events) or "",
            session_id=self._thread_id(events),
            usage=self._extract_usage(events),
            raw_events=events
            + [{"type": "gauntlet.stderr", "stderr": out.stderr}],
            exit_code=out.exit_code if out.exit_code is not None else -1,
        )

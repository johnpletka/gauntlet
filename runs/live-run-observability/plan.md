I now have enough grounding. Here is the plan.

---

# Implementation Plan: Live run observability (streamed step output)

**PRD:** `prd.md` (Draft v0.2, live-run-observability)
**Author:** builder agent
**Relationship:** Extends `operator-aids` (`logs`, `status --json`); amends no approved artifact. Sequenced after operator-aids.

---

## Orientation: what the code does today

The plan is shaped by four facts about the current tree, all verified against source:

1. **`run_with_timeout` (`adapters/process.py`) is the single choke point.** Every CLI invocation drains the child with `proc.communicate(input=stdin_text, timeout=timeout_s)` — one blocking call that buffers all stdout/stderr until exit, then returns a `ProcessOutput(stdout, stderr, exit_code, duration_s, timed_out)`. On timeout it `killpg`s the session and does a second `communicate()` to collect partial output. This is the *only* place stdin-feed, concurrent stdout+stderr drain, and timeout+kill are reconciled. Replacing it is the load-bearing risk (§1.3).

2. **Both CLI adapters already line-frame NDJSON, but only at end of step.** `ClaudeCodeAdapter._decode_events` and `CodexAdapter._decode_events` both do `for line in out.stdout.splitlines()` → `json.loads(line)`, strict at the end-of-step parse. They call `run_with_timeout(argv, timeout_s=…, stdin_text=prompt, cwd=cwd)` and then `_parse(out)`. The adapters never see bytes until the process exits. The end-of-step parse, usage extraction, `--output-last-message`, `--output-schema`, and structured-output paths are all downstream of `out.stdout` and must stay byte-identical.

3. **The redactor is already per-line and the live consumer is already built.** `RedactingWriter.append_line` / `append_jsonl` (`logging/redact.py`) redact one complete line before it touches disk — the exact unit streaming needs. On the consumer side `store.step_log` is offset-based with `events.jsonl` already in `ALLOWED_LOG_NAMES`, and `sse.py::log_tail_stream` emits `append` events from a growing file. Both are untouched-but-waiting.

4. **The engine — not the adapter — owns the `StepLogger`.** `handle_agent_task` (`engine/steptypes.py:213-230`) builds the logger via `step_logger(ctx)`, calls `adapter.run(...)`, then `logger.log_result(result)`. `cycle.py` mirrors this. So a live sink must be created by the engine and threaded *into* `adapter.run` → `run_with_timeout`; the adapter chooses whether to use it based on its output mode and the flag.

These four facts dictate the phase order: the reader (fact 1) must be proven in isolation before anything writes through it (facts 2–4 depend on it), which is exactly the PRD's riskiest-first ordering.

---

## Ground rules for every phase

- **Flag default-off for all of v1.** A new `RunConfig` field `stream_step_output: bool = False` (sits naturally beside `interrupted_step`, `reviewer_mutation`, etc. in `engine/config.py`) gates the streaming path. Off ⇒ today's exact `communicate()` path, bit-for-bit. Every phase ships it default-off and validates its behavior with the flag **explicitly enabled in tests**. Flipping the default is **out of scope** (PRD OQ-5) and is never an acceptance criterion of any phase here.
- **Fail closed.** A sink write error or redaction error fails the step; it never degrades to silent or un-redacted output (FR-6.2). The buffered path is the always-available fallback.
- **Parity is the contract.** Streaming changes *when* bytes land, never *what* the `AgentResult`/`transcript.md`/`structured.json`/usage are. Each phase that touches the hot path carries a parity assertion against the buffered path.
- **Each phase ends green (`uv run pytest`) and a single commit** (FR-9.2). Phases are strictly sequential; no phase depends on work a later phase delivers (FR-10.3).
- **No approved artifact is amended.** operator-aids' `logs` keeps its one-shot dump; this plan only *adds* `--follow`. `status --json`'s existing fields are untouched; freshness is purely additive.

---

## P1 — Deadlock-safe incremental reader in `run_with_timeout`

**Assumption it validates (the load-bearing one, §1.3):** we can give up `communicate()` and re-earn its bundled guarantees — concurrent stdout+stderr drain, concurrent stdin feed, hard timeout + `killpg` with partial capture — in a hand-rolled incremental reader that frames on `\n` and hands complete lines to a sink, with **field-for-field `ProcessOutput` parity** and **no deadlock**. Everything else depends on this; if it fails, the feature does not ship.

**Deliverables**
- `run_with_timeout` gains an optional `sink: Callable[[str], None] | None = None` parameter. When `sink is None` (or the flag is off, which the adapters honor by passing `None`), behavior is **exactly today's `communicate()` path** — unchanged code, unchanged tests.
- When a `sink` is provided, a `selectors`-based reader loop:
  - registers stdout + stderr for read and drains **both** concurrently (finite pipe buffers deadlock if either is neglected);
  - frames stdout on `\n`, invoking `sink(line)` once per **complete** (newline-terminated) line, in arrival order;
  - **decodes at the line boundary, never per chunk.** Raw stdout bytes accumulate until a complete newline-terminated line is available; only then is that whole line decoded to `str` with the **same encoding and `errors` policy as the buffered `text=True` path** (so a multi-byte UTF-8 character split across two OS reads is never decoded mid-character — it cannot raise or corrupt because no decode is attempted until its bytes are whole). Equivalently, an incremental decoder (`codecs.getincrementaldecoder`) may be fed each chunk so far as it preserves byte-identical results vs. the buffered decode. The trailing non-terminated segment is decoded the same way at end-of-step for the raw-buffer parity below;
  - **drains stderr concurrently for deadlock-safety only** — stderr is *not* passed to the sink (FR-2.6);
  - feeds stdin per FR-1.2: the stdin pipe is set non-blocking and registered for **write-readiness only while unsent bytes remain**; each write accounts for partial writes and advances an offset until every prompt byte is sent; stdin is then `close()`d **exactly once**; `BrokenPipeError` (child exits/closes its read end early) is swallowed and treated identically to `communicate(input=…)` — no error, no hang;
  - preserves the existing hard timeout → `killpg` (`_kill_process_group`) → drain-remaining → `timed_out=True` behavior; lines received before the kill are retained;
  - accumulates **separate raw byte buffers** for stdout and stderr, maintained independently of the line-sink: every byte read is appended to the raw buffer regardless of newline framing, so a trailing **non-terminated** segment (never handed to the sink) is still captured byte-for-byte in `ProcessOutput.stdout`/`stderr` on both the clean-exit and the timeout/`killpg` path — exactly as `communicate()` would return it;
  - **catches any exception raised by `sink(line)` (fail-closed, FR-6.2).** A sink that raises — disk write error or redaction error — does **not** simply propagate out of the reader loop. `run_with_timeout` catches it, runs the **same teardown as the timeout path** (`killpg` / `_kill_process_group` on the child's process group, then a best-effort drain of remaining pipe bytes into the raw buffers, then `close()` of stdin/stdout/stderr), and only then re-raises the original sink exception (wrapped so the adapter records a **failed** step). This guarantees a sink fault never leaves a live child, an undrained pipe, or a skipped process-group cleanup — the same no-deadlock / `killpg` guarantees P1 re-earns hold on the sink-fault path too.

**Simplest design (no speculation):** the sink is a bare `Callable[[str], None]`, not an interface or a class — P2 supplies a closure over the `StepLogger`. The reader works in bytes internally and decodes a line to `str` only once that line's bytes are whole (an incremental/at-boundary decode with the buffered path's exact encoding+`errors`), keeping parity assertions exact and making cross-read multi-byte splits a non-event. Sink-fault teardown reuses the existing `_kill_process_group` + drain path rather than inventing a second cleanup. No PTY — v1 relies on the CLIs' per-event flush; PTY allocation is deferred to PRD OQ-2 pending P2 promptness measurement. No public sink object, no stderr sink, no buffering knobs.

**Test strategy** (`tests/unit/test_process_streaming.py`)
- **FR-1.1 deadlock stress:** a fixture child emits N stdout lines over time while writing stderr volume **exceeding the OS pipe buffer**; assert no hang, all N lines reach the sink in order before exit.
- **FR-1.2 stdin:** (a) an over-pipe-buffer prompt to a child that emits output *before* draining stdin → no hang, every prompt byte delivered in order; (b) a child that closes stdin / exits early → no raise, `ProcessOutput` identical to the buffered path.
- **FR-1.3 timeout/kill:** a hanging child is killed at `timeout_s` (`timed_out=True`); lines streamed before the kill are present.
- **FR-1.4 parity:** (a) field-for-field equality of `ProcessOutput` across buffered vs. streaming modes for one deterministic fixture child; (b) a child whose **final line is not newline-terminated**, run against both a clean-exit and a killed child, with the trailing partial appearing byte-for-byte in `ProcessOutput.stdout` but **never** delivered to the sink.
- **FR-1.4 multi-byte decode:** a fixture child emits a line containing a multi-byte UTF-8 character positioned so its bytes land in **separate OS reads** (e.g. padded so the boundary falls mid-character); assert the sink receives the line decoded correctly (no `UnicodeDecodeError`, no mojibake / replacement char) and `ProcessOutput.stdout` is byte-identical to the buffered path.
- **FR-6.2 sink-fault teardown:** a sink that raises on the Kth line against a still-running child; assert the child's process group is killed, remaining bytes are drained into `ProcessOutput`, all pipes/stdin are closed (no leaked process, no hang), and the raised exception surfaces so the step is recorded failed.

**Exit criteria:** all P1 tests pass; pre-existing `process.py` tests pass unmodified (flag-off / `sink=None` path untouched); the deadlock stress matrix is green across N≥50 runs. Commit `P1: …`.

**Deferrals recorded:** PTY allocation (OQ-2); any sink→disk wiring (P2); adapter changes (P2).

---

## P2 — Live redacted persistence for both CLI adapters

**Assumption it validates:** the fail-closed redaction invariant survives streaming — *including* the cross-event value-containment that the per-line unit secretly depends on — and the authoritative result stays byte-identical, on **both** the Claude (`stream-json`) and Codex (`--json`) adapters. Depends on P1 (a proven reader + a growing file to redact into).

**Deliverables**
- **The streaming gate, persisted (`engine/config.py`).** Introduce `RunConfig.stream_step_output: bool = False` — the default-off knob the ground rules promise — together with its config parsing and serialization (it round-trips through the same load/dump path as the existing `RunConfig` fields beside `interrupted_step`/`reviewer_mutation`). An omitted/absent field deserializes to `False`. This is the single source of truth the engine wiring below reads; without it the advertised gate does not exist as persisted state. (P1 carried no config field — its `sink=None` default is the only off-switch it needs; the persisted knob lands here, where the engine and adapters first consume it.)
- **`StepLogger` streaming contract** (`logging/transcript.py`): `open_stream(*, suffix="") -> Sink`; `Sink.append_line(text)` redacts the **complete** line via the existing `RedactingWriter.append_line` and appends it to `events{suffix}.jsonl` — **no JSON parse required to persist** (FR-2.5); `Sink.close()`. Persistence is decoupled from validity: a parse failure never blocks an append. `log_result`'s final `transcript.md` render is **unchanged** and still runs once at step end (over the fully-assembled events from the unchanged end-of-step parse).
- **Adapter wiring.** `ClaudeCodeAdapter.run` and `CodexAdapter.run` gain a `sink: Callable[[str], None] | None = None` parameter, passed straight to `run_with_timeout`. The adapter uses it **only** when (a) the flag is on, (b) its output mode is the NDJSON streaming mode (`output_format == "stream-json"` for Claude; `exec --json` is always NDJSON for Codex), and (c) the adapter is declared line-streamable (see FR-2.8 below). Otherwise it passes `None` and the buffered path runs. The end-of-step `_parse` / `_decode_events` / `_extract_usage` / `--output-last-message` / `--output-schema` paths are **untouched** — they still read `out.stdout`.
- **Engine wiring.** `handle_agent_task` (`steptypes.py`) and the cycle attempt loop (`cycle.py`) open a `StepLogger` stream when the flag is on, thread its `append_line` as the sink into `adapter.run(...)`, and `close()` it in a `finally`. The existing `logger.log_result(result)` end-of-step call stays exactly as is — the live file and the final render are written by independent paths, so the final render remains the authoritative `events.jsonl` content on the buffered path and is consistent with the streamed file on the streaming path.
- **Per-adapter line→event granularity gate (FR-2.7/FR-2.8) — a *qualification* gate, not a runtime detector.** A per-adapter declaration (e.g. `supports_line_streaming: bool`) is set True for an adapter **only after** P2's fixture-based containment test (FR-2.7) proves that adapter's NDJSON mode emits at message granularity — i.e. a complete sensitive value always lands within a single newline-terminated line for that output format. An adapter that is not so qualified passes `None` and uses the buffered path. **This is explicitly a static, qualification-time property, not a per-line runtime check:** because no stateful cross-event carryover redactor is built (explicit deferral), the streaming path has no mechanism to detect a secret split across two adjacent events *before* it appends, and it does not claim to. The fail-closed guarantee is therefore obtained by **never enabling streaming for an adapter whose framing cannot be shown to contain whole values** — not by detecting a split mid-stream. If a future adapter (or a CLI version change) cannot pass the containment fixture, the correct outcome is that it stays unqualified and buffered; runtime split-detection is the deferred carryover-redactor work, not a P2 deliverable.
- **Fail-closed sink (FR-6.2):** a sink that raises (disk or redaction error) propagates as a step failure, recorded as a failure — never a silent success.

**Simplest design (no speculation):** reuse `RedactingWriter.append_line` verbatim — it is already the per-line redaction unit; build no new redactor. The freshness signal is **not** computed here (P4). No stateful cross-event redactor, no stderr live artifact, no `transcript.md` streaming. The granularity gate is a boolean, not a pluggable strategy.

**Test strategy** (`tests/unit/test_streaming_persistence.py`, per-adapter)
- **FR-2.1 liveness:** a controllable fake agent; read `events.jsonl` mid-run and find ≥1 event while the step is still "running."
- **FR-2.2 no-raw-secret:** inject a known secret into a streamed line; poll `events.jsonl` repeatedly *during* streaming and assert the raw secret never appears at any read.
- **FR-2.7 containment, per CLI:** a known secret in the agent's streamed output appears within a **single** event line, never split across two adjacent events — asserted for **both** Claude `stream-json` and Codex `--json`.
- **FR-2.8 qualification fail-closed:** an adapter whose containment fixture (FR-2.7) does **not** hold — a fixture mode that splits a planted secret across two events — is **never declared** `supports_line_streaming` and therefore passes `None` and runs buffered; assert such an adapter does not open a stream at all (no `events.jsonl` growth mid-step) and no raw secret reaches disk. (This verifies the qualification gate, not a mid-stream split detector, which is deferred with the carryover redactor.)
- **FR-2.6 stderr-not-persisted:** a child writing diagnostic + secret-bearing lines to stderr produces an `events.jsonl` containing **only** stdout events throughout the streaming window, while `ProcessOutput.stderr` matches the buffered path at step end.
- **FR-2.4 partial line:** a child killed mid-line leaves no partial JSON line in `events.jsonl`; the end-of-step partial matches the buffered path.
- **FR-2.3 / FR-7.2 parity, per CLI:** a golden parity test runs the same fake event stream through buffered and streamed modes and asserts identical `transcript.md`, `structured.json`, usage, and returned `AgentResult` (text/structured/usage) — for both adapters.
- **FR-6.1 config gate:** a `RunConfig` deserialized with `stream_step_output` **omitted** is `False` and the engine threads `sink=None` (buffered path, no `events.jsonl` growth mid-step); the same config with `stream_step_output: true` round-trips through serialization and drives the live sink path. With the flag off, the pre-existing buffered-path adapter tests pass unmodified.

**Exit criteria:** all P2 tests green for both adapters; the API/LiteLLM adapter's tests untouched (durable Non-Goal); flag-off buffered tests unmodified. Commit `P2: …`.

**Deferrals recorded:** stateful cross-event carryover redactor (FR-2.8 future); `logs --follow` (P3); console verification + freshness (P4); API adapter streaming (durable Non-Goal).

---

## P3 — `gauntlet logs <slug> --follow`

**Assumption it validates:** an agent or human can actively monitor a live step from the CLI. Depends on P2 (a growing `events.jsonl` to follow).

**Deliverables**
- `gauntlet logs <slug> --follow` (`cli.py`, the operator-aids command). It resolves the current step via the existing `operator.resolve_logs` path (unchanged resolution + containment), then tails the resolved step's `events.jsonl` by repeatedly reading appended bytes from the last offset and printing them, exiting cleanly when the step ends or on SIGINT.
- **Final drain before exit (no dropped tail).** Terminal step status is observed independently of the byte tail, so the manifest can flip to "ended" while bytes flushed just before completion still sit unread past the last offset. The loop therefore **must not exit immediately on observing terminal status**: after it sees terminal status it performs **one final read/drain from the last offset to EOF** (printing whatever remains) before exiting, so the last events appended at step end are never dropped.
- `--follow` on an **already-completed** step degrades to the existing one-shot dump and exits (no hang).
- Reads only redacted on-disk content (never the raw pipe); stays within the run dir; a traversal `--step` is rejected (inherited from `logs`).

**Simplest design (no speculation):** reuse the exact offset-tail logic already proven in `store._read_chunk` / `log_tail_stream` — a poll-read-from-offset loop with the same EOF/shrink handling — rather than inventing a new tailer; factor the shared offset read into `operator` if needed so CLI and console agree. "Step ended" is determined from the manifest/step status the resolver already reads, not from a sentinel byte — and observing it triggers the single final drain above, not an immediate `break`. v1 tails **only** the step's `events.jsonl`, not the orchestrator `.serve/<verb>.log` (PRD OQ-4 deferral).

**Test strategy** (`tests/unit/test_logs_follow.py`)
- **FR-3.1:** an integration-style test over a streaming fake step asserts incremental output, then a clean exit at step end (and on simulated SIGINT).
- **FR-3.1 no-dropped-tail:** a fixture that appends the **final** events to `events.jsonl` in the same window the manifest flips to terminal status; assert every appended byte (including those written after terminal status is first observable) appears in `--follow` output before exit — proving the final drain runs.
- **FR-3.2:** a finished step → immediate one-shot dump + exit, no hang.
- **FR-3.3:** a planted secret never appears in `--follow` output; a traversal `--step` is rejected.

**Exit criteria:** all P3 tests green; the one-shot `logs` behavior is unchanged when `--follow` is absent. Commit `P3: …`.

**Deferrals recorded:** second tail source (`.serve` log) for console-launched runs (OQ-4).

---

## P4 — Console live-tail verification + freshness signal

**Assumption it validates:** the already-built SSE consumer shows live activity for an in-flight streamed step with **zero** new surface, and the advisory freshness signal lands — resolving operator-aids OQ-1. Depends on P2.

**Deliverables**
- **FR-4 (verification, near-zero code):** a test that drives `log_tail_stream` over a *growing* `events.jsonl` fixture and asserts `append` events are emitted **before** completion — proving the P2 producer → existing consumer wiring with **no endpoint, no UI, no `sse.py`/`store.py` change**.
- **FR-5 freshness:** `status` / `status --json` expose advisory freshness under the **nested** path `current_step_freshness.last_event_age_s`. The `current_step_freshness` **object** is the nullable unit: `null` when not streaming/applicable; when present, `last_event_age_s` is always a number. There is **no** top-level `last_event_age_s`.
- **Pre-first-event state, defined explicitly (no ambiguous baseline).** `current_step_freshness` is `null` until the streamed step's `events.jsonl` **exists and has received at least one complete appended line**. Specifically: not streaming, or the file is absent (`stat` raises `FileNotFoundError`), or it exists but is empty (zero bytes / no line yet) ⇒ `current_step_freshness: null` — never `0`, never a stat error surfaced to the client, never an age derived from the file's open/create time. Only once ≥1 line has been appended does the object become present with a numeric `last_event_age_s`. This makes the "running but no event yet" window a single, well-defined state rather than three builder-dependent behaviours. Because `status_payload` is pure (no I/O), the value is computed in the status-computation path (the operator code that assembles `RunState`) and threaded into `status_payload` as a parameter alongside the existing `driver`/`rstate`/`reconciliation`. The §6.1 status schema (operator-aids' `schemas/status.json`) gains the additive nullable object; `_validate_status_payload` continues to gate emission.

**Simplest design (no speculation):** `last_event_age_s = now − mtime(current step's events.jsonl)` for a running, streamed step **whose file exists and is non-empty** — the file's last-append time is the freshness signal, requiring no event-body parse and matching the §6 note that a freshness parse must never block persistence. The existence/non-empty check is a single `stat` (size > 0); on `FileNotFoundError` or zero size the computation returns `None`, which maps to `current_step_freshness: null` — no separate code path, no surfaced error. A *worded* "looks stale" hint and a configurable threshold N are **not** built (PRD OQ-3 deferral); the field is the raw age only. Freshness drives **no gate and no automatic action** (FR-5.2).

**Test strategy** (`tests/unit/test_console_freshness.py`)
- **FR-4.1:** `log_tail_stream` over a growing fixture emits `append` before completion.
- **FR-5.1:** `--json` for a running streamed step carries `current_step_freshness: { "last_event_age_s": <number> }`; a non-streamed run carries `current_step_freshness: null`; schema validation passes for both.
- **FR-5.1 pre-first-event:** a running streamed step whose `events.jsonl` is **absent** and one whose file exists but is **empty** both yield `current_step_freshness: null` (not `0`, no raised error); once one line is appended the same step yields a numeric `last_event_age_s`. Schema validation passes in every case.
- **FR-5.2:** a deliberately stale value triggers no manifest/state change.

**Exit criteria:** all P4 tests green; `sse.py`/`store.py` unchanged; the existing `status`/`--json` fields and operator-aids tests unchanged (purely additive). Commit `P4: …`.

**Deferrals recorded:** worded staleness hint + threshold N (OQ-3); default-on flip after soak (OQ-5).

---

## Cross-cutting deferrals (named, not smuggled)

- **API/LiteLLM streaming** — durable Non-Goal (in-process, token-delta; breaks the per-line redaction unit). Untouched in every phase.
- **PTY allocation** — deferred (OQ-2); v1 relies on CLI per-event flush, re-measured in P2.
- **Stateful cross-event carryover redactor (runtime split detection)** — deferred (FR-2.8 future). v1 has **no** runtime detector for a secret split across adjacent events; instead it fails closed by *qualification* — an adapter is enabled for streaming only after a fixture proves its NDJSON framing contains whole values (message granularity), and any adapter that cannot be so qualified stays buffered. Runtime detection of a split before persistence is the deferred work, not a v1 mechanism.
- **Flipping the streaming flag default-on** — out of scope (OQ-5); a post-v1 soak decision. No phase makes default-on an acceptance criterion.
- **Tailing the orchestrator `.serve/<verb>.log` from `logs --follow`** — deferred (OQ-4); v1 tails step events only.
- **Worded "looks stale" hint / threshold N** — deferred (OQ-3).

---

```gauntlet-phases
- id: P1
  title: Deadlock-safe incremental reader
  goal: Add a selectors-based streaming mode to run_with_timeout that frames stdout on newline and feeds a line sink, behind a default-off flag, re-earning concurrent stdout+stderr drain, concurrent stdin feed, and timeout+killpg with partial capture. Validates the load-bearing §1.3 assumption — we can give up communicate() with field-for-field ProcessOutput parity and zero deadlock.
- id: P2
  title: Live redacted persistence (Claude + Codex)
  goal: Add StepLogger.open_stream/append_line/close routing each complete NDJSON line through the existing per-line redactor, wire the sink through both CLI adapters and the engine, and gate per-adapter line-streaming on verified message-granularity. Validates that the fail-closed redaction invariant (including cross-event value containment) survives streaming and the authoritative result stays byte-identical on both adapters.
- id: P3
  title: gauntlet logs --follow
  goal: Add --follow to the operator-aids logs command, tailing the current step's events.jsonl by offset and exiting cleanly at step end / on SIGINT, degrading to a one-shot dump for a finished step. Validates that an agent or human can actively monitor a live step from the CLI.
- id: P4
  title: Console live-tail verification + freshness signal
  goal: Verify the already-built SSE consumer lights up over a growing events.jsonl with no new surface, and add the advisory nested current_step_freshness.last_event_age_s to status/--json. Validates the producer→consumer wiring and lands the advisory freshness signal, resolving operator-aids OQ-1.
```
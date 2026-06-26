# PRD: Live run observability (streamed step output)

**Status:** Draft v0.2
**Author:** John Pletka
**Date:** 2026-06-25
**Working name:** live-run-observability
**Relationship to existing artifacts:** Does **not** amend any approved artifact
(`PRD-gauntlet.md`, `policy.yaml`, any approved `prd.md`/`plan.md`). Builds on:
the subprocess wrapper (`adapters/process.py`) and the CLI agent adapters —
Claude (`adapters/claude_code.py`) and Codex (`adapters/codex.py`); the transcript
logger + redacting writer (`logging/transcript.py`, `logging/redact.py`); and the
console's **already-built, offset-based** SSE log-tail
(`web/sse.py::log_tail_stream`, `web/store.py::step_log`). It is **sequenced
after** the `operator-aids` PRD and *extends* that PRD's `logs` command (adds
`--follow`) and `status --json` (adds a freshness field); it does not modify
operator-aids' approved scope and does not block it.

---

## §1 Overview

### 1.1 Problem statement

During a long in-flight agent step, **every text artifact on disk is silent**, so
an operator who suspects a step is wedged — or is simply impatient and does not
trust that it is progressing — has nothing to watch. The cause is structural:
every agent invocation runs through `run_with_timeout`, which drains the
subprocess with `proc.communicate()` ([process.py](src/gauntlet/adapters/process.py)) —
a blocking call that holds all stdout/stderr in OS pipe buffers + memory until
the process exits. Only then does `StepLogger.log_result` write `transcript.md`/
`events.jsonl` ([transcript.py](src/gauntlet/logging/transcript.py)), in a single
end-of-step write. While the step runs, those files do not exist yet, and the
orchestrator itself is blocked inside `communicate()`, so its log is silent too.

The console already ships a live SSE log-tail that is offset-based and
file-agnostic ([sse.py](src/gauntlet/web/sse.py)) — the *consumer* is built and
waiting. The only missing piece is a *producer* that grows a step's log on disk
as the agent works. Today there is none, so the live tail shows nothing until the
step ends, then the whole transcript appears at once.

### 1.2 Solution summary

Stream the CLI agents' line-delimited JSON events to disk **incrementally, as
they arrive**, instead of buffering them until exit. Both CLI adapters already
emit NDJSON — Claude via `--output-format stream-json`, Codex via `exec --json` —
so one primitive serves both. Concretely: replace the buffered `communicate()`
drain with a deadlock-safe incremental reader that **frames on the newline
delimiter** and hands each complete line to a sink, then route that sink through
the existing **per-line** redactor so each event is redacted and appended to
`events.jsonl` the moment it lands. This single change:

- makes `events.jsonl` for the current step grow live;
- lights up the console's existing SSE step-tail with **zero UI work** (it is
  already offset-based — it simply now has a growing file to read);
- enables `gauntlet logs <slug> --follow` for a terminal or a Claude session to
  actively monitor an in-flight step; and
- yields a real freshness signal (age of the last streamed event), resolving the
  alive-but-wedged question that `operator-aids` had to defer (its OQ-1).

The authoritative end-of-step parse (result extraction, usage, structured output)
and the final `transcript.md` render are **unchanged** — streaming changes only
*when bytes land on disk*, never *what the result is*.

### 1.3 The assumption this validates

The riskiest belief: **we can give up `communicate()`'s bundled guarantees and
re-earn them in an incremental reader without breaking correctness or the
fail-closed redaction contract.** `communicate()` is not lazy buffering — it is
the stdlib's deadlock-safe way to (a) drain stdout *and* stderr concurrently
(finite pipe buffers deadlock otherwise), (b) feed stdin concurrently, and (c)
enforce the hard timeout + process-group kill with partial capture. The feature
rests on being able to reproduce all of that *and* guarantee no un-redacted byte
ever reaches disk, with byte-for-byte parity on the final result. P1 attacks this
first, behind a flag, proving parity + deadlock-freedom before anything depends
on it.

---

## §2 Goals and Non-Goals

### 2.1 Goals

| ID | Outcome | Need it serves |
|----|---------|----------------|
| G1 | A step's `events.jsonl` grows incrementally *during* the step, not only at completion. | Gives the in-flight step a live, readable progress artifact. |
| G2 | No un-redacted secret ever reaches disk, even transiently, under streaming. | Preserves the fail-closed redaction invariant the whole project depends on. |
| G3 | The existing console SSE step-tail shows live agent activity for an in-flight step, with no new endpoint and no UI change. | Realizes the already-built live-tail surface. |
| G4 | `gauntlet logs <slug> --follow` streams the current step's output to a terminal or a Claude session until the step ends. | Lets an impatient human or an agent operator actively watch progress. |
| G5 | An advisory freshness signal (age of last streamed event) is available to `status`/`--json`. | Answers "is it actually progressing?" — resolves operator-aids OQ-1. |
| G6 | Zero correctness regression: timeout/kill, partial-capture-on-kill, end-of-step parse, usage + structured-output extraction behave exactly as before, on both CLI adapters. | The hot path stays trustworthy; streaming is observability-only. |

### 2.2 Non-Goals (v1)

- **No change to the redaction ruleset or to *what* is logged.** Only the *timing*
  of writes changes; the redactor and the event content are untouched.
- **No new gate and no automatic action on a "stale" signal.** Freshness is
  strictly advisory (mirrors operator-aids' posture); nothing auto-halts or
  auto-recovers from it.
- **Streaming covers the subprocess/NDJSON CLI adapters — Claude (`stream-json`)
  and Codex (`--json`) — in v1.** They share one primitive (differing only in a
  per-adapter line→event parser), so both land together. The **API/LiteLLM**
  adapter is a **durable Non-Goal**: it is an in-process call, not a subprocess,
  so none of the reader machinery applies, and it streams *token deltas* (a secret
  can span chunks), which breaks the per-line redaction unit — the same cross-event
  containment property that the CLIs must satisfy and that P2 verifies for them
  (FR-2.7/FR-2.8). (§4.2, §11 OQ-1.)
- **No PTY allocation in v1** unless parity testing shows a CLI does not flush per
  event (recorded as §11 OQ-2). v1 relies on the CLIs' per-event flush
  (`stream-json` / `--json`).
- **The final `transcript.md` render is not replaced.** `events.jsonl` streams;
  `transcript.md` remains a single rendered write at step end.
- **No console auth/transport/endpoint changes.** The loopback + per-serve-token
  model and the existing `/log/stream` endpoint are reused as-is.
- **No replacement of operator-aids' one-shot `logs`.** This PRD *adds* `--follow`;
  the post-hoc dump is unchanged.

---

## §3 Users and Personas

- **Human operator** — watches a long step from a terminal (`logs --follow`) or
  the console, to confirm progress or catch a wedge early.
- **Agent operator (Claude Code session)** — actively monitors an in-flight step
  via `logs --follow` (or the freshness field in `--json`) to decide whether to
  wait, or to `recover` (operator-aids).

---

## §4 System Architecture

### 4.1 Components

**Modified (the hot path):**
- `src/gauntlet/adapters/process.py` — `run_with_timeout` gains a **streaming
  mode**: an incremental, deadlock-safe reader (a `selectors` loop over stdout +
  stderr, with stdin fed via a **non-blocking, write-readiness-driven** path —
  registered for write only while unsent bytes remain, with partial-write
  accounting, closed exactly once when the full prompt is sent, and `BrokenPipe`
  handled as in the buffered path) that **frames on `\n`** and invokes a per-line
  `sink` as complete lines arrive, while preserving the existing timeout + `killpg`
  + partial-capture behavior. stderr is **drained concurrently for deadlock-safety
  only** — it is not routed to the sink (FR-2.6). The assembled `stdout`/`stderr` in
  `ProcessOutput` are accumulated in **separate raw byte buffers** maintained
  independently of the complete-line sink, so a trailing un-terminated segment
  (withheld from the live file per FR-2.4) is still captured byte-for-byte in
  `ProcessOutput` on both normal exit and the timeout/`killpg` path. The buffered
  path remains for the fallback/flag-off case and for the (non-streaming) API
  adapter.
- `src/gauntlet/adapters/claude_code.py` — when `output_format == "stream-json"`,
  pass a sink that routes each line to the step's live log; the end-of-step
  `_parse` is unchanged (it reads the fully-assembled events).
- `src/gauntlet/adapters/codex.py` — same wiring for `codex exec --json`: its
  existing `_decode_events` line parser runs incrementally against the sink; the
  end-of-step parse, `--output-last-message`, and `--output-schema` paths are
  unchanged. Identical primitive, different event schema.

**Extended:**
- `src/gauntlet/logging/transcript.py` — `StepLogger` gains an incremental
  append path (`open_stream()/append_line()/close()`) writing each complete line
  through the redactor; `log_result`'s final `transcript.md` render is unchanged.
- `src/gauntlet/logging/redact.py` — **reused**: `RedactingWriter.append_line`/
  `append_jsonl` already redact **per line** (the unit that makes streaming safe).
- `src/gauntlet/cli.py` — `gauntlet logs` (from operator-aids) gains `--follow`.
- `src/gauntlet/engine/operator.py` (from operator-aids) — add the advisory
  `last_event_age_s` to the computed state surfaced by `status`/`--json`.

**Reused unchanged:**
- `src/gauntlet/web/sse.py::log_tail_stream`, `src/gauntlet/web/store.py::step_log`
  — already offset-based; they light up once the producer grows the file.
- `src/gauntlet/adapters/_structured.py`, `web/jobproc.py` (its file-append
  capture pattern is the proven precedent for live-tailable writes).

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Replace `communicate()`, not "set a flush flag" | Hand-rolled incremental reader that re-earns concurrent stdout+stderr drain, stdin feed, and timeout+killpg. | There is no buffering knob; `communicate()` *bundles* deadlock-safety we must reproduce or reintroduce hangs. |
| Frame on the **newline**, not on JSON validity | The reader splits on `\n`; a complete line *is* a complete JSON object (NDJSON escapes any in-string newline). The live writer appends the **raw redacted line** — it never parses-to-validate before writing. | No bracket-counting streaming parser; robust to a stray non-JSON line (captured verbatim); strict JSON validation stays the end-of-step parse (parity). A truncated trailing line has no `\n`, so it is never written live (FR-2.4/2.5). |
| Redact **in-stream, per NDJSON line** — not redact-at-rest | Parent reads each complete line, redacts it via `RedactingWriter`, then appends. | Fail-closed: no raw byte ever hits disk, even transiently. The NDJSON line is the redaction unit. Per-line redaction is sufficient **only when a secret value is wholly contained in one event line** — which needs both (a) in-string newlines are escaped (one JSON string never spans lines) **and (b) the stream is message-granular, so a value is never split across consecutive events**. (b) is **verified for both CLIs in P2** (FR-2.7), not assumed — it is the same property that excludes the API token-delta stream; any stream found to split values fails closed to the buffered path until a stateful carryover redactor exists (FR-2.8). |
| Parent-side redaction → **reject** the simpler child→file redirect | Do **not** point the child's stdout at a file (the `jobproc` pattern), despite it being simpler. | A direct file redirect writes the child's **raw** output to disk, bypassing the redactor. Keeping redaction in the parent is the whole point. |
| End-of-step parse + `transcript.md` render unchanged | Streaming feeds `events.jsonl`; the authoritative result is still extracted once at step end. | Parity is the contract; streaming is observability, not a re-architecture of result handling. |
| Reuse the console's offset tail; build no new endpoint | The producer was the only missing half. | The SSE consumer is built, tested, and offset-based; lighting it up is near-free. |
| Feature-flag the streaming path; default off until proven | A config flag gates streaming; off ⇒ today's exact behavior. | Fail-closed rollout: the buffered path is the safe fallback until P1/P2 prove parity + redaction. |
| Claude **and Codex** in v1; API excluded | Both are subprocess/NDJSON CLIs sharing the FR-1 reader + line-level redaction; only the line→event parser differs. API is a durable Non-Goal. | Codex is the *same* primitive, not a separate project, so deferring it bought nothing. API is a different mechanism (in-process, token-delta) whose redaction unit can't be a line. |

---

## §5 Functional Requirements

### FR-1 — Deadlock-safe incremental capture (the primitive)

- **FR-1.1** `run_with_timeout` supports a streaming mode that invokes a line
  `sink` as complete (newline-terminated) stdout lines arrive, reading stderr
  concurrently so neither pipe can deadlock. *Acceptance:* a test child that emits
  N stdout lines over time while writing stderr volume **exceeding the OS pipe
  buffer** completes without hanging, and the sink receives all N lines in order
  before exit.
- **FR-1.2** stdin (the prompt) is fed without deadlocking against early child
  output, including a prompt larger than the pipe buffer. The write is **normative**:
  the stdin pipe is non-blocking; it is registered for write-readiness **only while
  unsent bytes remain**; each write accounts for partial writes and advances an
  offset until every prompt byte has been sent; stdin is then `close()`d **exactly
  once**; and `BrokenPipeError` (child exits or closes its read end before draining
  the prompt) is swallowed and treated **identically to the buffered
  `communicate(input=...)` path** — no error surfaced, no hang. *Acceptance:* (a) a
  test with an over-buffer-sized prompt and a child that emits output before draining
  stdin completes without hang and delivers every prompt byte in order; (b) a test
  whose child closes stdin early (or exits) before reading the whole prompt completes
  without raising, with the same `ProcessOutput` outcome as the buffered path.
- **FR-1.3** The hard timeout + process-group SIGKILL are preserved; lines
  received before the kill are retained. *Acceptance:* a hanging child is killed
  at `timeout_s` (`timed_out=True`), and the events streamed up to the kill are
  present on disk.
- **FR-1.4** `ProcessOutput` fields (`exit_code`, `duration_s`, `timed_out`,
  assembled stdout/stderr) are identical to the buffered path for a deterministic
  child. The assembled `stdout`/`stderr` are accumulated in **separate raw byte
  buffers** maintained independently of the complete-line live sink (every byte read
  is appended to the raw buffer regardless of newline framing); a trailing
  **non-newline-terminated** segment — withheld from the live file per FR-2.4 — is
  nonetheless retained byte-for-byte in `ProcessOutput.stdout`/`stderr` on **both**
  normal exit and the timeout/`killpg` path, exactly as `communicate()` would return
  it. The live sink governs only *live persistence*, never *assembled capture*.
  *Acceptance:* (a) a parity test asserts field-for-field equality across both modes
  for the same fixture child; (b) a test whose final line is **not** newline-terminated
  — run against both a clean-exit child and a killed child — asserts the partial
  trailing bytes appear in `ProcessOutput.stdout` identically to the buffered path,
  while never appearing in the live `events.jsonl` (cross-checked with FR-2.4).

### FR-2 — Live, redacted persistence

- **FR-2.1** Each complete NDJSON event line (Claude `stream-json` / Codex
  `--json`) is redacted and appended to `events.jsonl` as it arrives, so the file
  is non-empty and growing before the step completes. *Acceptance:* with a
  controllable fake agent, a test reads `events.jsonl` mid-run and finds ≥1 event
  while the step is still "running."
- **FR-2.2** No un-redacted secret is ever present on disk, even transiently.
  *Acceptance:* a test injects a known secret into a streamed line and, polling
  `events.jsonl` repeatedly **during** streaming, asserts the raw secret never
  appears at any read.
- **FR-2.3** The final `transcript.md`, `structured.json`, usage, and the returned
  `AgentResult` are byte/semantically identical to the buffered path. *Acceptance:*
  a golden parity test runs the same fake event stream through both paths and
  asserts identical outputs.
- **FR-2.4** A trailing partial (un-terminated) line is **not** written to the
  live file; it is only materialized if completed, otherwise it flows into the
  end-of-step partial via the existing lenient decode. *Acceptance:* a child
  killed mid-line leaves no partial JSON line in `events.jsonl`, and the
  end-of-step partial matches the buffered path.
- **FR-2.5** The live writer frames on the newline delimiter and appends the raw
  redacted line **without parsing-to-validate**; strict JSON validation remains
  the end-of-step parse (fail-closed policy unchanged). *Acceptance:* a non-JSON
  line emitted to stdout is captured verbatim (redacted) as one line in
  `events.jsonl`, the live tail does not error, and the end-of-step strict parse
  still fails closed exactly as the buffered path does today.
- **FR-2.6** **stderr is never live-persisted.** Only stdout NDJSON event lines are
  redacted and streamed to `events.jsonl`. stderr is drained concurrently **solely**
  to prevent pipe deadlock (FR-1.1) and is retained only in `ProcessOutput.stderr`
  for the existing end-of-step handling — it is **not** routed to the sink, **not**
  live-tailed, **not** written to `events.jsonl`, and **not** written to any separate
  live artifact in v1. Because no stderr byte is written live, no stderr-borne
  diagnostic or secret can leak via a live file; final stderr handling (and any
  redaction at that boundary) is unchanged from the buffered path. *Acceptance:* a
  streamed step whose child writes both diagnostic and secret-bearing lines to stderr
  produces an `events.jsonl` containing **only** stdout events (no stderr line, no
  planted stderr secret) throughout the streaming window, while `ProcessOutput.stderr`
  matches the buffered path at step end.
- **FR-2.7** **The per-line redaction unit is sound only for whole-event streams,
  and this is verified, not assumed.** Per-line redaction is sufficient **iff** every
  secret value is wholly contained within a single NDJSON event line, which requires
  **both**: (a) in-string newlines are escaped, so one JSON string never spans lines
  (already relied on); **and (b) a secret value is never split across consecutive
  event objects** — i.e., the stream is message-granular, not incremental
  token/content deltas. Property (b) is the same property that disqualifies the
  API/LiteLLM adapter (token-deltas, §2.2) and it is **not assumed** for the CLIs:
  P2 must verify it for **both** Claude `stream-json` and Codex `--json`.
  *Acceptance:* for **each** CLI adapter, a test in which a known secret is part of
  the agent's streamed output asserts the secret value appears within a **single**
  event line (never split across two adjacent lines/events), and the polled
  no-raw-secret test (FR-2.2) holds throughout.
- **FR-2.8** **Fail closed if a stream splits values across events.** If P2 — or any
  later evidence — shows a supported CLI emits incremental token/content deltas such
  that a secret value can span consecutive events, the per-line unit is
  **insufficient** for that adapter and its streaming path must **not** ship per-line
  redaction: it either falls back to the buffered path (which redacts the
  fully-assembled output) or waits for a **stateful streaming redactor that carries
  cross-event context** across the delta boundary. v1 ships per-line streaming
  **only** for streams proven message-granular (FR-2.7). *Acceptance:* a fixture
  stream that splits a planted secret across two events causes the per-line streaming
  path to be rejected/disabled for that adapter (buffered fallback engaged), with no
  raw secret reaching disk.

### FR-3 — `gauntlet logs --follow`

- **FR-3.1** `gauntlet logs <slug> --follow` prints appended lines of the current
  step's log as they arrive and exits cleanly when the step ends or on SIGINT.
  *Acceptance:* an integration-style test over a streaming fake step asserts
  incremental output, then a clean exit at step end.
- **FR-3.2** `--follow` on an already-completed step degrades to a one-shot dump
  and exits (no hang). *Acceptance:* a test asserts immediate dump + exit for a
  finished step.
- **FR-3.3** `--follow` reads only redacted on-disk content (never the raw pipe)
  and stays within the run dir. *Acceptance:* a planted secret never appears in
  `--follow` output; a traversal `--step` is rejected (inherited from `logs`).

### FR-4 — Console live tail (no new surface)

- **FR-4.1** The existing `/api/runs/{slug}/steps/{step}/log/stream` SSE emits
  `append` events for an in-flight step once `events.jsonl` streams. *Acceptance:*
  a test drives `log_tail_stream` over a growing `events.jsonl` fixture and
  asserts `append` events are emitted before completion (proving producer→consumer
  wiring, no endpoint change).

### FR-5 — Freshness signal (resolves operator-aids OQ-1)

- **FR-5.1** `status`/`status --json` expose advisory freshness under the **nested**
  path `current_step_freshness.last_event_age_s` (age of the newest streamed event)
  for a running, streamed step. The **`current_step_freshness` object is the nullable
  unit**: it is `null` when not streaming/applicable; when present, its
  `last_event_age_s` is always a number, never null. There is no top-level
  `last_event_age_s`. *Acceptance:* `--json` for a running streamed step carries
  `current_step_freshness: { "last_event_age_s": <number> }`; a non-streamed run
  carries `current_step_freshness: null`.
- **FR-5.2** The freshness value drives no gate and no automatic action.
  *Acceptance:* a deliberately stale value triggers no manifest/state change in a
  test.

### FR-6 — Fail-closed rollout

- **FR-6.1** Streaming is behind a config flag; with it off, behavior is exactly
  today's buffered path. *Acceptance:* with the flag off, the pre-existing
  buffered-path adapter tests pass unmodified.
- **FR-6.2** Any streaming-sink failure (disk write error, redaction error) fails
  the step closed rather than continuing with output silently dropped or
  un-redacted. *Acceptance:* a test that makes the sink raise asserts the step
  records a failure, not a silent success.

### FR-7 — Adapter coverage (Claude + Codex)

- **FR-7.1** Both the Claude (`stream-json`) and Codex (`exec --json`) adapters
  stream through the FR-1 primitive and FR-2 persistence, each with its own
  line→event parser; the API/LiteLLM adapter is unchanged (buffered) and out of
  scope. *Acceptance:* a streaming-mode test for **each** CLI adapter finds
  `events.jsonl` growing mid-run; the API adapter's tests are untouched.
- **FR-7.2** Codex's authoritative result is unaffected: `--output-last-message`,
  `--output-schema` validation, and `turn.completed` usage extraction are
  byte/semantically identical to the buffered path. *Acceptance:* a Codex golden
  parity test asserts identical `AgentResult` (text, structured, usage) across
  buffered and streamed modes.

---

## §6 Data & Schemas (normative excerpts)

- **`events.jsonl`** — format unchanged: append-only NDJSON, one redacted JSON
  event object per line (both adapters). Only the write *timing* changes
  (incremental vs single end-of-step write). The line is both the framing unit
  and the redaction unit.
- **`StepLogger` streaming contract** (adapter-agnostic; shared by both CLI
  adapters) — `open_stream(step_dir, *, suffix="") -> Sink`;
  `Sink.append_line(text: str)` (redacts the complete line, appends — **no JSON
  parse required to persist**); `Sink.close()`. Persistence is decoupled from
  validity: a best-effort parse for the freshness signal may run alongside, but a
  parse failure there must never block the append. The final `log_result` render
  is still invoked once at step end.
- **`status --json` freshness field** (additive to operator-aids' `schemas/status.json`).
  The **nullable unit is the `current_step_freshness` object as a whole**, not the
  numeric field. When the object is present, `last_event_age_s` is always a number
  (never null); the entire object is `null` for a non-streamed / not-applicable step.
  Consumers read the **nested** path `current_step_freshness.last_event_age_s`; there
  is no top-level `last_event_age_s`.
  ```jsonc
  // schema: current_step_freshness: null | { "last_event_age_s": number }
  "current_step_freshness": { "last_event_age_s": 3.2 }   // running, streamed step
  "current_step_freshness": null                          // not streaming / not applicable
  ```

---

## §7 Security & Privacy

- **Redaction must hold under streaming (the headline).** Every complete line is
  redacted in the parent, per NDJSON line, *before* the first disk write
  (`RedactingWriter.append_line`). No raw bytes at rest, ever — which is exactly
  why v1 **rejects** the simpler child→file redirect (it would write raw output).
  A truncated trailing line (no `\n`) is never written live (FR-2.4), so a partial
  secret cannot leak via the live file. Per-line redaction is sound **only because
  the supported CLI streams are message-granular** — a secret value lands in a single
  event, never split across consecutive events (FR-2.7); this is **verified** for
  both Claude and Codex in P2, not assumed, and any stream that splits values fails
  closed to the buffered path (FR-2.8). This is the same property that excludes the
  API token-delta stream (§2.2).
- **stderr is not live-persisted** (FR-2.6): it is drained only to prevent deadlock
  and kept in `ProcessOutput.stderr` for unchanged end-of-step handling; no stderr
  byte is ever written to a live file, so stderr cannot leak a secret via streaming.
- **Fail-closed on sink/redaction failure** (FR-6.2): a write or redaction error
  fails the step, never degrades to silent or un-redacted output.
- **The live console tail serves only redacted bytes** — it reads the same
  redacted `events.jsonl`; no new endpoint, no auth/transport change, loopback +
  per-serve token unchanged. Path containment for `logs`/tail is inherited.
- **No expansion of what is captured** — the same events as today, written sooner.

---

## §8 Implementation Plan (phased, assumption-validating)

Riskiest-assumption-first; no forward dependencies. Each phase ends green + a
commit. The streaming flag stays **off by default for all of v1** — every phase
ships it default-off; P2 proves parity + redaction and P1–P4 are each validated with
the flag **explicitly enabled in tests**. **Flipping the default to on is out of
scope for this PRD**: it is a separate post-v1 decision after a soak (§11 OQ-5), and
no phase here makes default-on an acceptance criterion.

| Phase | Deliverable | Assumption it validates |
|-------|-------------|--------------------------|
| **P1** | Incremental, deadlock-safe, newline-framing reader in `run_with_timeout` (sink = in-memory), behind the flag; the deadlock stress tests + the `ProcessOutput` parity tests. (FR-1) | **The load-bearing one (§1.3):** we can stream deadlock-free with field-for-field parity, giving up `communicate()`. Everything depends on this. |
| **P2** | Live redacted persistence for **both** CLI adapters: wire the sink to `StepLogger`→`RedactingWriter` per line, with the Claude (`stream-json`) and Codex (`--json`) line→event parsers; the no-raw-secret-on-disk test, the cross-event secret-containment verification for each CLI (and the split-value fail-closed test), the stderr-not-persisted test, + the per-adapter transcript/result parity tests. (FR-2, FR-6, FR-7) | The fail-closed redaction invariant survives streaming — including the cross-event containment the per-line unit depends on — and the authoritative result is byte-identical, on both adapters. Depends on P1. |
| **P3** | `gauntlet logs <slug> --follow`. (FR-3) | An agent/human can actively monitor a live step from the CLI. Depends on P2 (a growing file to follow). |
| **P4** | Console live-tail verification + `last_event_age_s` in `status`/`--json`. (FR-4, FR-5) | The already-built SSE consumer shows live activity, and the advisory freshness signal lands (resolving operator-aids OQ-1). Depends on P2. |

---

## §9 Success Metrics

- **Parity:** 100% of the pre-existing buffered-path adapter tests (Claude **and**
  Codex) pass with streaming on; the golden parity test (transcript/structured/
  result) is identical across modes for each CLI adapter (0 regressions).
- **Deadlock-freedom:** the stress matrix (stderr > pipe buffer × over-buffer
  stdin × slow-draining child) completes without hang in 100% of N≥50 runs.
- **Liveness of feed:** for a streamed step, `events.jsonl` contains the first
  event within ≤2 s of the agent emitting it (measured against a fake agent).
- **Redaction:** 0 occurrences of a planted secret in on-disk `events.jsonl`
  across the entire streaming window (polled).
- **`--follow`:** emits incremental output for a live step and exits cleanly at
  step end / on SIGINT (test green).

---

## §10 Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Hand-rolled reader reintroduces a pipe deadlock. | `selectors`-based concurrent stdout+stderr drain + the explicit stress matrix (FR-1.1/1.2); buffered fallback stays behind the flag. |
| A CLI block-buffers its stdout (events sit in the child's libc buffer when piped), so the feed lags regardless of our reads. | Rely on the CLIs' per-event flush (`stream-json` / `--json`); if parity testing shows lag, allocate a PTY (§11 OQ-2). |
| A secret leaks transiently under streaming. | Per-line redact-*in-stream* (never at-rest) + the polled no-raw-secret test (FR-2.2); never write a partial line (FR-2.4); reject the child→file redirect; fail-closed on redaction error (FR-6.2). |
| A secret value is split across consecutive events (token/content deltas), so per-line redaction misses each half. | Per-line redaction is sound only for message-granular streams; P2 **verifies** value-containment for both CLIs (FR-2.7); any splitting stream fails closed to the buffered path until a stateful carryover redactor exists (FR-2.8). The API (token-delta) adapter is excluded for exactly this reason (§2.2). |
| A complete line is not valid JSON (leaked log line / contract break). | The live writer captures it verbatim (redacted) and does not gate on validity (FR-2.5); the end-of-step strict parse applies the existing fail-closed policy, so behavior matches today. |
| Hot-path regression (timeout/kill/parse/usage) on either adapter. | Field-for-field + per-adapter golden parity tests (FR-1.4/FR-2.3/FR-7.2); flag default-off for **all of v1**; any default-on flip is a separate post-v1 decision (§11 OQ-5). |
| Partial/truncated final line on kill corrupts the live file. | Only complete (newline-terminated) lines are redacted + written (FR-2.4); a trailing partial is held, never written, and captured in the end-of-step partial as today. |

---

## §11 Open Questions

1. ~~**Adapter scope.**~~ **Resolved:** v1 streams **both** subprocess/NDJSON CLI
   adapters — Claude (`stream-json`) and Codex (`--json`) — since they share one
   primitive (only the line→event parser differs). The **API/LiteLLM** adapter is
   a **durable Non-Goal**, not a deferral: it is an in-process call (none of the
   reader machinery applies) and streams token deltas (a secret can span chunks),
   which breaks the per-line redaction unit; the value is marginal for its short
   classification calls. (§2.2, §4.2.)
2. **PTY vs pipe for promptness.** Whether the CLIs (`stream-json` / `--json`)
   flush per event over a pipe, or whether a PTY is needed to defeat child-side
   block buffering — defer until P1/P2 parity testing measures it (applies to both
   adapters).
3. **Freshness threshold (N).** If we later add a *worded* "looks stale" hint on
   top of the raw `last_event_age_s`, what age triggers it? Advisory only;
   judgment call; defer.
4. **Second tail source.** Should `logs --follow` also tail the orchestrator
   `.serve/<verb>.log` for console-launched runs (a coarser, complementary feed),
   or only the step's `events.jsonl`? Leaning: step events only in v1.
5. ~~**Default-on timing.**~~ **Resolved (fail-closed):** v1 ships the streaming flag
   **default-off through every phase**; flipping the default to on is **not** in this
   PRD's scope or acceptance. It is a separate post-v1 decision after a soak, made once
   parity + redaction have run default-off in the field. This removes the prior §8
   contradiction (which had implied a flip "after P4").

---

*Handoff: this is **Draft v0.2**, sequenced **after** `operator-aids` (it extends
that PRD's `logs` and `--json`; it does not block or amend it). The riskiest
assumption is §1.3 — giving up `communicate()` for an incremental reader with full
parity and fail-closed redaction — attacked first in P1 behind a flag. OQ-1
(adapter scope) and OQ-5 (default-on timing) are now **resolved** — Claude + Codex in
v1 with API a durable Non-Goal, and v1 shipping the flag default-off with the
default-on flip deferred to a post-v1 soak; OQ-2–4 remain live. Next step is `gauntlet run live-run-observability`, which
begins with **adversarial review** — not implementation. I ratify; the pipeline
executes.*

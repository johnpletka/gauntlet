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
  can span chunks), which breaks the per-line redaction unit. (§4.2, §11 OQ-1.)
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
  stderr, with stdin written without blocking) that **frames on `\n`** and invokes
  a per-line `sink` as complete lines arrive, while preserving the existing
  timeout + `killpg` + partial-capture behavior. The buffered path remains for the
  fallback/flag-off case and for the (non-streaming) API adapter.
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
| Redact **in-stream, per NDJSON line** — not redact-at-rest | Parent reads each complete line, redacts it via `RedactingWriter`, then appends. | Fail-closed: no raw byte ever hits disk, even transiently. The NDJSON line is the redaction unit — a secret can't span it (in-string newlines are escaped), so per-line redaction is sufficient. |
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
  output, including a prompt larger than the pipe buffer. *Acceptance:* a test
  with an over-buffer-sized prompt and a child that emits output before draining
  stdin completes without hang.
- **FR-1.3** The hard timeout + process-group SIGKILL are preserved; lines
  received before the kill are retained. *Acceptance:* a hanging child is killed
  at `timeout_s` (`timed_out=True`), and the events streamed up to the kill are
  present on disk.
- **FR-1.4** `ProcessOutput` fields (`exit_code`, `duration_s`, `timed_out`,
  assembled stdout/stderr) are identical to the buffered path for a deterministic
  child. *Acceptance:* a parity test asserts field-for-field equality across both
  modes for the same fixture child.

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

- **FR-5.1** `status`/`status --json` expose an advisory `last_event_age_s` for a
  running, streamed step (age of the newest streamed event); `null` when not
  streaming/applicable. *Acceptance:* `--json` for a running streamed step carries
  a numeric `last_event_age_s`; a non-streamed run carries `null`.
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
- **`status --json` freshness field** (additive to operator-aids' `schemas/status.json`):
  ```json
  "current_step_freshness": { "last_event_age_s": 3.2 }   // or null
  ```

---

## §7 Security & Privacy

- **Redaction must hold under streaming (the headline).** Every complete line is
  redacted in the parent, per NDJSON line, *before* the first disk write
  (`RedactingWriter.append_line`). No raw bytes at rest, ever — which is exactly
  why v1 **rejects** the simpler child→file redirect (it would write raw output).
  A truncated trailing line (no `\n`) is never written live (FR-2.4), so a partial
  secret cannot leak via the live file.
- **Fail-closed on sink/redaction failure** (FR-6.2): a write or redaction error
  fails the step, never degrades to silent or un-redacted output.
- **The live console tail serves only redacted bytes** — it reads the same
  redacted `events.jsonl`; no new endpoint, no auth/transport change, loopback +
  per-serve token unchanged. Path containment for `logs`/tail is inherited.
- **No expansion of what is captured** — the same events as today, written sooner.

---

## §8 Implementation Plan (phased, assumption-validating)

Riskiest-assumption-first; no forward dependencies. Each phase ends green + a
commit. The streaming flag stays **off by default** until P2 proves parity +
redaction; default flips after P4.

| Phase | Deliverable | Assumption it validates |
|-------|-------------|--------------------------|
| **P1** | Incremental, deadlock-safe, newline-framing reader in `run_with_timeout` (sink = in-memory), behind the flag; the deadlock stress tests + the `ProcessOutput` parity tests. (FR-1) | **The load-bearing one (§1.3):** we can stream deadlock-free with field-for-field parity, giving up `communicate()`. Everything depends on this. |
| **P2** | Live redacted persistence for **both** CLI adapters: wire the sink to `StepLogger`→`RedactingWriter` per line, with the Claude (`stream-json`) and Codex (`--json`) line→event parsers; the no-raw-secret-on-disk test + the per-adapter transcript/result parity tests. (FR-2, FR-6, FR-7) | The fail-closed redaction invariant survives streaming and the authoritative result is byte-identical, on both adapters. Depends on P1. |
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
| A complete line is not valid JSON (leaked log line / contract break). | The live writer captures it verbatim (redacted) and does not gate on validity (FR-2.5); the end-of-step strict parse applies the existing fail-closed policy, so behavior matches today. |
| Hot-path regression (timeout/kill/parse/usage) on either adapter. | Field-for-field + per-adapter golden parity tests (FR-1.4/FR-2.3/FR-7.2); flag default-off until proven; default flip only after P4. |
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
5. **Default-on timing.** Which release flips the streaming flag's default to on
   after parity is proven — this PRD's P4, or a later soak? Record.

---

*Handoff: this is **Draft v0.2**, sequenced **after** `operator-aids` (it extends
that PRD's `logs` and `--json`; it does not block or amend it). The riskiest
assumption is §1.3 — giving up `communicate()` for an incremental reader with full
parity and fail-closed redaction — attacked first in P1 behind a flag. OQ-1
(adapter scope) is now **resolved** — Claude + Codex in v1, API a durable Non-Goal;
OQ-2–5 remain live. Next step is `gauntlet run live-run-observability`, which
begins with **adversarial review** — not implementation. I ratify; the pipeline
executes.*

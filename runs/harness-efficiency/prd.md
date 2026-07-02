# PRD: Harness Efficiency & Resilience

**Status:** Draft v0.1
**Author:** John Pletka (drafted with Claude from a four-track code exploration, 2026-07-01)
**Date:** 2026-07-01
**Working name:** harness-efficiency
**Relationship to existing artifacts:** Does **not** amend `PRD-gauntlet.md` or any approved `prd.md`/`plan.md`/`policy.yaml`. Builds on existing machinery: the manifest write-ahead state machine (`engine/manifest.py`, `engine/orchestrator.py`), the adversarial cycle (`engine/cycle.py`), the adapter layer (`adapters/`), the status contract (`engine/operator.py`, `schemas/status.json`), and the console (`web/`). Two FRs (FR-6.4 doctor model probes, FR-8 gate context) deliver items already recorded as gaps in `BOOTSTRAP-NOTES.md` (#24, #54-adjacent) and `FUTURE.md`; they implement those recorded follow-ups rather than amending anything approved. No judge `policy.yaml` change is in scope (see Non-Goals).

## §1 Overview

### 1.1 Problem statement

Three failure classes observed in live runs (2026-06-29 → 2026-07-01) each cost human time or model spend:

1. **Usage-limit halts repeat expensive work.** No adapter detects a quota/429/usage-limit condition (`grep` over `src/` for `429|quota|rate.limit|overloaded` returns zero matches). A 5-hour-limit hit mid-implement surfaces as a generic `AgentFailedError`, the step is marked FAILED, and resume policy either parks on a dirty worktree or (`reset_to_base`) discards the partial work and re-runs the step from scratch — even though the CLI session id is persisted and both `claude --resume` and `codex exec resume` are wired (`adapters/claude_code.py:149`, `adapters/codex.py:134`). A killed adversarial cycle additionally loses the whole in-flight round: review findings, triage verdicts, and confirm output are all re-derived.

2. **Host sleep silently stalls or spuriously kills runs.** Timeouts are monotonic wall-clock deadlines in `adapters/process.py` with no suspend awareness and no heartbeat. Closing the laptop lid suspends the driver and its subprocesses; on wake the run either sits stalled with no signal distinguishing "agent thinking" from "host slept", or the elapsed deadline fires and SIGKILLs a healthy step. Recovery is manual every time.

3. **Malformed structured artifacts require hand-editing.** The plan-author writes a `gauntlet-phases` YAML block into `plan.md` with no validation at authoring time; the block is only parsed later by `phase_lint` (`engine/steptypes.py:145–186`), which fails closed (HALTED) with no repair loop and no sanctioned hand-edit-then-revalidate path.

Beyond the failure classes, two efficiency problems compound on larger PRDs: **context assembly inlines full documents** — every implement call receives the entire `prd.md` + `plan.md` verbatim (~60KB+ per call, × N phases; `engine/steptypes.py:454–494`), and artifact-mode review rounds 2+ re-send the full document (`engine/cycle.py:623–660`) — burning the same 5-hour usage budget the runs keep hitting. And **model capability knobs sit unused**: `--effort` (claude) and `model_reasoning_effort` (codex) are verified live in `.gauntlet/pins.yaml` but configured nowhere; escalation always jumps from `gpt-5-mini` to `gpt-5` (9× cost) regardless of finding severity; mechanical steps (commit-message drafting, resume-disposition) run on full-tier profiles.

Finally, the operator (human or `gauntlet-operator` skill) is partially blind: `status --json` exposes no current-step elapsed time, no token/cost trajectory, no step `notes`, and no structured halt reason — a HALTED step's cause lives only in the transcript, and gates omit convergence history and prior rejections.

### 1.2 Solution summary

Harden the run lifecycle end-to-end without changing the pipeline model: (a) classify adapter failures as transient-vs-terminal with usage-limit detection, park (don't fail) on transient, and resume by continuing the persisted CLI session against the preserved worktree; (b) checkpoint adversarial-cycle sub-steps so resume re-enters mid-round; (c) make deadlines suspension-aware via a driver heartbeat that detects sleep gaps and credits them back to the step; (d) validate agent-authored structured blocks at authoring time with a bounded in-session repair loop, and park-with-hand-edit-then-revalidate as the fallback; (e) scope context per stage — implement steps get the current phase's plan section plus artifact *paths* (the CLI agents already have Read access), artifact re-reviews get a diff since the last reviewed version; (f) wire `effort` into agent profiles, tier escalation by severity, and move mechanical steps to cheap profiles, with `doctor` probing every profile's model resolution; (g) enrich `status --json` and gate views so every park/halt/failure is explainable without opening a transcript.

### 1.3 The assumption this validates

**A CLI agent session interrupted by an external condition (usage limit, host sleep) can be continued — same session id, preserved worktree, short continuation prompt — and produce work equivalent to an uninterrupted run.** If this holds, interruption becomes a pause instead of a restart, and the harness's dominant waste (repeated work after halts) disappears. If it fails (e.g., resumed sessions lose tool state or drift), the fallback is today's behavior — full step re-run — and the remaining FRs (context scoping, tiering, observability) still stand on their own. P1 attacks this assumption directly.

## §2 Goals and Non-Goals

### 2.1 Goals

| ID | Outcome | Need served |
|----|---------|-------------|
| G1 | A usage-limit halt loses zero completed sub-steps and continues the same agent session on resume | Runs on large PRDs survive the 5-hour limit without repeating expensive work |
| G2 | A run spanning host sleep neither stalls silently nor kills a healthy step; state is explainable on wake | Laptop-hosted runs survive lid-close unattended |
| G3 | A malformed structured block (plan YAML, findings JSON) is repaired in-session or hand-fixable with revalidation — never a dead-end | No manual file surgery to unstick a run |
| G4 | Per-call context is scoped to what the stage needs; large artifacts travel by reference or diff | Faster, cheaper, sharper agent calls; slower usage-budget burn |
| G5 | Model effort/tier matches step difficulty; escalation cost is severity-gated; misconfigured models caught at `doctor` time | Quality where it matters, savings where it doesn't, no mid-run 404s |
| G6 | Every run state (parked/halted/failed/interrupted, elapsed, cost) is explainable from `status --json` alone | Humans and the operator skill decide without reading transcripts |

### 2.2 Non-Goals (v1)

- **Phase-completeness gating** (acceptance-clause→test mapping, completeness-critic review — BOOTSTRAP-NOTES #54 preventions). Deserves its own PRD; conflating it here would double this document's blast radius.
- **Worktree isolation / concurrent same-repo runs** (already deferred in FUTURE.md).
- **LLM-based summarization of artifacts** for context reduction. v1 scopes context deterministically (phase excerpts, diffs, paths) — determinism over cleverness; no new lossy agent step whose output quality we'd then have to review.
- **Dynamic/cost-based model routing** (choosing models per finding at runtime by predicted difficulty). v1 is static config: effort per profile, severity-gated escalation. Adaptive routing is post-v1.
- **Batch triage.** Point-by-point triage with untrusted-data wrapping is a deliberate injection-containment design (PRD-gauntlet §8); its per-call overhead is small on `gpt-5-mini`.
- **Judge `policy.yaml` changes** (e.g., the in-repo-write fast-path allow from BOOTSTRAP-NOTES #39). Governed by the retro proposal process, not this PRD.
- **New notification channels** (email, sounds) and provider-side prompt-cache tuning.
- **Amending any approved artifact.** This PRD adds fields and steps; it does not alter approved run artifacts or the canonical spec.

## §3 Users and Personas

- **The human operator (John)** — launches runs on a laptop, leaves for meetings, returns to gates/parks; needs status to be self-explanatory and recovery to be one command.
- **The `gauntlet-operator` skill** — consumes `status --json` and `next_actions` programmatically; every gap in the machine contract is a gap in its triage tree.
- **Pipeline agents (builder/reviewer/triager/fixer)** — receive the scoped context and effort settings this PRD changes; their output quality is the guardrail metric.
- **Scripts/CI** — consume `status --json` under the additive-compatibility policy.

## §4 System Architecture

### 4.1 Components

| Component | Change | New/Touched |
|---|---|---|
| `src/gauntlet/adapters/base.py` | `FailureInfo` classification model (`kind: transient_usage_limit \| transient_overload \| terminal`, `retry_after_s`), carried on `AgentFailedError` | Touched |
| `src/gauntlet/adapters/claude_code.py`, `codex.py`, `api.py` | Detect usage-limit/429/overload markers from structured CLI error output; populate `FailureInfo`; pass `effort` (claude `--effort`, codex `-c model_reasoning_effort=`, api `reasoning_effort`) — flag support already verified in `.gauntlet/pins.yaml` | Touched |
| `src/gauntlet/adapters/process.py` | Suspension-aware deadline accounting (consume heartbeat gap credits) | Touched |
| `src/gauntlet/engine/heartbeat.py` | Driver heartbeat file (monotonic + wallclock pair, every N s); sleep-gap detection | **New** |
| `src/gauntlet/engine/manifest.py` | `StepRecord.halt_reason` (engine-stamped enum), `parked_reason: usage_limit`, cycle sub-step checkpoint records, suspension intervals | Touched |
| `src/gauntlet/engine/orchestrator.py` | Transient-park handling (preserve worktree + session; bypass dirty-tree conflict logic for `usage_limit` parks), continuation-prompt resume path | Touched |
| `src/gauntlet/engine/cycle.py` | Sub-step checkpoint/reuse on resume; diff-scoped artifact re-review (round 2+ gets artifact diff vs last-reviewed snapshot) | Touched |
| `src/gauntlet/engine/steptypes.py` | Per-input context mode (`inline \| reference \| phase`) in `_render_prompt`; in-step artifact validators with bounded repair loop | Touched |
| `src/gauntlet/engine/validators.py` | Named artifact validators (`plan_phases` wrapping `planphases.extract_phases`, JSON-schema validators) invocable from `agent_task` | **New** |
| `src/gauntlet/engine/config.py` | `AgentProfile.effort`; `resume_on_quota` config; heartbeat/caffeinate knobs | Touched |
| `src/gauntlet/engine/operator.py`, `schemas/status.json` | Additive status fields: elapsed, timeout remaining, usage totals, step notes, halt_reason, suspension info, quota reset time | Touched |
| `src/gauntlet/web/gate.py`, templates | Convergence summary, prior-response history, per-finding triage reasoning in GateView | Touched |
| `src/gauntlet/cli.py` (`doctor`) | Per-profile model-resolution probe (one-token round trip) + effort-flag acceptance check | Touched |
| `pipelines/standard.yaml`, `.gauntlet/config.yaml` | Effort defaults; cheap profiles for `commit_message`/disposition; context-mode opt-ins | Touched |

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Transient failure handling | **Park** with `parked_reason=usage_limit`, never FAILED; worktree and session preserved | Fail closed (run halts) but recoverable without decision-cost; FAILED implies re-run semantics that destroy work. Determinism: the park is a first-class state, not a retry loop hidden in an adapter. |
| Failure classification | Allowlist of structured markers from CLI JSON error envelopes; anything unrecognized → `terminal` | Fail closed: never auto-continue past an unknown error. Markers pinned + contract-tested like `.gauntlet/pins.yaml` (BOOTSTRAP-NOTES #26 lesson: re-run contract tests when the pinned CLI changes). |
| Context reduction mechanism | Deterministic scoping (phase excerpt, artifact diff, path references) — no summarizer agent | CLI agents already have repo Read access; a path reference is lossless. Determinism over cleverness; nothing new to review. |
| Reference mode availability | Only valid for adapters with file access (`claude-code`, `codex`); pipeline load **fails** if a reference-mode input targets the `api` adapter | Fail closed at load time, not silently degraded mid-run. |
| Sleep handling | Credit detected suspension gaps back to the step deadline, bounded by a cap; heartbeat distinguishes "host slept" from "agent silent" | The timeout's intent is to bound *agent* runtime, not laptop lid position. Cap keeps the fail-closed property (a truly wedged step still dies). |
| Malformed-artifact recovery | In-step bounded repair loop (same session, error fed back) → then **park** with `parked_reason=artifact_invalid`; plain `resume` revalidates the (possibly hand-edited) artifact without re-running the author | Mirrors the proven schema-retry loop in `cycle.py:451–464`; the park+revalidate path makes hand-editing sanctioned and audited instead of off-book file surgery. `phase_lint` stays as the backstop gate. |
| Effort/tier configuration | Static per-profile/per-step config, severity-gated escalation | Inspectable, resumable, testable. Dynamic routing is a Non-Goal. |
| Status enrichment | Additive fields only; `schema_version` stays 1 per the documented compatibility policy | Existing consumers keep working. |

## §5 Functional Requirements

### FR-1 — Scoped context assembly

- **FR-1.1** `agent_task` inputs support a per-input mode: `inline` (today's behavior, default), `reference` (inject the artifact's repo-relative path plus a one-line instruction to read it), and `phase` (plan.md only: inject the current `foreach` phase's section of the plan plus the path to the full document). The shipped `standard.yaml` implement step uses `prd.md: reference`, `plan.md: phase`.
  *Acceptance:* for a 3-phase run with a 34KB PRD and 27KB plan, the persisted `prompt.md` of each implement step contains the phase excerpt and both paths, and is ≤ 25% of the byte size of the equivalent all-inline prompt; an integration run completes the phase normally.
- **FR-1.2** Artifact-mode adversarial cycles snapshot the artifact at each review handoff; rounds 2+ send the reviewer the unified diff (snapshot → current) plus carried findings plus the artifact path, not the full text.
  *Acceptance:* unit test: round-2 review prompt for a document with a 40-line edit contains the diff and carried findings, does not contain the full document body, and the snapshot used is the round-1 version.
- **FR-1.3** Pipeline load fails with a named error if a `reference`/`phase`-mode input is bound to a profile whose adapter lacks file access (`api`).
  *Acceptance:* unit test: loading such a pipeline raises before any step runs; error names the step, input, and profile.

### FR-2 — Structured-artifact validation with repair

- **FR-2.1** `agent_task` supports `validate: <name>` (e.g., `plan_phases`, or a JSON-schema ref) executed against the step's `output` artifact inside the step. On validation failure the engine re-invokes the same agent session with the exact parse/validation error and instruction to correct the artifact, bounded at 2 repair attempts.
  *Acceptance:* unit test with a stub adapter emitting malformed `gauntlet-phases` YAML then a corrected version: step succeeds on attempt 2; both attempts appear in the transcript; the persisted artifact parses.
- **FR-2.2** After repair attempts are exhausted, the step parks (not FAILED) with `parked_reason=artifact_invalid` and the validator error verbatim in `notes`. A plain `gauntlet resume` re-runs **only the validator**; if the artifact now passes (hand-edited or not), the step completes DONE with an audit note recording that validation passed on resume.
  *Acceptance:* unit test: park → hand-edit artifact on disk → `resume` → step DONE without a new adapter invocation; manifest notes record the revalidation path.
- **FR-2.3** The plan-author step in shipped pipelines carries `validate: plan_phases`; `phase_lint` remains in place as the fail-closed backstop.
  *Acceptance:* `pipelines/standard.yaml` inspection test; `phase_lint` behavior unchanged by existing tests.

### FR-3 — Usage-limit and transient-failure resilience

- **FR-3.1** Adapters classify every nonzero-exit/error outcome into `FailureInfo{kind, retry_after_s?}` where `kind ∈ {transient_usage_limit, transient_overload, terminal}`, using only structured fields of the CLI/API error output (claude JSON `is_error`/subtype/message markers; codex `turn.failed` payloads; LiteLLM exception classes for 429/overload). Unrecognized errors are `terminal`.
  *Acceptance:* unit tests with captured real error envelopes (one per adapter per kind) assert the classification; an unknown error envelope asserts `terminal`.
- **FR-3.2** On a `transient_*` failure of an agent step: status → PARKED with `parked_reason=usage_limit` (new enum value), the worktree is left untouched (no reset, no conflict park), `session_id` is preserved, and `retry_after_s`/reset time (when reported) is stamped into the step record.
  *Acceptance:* unit test: transient failure mid-implement leaves dirty worktree intact, manifest shows the park with reset time, and `status --json` reports state `parked_for_response`-distinct (`parked_usage_limit`) with a `resume` next-action.
- **FR-3.3** `gauntlet resume` on a `usage_limit` park continues the persisted session (`--resume <session>` / `codex exec resume`) with a short continuation prompt ("you were interrupted by a provider usage limit; continue the task; the worktree is as you left it") instead of the full original prompt. The dirty-worktree-vs-`base_sha` check is bypassed for this park kind only. If the adapter reports the session unknown/expired, fall back to today's full re-run path and record the fallback in `notes`.
  *Acceptance:* unit test: resume invokes the adapter with the stored session id and continuation prompt; expired-session stub falls back to full prompt with a manifest note. Integration test (marked `integration`): a claude-code session interrupted and resumed completes a small task with the file state produced before interruption intact.
- **FR-3.4** Auto-resume is config-gated: `resume_on_quota: notify` (default — park + notification carrying the reset time) or `auto` (driver schedules a resume at `retry_after`; notification either way).
  *Acceptance:* unit test: `notify` mode never re-invokes; `auto` mode with a stubbed clock resumes once at the scheduled time and re-parks (no hot loop) if the limit persists.

### FR-4 — Adversarial-cycle sub-step checkpointing

- **FR-4.1** Each cycle sub-step (review, per-finding triage batch, fix, confirm) records completion write-ahead in the manifest with its round number and artifact path. On resume of a RUNNING/INTERRUPTED cycle, completed sub-steps of the current round are loaded from their persisted artifacts and not re-executed; execution re-enters at the first incomplete sub-step.
  *Acceptance:* unit test: kill (simulated) after `findings.json` of round 1 is persisted → resume runs triage without re-invoking the reviewer, reusing the persisted findings; a second test resumes after triage and re-enters at fix.
- **FR-4.2** Checkpoint reuse is guarded by the round's handoff SHA: if the worktree/handoff moved since the checkpoint (e.g., manual commits during the park), checkpoints are invalidated and the round restarts, with the invalidation reason in `notes`.
  *Acceptance:* unit test: checkpoint + moved handoff SHA → full round re-run with audit note.

### FR-5 — Suspend/sleep resilience

- **FR-5.1** The driver writes a heartbeat (`monotonic_s`, `wallclock_utc`) to the run-instance dir every 15s. A suspension is detected when consecutive heartbeats show `Δwallclock − Δmonotonic > 30s`; detected intervals are appended to the manifest.
  *Acceptance:* unit test with injected clock pairs detects a synthetic 40-minute gap and records the interval; sub-threshold jitter records nothing.
- **FR-5.2** Step deadline accounting credits detected suspension intervals back to the running step's remaining timeout, up to a configurable cap (`suspend_credit_cap_s`, default 12h); past the cap the step halts with `halt_reason=timeout` as today.
  *Acceptance:* unit test: a step with a 600s timeout spanning a synthetic 1h suspension is not killed and completes; the same step with the cap set below the gap halts with `halt_reason=timeout` and the suspension interval in evidence.
- **FR-5.3** `status` (human and `--json`) surfaces heartbeat age and detected suspension intervals, and the stalled-run heuristic distinguishes `host_suspended` (heartbeat gap) from `driver_orphaned` (process dead) from `agent_silent` (alive, no events).
  *Acceptance:* unit test: three synthetic states produce the three distinct classifications in `status --json`.
- **FR-5.4** Opt-in config `keep_awake: true` wraps the driver in `caffeinate -i` on darwin (default **false** — changing host power behavior is an explicit human choice per CLAUDE.md's machine-state rule).
  *Acceptance:* unit test: command construction includes/excludes the wrapper per config; non-darwin ignores the flag with a warning.

### FR-6 — Model effort tiering and preflight

- **FR-6.1** `AgentProfile` gains optional `effort`; adapters pass it through (claude `--effort`, codex `-c model_reasoning_effort=`, api `reasoning_effort`). Cycle sub-agent overrides (`reviewer`, `triager`, `fixer`, `confirmer`) and per-step `effort:` accept the same values.
  *Acceptance:* unit tests per adapter assert the flag lands in the constructed command/params; profile-level and step-level settings compose with step winning.
- **FR-6.2** Triage escalation is severity-gated: low-confidence verdicts on `blocking`/`major` findings escalate to the escalation profile; low-confidence on `minor`/`nit` do not escalate — they carry to the gate marked `low_confidence` for human eyes.
  *Acceptance:* unit test: mixed-severity low-confidence verdicts produce exactly the blocking/major escalation calls; minor/nit appear in the gate payload flagged, with no escalation-profile invocation.
- **FR-6.3** Shipped config runs mechanical steps — commit-message drafting (`message_agent`) and resume-disposition emission — on a designated cheap profile (e.g., `mechanic: {adapter: api, model: gpt-5-mini}` or a haiku-class claude profile), not the builder profile.
  *Acceptance:* config inspection test + wiring test that the commit step invokes the cheap profile; drafted message still passes the format validator.
- **FR-6.4** `gauntlet doctor` probes every configured profile: a minimal live round-trip that verifies the model id resolves and (where set) the effort flag is accepted, reporting the resolved model name per profile. Probe failures are FAIL rows (delivers BOOTSTRAP-NOTES #24 / FUTURE.md F-004 residual).
  *Acceptance:* doctor run with a deliberately bad model alias in one profile reports that profile FAIL and the others PASS; probes are skipped with a WARN (not silent PASS) when the relevant CLI is unauthenticated.

### FR-7 — Status contract enrichment

- **FR-7.1** `status --json` adds (additive, `schema_version` stays 1): `current_step_elapsed_s`, `current_step_timeout_remaining_s`, `run_elapsed_s`, `totals` (run-level UsageTotals incl. cost), `agent_usage` (per-profile), and per-entry `steps[].duration_s`, `steps[].notes`, `steps[].halt_reason`; plus `quota` block (reset time) when parked on `usage_limit` and `suspension` block per FR-5.3.
  *Acceptance:* schema test: all new fields present (nullable, never omitted) and `additionalProperties: false` still validates; golden test comparing manifest fixture → status JSON.
- **FR-7.2** The engine stamps a structured `halt_reason` enum on every non-DONE terminal step record: `{timeout, budget, usage_limit, judge_deny, signal_kill, adapter_error, precondition, artifact_invalid, operator_recover}`. Human-authored `notes` remain free text; the enum is engine-written and mandatory for HALTED/FAILED/INTERRUPTED/PARKED.
  *Acceptance:* unit tests per terminal path assert the stamped enum; `gauntlet recover` stamps `operator_recover` with the operator identity.
- **FR-7.3** Human `gauntlet status` renders elapsed time, cost so far, and — when parked — the reason, the reset time (quota), or the awaited decision, so no parked state requires a transcript read to identify the next command.
  *Acceptance:* snapshot tests of the rendered footer for each park/halt kind include the identifying line and the next-action command.

### FR-8 — Gate decision context

- **FR-8.1** GateView (web + a new `status --json` `gate` block when parked at a gate) includes: cycle convergence summary (rounds run, findings raised/fixed/declined per round — sourced from existing `metrics`), prior human responses/rejections for this gate with timestamps, and per-escalated-finding triage `reasoning`.
  *Acceptance:* unit test: a two-round cycle with one rejection renders all three sections from manifest + artifacts alone (no transcript access).
- **FR-8.2** Gate `next_actions` entries carry a one-line consequence description (approve → what proceeds; reject → which cycle re-runs with the notes injected).
  *Acceptance:* golden test of `next_actions` payload for a gate downstream of an adversarial cycle names the cycle that a rejection re-runs.

## §6 Data & Schemas (normative excerpts)

**FailureInfo (adapter → engine, carried on AgentFailedError):**
```json
{ "kind": "transient_usage_limit | transient_overload | terminal",
  "retry_after_s": 1234,
  "marker": "usage_limit_reached",
  "raw_excerpt": "<=500 chars of the structured error field>" }
```

**StepRecord additions (manifest):**
```json
{ "halt_reason": "timeout | budget | usage_limit | judge_deny | signal_kill | adapter_error | precondition | artifact_invalid | operator_recover | null",
  "quota_reset_at": "2026-07-01T18-00-00Z | null",
  "checkpoints": [ { "sub_step": "review", "round": 1, "artifact": "artifacts/r1/findings.json", "handoff_sha": "abc123" } ] }
```

**Manifest additions:** `suspensions: [ { "start": "...Z", "end": "...Z", "gap_s": 2400 } ]`

**status.json additions (all always-present, nullable):** `current_step_elapsed_s`, `current_step_timeout_remaining_s`, `run_elapsed_s`, `totals {input_tokens, output_tokens, cached_input_tokens, cost_usd}`, `agent_usage {<profile>: totals}`, `quota {reset_at} | null`, `suspension {last_heartbeat_age_s, intervals[]} | null`, `gate {...} | null`, and per `steps[]` entry: `duration_s`, `notes`, `halt_reason`.

**Pipeline YAML — input modes and validators:**
```yaml
- id: implement
  type: agent_task
  agent: builder
  inputs:
    - { name: prd.md,  mode: reference }
    - { name: plan.md, mode: phase }
- id: plan-author
  type: agent_task
  validate: plan_phases
```

**Heartbeat file (`run-<ts>/heartbeat.json`, overwrite-in-place atomic):**
```json
{ "monotonic_s": 12345.6, "wallclock_utc": "2026-07-01T17-42-10Z", "pid": 4242 }
```

## §7 Security & Privacy

- **Failure classification fails closed.** Only structured error fields are matched against a pinned allowlist; free-text prose is never trusted for classification; unknown → `terminal` (run halts for a human). Markers live beside `.gauntlet/pins.yaml` contract tests and are re-verified when a pinned CLI version changes.
- **Continuation prompts add no new capability.** Resume reuses the same profile, judge hooks, and sandbox as the original invocation; the judge gates the resumed session identically (no bypass path).
- **Hand-edit revalidation is audited.** An `artifact_invalid` park that passes validation on resume records that the artifact changed while parked (content hash before/after) in the manifest — hand edits are visible in the audit trail, not laundered.
- **`keep_awake` is opt-in** and process-scoped (`caffeinate -i` on the driver only); default off because altering host sleep behavior is a human decision.
- **Reference-mode context widens nothing:** builder/reviewer agents already hold repo read access; a path reference exposes no file the inline mode didn't.
- **Status/gate enrichment reuses the existing redaction path** (`logging/redact.py`) for any content-bearing field (notes, raw_excerpt).

## §8 Implementation Plan (phased, assumption-validating)

| Phase | Deliverable | Assumption validated |
|---|---|---|
| P1 | Failure classification + `usage_limit` park + session-preserving resume (FR-3.1–3.3), incl. the marked integration test | **The core bet (§1.3):** an interrupted CLI session can be continued with preserved worktree and produce correct work. Also proves the markers are detectable from structured output. |
| P2 | Heartbeat + suspension detection + deadline credit + `keep_awake` (FR-5) | Sleep gaps are reliably detectable from clock skew and can be credited without masking real hangs (cap works). |
| P3 | Engine-stamped `halt_reason` + status contract enrichment (FR-7), surfacing P1/P2 states | The manifest already holds (or now holds) everything needed to explain any state without transcripts — pure additive rendering. |
| P4 | In-step validators + repair loop + `artifact_invalid` park/revalidate (FR-2) | An agent given its own parse error in-session fixes the artifact within 2 attempts most of the time; the hand-edit path covers the rest. |
| P5 | Cycle sub-step checkpointing (FR-4) | Persisted round artifacts + handoff SHA are sufficient to re-enter a round safely. |
| P6 | Scoped context: input modes + artifact-diff re-review (FR-1) | Reference/phase-scoped context does not degrade builder/reviewer output (guardrail: cycle findings-per-phase and gate outcomes on a comparison run). |
| P7 | Effort tiering + severity-gated escalation + cheap mechanical profile + doctor probes (FR-6) | Effort/tier settings measurably cut cost without raising blocking-finding escape rate. |
| P8 | Gate context enrichment (FR-8) | Gate decisions are makeable from the gate view alone. |

Auto-resume (`resume_on_quota: auto`, FR-3.4) lands in P2 (needs the driver to survive the wait; pairs with heartbeat). No phase depends on a later phase; P3 renders whatever P1/P2 stamped, P8 renders what P5's checkpoints and existing metrics already persist.

## §9 Success Metrics

- **Interruption waste:** after a usage-limit halt, resumed runs repeat **0** completed steps and **0** completed cycle sub-steps (manifest-verifiable); session continuation (vs full-re-run fallback) succeeds in ≥ 80% of quota resumes.
- **Sleep:** a run spanning ≥ 30 min of host sleep completes with **0** manual interventions and **0** spurious timeout kills; `status` names the suspension interval.
- **Structured artifacts:** **0** hand-edits-without-audit-trail needed for malformed `gauntlet-phases` blocks; ≥ 80% of validation failures repaired in-session within 2 attempts.
- **Context:** implement-step prompt payload ≤ 40% of the all-inline baseline on runs with ≥ 3 phases; artifact re-review round-2 payload ≤ diff + findings + 1KB scaffold.
- **Cost shape:** triage + escalation + mechanical steps ≤ 10% of run cost (per `agent_usage`); escalation-profile calls occur only for blocking/major findings.
- **Observability:** for every park/halt/fail state reachable in the test suite, `status --json` alone identifies cause and next command (asserted by a table-driven test); `doctor` catches a bad model alias before any run step executes.
- **Quality guardrail (must not regress):** blocking findings per phase and gate rejection rate on a comparison run with scoped context are within noise of the inline baseline (Open Question Q3 sets the comparison protocol).

## §10 Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Resumed CLI sessions drift or lose tool state, producing subtly wrong continuations | P1 integration test exercises a real interrupted resume; fallback to full re-run is automatic on unknown/expired session (FR-3.3); the adversarial cycle still reviews the result either way |
| Usage-limit error envelopes change across CLI versions | Markers pinned with captured fixtures + contract tests tied to `.gauntlet/pins.yaml`; unknown envelope fails closed to `terminal` (FR-3.1) |
| Suspension credit masks a genuinely hung step | Credit cap (FR-5.2) + `agent_silent` freshness classification (FR-5.3) keep a hard upper bound and a distinct signal |
| Scoped context degrades implementation quality | Opt-in per input, guardrail metric in §9, P6 comparison run before flipping shipped defaults; full documents always readable by path |
| Repair loop oscillates with an incorrigible author | Bounded at 2 attempts then `artifact_invalid` park (FR-2.2); backstop `phase_lint` unchanged |
| Checkpoint reuse resumes against a moved worktree | Handoff-SHA guard invalidates checkpoints (FR-4.2), falling back to a full round |
| Cheap mechanical profiles produce format-invalid commit messages | Existing message-schema validation loop retained; escalate-to-builder-profile fallback after retries |
| Status additions break `additionalProperties: false` consumers | Additive-only with always-present nullable fields, per the documented compatibility policy; schema test in FR-7.1 |

## §11 Open Questions

- **Q1 — Auto-resume default.** FR-3.4 proposes `notify` as default with `auto` opt-in. Is unattended auto-resume at quota reset acceptable on a laptop (driver must stay alive through the wait, possibly through sleep — interacts with FR-5)? *Proposal: notify-only default; auto documented as requiring `keep_awake` or an external scheduler.*
- **Q2 — Effort values.** Which effort levels per profile/step? *Proposal to react to: builder implement=high, builder fix=medium, reviewer round-1=high, re-review/confirm=medium, triage/mechanic=low.* Needs a live A/B before shipping as defaults.
- **Q3 — Scoped-context rollout.** Opt-in knob only in v1, or flip `standard.yaml` defaults after one clean comparison run? What is the comparison protocol (same PRD re-run, or side-by-side on the next real run)? *Proposal: opt-in in P6, flip defaults in a follow-up once §9 guardrail holds on one real run.*
- **Q4 — Escalation-park response coverage.** BOOTSTRAP-NOTES #51 recorded that `resume --response` did not reach an adversarial_cycle FR-10.4 park; later work (`test_cycle_resume_response.py`, the resume-terminal-cycle path) may have closed part of this. Verify remaining gaps during P1 planning; if a park variant is still unreachable by `--response`, add it to FR-3's scope.
- **Q5 — Threshold ratification.** The numbers in §9 (80% session-resume success, ≤40% payload, ≤10% cost share, 2 repair attempts, 15s/30s heartbeat constants, 12h credit cap) are proposals, not measurements. Ratify or adjust at the PRD gate.
- **Q6 — `signal_kill` attribution.** FR-7.2 stamps `operator_recover` when `gauntlet recover` kills a step, but an external `kill -9`/crash is indistinguishable from power loss at stamp time. Is post-hoc attribution on next resume ("stamped on reconciliation") sufficient? *Proposal: yes — stamp `signal_kill` during resume reconciliation with a note that attribution is inferred.*

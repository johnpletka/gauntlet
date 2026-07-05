I have enough grounding — the codebase matches the PRD's component map (`AdapterCapabilities` has `repo_write`/`structured_output`/`resume` but no `reads_repo`; `StepRecord` has `parked_reason` with values `upstream_conflict`/`cycle_escalation` but no `halt_reason`; `heartbeat.py`/`validators.py`/`ledger.py` don't yet exist). Here is the plan.

---

# Implementation Plan: Harness Efficiency & Resilience

**Plan for:** `runs/harness-efficiency/prd.md` (Draft v0.4, approved)
**Author:** builder agent, 2026-07-02
**Status:** Draft for adversarial review

## Orientation

This plan executes the PRD's §8 phasing verbatim in ordering and phase membership — the human ratified §8, and the engine's `foreach: plan.phases` fans over the machine-readable block below, so the two must not drift. Each phase kills one assumption, ends in passing tests and a single `PN:` commit, and depends only on phases before it (FR-10.3). The riskiest bet — that an interrupted CLI session resumes cleanly (§1.3) — is P1, so a failure there reshapes the rest before we've built on it.

The organizing constraint (PRD §1.2): **the builder's provider window is the scarce resource.** Phases P1–P3 protect and explain it; P4–P8 sharpen and observe it; P9–P11 bound and meter it.

### Cross-cutting conventions (apply to every phase)

- **Fail closed.** Every new classifier, validator, or admission check defaults to halt/park on the unrecognized case. No new path silently continues.
- **Additive schemas only.** `schemas/status.json` and manifest records gain fields; none are removed or renamed. `schema_version` stays `1` (FR-7.1).
- **Two disjoint reason fields.** `parked_reason` (park enum) and `halt_reason` (terminal enum) are never both set. P1 writes only `parked_reason`; **P2 introduces the `halt_reason` field with the single `timeout` value it needs** (the suspend-cap halt, FR-5.2) and never sets it alongside `parked_reason`; P3 completes the `halt_reason` enum and the disjointness invariant. No phase sets both fields on one record.
- **Config is opt-in.** Every new knob defaults to today's behavior (`inline` context, `notify` auto-resume, `keep_awake: false`, advisory ledger, `keep` checkpoints). Shipped `standard.yaml` defaults flip only where the PRD says so, and effort *values* are explicitly out of scope (Q2).
- **Pinned markers carry fixtures.** Any CLI-error or effort-flag fact is pinned beside `.gauntlet/pins.yaml` with a captured fixture and a contract test (BOOTSTRAP-NOTES #26).

---

## P1 — Failure classification + `usage_limit` park + session-preserving resume

**Assumption validated (the core bet, §1.3):** an interrupted CLI session — same `session_id`, preserved worktree, short continuation prompt — continues and produces work equivalent to an uninterrupted run. Secondarily: usage-limit conditions are detectable from *structured* CLI error envelopes, not prose.

**Deliverables (FR-3.1–3.3):**
- `adapters/base.py`: a `FailureInfo` model (`kind: transient_usage_limit | transient_overload | terminal`, `retry_after_s: int | None`, `marker: str`, `raw_excerpt: str` ≤500 chars) carried on `AgentFailedError`. A per-adapter `classify_failure` that matches **only** structured field paths against the pinned allowlist (§6 table): claude JSON `is_error`/`subtype` (and a pinned exact-string/regex `message` entry), codex `turn.failed` `error.code`/`error.type`, LiteLLM exception classes. `retry_after_s` read only from a typed field. Anything unmatched → `terminal`.
- The failure-marker allowlist + captured real error-envelope fixtures (one per adapter per kind) stored beside `.gauntlet/pins.yaml`; a contract test asserts every allowlist entry has a matching fixture.
  - **Execution note (fixture source):** harvest these fixtures from existing failed-run transcripts — `runs/*/run-*/steps/*/events.jsonl` from the 2026-06-29 through 2026-07-01 quota halts — rather than waiting to hit a live usage limit, so the classifier is validated against the observed failure shape and not a synthetic stand-in. Before a harvested envelope is pinned it is passed through the existing redaction path (`logging/redact.py`) and truncated to the classifier's typed fields plus the ≤500-char `raw_excerpt`; only the structured field paths the allowlist matches on are retained. No raw transcript bytes, credentials, or free-form prose beyond the truncated excerpt land in a committed fixture.
- `engine/orchestrator.py`: on a `transient_*` failure of an agent step → status PARKED, `parked_reason=usage_limit` (new value on the existing field), worktree **left untouched** (bypass the reset/dirty-tree-conflict path for this park kind only), `session_id` preserved, `quota_reset_at`/`retry_after_s` stamped into the step record.
- `engine/orchestrator.py` resume path: `gauntlet resume` on a `usage_limit` park continues the persisted session (`claude --resume` / `codex exec resume`, already wired) with a short continuation prompt, bypassing the dirty-worktree-vs-`base_sha` check for this kind only. Unknown/expired session → fall back to today's full re-run and record the fallback in `notes`.
- `engine/operator.py`: map `parked_reason=usage_limit` to a distinct status state (`parked_usage_limit`) with a `resume` next-action (minimal surfacing; full status enrichment is P3).
- `engine/cycle.py`: extend the same transient→park behavior to **adversarial_cycle sub-agent invocations**. A `FailureInfo kind=transient_*` raised from any cycle sub-agent call — reviewer, triager, fixer, or confirmer, **including the schema-retry re-invocations inside `_run_sub`** — parks the **cycle step** with `parked_reason=usage_limit`, worktree left untouched, and the failing sub-agent's `session_id` persisted on the step record; it is **not** a terminal cycle failure and does **not** demand `gauntlet resume --response`. This is the observed real failure mode (a cycle killed by a CLI quota error) and it must park like any other usage-limit hit. On this park, whatever cycle progress the round has recorded is preserved; **plain** `gauntlet resume` (not `--response`) re-drives the cycle. In P1 — before P5's per-round checkpoints exist — resume continues the persisted sub-agent session per FR-3.3 but re-enters the round at its start (the round-loss deferral below); P5 tightens resume to re-enter at the first *incomplete* sub-step.

**Simplest design / non-goals for this phase:** classification is a static allowlist match, not a scoring model. No auto-resume scheduling yet (that is P2, and needs the heartbeat). No `halt_reason` field yet (P3). The continuation prompt is a fixed template string, not a reconstructed context.

**Test strategy:**
- Unit: captured-fixture classification per adapter per kind → asserts `kind` and recorded `marker`; a typed envelope whose only signal is an *unlisted* `message` → `terminal`; unknown/unstructured error → `terminal`; contract test fixture↔allowlist coverage.
- Unit: transient failure mid-implement leaves dirty worktree intact; manifest shows the park + reset time; `status --json` reports `parked_usage_limit` + resume next-action.
- Unit: resume invokes the adapter with stored session id + continuation prompt; expired-session stub → full-prompt fallback with a manifest note.
- Unit (cycle sub-agent park): a `transient_usage_limit` envelope raised from a reviewer sub-step mid-round → the **cycle step** parks `parked_reason=usage_limit` (not a terminal failure, not `cycle_escalation`/`response`), worktree untouched, the reviewer sub-agent `session_id` recorded; a second test raises the transient failure from a schema-retry re-invocation inside `_run_sub` and asserts the same park. Plain `gauntlet resume` (no `--response`) re-drives the parked cycle continuing that session.
- **Integration (`@pytest.mark.integration`, the decisive test):** a real claude-code session is interrupted and resumed on a small task; the file state produced before interruption survives intact.

**Exit criteria:** all unit tests pass under `pytest -m "not integration"`; the integration test passes locally before review handoff; a `usage_limit` park round-trips through park → resume → completion in a unit harness.

**Deferrals:** auto-resume scheduling → P2. `halt_reason` enum + full status fields → P3. Cycle sub-step checkpointing → P5: P1 parks the cycle on a transient sub-agent failure and persists the sub-agent session, but a killed *cycle* still loses its round's completed sub-steps until P5's checkpoints let resume re-enter at the first incomplete sub-step.

---

## P2 — Suspend/sleep resilience + auto-resume scheduler

**Assumption validated:** host-sleep gaps are reliably detectable from clock behavior and can be credited back to a step's deadline without masking a genuinely hung step (the cap holds); and a driver that stays alive across a quota wait can self-resume deterministically.

**Deliverables (FR-5.1–5.4, FR-3.4):**
- `engine/heartbeat.py` (**new**): the driver writes `heartbeat.json` (`{monotonic_s, wallclock_utc, pid}`, atomic overwrite) every 15s. **Primary** suspension detector: consecutive heartbeats with `Δwallclock − Δmonotonic > 30s`. **Fallback** detector (runs unconditionally, for clocks whose monotonic advances through sleep): `Δwallclock > heartbeat_interval + 30s` while the heartbeat *was* written (distinguishing suspend from `driver_orphaned`). Recorded interval duration is `Δwallclock` in both paths. Detected intervals appended to the manifest (`suspensions: [{start, end, gap_s}]`).
- **State-classification predicates (exact, fail-closed).** A stale heartbeat alone does not name a cause. **Classification input is the persisted consecutive heartbeat-pair event the detector appended (the `suspensions[]` interval above), not a fresh re-read of the live `heartbeat.json` file age** — so a suspension detected across a pair stays creditable even after the driver, on wake, writes the next (fresh) heartbeat that resets the live file's age. The classifications are decided from observables sampled together against that pair event at detection time: `pid_alive` (liveness of the recorded `heartbeat.pid` via `os.kill(pid, 0)`); `pair_gap_s` = `Δwallclock` of the straddling consecutive pair (the recorded interval width); `hb_age_s` = `now_wallclock − heartbeat.wallclock_utc` of the *latest* heartbeat (only meaningful when **no** post-gap heartbeat was written, i.e. the writer died mid-gap); `clock_skew` = the primary **or** fallback detector fired on that consecutive-heartbeat pair (real-time jump the monotonic clock did not see); `agent_output_age_s` = time since the running step's adapter child last appended to `events.jsonl`. The predicates are disjoint and evaluated in this order:
  1. **`host_suspended`** ⟺ a persisted heartbeat-pair event carrying `clock_skew` (`pair_gap_s > interval + 30s`) **and** `pid_alive`. The driver survived the sleep and wrote the post-wake heartbeat, so the **pair** — not the live file age — carries the gap; classification reads the pair event and credits even when that just-written heartbeat makes `hb_age_s` fresh. The gap is explained by a real-time jump; the pair's interval is recorded and **credited** to the deadline (bounded by the cap).
  2. **`driver_orphaned`** ⟺ `hb_age_s > interval + 30s` **and not** `pid_alive`. The writer died mid-gap, so no post-gap heartbeat was written and no skew pair exists (this is why the live file age, not a pair event, is the observable here); the interval is **not** credited (there was no suspend to credit) and recovery follows the orphaned-driver path.
  3. **`agent_silent`** ⟺ `pid_alive` **and** `hb_age_s ≤ interval + 30s` (driver writing on schedule) **and** `agent_output_age_s` past the `agent_silence_s` threshold. The driver is healthy and the clock is continuous; the agent itself is producing nothing. **No credit** — the deadline keeps running.
  4. **Ambiguous stale heartbeat** — `hb_age_s > interval + 30s` **and** `pid_alive` **and no** `clock_skew` (a live driver that stopped writing without any clock evidence of suspend: blocked, starved, or a wedged driver loop). This shape is otherwise indistinguishable from a suspend by a bare timeout, which is exactly why `pid_alive` + `clock_skew` are required: **fail closed — never classified `host_suspended`, never credited.** It is surfaced as `agent_silent` (hung), so a genuinely stuck step is not masked as sleep.
- `adapters/process.py`: deadline accounting credits detected suspension intervals back to the running step's remaining timeout, bounded by `suspend_credit_cap_s` (default 12h); past the cap the step halts with `halt_reason=timeout`.

  *Note:* P2 must stamp `timeout` on this path even though the full `halt_reason` enum arrives in P3. Resolution: P2 introduces the `halt_reason` field and the single `timeout` value it needs; P3 completes the enum and the disjointness invariant. This keeps P2 self-contained without depending on P3.
- `engine/config.py`: `keep_awake: bool` (default false), `suspend_credit_cap_s`, `agent_silence_s` (the `agent_silent` threshold, default 300s), heartbeat interval knobs. `keep_awake: true` wraps the driver in `caffeinate -i` on darwin; non-darwin ignores with a warning.
- Auto-resume (FR-3.4): `resume_on_quota: notify` (default) | `auto`. In `auto`, before parking the engine persists a `scheduled_resume` record (`attempt_at = now + retry_after_s`, else `quota_reset_at`; `attempts`, `max_attempts` default 3). The **live driver** waits until `attempt_at` (heartbeat keeps the wait suspend-aware) then performs the FR-3.3 continuation resume. Reconciliation on driver start and every `gauntlet resume`: past `attempt_at` → resume now; not yet → re-arm (live) or manual override resumes now. Re-park + re-schedule only while `attempts < max_attempts`, then a plain `usage_limit` park with an exhaustion note. `auto` without `keep_awake` and without a declared external scheduler → config-load warning. **No external scheduler/daemon ships** — "external scheduler" means the operator re-invokes `gauntlet resume` (cron/launchd), made safe by reconciliation.

**Simplest design:** heartbeat is a single JSON file polled by the deadline accountant; no watchdog thread beyond the existing driver loop. The "sleep clock source" is pinned by a darwin contract test (see below) so we assert, not assume, which detector is authoritative. Auto-resume is **in-process only** — no daemon, no OS timer.

**Test strategy:**
- Unit (injected clock pairs): 40-min gap with monotonic *excluding* suspend → primary rule fires; 40-min gap with monotonic *advancing through* suspend (skew ≈0) → fallback fires; both record the interval; sub-threshold jitter records nothing.
- Unit: 600s-timeout step spanning a synthetic 1h suspension completes (credited); same step with cap below the gap halts `halt_reason=timeout` with the interval in evidence.
- Unit (state predicates, one observable-tuple per case): a persisted skew-pair event with `pid_alive` **and a fresh, just-written post-wake heartbeat** (the exact shape the finding names — the pair carries the gap while the live `hb_age_s` is fresh) → `host_suspended` + credited pair interval, asserting classification reads the pair event and not the live file age; `(pid_dead, stale live heartbeat, no post-gap pair)` → `driver_orphaned` + **no** credit; `(pid_alive, fresh-heartbeat, agent_output_age > agent_silence_s)` → `agent_silent` + no credit; each renders the matching classification in `status --json`.
- Unit (ambiguous stale heartbeat, fail-closed): `(pid_alive, stale, NO clock_skew)` — the shape a bare timeout would misread as suspend — is classified `agent_silent` (hung), **not** `host_suspended`, and credits **nothing** to the deadline; a companion case where the same stale gap *does* carry `clock_skew` flips it to `host_suspended` + credit, proving the skew predicate is what separates them.
- Unit: `caffeinate` wrapper present/absent per config; non-darwin warns.
- Unit (stubbed clock): `notify` never re-invokes / persists no `scheduled_resume`; `auto` (a) parks with a `scheduled_resume`, (b) resumes exactly once at `attempt_at`, (c) re-parks/re-schedules then stops at `max_attempts` with an exhaustion note, (d) a restart before `attempt_at` re-arms from disk, (e) a restart after `attempt_at` resumes on reconciliation. Config-load test: `auto` without `keep_awake`/scheduler warns.
- **Integration (`@pytest.mark.integration`, darwin):** a real short suspend (or an injected suspend-excluding uptime reading) records an interval, pinning the authoritative detector on darwin.

**Exit criteria:** unit suite green; darwin integration test pins the detector; a run spanning a synthetic ≥30-min suspension completes with zero spurious kills in the harness.

**Deferrals:** full `halt_reason` enum + status enrichment → P3. Window-based *proactive* parking → P10 (P2 is reactive-only).

---

## P3 — Engine-stamped reason enums + status contract enrichment

**Assumption validated:** the manifest already holds (or, after P1/P2, now holds) everything needed to explain any run state without opening a transcript — this phase is pure additive rendering.

**Deliverables (FR-7.1–7.4):**
- `engine/manifest.py`: complete the disjoint reason model. `halt_reason ∈ {timeout, budget, judge_deny, signal_kill, adapter_error, precondition, operator_recover}` on HALTED/FAILED/INTERRUPTED with `parked_reason=null`; `parked_reason ∈ {usage_limit, usage_window, artifact_invalid, response, gate}` on PARKED with `halt_reason=null`. The applicable enum is mandatory for every non-DONE terminal/parked step. On an `artifact_invalid` park (built in P4) the `revalidation` content-hash pair is recorded — P3 defines the field shape; P4 populates it.
- **Legacy parked-reason compatibility contract (normative — one rule, not a menu).** The engine today persists two current-state values absent from the PRD enum — `upstream_conflict` (a builder `agent_task` conflict park) and `cycle_escalation` (a reviewer/triager `adversarial_cycle` park) — and stamps `parked_reason=null` on a `human_gate` park; all three are human-decision-resolvable via `gauntlet resume --response`. P3 reconciles them to the PRD enum as follows:
  1. **Writes stamp PRD enum values only.** New parks stamp `parked_reason=response` for both the builder-conflict and cycle-escalation cases and `parked_reason=gate` for a `human_gate` park. No record is newly written with a legacy value or a null parked-reason on a park.
  2. **Reads normalize through a one-way mapper.** A pure `normalize_parked_reason` maps any legacy persisted value on load — `upstream_conflict → response`, `cycle_escalation → response`, and `null` on a `human_gate` record → `gate` — so the manifest validator, resume-response logic, and status rendering all operate on the PRD enum set. Legacy manifests on disk are read through the mapper, never rewritten in place.
  3. **`status --json` and the schema never emit or accept legacy values.** The status `parked_reason` field and its schema enum are exactly the PRD set; legacy values enter only through the read-side mapper, never the output.
  4. **Routing is preserved by step type, not the collapsed value.** The builder-conflict / cycle-escalation distinction the two legacy values carried is already recovered from the step *type* (`agent_task` vs `adversarial_cycle`) that drives the `--response` re-drive (`RESPONDABLE_STEP_TYPES`), so mapping both to `response` loses no resume-routing information; `RESPONSE_RESOLVABLE_PARK_REASONS` becomes `{response}` with the two legacy values accepted only on read.
  5. **Any current-state value that cannot map 1:1 to a PRD enum is an UPSTREAM CONFLICT**, surfaced — never a silent amendment to the PRD enum.
- The engine stamps `halt_reason` on every terminal path (timeout, budget, judge_deny, signal_kill, adapter_error, precondition); `gauntlet recover` stamps `operator_recover` with operator identity.
- `schemas/status.json` + `engine/operator.py`: additive always-present, nullable fields — `current_step_elapsed_s`, `current_step_timeout_remaining_s`, `run_elapsed_s`, `totals` (UsageTotals incl. cost), `agent_usage` (per-profile), per-`steps[]` `duration_s`/`notes`/`halt_reason`/`parked_reason`, plus `quota {reset_at}` when parked `usage_limit` and `suspension {last_heartbeat_age_s, intervals[]}` (from P2). `additionalProperties: false` continues to validate against the *shipped* schema; `schema_version` stays 1.
- Human `gauntlet status` footer renders elapsed, cost-so-far, and — when parked — reason + reset time (quota) or awaited decision + the next command.
- `gauntlet report` gains cache-effectiveness columns from data already recorded (`cached_input_tokens` per step): per step-type and per profile, cache-read share `cached/(input+cached)` and fresh-input tokens from cold session starts (FR-7.4).

**Simplest design:** no new persistence — every field is computed from existing manifest data (P1/P2 additions included). Reason enums are string constants + a validator asserting disjointness, not a state hierarchy.

**Test strategy:**
- Unit per terminal path → asserts stamped `halt_reason`; unit per park path (`usage_limit`, `usage_window` [shape only, wired P10], `artifact_invalid` [shape only, wired P4], `response`, `gate`) → asserts `parked_reason` + null `halt_reason`; `recover` → `operator_recover` + identity.
- Unit (legacy compatibility): a fixture manifest carrying legacy `upstream_conflict` and one carrying `cycle_escalation` both normalize to `response` on read; a legacy `human_gate` record with `parked_reason=null` normalizes to `gate`; `status --json` emits only PRD enum values for all three; `resume --response` on each legacy fixture routes by step type exactly as its normalized-value equivalent does; the on-disk manifest bytes are unchanged (read-through mapper, not rewrite).
- Schema test: all new fields present/nullable/never-omitted, `additionalProperties: false` validates against shipped schema; **regression test** validating new output against a captured *v0* schema copy with `additionalProperties: false` **fails** (documents the re-pin cost, FR-7.1).
- Golden test: manifest fixture → status JSON.
- Snapshot tests of the rendered footer per park/halt kind include the identifying line + next-action.
- Golden test (FR-7.4): manifest fixture → cache-read share per step-type/profile; zero-cache fixture renders `0%`, not blank.

**Exit criteria:** every park/halt/fail state reachable in the suite is identifiable from `status --json` alone (table-driven test); schema regression + golden tests green.

**Deferrals:** `usage_window` park *behavior* → P10; `artifact_invalid` *population* → P4; gate-context enrichment (`gate` block bodies) → P8 (P3 ships the field, P8 fills the convergence/history sections).

---

## P4 — In-step artifact validators + repair loop + `artifact_invalid` park/revalidate

**Assumption validated:** an agent handed its own parse/validation error in-session corrects the artifact within 2 attempts most of the time; the hand-edit-then-revalidate park covers the remainder — no dead-end, no off-book file surgery.

**Deliverables (FR-2.1–2.3):**
- `engine/validators.py` (**new**): named validators invocable from `agent_task` — `plan_phases` (wrapping `engine/planphases.extract_phases`) and JSON-schema-ref validators (reusing `schemas/*.json`).
- `engine/steptypes.py`: `agent_task` supports `validate: <name>`, run against the step's `output` artifact inside the step. On failure, re-invoke the **same agent session** with the exact validation error + a correction instruction, bounded at 2 repair attempts. Both attempts appear in the transcript.
- After exhaustion → **park** (not FAILED) with `parked_reason=artifact_invalid` (field from P3) and the validator error verbatim in `notes`. A plain `gauntlet resume` re-runs **only the validator**; on pass the step goes DONE with an audit note. Populate the `revalidation` content-hash pair (`hash_at_park`/`hash_at_resume`/`changed_while_parked`/`passed_on_resume`) defined in P3.
- `pipelines/standard.yaml`: the plan-author step carries `validate: plan_phases`; `phase_lint` remains as the fail-closed backstop, unchanged.

**Simplest design:** mirrors the proven schema-retry loop already in `cycle.py`. The repair loop is a bounded `for` over the same session, not a new sub-state machine. Revalidation on resume runs the validator against on-disk bytes — no adapter invocation.

**Test strategy:**
- Unit (stub adapter emitting malformed then corrected `gauntlet-phases`): step succeeds on attempt 2; both attempts in transcript; persisted artifact parses.
- Unit: exhaust attempts → park `artifact_invalid` → hand-edit artifact on disk → `resume` → DONE **without a new adapter invocation**; manifest notes record the revalidation path; `revalidation` hashes recorded (`changed_while_parked=true` when edited).
- Inspection test: `standard.yaml` plan-author carries `validate: plan_phases`; existing `phase_lint` tests unchanged.

**Exit criteria:** zero-hand-edit-without-audit-trail for malformed phase blocks (the audit note + hash pair prove it); backstop `phase_lint` behavior unchanged.

**Deferrals:** validators for cycle artifacts beyond what shipped pipelines use — add on demand, not speculatively.

---

## P5 — Adversarial-cycle sub-step checkpointing

**Assumption validated:** persisted per-round artifacts plus the round's handoff SHA are sufficient to re-enter a killed cycle mid-round safely, without re-deriving completed review/triage/fix/confirm work.

**Deliverables (FR-4.1–4.2):**
- `engine/manifest.py`: `checkpoints: [{sub_step, round, artifact, handoff_sha}]` on the cycle step record, written write-ahead as each sub-step (review, per-finding triage batch, fix, confirm) completes.
- `engine/cycle.py`: on resume of a RUNNING/INTERRUPTED cycle, completed sub-steps of the current round load from their persisted artifacts and are **not** re-executed; execution re-enters at the first incomplete sub-step.
- **Compose with the P1 `usage_limit` cycle park (FR-4.1/FR-3.3).** When a cycle parked `usage_limit` on a transient sub-agent failure (P1), a **plain** `gauntlet resume` now re-enters at the first incomplete sub-step using these checkpoints — completed reviewer/triager/fixer/confirmer sub-steps are **not** re-run — and continues the failing sub-agent's persisted `session_id` (P1) per FR-3.3 where the adapter supports it, falling back to a fresh sub-agent invocation on an unknown/expired session with a `notes` record. This is the P1 round-loss deferral closing: P1 parks and preserves the session; P5 makes resume lose zero completed sub-steps (PRD G1).
- Checkpoint reuse guarded by the round's handoff SHA: if the worktree/handoff moved since the checkpoint (e.g., manual commits during a park), checkpoints are invalidated, the round restarts, and the invalidation reason is written to `notes`.

**Simplest design:** checkpoint = a manifest record pointing at the artifact the cycle already persists; reuse is "load the file if the record exists and the SHA matches," not a serialized cycle-state blob. The SHA guard is the only correctness gate.

**Test strategy:**
- Unit: simulated kill after round-1 `findings.json` persisted → resume runs triage **without** re-invoking the reviewer, reusing persisted findings; second test resumes after triage → re-enters at fix.
- Unit (usage_limit cycle park + resume, composing P1): a `transient_usage_limit` from a reviewer sub-step *after* the triage-or-earlier checkpoints of the round have persisted → cycle parks `usage_limit` with checkpoints intact; plain `gauntlet resume` re-enters at the first incomplete sub-step, re-runs **none** of the completed sub-steps, and continues the persisted reviewer session (expired-session stub → fresh invocation + `notes`). Manifest verifies zero completed sub-steps re-executed.
- Unit: checkpoint + moved handoff SHA → full round re-run with audit note.

**Exit criteria:** a killed cycle resumes with zero completed sub-steps re-executed (manifest-verifiable); the SHA guard forces a clean re-run on any handoff movement.

**Deferrals:** the *concurrent* triage execution and its failure-path checkpoint fragment → P11 (P5 checkpoints the sequential batch as one sub-step; P11 refines it to per-finding).

---

## P6 — Scoped context assembly (input modes + artifact-diff re-review)

**Assumption validated:** reference/phase-scoped context does not degrade builder/reviewer output (guardrail: findings-per-phase and gate outcomes on the next real run vs. the recent-runs inline baseline, per Q3), while cutting per-call payload well below the inline baseline.

**Deliverables (FR-1.1–1.3):**
- `adapters/base.py`: add `reads_repo: bool` to `AdapterCapabilities`; claude-code/codex declare `true`, api declares `false`. A profile's *effective* capability is the adapter's value under that profile's actual sandbox (cwd/repo-root/path exposure), not the class alone.
- `engine/steptypes.py` `_render_prompt`: per-input `mode: inline | reference | phase`. `inline` = today (default). `reference` = inject the artifact's repo-relative path + a one-line "read it" instruction. `phase` (plan.md only) = inject the current `foreach` phase's plan section + the full-document path.
- **Load-time enforcement (fail closed):** pipeline load fails, naming step/input/profile/path, if a `reference`/`phase` input binds to a profile whose effective `reads_repo` is false; if a `reference` input's repo-relative path does not resolve to a file under the repo root; **and** if a `phase` input's referenced plan artifact does not resolve to an existing `plan.md` file under the repo root. Both `reference` and `phase` referenced-artifact paths are validated at load — no referenced path (reference or phase) escapes preflight to fail later inside a prompt.
- **Preflight enforcement:** compose with `doctor` (delivered in P7) — a one-file repo-read probe per reference-capable profile. *Note:* P6 ships the load-time checks (self-contained); the doctor read-probe rides on P7's doctor work to avoid P6 depending on P7. P6's acceptance tests (a)/(b)/(c) are load-time; (d) is asserted in P7.
- `engine/cycle.py`: artifact-mode cycles snapshot the artifact at each review handoff; rounds 2+ send the reviewer the unified diff (snapshot → current) + carried findings + artifact path, not full text.
- `pipelines/standard.yaml`: the implement step opts into `prd.md: reference`, `plan.md: phase`. **P6 ships this `standard.yaml` flip per FR-1.1** — it is the sanctioned default, not held opt-in behind a gate. The FR-1.1-vs-Q3 tension is resolved (human decision, plan-cycle): the Q3 guardrail comparison **is** the first real run after this PRD lands, measured against the recent-runs inline baseline; a guardrail miss reverts the flip through a **config-only** change (flip the two modes back to `inline`), not a code change. The P6 builder therefore **does not** halt on an UPSTREAM CONFLICT between FR-1.1 and Q3 — ship the flip.

**Simplest design:** modes are a small enum handled in prompt rendering; no summarization, no new agent step (Non-Goal). Diff is `git diff` between the snapshot ref and current — deterministic and lossless-by-path.

**Test strategy:**
- Unit (FR-1.1): 3-phase run, 34KB PRD + 27KB plan → each implement `prompt.md` contains the phase excerpt + both paths and is ≤25% of the all-inline byte size.
- Unit (FR-1.2): round-2 review prompt for a 40-line edit contains the diff + carried findings, **not** the full body; snapshot used is the round-1 version.
- Unit (FR-1.3): (a) `reference`/`phase` input on an api-profile raises at load naming step/input/profile; (b) a `reference` path not under repo root raises a named path error at load; (c) a `phase` input whose plan path is missing / is not a file / is not `plan.md` / does not resolve under the repo root raises a named path error at load. [(d) doctor read-probe → asserted in P7.]
- Integration: a real implement phase completes normally with reference/phase context.

**Exit criteria:** implement-step payload ≤40% of the all-inline baseline on a ≥3-phase run; load-time fail-closed checks green; the shipped `standard.yaml` flip is in place (per FR-1.1). The Q3 guardrail comparison — findings-per-phase and gate outcomes vs. the recent-runs inline baseline — is the **first real run after this PRD merges**, measured post-merge and **not** a phase-handoff gate; a guardrail miss there triggers a **config-only revert** of the flip.

**Deferrals:** LLM summarization of artifacts (Non-Goal). Flipping any *other* pipeline's defaults beyond the shipped implement step.

---

## P7 — Effort tiering plumbing + severity-gated escalation + cheap mechanical profiles + doctor probes

**Assumption validated:** effort/tier plumbing is reproducibly testable *without* ratified default values; whether specific defaults cut cost without raising blocking-finding escape rate is a separate live-measurement question (Q2). This phase ships wiring, not tuned numbers.

**Deliverables (FR-6.1–6.4):**
- `engine/config.py`: `AgentProfile.effort` from the **canonical enum `{minimal, low, medium, high}`**. Adapters map canonical → accepted surface: claude `--effort {low, medium, high}` (canonical `minimal` → `low` with a load-time warning), codex `-c model_reasoning_effort={minimal,low,medium,high}`, api `reasoning_effort` per pinned model. An effort value an adapter/model can't accept is a **config-load error**. Cycle sub-agent overrides (`reviewer`/`triager`/`fixer`/`confirmer`) and per-step `effort:` accept the same enum; step wins over profile. The accepted per-adapter surface is pinned (verified in `.gauntlet/pins.yaml`) — independent of default *values* (Q2).
- `engine/cycle.py` (triage): severity-gated escalation — low-confidence verdicts on `blocking`/`major` escalate to the escalation profile; low-confidence on `minor`/`nit` do **not** escalate, carrying to the gate marked `low_confidence`.
- `.gauntlet/config.yaml` + `pipelines/standard.yaml`: bind the two mechanical emissions to a designated cheap profile (e.g. `mechanic: {adapter: api, model: gpt-5-mini}`), not the builder profile — profile *assignment* only; the cheap profile's effort *value* is Q2. Each is named concretely so the binding is inspectable and testable:
  1. **Commit-message drafting** — the existing `message_agent` field on the `commit` step (`standard.yaml` step id `phase-commit`, currently `message_agent: triage`) re-bound to `mechanic`. Output artifact: the drafted commit message; validator: the existing commit-message format check in the `_draft` redraft loop (`engine/commit_format.py`).
  2. **Resume-disposition emission** — a new optional `disposition_agent:` field on the respondable step types (`agent_task`, `adversarial_cycle`), mirroring `message_agent`; unset → the step's primary `agent` (today's behavior, unchanged). `standard.yaml` sets `disposition_agent: mechanic` on the `implement` step (the `agent_task` carrying `halt_on: "UPSTREAM CONFLICT"`), so the structured `disposition` a step emits on a `gauntlet resume --response` is drafted by `mechanic`, not the builder. Output artifact: the structured resume-disposition object (`RESUME_DISPOSITION_SCHEMA` = `schemas/resume-disposition.json`). Validator: that schema **plus** the fail-closed engine check `steptypes._resume_disposition_result`/`_conflict_shape_error` (an unrecognized or mis-shaped disposition fails the step rather than advancing — so a cheap-profile emission is bounded by the same oracle as a builder one).
- `cli.py`/`engine/doctor.py`: `doctor` probes every configured profile — a minimal live round-trip verifying the model id resolves and (where set) the effort flag is accepted, reporting the resolved model name; and, for any profile a shipped pipeline uses in `reference`/`phase` mode, the FR-1.3 repo-read probe. Probe failures are FAIL rows; unauthenticated CLI → WARN (not silent PASS).

**Simplest design:** static config throughout — no runtime/cost-based routing (Non-Goal). Escalation gate is a severity check on the existing triage verdict, not a new model. Doctor probe is a one-token round trip + a one-file read.

**Test strategy:**
- Unit per adapter: canonical value maps to and lands in the constructed command/params (incl. `minimal→low` claude warning); unsupported value raises at config load; profile+step compose with step winning.
- Unit: mixed-severity low-confidence verdicts → exactly the blocking/major escalation calls; minor/nit in the gate payload flagged, zero escalation-profile invocations.
- Config-inspection test: both mechanical bindings resolve to the cheap profile, not builder — the `phase-commit` step's `message_agent` is `mechanic`, and the `implement` step's `disposition_agent` is `mechanic`. Wiring test (a): the commit step invokes the cheap profile and the drafted message still passes the `commit_format` validator. Wiring test (b), analogous: on a `--response` resume the `implement` step draws the disposition-emission agent from `disposition_agent` (the cheap profile), and a `mechanic`-drafted disposition still passes `schemas/resume-disposition.json` + the fail-closed `_resume_disposition_result` check; a step with `disposition_agent` unset still uses its primary `agent`. (All assertions independent of any effort *value*.)
- Doctor test: a bad model alias in one profile → that profile FAIL, others PASS; a reference-capable profile whose sandbox can't read a repo file → FAIL on the read probe (satisfies FR-1.3(d)); unauthenticated CLI → WARN.

**Exit criteria:** plumbing/wiring tests green with no dependency on ratified default values; doctor catches a bad alias and a blind reference profile before any run step executes.

**Deferrals:** **Shipped default effort *values* per profile/step are out of P7 scope (Q2)** — set in a later config-only change gated on live A/B measurement, following the Q3 rollout pattern. Dynamic/cost-based routing (Non-Goal).

---

## P8 — Gate decision context enrichment

**Assumption validated:** a gate decision is makeable from the gate view alone — convergence history, prior responses, and per-finding triage reasoning are all reconstructable from the manifest + artifacts, no transcript needed.

**Deliverables (FR-8.1–8.2):**
- `web/gate.py` + templates, and a `gate` block in `status --json` when parked at a gate (the field shape shipped in P3): cycle convergence summary (rounds run; findings raised/fixed/declined per round, sourced from existing `metrics`), prior human responses/rejections for this gate with timestamps, and per-escalated-finding triage `reasoning`.
- `next_actions` gate entries carry a one-line consequence: approve → what proceeds; reject → which cycle re-runs with the notes injected.

**Simplest design:** pure rendering over data already persisted by P5's checkpoints and the existing cycle `metrics` — no new capture. Reuses `logging/redact.py` for any content-bearing field.

**Test strategy:**
- Unit: a two-round cycle with one rejection renders all three sections from manifest + artifacts alone.
- Golden test: `next_actions` payload for a gate downstream of an adversarial cycle names the cycle a rejection re-runs.

**Exit criteria:** the three gate sections render for a multi-round cycle without transcript access; consequence lines present in `next_actions`.

**Deferrals:** none new; evidence-tiered gate auto-approval is a Non-Goal (pipeline-effectiveness PRD).

---

## P9 — Intra-phase checkpoint commits + checkpoint-aware recovery

**Assumption validated:** a prompt-directed commit discipline is followed reliably enough to bound worst-case lost work to one intra-phase milestone — the deterministic hedge for P1's bet on CLI session resume (the two compose: session resume when it works, checkpoint rewind when it doesn't).

**Deliverables (FR-11.1–11.2):**
- `prompts/implement-phase.md`: instruct the builder to commit at each passing-test milestone with subject `P<N> wip: <milestone ≤60 chars>`; the final `P<N>:` phase commit is unchanged.
- `engine/config.py`: `checkpoint_commits: keep | squash` (default `keep`).
- **Git-history contract (normative, preserving CLAUDE.md §1 clean-commit invariant):** the phase **always** terminates in a `PN:` commit, and that commit's SHA is **always** the reviewer handoff SHA — never a `wip:` commit. If uncommitted changes remain, the `PN:` commit captures them; if the last `wip:` already committed everything, the `PN:` commit is an **explicit empty marker** (`git commit --allow-empty`) whose body lists the milestones. Reviewers receive the range diff `base_sha..<PN: SHA>` (identical whether one commit or many `wip:` + marker). With `squash`, `wip:` commits squash into one non-empty `PN:` commit whose body lists the milestones; handoff SHA and reviewed range diff unchanged.
- `engine/orchestrator.py`: interrupted-step recovery rewinds a dirty worktree to the **latest intra-phase checkpoint commit** (newest `^P<N> wip:` that is a descendant of the step's `base_sha`) instead of `base_sha`, falling back to `base_sha` when none exists; the pre-rewind backup ref is still taken; the continuation/re-run prompt names the checkpoint it resumes from.

**Simplest design:** checkpoints are ordinary git commits — no new storage. Recovery target selection is a `git log`-style descendant match on the run branch. The empty marker commit is the only special case, and it exists solely to keep the handoff on a `PN:` commit.

**Test strategy:**
- Prompt-content test (instruction present).
- History-shape tests: `keep` + residual changes → ends `wip:*, PN:`, `base..<PN:>` reviews identically to a single-commit phase; `keep` + no residual → `PN:` is empty (`--allow-empty`), is the handoff SHA, lists milestones; `squash` → one non-empty commit whose body lists the milestones. **All three assert the handoff SHA is a `PN:` commit, never `wip:`.**
- Unit: kill after `P3 wip: model layer` + further dirty edits → recovery lands on the wip commit, milestone files survive, backup ref exists, re-run prompt references the checkpoint.

**Exit criteria:** worst-case repeated work after interruption ≤1 milestone whenever ≥1 checkpoint existed (manifest-verifiable rewind target); the clean-commit invariant holds in all three history shapes.

**Deferrals:** milestone-quality enforcement — quality is observable in history and correctable via prompt iteration; no runtime gate on milestone content.

---

## P10 — Usage-window ledger + step admission

**Assumption validated:** local manifest data approximates the shared provider window well enough that proactive warnings/parks beat reactive halts — and, because a wrong *continue* is now survivable via P1, an advisory-by-default ledger is a pure improvement, not a correctness dependency.

**Deliverables (FR-10.1–10.3):**
- `engine/ledger.py` (**new**): every run appends per-step provider usage to `~/.gauntlet/usage-ledger.jsonl` (append-only, content-free: provider, model, profile, step_type, repo-root **hash**, run_id, tokens, cost, started/ended, duration) — the same data the manifest records, aggregated across runs/repos (parallel runs share one account window). A sliding-window headroom query.
- **One-shot ledger backfill (operator-mandated, plan-cycle-resp-3):** a `gauntlet ledger backfill` command (and the underlying `ledger.py` function it calls) that reconstructs the ledger from **existing run manifests** — converting each manifest's per-step usage records into the same content-free ledger rows — so the median estimator has history from the first enforced run instead of a cold start. **Idempotent by construction:** every ledger row (append-time and backfilled) carries a stable de-dup key (`run_id ‖ step_id`); backfill skips any manifest-derived row whose key already appears in the ledger, so a re-run appends nothing and the sliding-window sums are unchanged. Backfill reports rows added vs. rows skipped-as-duplicate.
- `engine/config.py`: `providers.<name>: {window_hours, window_budget: <tokens|cost>, enforce: bool}`. Before launching an agent step on a window-constrained provider, estimate usage (median of historical same-type/same-profile steps; configured fallback with no history) vs. remaining headroom.
- On insufficient headroom: default = a **warning** stamped into the manifest + `status` + a notification (advisory: the ledger can't see non-gauntlet usage). With `enforce: true`, the run **parks before the step starts** with `parked_reason=usage_window` (wired here; field shipped P3) + projected replenishment time — a clean-boundary park with zero work in flight.

**Simplest design:** JSONL append + an in-memory sliding-window sum; estimate is a median, not a model. No self-calibration (Q9 → post-v1). Machine-global, not account-keyed (Q7 → post-v1).

**Test strategy:**
- Unit: two simulated runs append; a sliding-window query returns the correct per-provider sum.
- Unit (backfill, operator-mandated): backfill over **two fixture run manifests** produces ledger rows whose sliding-window sums equal the expected per-provider totals; a **second** backfill over the same manifests adds **zero** rows (de-dup by `run_id ‖ step_id`) and leaves the window sums byte-for-byte identical — proving idempotency.
- Unit: synthetic ledger + config → correct headroom + estimate; no-history → fallback.
- Unit: advisory mode launches with a recorded warning; enforce mode parks pre-step with the projection in the step record + a `resume` next-action.

**Exit criteria (deterministic, verifiable at phase handoff):** ledger append + sliding-window query tests green; one-shot backfill from two fixture manifests yields correct sliding-window sums and is idempotent on re-run (zero added rows, unchanged sums); admission estimate tests green (median from history; configured fallback with no history); advisory mode records a warning and never blocks a launch; with `enforce: true` and a synthetic ledger + config that give insufficient headroom, the run parks pre-step with `parked_reason=usage_window` + projected replenishment before any adapter call, with a `resume` next-action present. No exit criterion depends on naturally occurring quota events or live provider-window behavior.

**Post-merge success metric (live measurement, not a handoff gate):** with `enforce: true`, ≥80% of real usage-limit interruptions become pre-step `usage_window` parks rather than mid-step `usage_limit` parks — measured on live runs after merge, never asserted at phase handoff.

**Deferrals:** ledger self-calibration from observed halt times (Q9), per-account keying (Q7) — both post-v1.

---

## P11 — Concurrent triage + judge decision cache

**Assumption validated:** concurrency and caching change wall-clock and cost without changing any persisted artifact byte (all-success rounds) or any judge outcome — latency that changes no decision is pure waste.

**Deliverables (FR-9.1–9.2, FR-12.1–12.2):**
- `engine/cycle.py` (triage loop): per-finding triage calls run concurrently with a bounded pool (`triage_concurrency`, default 4), preserving per-finding prompt isolation (injection containment, PRD-gauntlet §8). Results merge in **finding-id order**. **Byte-identity is scoped to an all-success round:** the persisted final `triage.json` is byte-identical to the sequential output for the same fixtures. On any single-call failure the round's triage step fails (fail closed, unchanged semantics); the final `triage.json` is **not** written — instead completed verdicts persist to a **separate checkpoint fragment** (composes with P5): verdicts sorted by finding id, one record per completed finding, incomplete findings listed as pending. Resume re-runs only the incomplete findings; once all succeed the final `triage.json` is written (satisfying byte-identity). The fragment is explicitly *not* claimed byte-identical across runs (different subsets may complete before a failure) — only within-fragment ordering and the final artifact are deterministic.
- `judge/core.py`: cache **allow** decisions per run, keyed on `sha256(tool_name ‖ canonical_json(payload) ‖ repo_root ‖ sha256(policy.yaml) ‖ agent_profile)`. `deny`/`ask` never cached; any policy change rotates the key; cache dies with the run. Hits recorded in the audit log with the original decision id + `cached: true`.

**Simplest design:** a bounded `ThreadPool`/`asyncio` gather over independent calls + an id-ordered merge — the independence is already guaranteed by the point-by-point design (Non-Goal: batch triage). The judge cache is an in-memory dict keyed on a hash; no persistence, so it cannot fail open across runs.

**Test strategy:**
- Unit (instrumented stub, fixed delays): each triage stub records its start/end against a **fake clock** (or a monotonic counter incremented per stub) with a fixed per-call delay; the assertion is that ≥2 calls' `[start, end]` intervals **overlap** (concurrency observed) and that all N stubs were entered before the first returned under a pool of size ≥2 — never a wall-clock ratio. All-success round → final `triage.json` identical to a sequential run of the same fixtures.
- Unit: one stubbed failure among five → step fails, **no** final `triage.json`, a checkpoint fragment holds the four completed verdicts sorted by id with the fifth pending; resume issues exactly one triage call; resumed all-success round → `triage.json` byte-identical to sequential.
- Unit (judge): two byte-identical calls → one evaluation + one audited hit; a denied call repeated → two evaluations; a policy edit between identical calls → two evaluations; a stubbed classifier is **not** invoked on the second identical allow — asserted by an invocation **count** on the classifier stub (zero calls on the cache hit), not a latency threshold.

**Exit criteria (deterministic — no wall-clock thresholds).** All measured on the instrumented harness with fixed stub delays / a fake clock, so CI load cannot flip a pass/fail:
- **Concurrency:** on a 5-finding round under a pool of size ≥2, the recorded per-call intervals show ≥2 overlapping in-flight triage calls, and all 5 stubs are entered before the first completes — the observable that a wall-clock ratio was standing in for.
- **Cache effectiveness:** on a repeat-heavy step, the judge LLM-rung **evaluation count** drops ≥50% vs. the no-cache count for the same call sequence (counted on the classifier stub), with byte-identical decisions on every cached hit.
- **Invariants:** every artifact-byte/decision invariant (all-success `triage.json` byte-identity, failure-path fragment ordering, allow-only caching) asserted green.

**Deferrals:** ensemble/multi-lens review, batch triage (Non-Goals). Cross-run judge caching (the cache is deliberately run-scoped for fail-closed safety).

---

## Sequencing notes

- **No phase depends on a later phase (FR-10.3).** P1 parks using only `parked_reason` (existing field). P2 introduces the single `halt_reason=timeout` value it needs and the `scheduled_resume`/heartbeat records; P3 completes the `halt_reason` enum + disjointness and renders whatever P1/P2 stamped. P4 populates the `artifact_invalid` `revalidation` fields whose shape P3 defined. P5 checkpoints cycle sub-steps; P8 renders them + existing `metrics`; P11 refines P5's triage checkpoint to per-finding. P9–P11 are independent of each other. The **usage_limit cycle park** splits cleanly across the ordering: P1 delivers the park itself (transient sub-agent failure → `usage_limit` park, worktree + sub-agent session preserved) and P5 — using its own checkpoints — makes plain resume re-enter at the first incomplete sub-step; P1 stands alone (resume re-enters at round start), so P5 tightens P1 rather than P1 depending on P5.
- **P6/P7 coupling handled without a cycle:** P6 ships the FR-1.3 *load-time* checks (self-contained); the FR-1.3 *doctor read-probe* rides on P7's doctor work (P6 acceptance (d) asserted in P7). P6 precedes P7, so nothing in P6 depends on P7 code — only the probe *test* lands in P7.
- **P9 placement** is by risk, not code dependency: its risk is behavioral (does the builder follow the commit discipline?), which P1's integration experience informs. It has no code dependency on P2–P8.
- **Self-hosting:** per CLAUDE.md §"self-hosting switchover", from the point the pipeline engine supports it these phases run through `gauntlet run`; any forced fallback to manual execution is recorded in `BOOTSTRAP-NOTES.md`.

## Open questions carried into execution (do not resolve by amending the PRD)

- **Q2 (effort default values)** — P7 ships plumbing only; default values are a later config-only change gated on live A/B. If a phase tempts a value choice, record it as a deferral, not a commit.
- **Q6 (`signal_kill` attribution)**, **Q7 (ledger scope)**, **Q8 (squash default)**, **Q9 (window budget)** — proposals stand as drafted (P3/P10/P9 respectively); any evidence contradicting a proposal during execution is an UPSTREAM CONFLICT to surface, not amend.

---

```gauntlet-phases
- id: P1
  title: Failure classification + usage_limit park + session-preserving resume
  goal: Classify transient-vs-terminal adapter failures, park (not fail) on a usage limit with worktree and session preserved, and resume by continuing the persisted CLI session. Validates the core bet (§1.3) that an interrupted session continues correctly.
- id: P2
  title: Suspend/sleep resilience + auto-resume scheduler
  goal: Add a driver heartbeat with dual sleep-gap detectors, credit suspension gaps back to step deadlines under a cap, and in-process auto-resume for quota parks. Validates that sleep is detectable and creditable without masking real hangs.
- id: P3
  title: Engine-stamped reason enums + status contract enrichment
  goal: Introduce disjoint halt_reason/parked_reason enums and add additive status.json fields (elapsed, timeout remaining, usage totals, notes, suspension, quota, cache columns). Validates that any run state is explainable from status --json alone.
- id: P4
  title: In-step artifact validators + repair loop + artifact_invalid park
  goal: Validate agent-authored structured artifacts in-session with a bounded 2-attempt repair loop, then park with a hand-edit-then-revalidate path. Validates that most malformed artifacts self-repair and the rest are fixable without off-book file surgery.
- id: P5
  title: Adversarial-cycle sub-step checkpointing
  goal: Write-ahead checkpoint each cycle sub-step and re-enter a killed round at the first incomplete sub-step, guarded by the round handoff SHA. Validates that persisted round artifacts plus the handoff SHA suffice to resume mid-round safely.
- id: P6
  title: Scoped context assembly (input modes + artifact-diff re-review)
  goal: Add inline/reference/phase input modes and diff-scoped round-2+ re-review, gated on a declared reads_repo capability with fail-closed load checks. Validates that scoped context cuts payload sharply without degrading output quality.
- id: P7
  title: Effort tiering plumbing + severity-gated escalation + cheap mechanical profiles + doctor probes
  goal: Wire a canonical effort enum through adapters, gate triage escalation by finding severity, bind mechanical steps to a cheap profile, and add doctor model/effort/repo-read probes. Validates that effort/tier plumbing is testable without ratified default values (Q2).
- id: P8
  title: Gate decision context enrichment
  goal: Render convergence summary, prior responses/rejections, and per-finding triage reasoning into the gate view and status gate block, with consequence lines on gate next_actions. Validates that a gate decision is makeable from the gate view alone.
- id: P9
  title: Intra-phase checkpoint commits + checkpoint-aware recovery
  goal: "Add prompt-directed PN-wip milestone commits with a normative git-history contract (handoff always a PN: commit) and rewind recovery to the latest checkpoint. Validates that the commit discipline bounds worst-case lost work to one milestone."
- id: P10
  title: Usage-window ledger + step admission
  goal: Append content-free per-step usage to a machine-global ledger (with a one-shot idempotent backfill from existing run manifests) and admit window-constrained steps by median-estimate vs headroom, warning by default and parking pre-step under enforce. Validates that local data approximates the shared window well enough to beat reactive halts.
- id: P11
  title: Concurrent triage + judge decision cache
  goal: Run independent per-finding triage calls concurrently with id-ordered merge and a failure-path checkpoint fragment, and cache allow-only judge decisions per run. Validates that concurrency and caching change latency and cost without changing any artifact byte or judge outcome.
```
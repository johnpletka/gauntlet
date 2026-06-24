# PRD: Resume-with-response — human decision mechanism for upstream conflicts

**Feature slug:** `gauntlet-resume-response`  
**Status:** Draft for adversarial review (revised again to address this round's F-001 through F-011)  
**Date:** 2026-06-24  
**Author:** John Pletka

---

## 1. Problem statement

When a builder halts with an `UPSTREAM CONFLICT` (FR-10.4 in PRD-gauntlet.md), the human must decide how to proceed. Currently there is **no formalized way** to communicate that decision back to the orchestrator.

### Current symptoms
- Human reads the conflict in the transcript
- Human wants to signal their decision  
- `gauntlet resume <slug>` re-runs the builder, which re-surfaces the unchanged conflict
- No audit trail of what the human decided or why
- Halted runs stay stuck indefinitely

### Why it matters
- **Data over inference:** no record of human intent (CLAUDE.md §2)
- **Fail closed:** upstream-conflict gate (FR-10.4) cannot halt — runs stay parked indefinitely
- **Process fidelity:** the bootstrap dogfood (prd-authoring-aids) is blocked at a parked conflict with no resolution path

---

## 2. Relationship to existing artifacts

`gauntlet resume` currently has two paths:
- `gauntlet resume <slug>` with a `human_gate` step parked → marks the step done and proceeds
- `gauntlet resume <slug>` with an `agent_task` parked → re-runs the agent

When an agent halts with an `UPSTREAM CONFLICT`, it does not mark the step done — it parks with a halt signal (status="parked"). The existing `human_gate` flow (which waits for a human to explicitly `approve`/`reject`) doesn't apply because the agent is still mid-task, not waiting for gating.

**Why not extend `approve`/`reject`?**
- `approve` (run.py:824) marks the step done and skips to the next step — no re-run
- `reject` (run.py:850) fails the whole run and halts it (orchestrator.py:123-129); the notes never reach a re-run
- Neither feeds human text back into a re-run of the agent

`--response` is a new path: re-run the agent with the human's context injected so it can re-evaluate.

---

## 3. System architecture

### Components touched

- **CLI (cli.py):** `gauntlet resume` command accepts `--response` flag
- **Run orchestration (run.py):** `RunManager.resume()` records response in manifest, passes to step handler
- **Step handling (steptypes.py, orchestrator.py):** Agent-task resume path injects response into prompt context
- **Manifest (manifest.json):** Step record includes `human_responses` array for audit trail
- **Builder prompt template (prompts/implement-phase.md, etc.):** Receives injected `## Human decision` section

### Data flow

```
human: gauntlet resume <slug> --response "..."
  ↓
CLI validates: --response present and required on parked step
  ↓
RunManager.resume():
  - Read manifest, locate parked step
  - Validate step is agent_task with parked_reason == "upstream_conflict"
  - If latest response is state=pending, reuse it (idempotent recovery); else
    append {response_id, response_text, timestamp, user, response_attempt,
    state=pending} and persist atomically
  - Pass step + recorded response to handler; mark state=consumed on outcome
  ↓
Agent handler:
  - Re-render agent prompt with injected `## Human decision` section
  - Re-run agent with context
  - Agent receives: prior conflict + human response + prior attempt artifacts
  ↓
Agent re-evaluates (classifies via FR-3.0 precedence, emits FR-10 disposition):
  - Proceeds in-place only if response is fully consistent with every approved artifact
  - Surfaces amendment-required (parks for FR-10.4 re-gate) if response requires
    OR proceeds-despite a PRD/plan change — no proceed-now-amend-later path
  - Surfaces new conflict if response is ambiguous or impossible
  ↓
Result:
  - If resolves: step completes/commits; run proceeds to next step
  - If new conflict: step parks again with new conflict; human can provide another response
```

---

## 4. Solution overview

Implement `gauntlet resume <slug> --response "<human instructions>"` such that:

1. **CLI accepts arbitrary-text response** — no fixed menu of options
2. **Response is recorded in manifest** — timestamped, persisted, auditable
3. **Response is injected into builder prompt** — via synthetic artifact in step context
4. **Builder re-evaluates, not re-surfaces** — uses decision context to proceed in-place, route artifact changes back through their own gate (no in-place amend), or surface a new conflict

Examples:
- `--response "Ratify option 1: remove asset_root change from test strategy"` → proceed (conflict resolves in-place)
- `--response "Rewrite F-004 to infer asset_root; implement refresh on asset_root change"` → requires amending plan → routed through plan gate (see FR-3)
- `--response "Defer this to post-v1; proceed with option 1 and record in FUTURE.md"` → proceed (deviation logged)

---

## 5. Requirements

### FR-1: CLI interface
- **Acceptance:** `gauntlet resume <slug> --response "<text>"` accepts arbitrary-text instruction (required when supplied, no default)
- Text is passed verbatim; no validation or parsing
- Errors if run is not parked: "run '<run_id>' is not parked; cannot resume with --response"
- Errors if step is not agent_task: "step '<step_id>' is a <type>; --response only applies to agent_task steps"

#### FR-1.1: `--response` required only for conflict parks (scoping)
The discriminator that decides whether `--response` is required is the
`parked_reason` field (FR-2.1), **not** bare `status == "parked"`. This narrows
the behavior change so existing flows are not broken:
- Parked agent_task with `parked_reason == "upstream_conflict"`: this is the new
  path. `gauntlet resume <slug>` **without** `--response` errors:
  "step '<step_id>' parked on an upstream conflict; resume it with
  --response \"<decision>\" (see `gauntlet resume --help`)". Resuming with
  `--response` follows the FR-2/FR-4/FR-5 flow.
- Parked agent_task **without** `parked_reason == "upstream_conflict"` (e.g. a
  generic park): `gauntlet resume <slug>` keeps its **existing** response-less
  re-run behavior unchanged. `--response` is still accepted there and recorded,
  but is not required.
- **Acceptance:** a conflict park requires `--response`; a non-conflict agent_task
  park resumes without it exactly as before this feature.

### FR-2: Manifest recording (audit trail)

- **Acceptance:** Each `--response` invocation appends one entry to
  `steps[N].human_responses` (append-only array) with these fields:
  - `response_id` — stable unique id assigned at append time:
    `"<step_id>-resp-<ordinal>"` where `<ordinal>` is the 1-based position in
    the array. Once written it never changes; it is how the builder references
    a response (FR-5, FR-10) and how recovery deduplicates (FR-7.1 idempotency).
  - `response_text` (the instruction, verbatim)
  - `timestamp` (ISO 8601, from injected orchestrator clock for determinism)
  - `user` (the human operator, resolved per FR-9 — never agent identity)
  - `response_attempt` — 1-based ordinal of this response on this step. This is
    a **response counter**, distinct from the failure-retry counter (see FR-6);
    it is *not* `StepRecord.attempts`.
  - `state` — `"pending"` immediately after append (before the agent launches),
    transitioned to `"consumed"` once the resumed agent reaches a terminal
    outcome (proceeds, parks, or fails). Drives idempotent recovery (FR-7.1).

#### FR-2.1: Conflict-park discriminator
- When an agent halts with `UPSTREAM CONFLICT`, the orchestrator records
  `steps[N].parked_reason = "upstream_conflict"` alongside `status = "parked"`.
  Other park causes (human_gate, generic agent re-run) leave `parked_reason`
  unset or set to their own value. This is the **only** signal `--response`
  uses to decide whether a parked step is a conflict park (see FR-1.1). It
  is a single enum field, not the structured-conflict metadata deferred in §7.

#### FR-2.2: Durability and commit ownership
- The manifest is written **atomically** (write to a temp file in the same
  directory, then `os.replace` over `manifest.json`) on every mutation, so a
  crash mid-write never leaves a truncated or partial manifest.
- The **orchestrator** owns manifest persistence and its git commit, on the
  existing manifest-checkpoint path used for all other `StepRecord` mutations —
  `--response` introduces no new committer and no direct-write path for the
  builder. The append (state `pending`) and the later state→`consumed`
  transition are each persisted and committed as ordinary manifest checkpoints,
  so every response entry reaches git history regardless of where a crash lands.
- **Acceptance:** after recording a response, `manifest.json` contains the entry
  with `state="pending"`; after the resumed agent terminates, the same entry
  reads `state="consumed"`; both states are reachable from git history (the
  entry is never silently dropped or overwritten).
- **Acceptance:** `manifest.json` is human-readable (JSON structure, not binary)
  and `human_responses` persists across future resumes (all prior responses
  visible to the next agent re-run).

### FR-3: Response taxonomy (critical for FR-10.4 compliance)

#### FR-3.0: Classification precedence (deterministic decision rule)

The builder classifies a response by applying these tests **in order** and
stopping at the first that matches. This removes "is it (a) or (c)?" model
discretion: anything touching an approved artifact is forced down the gate path.

1. **Contradicts or asks to change an approved artifact?** If the response
   requests, or acknowledges and proceeds despite, any divergence from the text
   of an approved PRD or plan, classify as **(b) amendment-required**. A
   response that says "proceed even though this contradicts the plan" is an
   amendment, not a deviation note — it does not get to bypass the gate.
2. **Ambiguous / does not unambiguously resolve the conflict?** Classify as
   **new clarification conflict** (park, per FR-5) — never "proceed."
3. **Fully consistent with every approved artifact?** Only then may it be
   **(a) proceed-in-place** or **(c) deviation-log** (below). A response is
   eligible for proceed only when proceeding contradicts no approved artifact.

When uncertain between two categories, the builder picks the **earlier** one in
this list (fail closed toward the gate). The classification is emitted as the
structured `disposition` of FR-10, so it is testable, not prose-only.

#### FR-3(a): Conflict-resolution responses — resolve the conflict *in place* without amending approved artifacts
- Example: "The asset_root change case is now clarified: as expected, it's a customization (treated as stale-warn per F-004). Template-version bump → refresh as expected. No contradiction remains." (conflict resolved through understanding, not artifact editing)
- Handler: Builder proceeds; implements no artifact changes; may commit as-is
- **Acceptance:** Builder emits `disposition = "proceed_in_place"`, references the
  consumed `response_id`(s), describes how the conflict is resolved, commits

#### FR-3(b): Artifact-amendment responses — require changing a PRD or approved plan to resolve the conflict
- Example: "Rewrite F-004 algorithm to infer asset_root; implement refresh on asset_root change" (changes algorithm, impacts plan tests)
- This category **also covers** any response that asks the builder to proceed
  while knowingly contradicting approved artifact text. There is no "proceed now,
  amend later" path: the halt-and-regate in FR-10.4 is mandatory before such
  work can land. (This closes the deviation-note bypass.)
- Handler: Builder MUST NOT directly amend the artifact. Instead:
  - Builder surfaces: "Your response requires amending PRD §X / plan FR-Y. This change must go through that artifact's own review-and-gate cycle per FR-10.4. Please revise the PRD/plan on its own feature branch, go through its gate, then resume this run with a new response that does not require further artifact changes."
  - Status: step parks again (`parked_reason = "upstream_conflict"`) with a new conflict surfacing the gate requirement — it does not re-emit the old conflict unchanged
  - Human must handle the PRD/plan amendment separately (in its own branch/PR), then provide a new response
- **Acceptance:** Builder emits `disposition = "amendment_required"` with a
  `conflict` body explaining the FR-10.4 path; no implementation lands

#### FR-3(c): Deviation-note responses — defer work that the approved artifacts already permit
- Permitted **only** when the deviation does **not** contradict an approved
  artifact — e.g., choosing among options the artifacts leave open, or deferring
  out-of-scope follow-up work to FUTURE.md. A deviation that contradicts an
  approved artifact is category (b), per FR-3.0 step 1, and must halt-and-regate.
- Example: "Proceed with option 1; defer the post-v1 enhancement to FUTURE.md" (where both options are within what the artifacts allow)
- Handler: Builder acknowledges the deviation, records it (e.g., FUTURE.md entry), commits with body noting "Human decided: <response>"
- **Acceptance:** Builder emits `disposition = "proceed_with_deviation"`,
  records the deviation, commits; the recorded deviation contradicts no approved
  artifact

### FR-4: Prompt injection mechanism

- **Acceptance:** Responses are passed to the builder via a **single
  invocation-local synthetic artifact** that holds the *complete ordered
  response history*, not one file per response:
  - On each resume, the orchestrator rebuilds `human-response.md` from the
    manifest's `human_responses` array, in chronological order. There is exactly
    one file with one fixed name; repeated resumes regenerate it rather than
    accumulating differently-named files — so there is **no filename collision**
    and no per-attempt naming scheme to manage.
  - Content (one block per recorded response, oldest first):
    ```markdown
    # Human decisions (chronological)

    ## Response <response_id> — attempt <response_attempt>
    Response: <response_text>
    Timestamp: <ISO 8601>
    User: <human operator email>

    ## Response <response_id> — attempt <response_attempt>
    ...
    ```
  - The artifact is added to an **invocation-local copy** of the step's inputs
    list used only to render this one prompt. It is **not** written into
    `manifest.json`, and the approved pipeline definition and the step's
    persisted `inputs:` are **not mutated** — the durable record of responses is
    the `human_responses` array (FR-2), and this file is a derived view of it.
  - Builder receives it as a `--- input artifact: human-response.md ---` block
    via the existing `_render_prompt` path (steptypes.py:217-233); no new `{{}}`
    token interpolation is added.

#### FR-4.1: Artifact lifecycle (single source of truth)
- `human-response.md` is an **ephemeral, regenerated render input**, not a
  persisted run artifact. Lifecycle, stated once to avoid the inconsistency
  flagged in review:
  - **Path:** the step's working/prompt-render area, written fresh from the
    manifest at the start of each resume invocation.
  - **Persistence:** none of its own — it carries no state the manifest doesn't
    already hold; it is fully reconstructible from `human_responses`.
  - **Cleanup:** it is overwritten (rebuilt) on the next resume and may be left
    in the working tree between resumes; because it is derived, a stale copy is
    harmless and is not relied upon. It is **not** committed as a step artifact.
  - **Manifest representation:** the response data lives in `human_responses`
    (FR-2); the file itself has no manifest entry.
  - **On re-park / crash:** nothing to reconcile — the next resume regenerates it
    from the durable array, so a crash mid-write cannot corrupt response state.
- **Acceptance:** Builder receives the full ordered response history in artifact
  blocks and acknowledges it in output; the pipeline definition and persisted
  step inputs are byte-for-byte unchanged before and after the resume.

### FR-5: Builder resume logic

- **Acceptance:** When resuming a parked agent_task with prior human response(s):
  - Builder receives all prior responses (chronologically ordered, FR-4).
  - Builder classifies the latest response per the FR-3.0 precedence rule and
    emits the FR-10 structured `disposition`.
  - If response resolves conflict per FR-3(a): proceeds normally.
  - If response requires artifact amendment per FR-3(b): emits
    `amendment_required` and parks per the FR-3(b) handler.
  - If response is ambiguous: emits `new_conflict` asking for clarification.
- **Acceptance (observable, not novelty-based):** rather than the unenforceable
  "must not re-surface the *exact same* conflict" (which the deferred conflict
  metadata in §7 cannot define or test), the testable requirement is:
  - The builder's structured output **must list the `response_id`(s) it
    consumed** in `responses_considered`, and
  - its `disposition` and `action_summary` must be a function of that response —
    a `new_conflict` disposition must carry a `conflict` body whose
    `requested_input` names what is still missing *given* the supplied response
    (i.e. it asks for something the response did not provide), not a verbatim
    repeat of the prior park's text.
  This is checkable against the structured output (FR-10) with deterministic
  fixtures, with no need to compute conflict identity or fingerprints.

### FR-6: Retry-budget interaction (two distinct counters)

There are **two separate counters**; conflating them is the defect this section
exists to prevent. Neither is overloaded onto the other.

| Counter | Field | Increments when | Governs |
|---|---|---|---|
| Failure-retry | `StepRecord.attempts` (existing, manifest.py:61) | a step run ends in **failure** (agent error / non-conflict abnormal exit) and `on_fail` routes back to the builder | the `max_retries` budget |
| Response-attempt | `human_responses[*].response_attempt` (FR-2) | a `--response` resume **appends a response** (regardless of outcome) | nothing budget-related; audit ordering only |

- **`max_retries` calculation:** the `on_fail` handler compares
  `StepRecord.attempts` (failure-retry counter) against `max_retries`. A run
  exhausts its budget when `StepRecord.attempts > max_retries`. The
  response-attempt counter is **never** part of this comparison.
- A conflict park (`parked_reason = "upstream_conflict"`) is **not a failure**:
  it does **not** call `on_fail` and does **not** increment
  `StepRecord.attempts`. Therefore arbitrarily many `--response` cycles on the
  same step never advance the failure-retry counter and never exhaust
  `max_retries`.
- A `--response` re-run that ends in genuine **failure** (agent internal error,
  not a conflict) *does* trigger `on_fail` and *does* increment
  `StepRecord.attempts` exactly once, like any other failed run.
- **Acceptance:** with `max_retries = N`, a step can be resumed with
  `--response` more than `N` times while it keeps parking on conflicts, and the
  run is never failed for exhausting retries; but `N+1` genuine agent failures
  do exhaust the budget. Both are checkable from `StepRecord.attempts` vs.
  `len(human_responses)` in the manifest.
- **Acceptance:** Builder instructions state: "Conflicts do not consume the
  retry budget; only failures do. You can be asked for multiple --response
  cycles on the same step without hitting the retry limit."

### FR-7: No forced retry loop

- **Acceptance:** `gauntlet resume --response` is one invocation; it does not loop internally
- If builder surfaces a new conflict in response to the human's decision, the run parks again and awaits another `--response`
- Human decides whether to provide another response or abort
- **Acceptance:** Prevents infinite agent loops; humans decide stop condition

#### FR-7.1: Idempotent resume-with-response (crash recovery)
A `--response` invocation must be a single idempotent state transition, so that
re-running the command after a crash never appends a duplicate response or
double-counts an attempt. The protocol, keyed on the `state` field (FR-2):

1. **Append (pending):** assign `response_id`, write the entry with
   `state = "pending"`, persist atomically and commit (FR-2.2) — this happens
   **before** the agent launches.
2. **Launch:** render the prompt (FR-4) and run the agent.
3. **Consume:** on the agent's terminal outcome, transition the same entry to
   `state = "consumed"`, persist atomically and commit.

Recovery rule — when `gauntlet resume` runs and the step's **latest**
`human_responses` entry is `state == "pending"` (a prior invocation crashed
between steps 1 and 3):
- The orchestrator does **not** append a new entry. It **re-launches with the
  existing pending response** (step 2 onward), making the command idempotent.
- A `--response` argument identical to the pending entry's `response_text` is
  accepted (treated as a retry of the same transition). A **different**
  `--response` errors: "a pending response (<response_id>) is awaiting
  processing; re-run `gauntlet resume <slug>` to finish it, or abort the run —
  do not supply a new response over a pending one."
- Because `response_id` is assigned once at append and the recovery path reuses
  it, no crash sequence can produce two entries for one human decision, and
  `response_attempt` / `StepRecord.attempts` cannot be incremented twice.
- **Acceptance:** killing the process after the pending append and re-running the
  resume yields exactly one `human_responses` entry (now `consumed`) and one
  agent re-run; counters are unchanged from a clean single run.

### FR-8: Integration with non-conflict parked steps

- **Acceptance:** If a human_gate step is parked (not an agent_task), `gauntlet resume --response` errors: "use `gauntlet approve` or `gauntlet reject` for human_gate steps; --response is for agent_task steps"
- Does not break existing approve/reject flows

### FR-9: Operator identity resolution (audit field)

The `user` field (FR-2) is a required audit field; its resolution is fully
specified and **fails closed** rather than recording an empty or malformed value.

- **Precedence:** `GAUNTLET_USER_EMAIL` env var wins **only if** it is set and
  non-empty after trimming surrounding whitespace; otherwise fall back to
  `git config user.email`.
- **Normalization:** trim leading/trailing whitespace from whichever source is
  used. A value that is empty or whitespace-only is treated as **unset** (so an
  exported-but-empty `GAUNTLET_USER_EMAIL` does not shadow git config).
- **Failure handling (fail closed):** if neither source yields a non-empty value
  — env unset/blank **and** git config missing or the `git config` invocation
  fails (non-zero exit) — the resume **errors and records nothing**:
  "cannot resolve operator identity for the audit trail: set
  `GAUNTLET_USER_EMAIL` or `git config user.email`". No response entry is
  appended in this case (the FR-2 append does not run), so the manifest never
  gains an entry with an empty `user`.
- **Validation:** minimal sanity check — the resolved value must be non-empty
  after trimming; v1 does **not** enforce RFC-5322 email syntax (deferred, §7),
  but it must not be blank. Malformed-but-nonblank values are recorded verbatim
  (the human is responsible for their own git config), matching the "no response
  validation" stance while still guaranteeing a present, non-empty audit field.
- **Acceptance:** with both sources unset/blank, `--response` errors and the
  manifest is unchanged; with a whitespace-padded `GAUNTLET_USER_EMAIL`, the
  recorded `user` is the trimmed value; with an empty `GAUNTLET_USER_EMAIL` and a
  valid git config, the git-config value is used.

### FR-10: Structured resume disposition output (deterministic test oracle)

So acceptance tests do not depend on subjective natural-language judgments
(acknowledgement, paraphrase quality, conflict novelty), the builder emits a
**structured disposition** at the end of every `--response` resume, in addition
to its prose. This is the oracle the FR-3 / FR-5 / §8 criteria are checked
against.

- **Schema** (a new `schemas/resume-disposition.json`, validated like other
  structured agent outputs):
  ```json
  {
    "disposition": "proceed_in_place | amendment_required |
                    proceed_with_deviation | new_conflict",
    "responses_considered": ["<response_id>", "..."],
    "action_summary": "<one-line description of what the builder did/will do>",
    "conflict": {
      "summary": "<what is still blocked>",
      "requested_input": "<what the human must supply or change next>",
      "artifact": "<PRD §X / plan FR-Y, when disposition=amendment_required>"
    }
  }
  ```
  `conflict` is required when `disposition` is `amendment_required` or
  `new_conflict`, and null/omitted otherwise.
- The `disposition` enum maps 1:1 to the FR-3 categories and the FR-5 outcomes,
  making classification machine-checkable rather than inferred from prose.
- **Testing protocol:** P2 and the end-to-end test drive the builder through a
  **deterministic adapter fixture** (a scripted/replayed adapter, not a live
  model) that returns canned disposition outputs, so CI assertions are
  reproducible and do not require live-model judgment. The fixture-returned
  `disposition`, `responses_considered`, and `conflict` are asserted directly.
- **Acceptance:** every resume produces a schema-valid disposition object whose
  `responses_considered` lists the `response_id`(s) the builder consumed; tests
  assert on these fields, never on substring matches of prose.

---

## 6. Data sources (reuse — do not reinvent)

- **User identity:** Resolved per **FR-9** (operator email, not agent identity).
  `config.identity()` returns `agent@gauntlet.local`, which would defeat the
  audit trail, so it is never used for this field.
- **Timestamp:** Use injected orchestrator clock (`RunManager.clock`, default `datetime.now(timezone.utc).isoformat()`; orchestrator.py:45,63). Enables deterministic test replay; avoids deprecated naive datetime forms.
- **Attempt tracking:** Two distinct counters per **FR-6** — the existing
  `StepRecord.attempts` (manifest.py:61) is the **failure-retry** counter and is
  incremented only on failure routes, **not** on conflict resumes; the
  per-response `response_attempt` ordinal (FR-2) is incremented on each
  `--response` append. They are not the same field.
- **Artifact injection:** Mimic existing input-artifact flow (_render_prompt, steptypes.py:217-233) — add the synthetic input to an **invocation-local** copy of the `inputs:` list (FR-4), auto-appended as artifact blocks; no new interpolation path and no mutation of the persisted definition.
- **Parked-step detection:** Reuse existing `status == "parked"` check
  (manifest.py:27) to find the parked step; use the new `parked_reason` enum
  (FR-2.1) to discriminate conflict parks from other parks. `parked_reason` is
  the one small field this feature adds — it is not the structured-conflict
  metadata deferred in §7.

---

## 7. Non-goals / Deferrals

- **Interactive prompt on resume** — deferred; --response is CLI-only
- **Validation of response text** — deferred; humans decide what's valid
- **Email-syntax validation of operator identity** — deferred (FR-9 enforces
  only non-empty; RFC-5322 validation is out of scope for v1)
- **Structured conflict metadata** (rich conflict bodies, fingerprints) —
  deferred. v1 adds exactly **one** discriminator field, `parked_reason`
  (FR-2.1), to tell a conflict park from other parks. That single enum is **not**
  the deferred metadata; it carries no conflict content.
- **Conflict fingerprinting / dedup / identity comparison** — deferred. v1 does
  **not** compute whether two conflicts are "the same." The re-surface guard is
  reframed (FR-5) as an observable property of the structured output referencing
  the supplied response, which needs no fingerprinting.

---

## 8. Acceptance criteria

### CLI layer
- ✓ `gauntlet resume <slug> --response "<text>"` parses and accepts arbitrary text
- ✓ Error if `--response` not provided and step parked with `parked_reason == "upstream_conflict"` (FR-1.1)
- ✓ Non-conflict agent_task park still resumes **without** `--response` exactly as before (FR-1.1)
- ✓ Error if run is not parked, or step is not agent_task

### Manifest
- ✓ Response + metadata (`response_id`, `state`, `response_attempt`) recorded in `manifest.json` under `steps[N].human_responses` array
- ✓ Array persists across multiple resume attempts (append-only)
- ✓ Manifest written atomically; pending→consumed transition reachable from git history (FR-2.2)
- ✓ Re-running a crashed resume yields exactly one entry, not a duplicate (FR-7.1 idempotency)
- ✓ User identity resolved per FR-9: trimmed, env-override only when non-empty, fail-closed error (no entry) when neither source resolves
- ✓ Timestamp sourced from injected orchestrator clock (deterministic, no ad-hoc datetime calls)

### Prompt injection
- ✓ Single `human-response.md` artifact holds the **full chronological response history**, rebuilt from the manifest each resume (FR-4)
- ✓ Pipeline definition and persisted step `inputs:` are unchanged before/after the resume (no mutation)
- ✓ No new interpolation logic needed (standard artifact flow)

### Builder integration (checked against the FR-10 structured disposition)
- ✓ Builder emits a schema-valid `disposition` whose `responses_considered` lists the consumed `response_id`(s)
- ✓ `disposition` correctly reflects the FR-3.0 precedence: any artifact-contradicting response → `amendment_required`; ambiguous → `new_conflict`; only fully-consistent → `proceed_in_place`/`proceed_with_deviation`
- ✓ A response asking to proceed while contradicting an approved artifact yields `amendment_required` (no implementation lands) — the deviation bypass is closed (F-001)
- ✓ `new_conflict` carries a `conflict.requested_input` naming what the supplied response did not provide (FR-5) — checked from structured output, not prose
- ✓ If response resolves or is accepted, builder commits (no re-park)

### Retry-budget interaction
- ✓ Conflict parks do not increment `StepRecord.attempts`; only genuine failures do (FR-6)
- ✓ With `max_retries = N`, > N conflict resumes never fail the run; N+1 genuine failures do exhaust the budget

### End-to-end test (deterministic fixtures, FR-10)
- ✓ Driven by a scripted adapter fixture returning canned dispositions — assertions are on structured fields, reproducible in CI, no live-model judgment
- ✓ Synthetic test: trigger upstream conflict, provide --response, assert builder disposition is `proceed_in_place` or `new_conflict` per fixture
- ✓ Dogfood validation: resume prd-authoring-aids run with --response, verify manifest audit trail (ids/state/user), verify step commits and proceeds

---

## 9. Implementation phases

### P1: CLI + manifest plumbing + prompt injection
- Add `--response` argument to `gauntlet resume` command (cli.py)
- Validate parked step via `parked_reason` discriminator + error handling (run.py, FR-1.1)
- Record `upstream_conflict` discriminator when an agent halts on conflict (orchestrator.py, FR-2.1)
- Record response entry (`response_id`, `state="pending"`, `response_attempt`) atomically in manifest; idempotent recovery on pending entries (manifest.py, RunManager.resume — FR-2.2, FR-7.1)
- Resolve operator identity per FR-9 (fail closed)
- Rebuild `human-response.md` from the manifest into an invocation-local inputs copy and inject into step context (orchestrator.py, steptypes.py — FR-4)
- **Test:** `gauntlet resume <slug> --response "..."` appends one `human_responses` entry and transitions it pending→consumed; the regenerated `human-response.md` carries the full ordered history; the pipeline definition and persisted step inputs are unchanged; killing and re-running mid-resume does not duplicate the entry

### P2: Builder resume logic
- Update agent instructions: FR-3.0 precedence + FR-3 taxonomy, reference consumed `response_id`(s), re-evaluate conflict
- Add `schemas/resume-disposition.json`; builder emits the FR-10 structured disposition
- Builder re-runs with injected response context
- **Test (deterministic adapter fixture, FR-10):** scripted disposition outputs assert that an artifact-contradicting response → `amendment_required`, an ambiguous response → `new_conflict` with `requested_input`, and a consistent response → `proceed_in_place`/`proceed_with_deviation`; assertions are on structured fields, not prose substrings

### P3: Integration test (dogfood)
- **Primary:** Synthetic test — trigger upstream conflict in a fresh gauntlet run, provide --response, verify full cycle
- **Secondary:** Resume prd-authoring-aids with --response, verify manifest audit trail, verify P1 commits and review cycle proceeds
- **Test:** Manifest shows human response + timestamp; run is un-stuck

---

## 10. Risk & tradeoffs

### Risk: Arbitrary text is too permissive
- **Mitigation:** Builder is instructed to reject artifact amendments (FR-3(b)); humans must route those through the artifact's own gate

### Risk: Builder interprets response differently than intended
- **Mitigation:** Audit trail in manifest preserves what human said; reviewers can validate at gate

### Risk: Encourages humans to bypass conflict resolution instead of fixing root cause
- **Mitigation:** Deviation notes (FR-3(c)) are recorded and surface in FUTURE.md; decisions are auditable

### Tradeoff: No upfront validation of response text
- **Pro:** Maximum flexibility; no constraints
- **Con:** Humans can type nonsense; builder must handle gracefully
- **Chosen:** Flexibility; builder asks for clarification if needed

### Tradeoff: Synthetic artifact vs. new interpolation path
- **Pro:** Reuses existing artifact-injection logic; no new prompt-render machinery
- **Con:** Writes a derived `human-response.md` render input each resume
- **Lifecycle (single source of truth, per FR-4.1):** the file is an ephemeral,
  regenerated render input — not a persisted/committed step artifact. It is
  rebuilt from the `human_responses` manifest array on every resume and may be
  left in the working tree between resumes (a stale copy is harmless because it
  is derived). The durable record is the manifest array, never the file.
- **Chosen:** Simpler, lower risk

---

## 11. Success criteria for v1

- ✓ `gauntlet resume <slug> --response "..."` is the standard documented way to resume from a conflict
- ✓ FR-10.4 compliance: artifact amendments are rejected and routed back through their own gates
- ✓ prd-authoring-aids run can be un-stuck using this mechanism
- ✓ Manifest audit trail is immutable (appended to git history, not overwritten)
- ✓ Builder's resume logic no longer re-surfaces unchanged conflicts after receiving a response

---

## Appendix: Example — prd-authoring-aids conflict resolution (revised)

**Before (broken flow):**
```
builder → UPSTREAM CONFLICT (P1 implementation contradicts plan)
human → manually edit plan.md (violates FR-10.4)
gauntlet resume prd-authoring-aids
builder → re-run, re-surface SAME conflict (no signal that human decided)
run → still parked (stuck)
```

**After (with --response).** Note the FR-3.0 precedence: because the
implementation contradicts the approved plan's test-strategy wording, the
"proceed now, amend later" shortcut is **not** available — the contradiction
must clear the plan's own gate first. The mechanism makes that explicit instead
of silently proceeding.

```
builder → UPSTREAM CONFLICT (P1 implementation contradicts plan test-strategy)
          orchestrator records parked_reason = "upstream_conflict"

Stage 1 — human tries to proceed despite the contradiction:

gauntlet resume prd-authoring-aids --response "The F-004 algorithm and
never-clobber principle are correct. The test-strategy bullet mentioning
asset_root change is overstated. Implement option 1 now and note that
it contradicts the old plan wording; I'll fix the plan later."

orchestrator → appends response (state=pending → consumed), rebuilds
               human-response.md from the manifest

builder → classifies via FR-3.0 step 1: the response asks to proceed while
          contradicting approved plan text → disposition = "amendment_required".
          Emits: "This contradicts plan test-strategy wording. Per FR-10.4 it
          cannot land until the plan is amended through its own review-and-gate
          cycle. Revise the plan on its own branch, gate it, then resume with a
          response that no longer contradicts an approved artifact."
          NO implementation lands; step parks again (parked_reason unchanged).

Stage 2 — human amends the plan through ITS gate (separate branch/PR), then:

gauntlet resume prd-authoring-aids --response "Plan test-strategy has been
amended and re-gated (PR #NN) so option 1 no longer contradicts it.
Implement option 1 (template-version → refresh, asset_root change → warn)."

builder → classifies via FR-3.0 step 3: now fully consistent with the amended,
          approved plan → disposition = "proceed_in_place". Implements option 1,
          commits "P1: ..." with body referencing the consumed response_id.

run → proceeds to review cycle (no new conflict surfaces)

manifest → human_responses array shows both responses (ids, state=consumed,
           timestamps, operator email)
```

---

**Next step:** This revision addresses the current round's findings:
F-001 (closed the deviation-note bypass — artifact contradictions now force the
FR-3(b) halt-and-regate; FR-3.0 precedence + appendix rewrite),
F-002 (`parked_reason` conflict discriminator, FR-1.1/FR-2.1 — preserves
response-less resume for non-conflict parks),
F-003 (single invocation-local ordered-history artifact, no collisions, no
definition/input mutation, FR-4),
F-004 (atomic manifest persistence + commit ownership + crash recovery, FR-2.2),
F-005 (split failure-retry vs. response-attempt counters with exact increment
points and `max_retries` calc, FR-6),
F-006 (re-surface guard reframed as an observable property of the structured
output, FR-5; fingerprinting stays deferred),
F-007 (deterministic FR-3.0 classification precedence),
F-008 (idempotent resume with `response_id` + pending/consumed state, FR-7.1),
F-009 (single artifact lifecycle specified, FR-4.1 / §10),
F-010 (structured `resume-disposition` output + deterministic adapter fixtures,
FR-10),
F-011 (operator-identity precedence/normalization/fail-closed, FR-9).
Ready for re-review.

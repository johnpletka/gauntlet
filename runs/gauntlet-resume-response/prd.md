# PRD: Resume-with-response — human decision mechanism for upstream conflicts

**Feature slug:** `gauntlet-resume-response`  
**Status:** Draft for adversarial review (revised after F-001/F-002/F-003/F-004 feedback)  
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
- `reject` (run.py:850) fails the step and triggers on_fail retry logic — not a resume with fresh context
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
  - Validate step is agent_task with parked status
  - Record: {response_text, timestamp, user, attempt_number}
  - Pass step + recorded response to handler
  ↓
Agent handler:
  - Re-render agent prompt with injected `## Human decision` section
  - Re-run agent with context
  - Agent receives: prior conflict + human response + prior attempt artifacts
  ↓
Agent re-evaluates:
  - Proceeds if response resolves conflict in-place
  - Proceeds-and-amends if response requires PRD/plan change (see FR-3 split)
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
4. **Builder re-evaluates, not re-surfaces** — uses decision context to proceed, amend (with re-gating), or surface new conflict

Examples:
- `--response "Ratify option 1: remove asset_root change from test strategy"` → proceed (conflict resolves in-place)
- `--response "Rewrite F-004 to infer asset_root; implement refresh on asset_root change"` → requires amending plan → routed through plan gate (see FR-3)
- `--response "Defer this to post-v1; proceed with option 1 and record in FUTURE.md"` → proceed (deviation logged)

---

## 5. Requirements

### FR-1: CLI interface
- **Acceptance:** `gauntlet resume <slug> --response "<text>"` accepts arbitrary-text instruction (required, no default)
- Text is passed verbatim; no validation or parsing
- Errors if run is not parked: "run '<run_id>' is not parked; cannot resume with --response"
- Errors if step is not agent_task: "step '<step_id>' is a <type>; --response only applies to agent_task steps"

### FR-2: Manifest recording (audit trail)
- **Acceptance:** Each `--response` invocation records in step's metadata:
  - `response_text` (the instruction, verbatim)
  - `timestamp` (ISO 8601)
  - `user` (from `config.identity()` per existing identity plumbing, not new source)
  - `attempt_number` (mapped to StepRecord.attempts field)
- Under `steps[N].human_responses` array (append-only, immutable in git history)
- Persists across future resumes (all prior responses visible to next agent re-run)
- **Acceptance:** `manifest.json` is human-readable (JSON structure, not binary)

### FR-3: Response taxonomy (critical for FR-10.4 compliance)

Responses fall into three categories; handlers differ:

**(a) Conflict-resolution responses** — resolve the conflict *in place* without amending approved artifacts
- Example: "Ratify option 1: remove asset_root change from test strategy" (conflict is already resolved by understanding it better)
- Handler: Builder proceeds; implements no artifact changes; may commit as-is
- **Acceptance:** Builder output acknowledges the response, describes how conflict is resolved, commits

**(b) Artifact-amendment responses** — require changing a PRD or approved plan to resolve the conflict
- Example: "Rewrite F-004 algorithm to infer asset_root; implement refresh on asset_root change" (changes algorithm, impacts plan tests)
- Handler: Builder MUST NOT directly amend the artifact. Instead:
  - Builder surfaces: "Your response requires amending PRD §X / plan FR-Y. This change must go through that artifact's own review-and-gate cycle per FR-10.4. Please revise the PRD/plan on its own feature branch, go through its gate, then resume this run with a new response that does not require further artifact changes."
  - Status: step parks with a new conflict (not re-surfaces old one)
  - Human must handle the PRD/plan amendment separately (in its own branch/PR), then provide a new response
- **Acceptance:** Builder rejects artifact amendments and explains the FR-10.4 path

**(c) Deviation-note responses** — proceed with the conflict *acknowledged but unresolved*, logging the decision for later triage
- Example: "Proceed with option 1; defer full solution to post-v1; record in FUTURE.md"
- Handler: Builder acknowledges deviation, implements placeholder/comment (e.g., FUTURE.md entry), commits with body noting "Human decided: <response>"
- **Acceptance:** Builder commits with deviation recorded; no re-surfacing of original conflict

### FR-4: Prompt injection mechanism

- **Acceptance:** Response is passed to builder via **synthetic input artifact** approach:
  - RunManager creates a temporary `human-response.md` file with content:
    ```markdown
    # Human decision (resume attempt N)
    
    Response: <response_text>
    
    Timestamp: <ISO 8601>
    User: <identity>
    
    [Metadata for audit]
    ```
  - Artifact is added to step's inputs before prompt render
  - Agent-task prompt template includes: `## Human decision\n\n{{inputs.human-response.md}}`
  - Builder receives the response as part of its normal artifact context (no special interpolation needed)
- **Acceptance:** Builder receives response in prompt and acknowledges it in output

### FR-5: Builder resume logic

- **Acceptance:** When resuming a parked agent_task with prior human response(s):
  - Builder receives all prior responses (chronologically ordered)
  - Builder acknowledges: "The human responded: <paraphrase>. I will <action>."
  - Builder does NOT re-surface the exact same conflict unchanged
  - If response resolves conflict per FR-3(a): proceeds normally
  - If response requires artifact amendment per FR-3(b): surfaces new conflict per FR-3(b) handler
  - If response is ambiguous: surfaces new conflict asking for clarification (NEW conflict, not re-surface)
- **Acceptance:** Transcript shows builder acknowledged response and took action (no unchanged re-surface)

### FR-6: Retry-budget interaction

- **Acceptance:** `--response` re-runs are subject to the step's existing `on_fail` retry budget (pipeline.yaml on_fail handler)
- If `--response` re-run fails (e.g., agent internal error, not a conflict park), the on_fail handler triggers: route_to implements, max_retries decrements
- If `--response` re-run parks with a new conflict, status="parked" — no retry budget consumed, human can respond again
- **Acceptance:** Clarify in builder instructions: "Conflicts do not consume the retry budget; only failures do. You can be asked for multiple --response cycles on the same step without hitting the retry limit."

### FR-7: No forced retry loop

- **Acceptance:** `gauntlet resume --response` is one invocation; it does not loop internally
- If builder surfaces a new conflict in response to the human's decision, the run parks again and awaits another `--response`
- Human decides whether to provide another response or abort
- **Acceptance:** Prevents infinite agent loops; humans decide stop condition

### FR-8: Integration with non-conflict parked steps

- **Acceptance:** If a human_gate step is parked (not an agent_task), `gauntlet resume --response` errors: "use `gauntlet approve` or `gauntlet reject` for human_gate steps; --response is for agent_task steps"
- Does not break existing approve/reject flows

---

## 6. Data sources (reuse — do not reinvent)

- **User identity:** Use existing `config.identity()` plumbing (steptypes.py:291) — do not invent new env-var source
- **Timestamp:** Use `datetime.utcnow().isoformat()` (standard Python, used elsewhere in manifest)
- **Attempt tracking:** Map to existing `StepRecord.attempts` field (manifest.py:61, incremented on every resume)
- **Artifact injection:** Mimic existing input-artifact flow (orchestrator.py _render_prompt, line 217) — add synthetic input, not new interpolation path
- **Parked-step detection:** Reuse existing `status == "parked"` check (manifest.py:27) — no new metadata field needed for v1

---

## 7. Non-goals / Deferrals

- **Interactive prompt on resume** — deferred; --response is CLI-only
- **Validation of response text** — deferred; humans decide what's valid
- **Structured conflict metadata** — deferred (removed to resolve F-003 contradiction); v1 uses simple "re-run with response injected"
- **Conflict fingerprinting / dedup** — deferred; v1 does not prevent same conflict being raised twice

---

## 8. Acceptance criteria

### CLI layer
- ✓ `gauntlet resume <slug> --response "<text>"` parses and accepts arbitrary text
- ✓ Error if `--response` not provided and step is parked (and is agent_task)
- ✓ Error if run is not parked, or step is not agent_task

### Manifest
- ✓ Response + metadata recorded in `manifest.json` under `steps[N].human_responses` array
- ✓ Array persists across multiple resume attempts (append-only)
- ✓ User identity sourced from config.identity(), not new env-var

### Prompt injection
- ✓ Synthetic `human-response.md` artifact created and passed to agent
- ✓ Agent receives response in prompt and acknowledges it
- ✓ No new interpolation logic needed (standard artifact flow)

### Builder integration
- ✓ Builder acknowledges response and describes action
- ✓ Builder categorizes response as (a) conflict-resolution, (b) requires artifact amendment → rejects per FR-3, or (c) deviation-note
- ✓ Builder does not re-surface unchanged conflict after receiving response
- ✓ If response resolves or is accepted, builder commits (no re-park)
- ✓ If response ambiguous or requires amendment, builder surfaces NEW conflict (with explanation)

### Retry-budget interaction
- ✓ Conflicts do not consume on_fail retry budget; only failures do
- ✓ Multiple --response cycles possible on same step without hitting max_retries

### End-to-end test
- ✓ Synthetic test: intentionally trigger upstream conflict, provide --response, verify builder proceeds or surfaces new conflict
- ✓ Dogfood validation: resume prd-authoring-aids run with --response, verify manifest audit trail, verify step commits and proceeds

---

## 9. Implementation phases

### P1: CLI + manifest plumbing + prompt injection
- Add `--response` argument to `gauntlet resume` command (cli.py)
- Validate parked step, error handling (run.py)
- Record response + metadata in manifest (manifest.py, RunManager.resume)
- Create synthetic `human-response.md` artifact and inject into step context (orchestrator.py, steptypes.py)
- **Test:** `gauntlet resume <slug> --response "..."` updates manifest, file appears in step artifacts

### P2: Builder resume logic
- Update agent instructions: FR-3 taxonomy, acknowledge response, re-evaluate conflict
- Builder re-runs with injected response context
- Builder avoids re-surfacing unchanged conflict (checks prior response history)
- **Test:** Builder receives response, either (a) proceeds, (b) rejects artifact amendments per FR-3, or (c) proceeds with deviation logged

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
- **Con:** Creates temporary file (ephemeral, cleaned up after step)
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

**After (with --response):**
```
builder → UPSTREAM CONFLICT (P1 implementation contradicts plan)

Human reviews and decides: "The implementation is correct.
The plan test-strategy has a contradiction. Rather than amend the plan
directly (which would bypass its gate), I'll acknowledge the contradiction
and ask the builder to handle it."

gauntlet resume prd-authoring-aids --response "The F-004 algorithm and
never-clobber principle are correct. The test-strategy bullet mentioning
asset_root change is overstated. Implement option 1 (template-version
→ refresh, asset_root change → warn) and note that this contradicts
the old plan wording; I will amend the plan through its own review
cycle in a follow-up PR."

orchestrator → records response in manifest, creates human-response.md

builder → receives response, acknowledges: "You've confirmed option 1
is correct. I will implement this behavior and commit. The test-strategy
plan wording is a separate artifact amendment that you will handle
through plan review."

builder → implements option 1, commits "P1: ..." with body noting
the upstream-conflict resolution

run → proceeds to review cycle (no new conflict surfaces)

manifest → shows human response, timestamp, user in human_responses array
```

---

**Next step:** This revision addresses F-001 (taxonomy split), F-002 (concrete injection mechanism), F-003 (removed metadata contradiction), F-004 (relationship section), F-005 (adopted house structure), F-006 (immutability claim), F-007 (retry-budget FR-6), F-008 (config.identity source), F-009 (reordered tests). Ready for re-review.

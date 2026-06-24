# PRD: Resume-with-response — human decision mechanism for upstream conflicts

**Feature slug:** `gauntlet-resume-response`  
**Status:** Draft for adversarial review  
**Date:** 2026-06-24  
**Author:** John Pletka

---

## 1. Problem statement

When a builder halts with an `UPSTREAM CONFLICT` (FR-10.4 in PRD-gauntlet.md), the human must decide how to proceed (e.g., "ratify option 1, amend the plan", "rewrite the algorithm to handle X", "defer to post-v1"). Currently there is **no formalized way** to communicate that decision back to the orchestrator.

### Current symptoms
- Human reads the conflict in the transcript
- Human wants to signal their decision (manually edited artifact, direct prompt, etc.)
- `gauntlet resume <slug>` re-runs the builder, which re-surfaces the unchanged conflict
- No audit trail of what the human decided or why
- The conflict decision cannot be programmatically validated or replayed

### Why it matters
- Halted runs stay stuck indefinitely without a decision mechanism
- No record of human intent (violates "data over inference" principle)
- Future decisions on the same conflict are not guided by prior human choices
- The system treats humans as having no input on conflict resolution

---

## 2. Solution overview

Implement `gauntlet resume <slug> --response "<human instructions>"` such that:

1. **CLI accepts arbitrary-text response** — no fixed menu of options, human gives instructions in natural language or structured form as needed
2. **Response is recorded in the manifest** — timestamped, persisted, auditable
3. **Response is passed to builder in next prompt** — builder sees: "the human decided X; re-evaluate the conflict in light of that"
4. **Builder re-evaluates, not re-surfaces** — builder uses the decision context to either proceed, proceed-with-changes, or surface a new conflict

Examples of valid responses:
- `--response "Ratify option 1: remove asset_root change from test strategy"`
- `--response "Rewrite F-004 algorithm to infer asset_root; implement asset_root-change refresh"`
- `--response "This reveals a gap in PRD §6. Halt and do not proceed until PRD is revised"`
- `--response "Proceed with option 1; defer full asset_root refresh to post-v1 as FUTURE.md entry"`

---

## 3. Requirements

### FR-1: CLI interface
- `gauntlet resume <slug> --response "<text>"` accepts arbitrary-text instruction (required, no default)
- Text is passed verbatim; no validation or parsing (humans decide what's valid)
- Error if neither a parked run nor --response is provided (fail closed)

### FR-2: Manifest recording (audit trail)
- Each `--response` invocation records:
  - `response_text` (the instruction)
  - `timestamp` (when it was recorded)
  - `user` (who provided it; from git config user.email or env var)
  - `attempt_number` (which resume attempt this is)
  - `prior_conflict_id` (reference to what conflict this was responding to, if known)
- Recorded in the step's metadata in `manifest.json`, under a `human_responses` array
- Persisted across all future resume attempts (immutable audit trail)

### FR-3: Builder integration
- When resuming a parked step, the orchestrator passes the human response(s) to the builder in the prompt
- Format in prompt: a `## Human decision` section stating:
  ```
  ## Human decision (resume attempt <N>)
  
  The human responded: "<response_text>"
  
  [timestamp and user info for audit]
  ```
- Builder receives all prior human responses (if multiple resumes), in chronological order
- Builder's instructions:
  - Re-evaluate the conflict in light of the human's direction
  - If the response resolves the conflict, proceed with implementation
  - If the response requests a specific change (rewrite, defer, etc.), implement that change
  - If the response is ambiguous, ask for clarification (surface a new conflict asking "did you mean X or Y?")
  - Do not re-surface the exact same conflict unchanged (that signals "I didn't understand your response")

### FR-4: Builder resume logic
- When resuming from a parked step with a prior UPSTREAM CONFLICT:
  - **OLD behavior:** re-run implementation, re-surface unchanged conflict
  - **NEW behavior:** re-run implementation, receive human response, re-evaluate conflict:
    - If response implies "proceed despite the contradiction", implement and commit normally
    - If response implies "rewrite X", implement the rewrite and commit
    - If response is unactionable or contradicts the code, surface a NEW conflict asking for clarification
    - Never re-surface a conflict that the human has already responded to unless implementation reveals new info

### FR-5: Conflict metadata (supporting FR-4)
- Each UPSTREAM CONFLICT signal includes a stable `conflict_id` (hash of phase + conflict description)
- Human response references this ID implicitly (in `prior_conflict_id`)
- Builder can check: "has the human responded to conflict X already?" and avoid re-surfacing unchanged

### FR-6: No forced retry loop
- `gauntlet resume --response` succeeds once; it does not loop on builder re-runs
- If the builder surfaces a new conflict in response to the human's decision, the run parks again
- Human can then provide another `--response`, or abort
- (Prevents infinite loops; humans decide when to stop retrying)

### FR-7: Integration with other resume scenarios
- Works for parked steps of any type (agent_task, etc.), any conflict category (implementation, validation, etc.)
- Does not break non-conflict parked steps (e.g., human_gate that's waiting for approval)
- `gauntlet resume` without `--response` on a conflict-parked step reports the conflict and suggests `--response`

---

## 4. Non-goals / Deferrals

- **Interactive prompt on resume** — deferred; `--response` is CLI-only
- **Validation of response text** — deferred; humans decide what's valid
- **Auto-recovery logic** — deferred; humans decide what to do on new conflicts
- **Conflict metadata standardization** — deferred; starting with simple hash-based IDs

---

## 5. Acceptance criteria

### CLI layer
- ✓ `gauntlet resume <slug> --response "<text>"` parses and accepts arbitrary text
- ✓ Error with actionable message if `--response` not provided and step is parked with conflict
- ✓ Error if run is not parked (don't allow `--response` on completed/failed runs)

### Manifest
- ✓ Response + metadata recorded in `manifest.json` under `steps[N].human_responses` array
- ✓ Array persists across multiple resume attempts
- ✓ Manifest is human-readable (JSON, not binary)

### Builder integration
- ✓ Builder receives response(s) in prompt under `## Human decision` section
- ✓ Builder acknowledges receipt: "The human responded: <paraphrase>. I will <action>."
- ✓ Builder does not re-surface unchanged conflict after receiving response
- ✓ Builder implements changes requested in response (if applicable) and commits
- ✓ Builder surfaces a NEW conflict (with different ID/message) if the response is ambiguous

### End-to-end test
- ✓ Trigger an UPSTREAM CONFLICT in a gauntlet run (or use existing prd-authoring-aids run)
- ✓ `gauntlet resume <slug> --response "..."` is invoked
- ✓ Response is recorded in manifest
- ✓ Builder proceeds without re-surfacing the original conflict
- ✓ Run either completes or surfaces a new conflict (not the same one)

---

## 6. Implementation phases

### P1: CLI + manifest plumbing
- Add `--response` argument to `gauntlet resume` command
- Validate that a run is parked (refuse if not)
- Record response + metadata in manifest
- Pass response to builder via prompt injection (simple string in prompt)
- **Test:** `gauntlet resume <slug> --response "..."` updates manifest correctly

### P2: Builder resume logic
- Update builder's agent instructions to handle human responses
- Builder acknowledges response and describes its action
- Builder avoids re-surfacing the same conflict unchanged
- Implement conflict_id hash and check for prior responses
- **Test:** Builder receives response, proceeds or surfaces new conflict (not same one)

### P3: Integration test (dogfood)
- Resume the prd-authoring-aids run with `--response "Ratify option 1: ..."`
- Verify P1 implementation is committed and proceeds to review cycle
- Verify manifest audit trail shows the human response + timestamp
- **Stretch:** Test with a new gauntlet run that intentionally triggers a conflict, respond, and verify full cycle

---

## 7. Risk & tradeoffs

### Risk: Arbitrary text is too permissive
- **Mitigation:** Builder is instructed to ask for clarification if response is ambiguous; human can provide a second response

### Risk: Builder interprets response differently than intended
- **Mitigation:** Audit trail in manifest preserves what human said; can be reviewed post-hoc

### Risk: Encourages humans to bypass conflict resolution instead of fixing root cause
- **Mitigation:** Each response is recorded; decisions can be reviewed at gate; FUTURE.md tracks deferred work

### Tradeoff: No validation of response text upfront
- **Pro:** Maximum flexibility; humans are not constrained by pre-defined options
- **Con:** Humans can type nonsense; builder must handle gracefully
- **Chosen:** Flexibility wins; builder will ask for clarification

---

## 8. Success criteria for v1

- ✓ `gauntlet resume <slug> --response "..."` is the standard, documented way to resume from a conflict
- ✓ The prd-authoring-aids run can be un-stuck using this mechanism
- ✓ Manifest audit trail is immutable and auditable (git history preserved)
- ✓ Builder's resume logic no longer re-surfaces unchanged conflicts after receiving a response

---

## Appendix: Example — prd-authoring-aids conflict resolution

**Before (broken flow):**
```
builder → UPSTREAM CONFLICT (P1 implementation contradicts plan)
human → manually edit plan.md
gauntlet resume prd-authoring-aids
builder → re-run, re-surface SAME conflict (no signal that human decided)
run → still parked (stuck)
```

**After (with --response):**
```
builder → UPSTREAM CONFLICT (P1 implementation contradicts plan)
human reads conflict, decides
gauntlet resume prd-authoring-aids --response "Ratify option 1: remove asset_root change from test strategy. Amend plan test-strategy bullet."
orchestrator → records response in manifest, passes to builder
builder → receives response, acknowledges: "Human decided option 1. I will update the test strategy in the plan and proceed."
builder → amends plan, re-evaluates, no longer sees contradiction
builder → completes P1, commits with message noting the upstream-conflict resolution
run → proceeds to review cycle
manifest → shows human response + timestamp for audit
```

---

**Next step:** Scaffold this PRD with `gauntlet new gauntlet-resume-response`, schedule adversarial review, implement P1–P3.

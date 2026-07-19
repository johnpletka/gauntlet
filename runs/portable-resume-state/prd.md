# PRD — Portable, self-contained run resume state

- **Status:** Draft v0.1
- **Author:** John Pletka (with Claude, co-author)
- **Date:** 2026-07-17
- **Working name:** Portable resume state (`portable-resume-state`)
- **Target codebase:** Gauntlet (`gauntlet-spec`) — *not* Right Quote. This PRD is authored in the Right Quote repo for convenience and is meant to be moved into the Gauntlet source repo before `gauntlet run`.
- **Relationship to existing artifacts:** Does **not** amend any approved artifact. Builds on existing machinery: the Claude Code adapter (`gauntlet/adapters/claude_code.py`), the shared adapter failure classifier (`gauntlet/adapters/failure_markers.py`, `gauntlet/adapters/base.py`), the step-execution resume/fallback path (`gauntlet/engine/steptypes.py`, FR-3.3), and the run layout / manifest (`gauntlet/engine/run.py`, `gauntlet/engine/manifest.py`). It refines behavior these define; it does not redefine the pipeline, the manifest contract, or the safety judge.

---

## §1 Overview

### 1.1 Problem statement

A Gauntlet run's **resumable state is not self-contained in the project**. Everything the pipeline treats as durable — the branch commits, `manifest.json`, `prd.md`, `plan.md`, `RUN.md` — travels with git. But the one artifact required to *resume a parked agent step* does not: the **Claude Code conversation**. Gauntlet resumes an interrupted agent step by re-invoking `claude --resume <session_id>` (`ClaudeCodeAdapter._build_argv`, `claude_code.py:165–166`), and the CLI stores that conversation locally at `~/.claude/projects/<cwd-hash>/<session-id>.jsonl` — outside the repo, outside the run directory, keyed by the repo's absolute path on that machine.

The consequence is concrete and was hit in production. A real run (`clerk-auth`) parked mid-`implement` (phase 4) on a provider usage limit, on machine A. On machine B — a fresh clone of the same branch — `gauntlet resume` drove the step, which called `claude --resume b9ae911f-…`. Machine B's `~/.claude` had no such session, so Claude Code wrote `No conversation found with session ID: b9ae911f-…` to **stderr** and produced **empty stdout**, and the step **hard-failed**. The same failure would occur on the *original* machine if `~/.claude` were cleared, or if the repo were moved to a different path (the `cwd-hash` directory would change).

Two distinct defects combine here:

1. **Non-portability (primary).** The resumable conversation is machine-local and path-keyed, so a parked run cannot be resumed anywhere but the exact machine + path that started it.
2. **Opaque, wrong-path failure (secondary).** Gauntlet already has a graceful recovery for a missing resume session — catch `SessionNotFoundError`, fall back to a logged sessionless re-run (`steptypes.py:933–949`, FR-3.3). That path never fired, because the "session not found" signal arrived on *stderr* with empty *stdout*: `_decode_events` ran `json.loads("")` and raised `MalformedOutputError` ("did not return JSON") *before* any failure classification, and the detector `looks_like_session_not_found()` (`failure_markers.py:319–326`) only inspects the parsed JSON `result` text, never stderr or exit code. A recoverable condition was reported as an unrecoverable parse error.

Who this hurts: anyone operating a Gauntlet run across more than one machine (a laptop that sleeps and a workstation that resumes; a run handed off between people; a CI/cloud driver picking up a locally-started run), and anyone whose `~/.claude` is transient (containers, ephemeral CI). Today, a usage-limit park — an *expected, routine* pause on long runs — becomes unrecoverable the moment the resume happens somewhere else.

### 1.2 Solution summary

Make a run's resumable conversation state **either portable as a first-class run artifact, or explicitly and safely non-portable with a correct fallback** — and in all cases make "the resume session is unavailable" a **robust, auditable recovery**, never an opaque crash. Concretely, the work has two halves that can ship independently:

- **Portability (Problem 1):** capture what is needed to continue a parked agent step as part of the run's durable artifacts (or reconstruct it), so a machine that has only *(the branch + the run directory)* — and never ran the step — can resume it. The *mechanism* is deliberately left open (see §4.2 and Open Questions): persist/restore the session file, relocate Claude's session store into the run tree, or drop conversation-continuity in favor of a guaranteed non-lossy re-run from committed state. This PRD requires the *outcome* and defines a P1 feasibility spike to choose the mechanism on evidence.
- **Recovery robustness (Problem 2):** detect a missing/unresumable session regardless of how the CLI reports it (JSON `result`, stderr, exit code), route it to the existing FR-3.3 recovery, and keep the recovery fail-closed and audited.

### 1.3 The assumption this validates

The riskiest belief: **the state needed to continue a parked agent step can be made portable across machines without either (a) faithfully re-hydrating a Claude Code session that was born under a different working-directory path, or (b) committing conversation transcripts — which may contain secrets — into the repo.** If neither session re-hydration nor a secret-safe transport works, then true "resume the same conversation elsewhere" is infeasible and the correct outcome is a *guaranteed non-lossy sessionless re-run from committed/checkpointed state*. Phase 1 is a feasibility spike whose entire job is to prove or disprove this, before any mechanism is built on top of it. (Guiding principle: **data over inference** — we do not design the mechanism until the spike settles which mechanism is even possible.)

---

## §2 Goals and Non-Goals

### 2.1 Goals

| ID | Outcome | Need it serves |
|----|---------|----------------|
| G1 | A run parked on one machine can be resumed on another that has only the branch + run directory. | Cross-machine / cross-clone operation; the core pain. |
| G2 | Resuming does not depend on the repo living at the same absolute path it started at. | Path-independence (moved clones, containers, differing home dirs). |
| G3 | An unavailable/unresumable resume session produces a defined, audited recovery (restore, non-lossy re-run, or clean halt) — never an opaque "did not return JSON" hard-failure. | Turns a routine usage-limit park into a recoverable event, not a dead run. |
| G4 | "Session not found" is detected regardless of whether the CLI signals it via stdout JSON, stderr, or exit code. | Correctness of the existing FR-3.3 fallback. |
| G5 | No secret material is newly exposed by whatever transport carries resume state. | Fail-closed secrets posture; must not trade portability for a leak. |

### 2.2 Non-Goals (v1)

- **Portability of the *other* machine-local run files** — the gitignored `active-run.txt` pointer and the per-run `pipeline.yaml` snapshot (which also failed to travel in the incident). Explicitly out of scope for this PRD; they are separately reconstructable (the pointer is the run-id; the pipeline snapshot is hash-checked against the repo pipeline) and warrant their own decision. *(Recorded so it is not silently absorbed.)*
- **Concurrent multi-machine driving** of the same run (two live drivers). Out of scope; the worktree lock already guards single-drive, and this PRD does not introduce distributed locking.
- **A general session-sync service / daemon.** No background service, no network sync. Whatever we build is local, invoked by existing verbs.
- **Retroactive recovery of the `clerk-auth` run's phase-4 conversation.** That specific lost session is unrecoverable; this PRD prevents the *class* of failure, it does not resurrect that instance.
- **Changing the safety judge, permission model, or the `--no-judge` prohibition.** Untouched.
- **Adapters other than Claude Code, beyond a parity note.** The Codex adapter has an analogous `SessionNotFoundError` path (`adapters/codex.py:196`); whether the Problem-2 detection fix must apply to it too is an Open Question (OQ-5), not a committed v1 goal.

---

## §3 Users and Personas

Reader-roles who touch this:

- **The operator** (human or agent running `gauntlet resume`) — the direct beneficiary; wants a parked run to resume wherever they are, or to get a clear, actionable diagnostic when it can't.
- **The pipeline engine** (`steptypes._invoke` and the resume path in `run.py`) — the code that will call the new capture/restore or the improved detection.
- **The Gauntlet maintainer** — extends the adapter and engine; needs the contract (what is captured, where, when, and the fail-closed rules) to be unambiguous and testable.

---

## §4 System Architecture

### 4.1 Components

Real modules this will add or touch (Gauntlet repo paths):

- `gauntlet/adapters/claude_code.py` *(touch)* — `_build_argv` (adds `--resume`), `_parse`, `_decode_events`. Home of Problem 2: `_decode_events` must not mask a session-not-found reported via stderr/empty-stdout. Possibly the capture/restore hook for Problem 1 (knows the `session_id` and the CLI contract).
- `gauntlet/adapters/failure_markers.py` *(touch)* — `looks_like_session_not_found()` / `_SESSION_NOT_FOUND_RE` / `classify_claude_failure()`. Must classify from stderr and/or exit code, not only the JSON `result` text.
- `gauntlet/adapters/base.py` *(touch, likely)* — `SessionNotFoundError`, `ProcessOutput` (already carries `.stderr`, `.exit_code`), `AgentResult`. May grow a typed "session unavailable" classification surfaced before strict decode.
- `gauntlet/engine/steptypes.py` *(touch)* — the resume/fallback orchestration (`_invoke`, the `except SessionNotFoundError` at ~933 and ~991). Where a restore attempt would be wired in ahead of the sessionless re-run, and where non-lossy vs lossy re-run is decided.
- `gauntlet/engine/run.py` *(touch, if capture/restore chosen)* — `RunLayout` (run/step dirs, `active_run_dir`), the resume driver, checkpoint plumbing. Owns where a session artifact would live under `run_dir/steps/<step>/`.
- `gauntlet/engine/manifest.py` *(touch, maybe)* — `StepRecord` already has `session_id`, `checkpoints`, `base_sha`, `resumed_from_checkpoint`; may add a pointer/flag describing captured resume state.
- **New (if capture/restore chosen):** a small session-artifact module (e.g. `gauntlet/engine/session_store.py`) that captures/restores the Claude Code session for a step, isolating the `~/.claude/projects/<cwd-hash>/` coupling in one place.
- **Test/fixture support:** `failure_markers.py` contract-test hooks (`classify_captured`) — a captured fixture of the real failure (empty stdout + stderr "No conversation found with session ID: …" + nonzero exit) is required by FR-4/FR-6.

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Portability mechanism | **Deferred to P1 feasibility spike** (Open Question OQ-1). Candidates: (A) persist the session `.jsonl` into `run_dir` and restore it into local `~/.claude` before `--resume`; (B) relocate Claude's session store into the run tree via `CLAUDE_CONFIG_DIR`; (C) drop conversation continuity and guarantee a non-lossy re-run from committed state. | Each has an unverified feasibility risk (A: does `--resume <id>` resolve a session captured under a *different* `cwd-hash`? B: same cross-hash question for a relocated store; C: is a fresh session's re-run acceptable/complete?). Choosing before evidence would launder an unmade decision — **data over inference**. |
| Secrets vs. transport (load-bearing) | Portability transport must be **secret-safe**; committing raw conversation transcripts to git is a candidate *violation*, not a default. | Session `.jsonl` transcripts can contain secrets pasted or read during the run. "Make it travel with the repo via git" directly collides with the never-commit-secrets rule. This tension may *force* mechanism (C), or force (A)/(B) to use a non-git side channel or scrubbing. Resolved in P1 (OQ-2). |
| Problem-2 detection point | Detect "session unavailable" **before** strict JSON decode, from `ProcessOutput.stderr` and/or non-zero `exit_code`, in addition to the existing parsed-`result` path. | The current order (`_decode_events` first) makes an empty-stdout + stderr failure unclassifiable. Fixing the *order/inputs* of detection is the minimal correct change and preserves the existing FR-3.3 wiring. **Fail closed**: an *unrecognized* empty-stdout failure still errors — only a *matched* session-not-found signal routes to recovery. |
| Fallback losslessness | Open Question OQ-3: keep the current lossy "full re-run with no session" (`steptypes.py:949`) or require the re-run continue from committed/checkpointed state. | The existing fallback discards in-progress phase work. Whether that is acceptable, or the re-run must be made non-lossy, changes both scope and success metrics. |
| Fail-closed default everywhere | On any capture/restore failure, corrupt artifact, or ambiguous signal: fall back to the *defined* recovery or halt with a clear diagnostic. Never silently continue a broken/partial conversation. | A silently-wrong resume corrupts the step's work invisibly — worse than a clean halt. **Determinism over cleverness; fail closed.** |
| Scope of the fix to adapters | Problem-2 detection fix is specified for Claude Code; Codex parity is OQ-5. | Avoids silent scope creep into an adapter we did not diagnose. |

---

## §5 Functional Requirements

**Problem 2 — robust detection & recovery (independently shippable):**

- **FR-1** — A session-unavailable failure MUST be classified as such when the CLI reports it via stderr with empty/blank stdout and/or a non-zero exit code, not only via a parsed JSON `result` event.
  **Acceptance:** Given a captured `ProcessOutput` fixture `{stdout: "", stderr: "No conversation found with session ID: <id>\n", exit_code: <nonzero>}`, the adapter raises `SessionNotFoundError` (carrying the session id and stderr excerpt), *not* `MalformedOutputError`. A unit/contract test asserts the exception type on that exact fixture.

- **FR-2** — Session-unavailable detection MUST NOT reclassify a genuinely malformed/empty output that carries no session-not-found signal.
  **Acceptance:** A fixture `{stdout: "", stderr: "segfault\n", exit_code: 139}` still raises `MalformedOutputError` (fail-closed). Test asserts the negative case.

- **FR-3** — On the usage-limit / quota resume path, a detected session-unavailable MUST trigger the FR-3.3 recovery and the step MUST reach a normal terminal state (done, or a legitimate park), not `failed`, when the recovery itself succeeds.
  **Acceptance:** An integration test that resumes a usage-limit-parked step whose stored session is absent completes the step (status `done`) via the recovery path; the manifest step status is never left `failed` solely because the session was gone.

- **FR-4** — The recovery MUST be auditable: the session-loss and the chosen recovery action are recorded in the step's evidence directory.
  **Acceptance:** After the FR-3 scenario, the step dir contains a recovery log (e.g. `session-expired.txt` or successor) naming the missing session id and stating the action taken (restored / re-ran sessionless / halted).

- **FR-5** — Off the quota-resume path, a session-unavailable MUST remain fail-closed unless a portability restore (if implemented) succeeds.
  **Acceptance:** A non-quota resume with a missing session and no successful restore surfaces an error and does not silently start a fresh conversation; test asserts the error (or, if restore succeeded, asserts the restore path ran).

**Problem 1 — portability (independently shippable, gated on P1 spike):**

- **FR-6** — The resume state required to continue a parked agent step MUST be capturable such that a machine with only *(the run's branch + `run_dir`)* and an empty `~/.claude` can continue the step. *(Mechanism per OQ-1; this FR is outcome-level.)*
  **Acceptance:** North-star test — a step is parked on host/config A; the branch and `run_dir` are transported to environment B (fresh `~/.claude`, **different absolute repo path**); `gauntlet resume` continues and completes the parked step. Passes for whichever mechanism P1 selects.

- **FR-7** — Portability MUST NOT depend on the repo residing at the same absolute path across environments.
  **Acceptance:** The FR-6 test uses a deliberately different checkout path in environment B and still resumes.

- **FR-8** — Capture/restore MUST be fail-closed: if resume state cannot be captured or restored faithfully, the system performs the defined fallback (FR-3 recovery) or halts with a clear diagnostic — never continues a partially-restored conversation.
  **Acceptance:** With a corrupted/truncated captured artifact, resume does not proceed on the broken session; test asserts fallback-or-halt with a diagnostic naming the cause.

- **FR-9** — Whatever transport carries resume state MUST NOT newly expose secrets. If the artifact can contain conversation content, it MUST NOT be committed to the repo in a form that violates the no-secrets-in-git rule (scrubbed, gitignored + out-of-band, or the mechanism avoids transcripts entirely).
  **Acceptance:** A test/CI guard asserts that no session/transcript artifact introduced by this feature is tracked by git in raw form; the secrets posture is documented in §7. *(If P1 selects a git-tracked transport, this FR forces a scrub/redaction step with its own test.)*

- **FR-10** — Capture MUST occur at the boundaries where a step can later be resumed (at minimum: park, and any checkpoint the engine records), so the captured state matches the manifest's view of the step.
  **Acceptance:** After a step parks, the captured resume-state artifact exists and corresponds to the session id / checkpoint recorded in the manifest `StepRecord`; test asserts presence + correspondence.

---

## §6 Data & Schemas (normative excerpts)

- **Claude Code session file (input, existing, not owned by us):** `~/.claude/projects/<cwd-hash>/<session-id>.jsonl`, JSONL of conversation events. `<cwd-hash>` is derived from the working directory path (dashified in the observed layout, e.g. `-Users-johnpletka-projects-right-quote`). The exact hashing and cross-hash `--resume` resolution are **P1 findings**, not assumed here.
- **Manifest `StepRecord` (existing, may extend):** already carries `session_id`, `checkpoints`, `base_sha`, `resumed_from_checkpoint`. Any new pointer to captured resume state is additive and MUST be documented as a schema excerpt if added.
- **Captured resume-state artifact (new, shape TBD in P1):** if a file is persisted under `run_dir/steps/<step>/`, its name, format, and whether it is git-tracked are normative outputs of P1 and MUST be specified before P2 builds on them.
- **Relevant `claude` CLI flags (existing):** `--resume [id]`, `--session-id <uuid>` (caller-chosen id — enables deterministic naming), `--fork-session` (new id on resume), `--no-session-persistence`. Whether any of these (esp. `--session-id` + a relocated store) satisfies FR-6/FR-7 is a P1 finding.

---

## §7 Security & Privacy

- **Fail-closed on every external call.** `claude` timeout, parse error, unexpected exit, empty output: the default is halt/error unless the output *positively matches* a known-recoverable signal (session-not-found → recovery). FR-2 encodes the negative case so the new detection cannot become a "treat unknown failures as recoverable" hole.
- **Secrets.** Conversation transcripts may contain secret values surfaced during a run. This feature MUST NOT create a new path that commits such content to git (FR-9). This is the sharpest constraint on the portability mechanism and may rule out git-as-transport for raw sessions. Secret *names* only, never values, per the repo rule — applies to any logging this feature adds (FR-4 logs must not echo secret content).
- **Judge / policy.** Restoring a file into `~/.claude` (mechanism A) is a filesystem write outside the repo/worktree; confirm no safety-judge policy forbids it and that it does not run under a bypassed judge. No `--no-judge`, ever. *(Recorded as OQ-4.)*
- **Determinism.** Capture/restore must be deterministic and idempotent — resuming twice from the same artifact yields the same starting state.

---

## §8 Implementation Plan (phased, assumption-validating)

Ordered riskiest-assumption-first. No phase depends on a later phase.

| Phase | Deliverable | Assumption it validates |
|-------|-------------|-------------------------|
| **P1 — Feasibility spike (portability)** | A written findings note + throwaway probe: does `claude --resume <id>` (and/or `--session-id` / `CLAUDE_CONFIG_DIR`) resolve a session captured under a *different* `cwd-hash`/host? Is any transport secret-safe? Recommendation among mechanisms A/B/C. | §1.3 riskiest assumption — whether cross-machine session re-hydration is possible at all, and secret-safely. Resolves OQ-1, OQ-2. **Gate:** P2/P4 mechanism is chosen here on evidence. Ends in a committed findings doc + a decision. |
| **P2 — Problem-2 detection fix** | `_decode_events`/`_parse`/`failure_markers` detect session-unavailable from stderr + exit code before strict decode; route to `SessionNotFoundError`. | FR-1/FR-2 — that the signal is reliably classifiable and the negative case stays fail-closed. Independent of P1's outcome (pure robustness). Ends: unit + contract tests green, commit. |
| **P3 — Recovery correctness & audit** | Wire the FR-3.3 recovery so a detected session-loss on the quota path recovers to a terminal state, audited (FR-3/FR-4/FR-5). Decide lossy vs non-lossy re-run (OQ-3) — if "keep lossy," document it; if "non-lossy," implement continue-from-committed. | FR-3/FR-4/FR-5 — that the existing fallback, now actually reachable, produces a correct, audited outcome. Ends: integration test resuming a missing-session usage-limit park → `done`, commit. |
| **P4 — Portability capture/restore** | Implement the P1-selected mechanism: capture resume state at park/checkpoint into a portable, secret-safe form; restore-or-fallback on resume (FR-6–FR-10). | FR-6/FR-7/FR-8/FR-9/FR-10 — that the chosen mechanism actually resumes cross-machine, path-independently, fail-closed, without leaking secrets. Ends: north-star cross-machine test green (differing path, empty `~/.claude`), commit. |

*Rationale for ordering:* P1 kills the central unknown before any build. P2+P3 deliver real value (a routine usage-limit park stops being a dead run) **even if P1 concludes true portability is infeasible** — because the correct fallback then becomes the answer. P4 is gated on P1 and builds only what the spike proved possible.

---

## §9 Success Metrics

- **M1 (portability):** The FR-6 north-star scenario — park on env A, resume on env B (fresh `~/.claude`, different path) — succeeds in an automated test. Target: 100% for the selected mechanism; **or**, if P1 proves re-hydration infeasible, M1 is redefined to "resume on env B completes the step via a non-lossy re-run" and that test passes.
- **M2 (no opaque failures):** Zero occurrences of a session-unavailable condition surfacing as `MalformedOutputError`/"did not return JSON". Measured by the FR-1 contract test on the captured real-world fixture (empty stdout + stderr "No conversation found").
- **M3 (recovery reachability):** A usage-limit-parked step whose session is gone reaches a terminal `done`/park state, never `failed`-due-to-missing-session, in the FR-3 integration test. Target: 100%.
- **M4 (no new secret exposure):** The FR-9 CI guard reports zero raw session/transcript artifacts tracked by git. Target: 0.
- **M5 (fail-closed integrity):** The FR-2 and FR-8 negative tests pass — unknown failures and corrupt artifacts never route to a silent continue. Target: 100%.

---

## §10 Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Cross-hash `--resume` is impossible (session can't be re-hydrated elsewhere). | P1 spike surfaces this *before* building; if true, pivot to mechanism C (non-lossy re-run) — P2/P3 already deliver the recoverable-fallback value. (FR-6 acceptance allows either mechanism.) |
| Session transcripts contain secrets → git transport would leak. | FR-9 + §7 make secret-safety a hard requirement; P1 evaluates non-git transport / scrubbing / avoiding transcripts. |
| Claude Code session format or storage path changes across CLI versions. | Isolate the coupling in one module (`session_store.py`); pin-test against the doctor-pinned CLI version; the Problem-2 detection (P2) is format-agnostic (stderr/exit-code based) and degrades safely. |
| Detection change accidentally swallows real malformed output as "recoverable." | FR-2 negative test is mandatory; recovery fires only on a positive session-not-found match, default stays fail-closed. |
| Making the re-run non-lossy (OQ-3) balloons scope. | Kept as an explicit Open Question; P3 can ship the documented lossy behavior and defer non-lossy to a follow-up if the decision goes that way. |
| Fix diverges Claude vs Codex adapters. | OQ-5 decides parity explicitly; Non-Goal fences it if deferred. |

---

## §11 Open Questions

- **OQ-1 (mechanism):** Which portability mechanism — (A) persist/restore session file, (B) relocate store via `CLAUDE_CONFIG_DIR`, (C) non-lossy sessionless re-run? **Resolved by P1 on evidence.** Plan (P4) depends on this — must be resolved before P4.
- **OQ-2 (secret transport):** Can any transport carry resume state secret-safely, or does the secrets constraint force mechanism C? **Resolved by P1.** Plan-blocking.
- **OQ-3 (losslessness):** Must the sessionless re-run continue from committed/checkpointed state (non-lossy), or is the current full re-run (lossy) acceptable for v1? A judgment call; cheap to defer to P3. Affects M1/M3 wording.
- **OQ-4 (judge/policy):** Does restoring a file into `~/.claude` (mechanism A) require any safety-judge policy allowance? Cheap to check in P1.
- **OQ-5 (adapter parity):** Does the Problem-2 detection fix (P2) also apply to the Codex adapter (`adapters/codex.py`)? Judgment call; record and decide at P2, do not design around it now.
- **OQ-6 (`active-run.txt` / `pipeline.yaml`):** These other machine-local files also failed to travel in the incident. Confirmed a **Non-Goal** here — but is a sibling PRD wanted? (Decision, not a blocker for this PRD.)

---

### Handoff notes

- **Riskiest assumption:** cross-machine re-hydration of a Claude Code session (or a secret-safe substitute) is possible — §1.3, attacked first by **P1**.
- **Still open:** OQ-1 and OQ-2 are plan-blocking and are the explicit output of P1; OQ-3/OQ-4/OQ-5/OQ-6 are deferrable.
- **This document is Draft v0.1** and has *not* been through adversarial review. When you move it into the Gauntlet repo, the next step is `gauntlet run <slug>` there — which begins with adversarial review, not implementation. You ratify; the pipeline executes.

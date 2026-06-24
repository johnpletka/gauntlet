I have what I need. Here is the implementation plan.

---

# Implementation Plan: Resume-with-response — human decision mechanism for upstream conflicts

**Feature slug:** `gauntlet-resume-response`
**Plan status:** Draft for adversarial review
**PRD:** `prd.md` (approved), FR-1 … FR-10
**Author:** John Pletka (builder agent)

---

## 0. Strategy: how the phases are ordered

The PRD groups the work into three coarse buckets (CLI/manifest, builder logic, integration). This plan decomposes those into **six sequential phases ordered to kill the riskiest assumptions first**, where "riskiest" means *most likely to invalidate the design or regress existing behavior*:

1. **Can we durably distinguish a conflict park from every other park without breaking the existing resume/approve/reject flows?** (FR-2.1, FR-1.1) — if `parked_reason` discrimination is unreliable, the whole scoping story collapses and existing flows regress. This is the foundation everything else builds on, so it goes **first** (P1).
2. **Can the response record survive `kill -9` as a single idempotent state transition, with the orchestrator owning persistence and commit, and without coupling response cycles to the failure-retry budget?** (FR-2, FR-2.2, FR-6, FR-7.1) — this is the determinism/fail-closed core, the hardest thing to get right. It depends only on P1's schema, so it goes early (P3, after the small pure identity unit in P2).
3. **Can we feed the response history into the prompt through the existing artifact path without mutating an approved pipeline definition or persisted inputs?** (FR-4, FR-4.1) — P4.
4. **Can the builder classify deterministically and emit a machine-checkable disposition we can test without a live model?** (FR-3.0, FR-3, FR-5, FR-10) — P5.
5. **Does the whole loop un-stick a real parked run end to end?** (§8 e2e, dogfood) — P6.

Every phase ends with `uv run pytest` green and exactly one commit (FR-9.2). No phase depends on work a later phase delivers (FR-10.3). Each phase below names its **assumption**, **deliverables**, **test strategy**, **exit criteria**, and **explicit deferrals**.

Key code anchors this plan binds to (verified against the current tree):

- `RunManager.resume()` — `run.py:768`; `approve()` `:824`; `reject()` `:850`; clock passed through to `_drive`.
- Orchestrator re-executes any non-`DONE`/`SKIPPED` step on `drive()`; a PARKED agent_task is re-executed via `_execute` (`orchestrator.py:151-177`, `:208-252`), and `resuming` is only true for `RUNNING`/`INTERRUPTED` (`:211`), so a conflict park re-runs the handler fresh.
- `max_retries` is governed by the **in-memory `retries` dict** in `_run_steps` and only advances on `status == FAILED` via `on_fail` (`orchestrator.py:170-175`); PARKED returns at `:176` and never touches it. `StepRecord.attempts` (`manifest.py:69`) currently increments on **every** `_execute` (`orchestrator.py:224`) — FR-6 requires decoupling this (see P3).
- Atomic manifest write already exists: `Manifest.write_atomic` (`manifest.py:130-154`, tempfile + fsync + `os.replace`). **But `_persist` (`orchestrator.py:470-474`) only writes the manifest atomically and regenerates `RUN.md`; it creates no git commit** — ordinary runs reach git history only through a separate `commit` step (`handle_commit`, `steptypes.py:244`, e.g. `phase-commit`). FR-2.2 requires *both* the `pending` and the `consumed` state of a response to be reachable in git history, which a single later phase commit cannot preserve (it would collapse the two intermediate states into one). P3 therefore adds an **orchestrator-owned manifest-checkpoint commit** built on `gitops.commit_paths` (`gitops.py:223`) + a fixed engine `Identity` (`gitops.py:188`).
- Prompt rendering appends `--- input artifact: {name} ---` blocks by iterating `step.inputs` against `ctx.artifacts` / `ctx.artifact_root` (`steptypes.py:217-233`). Conflict parks are produced by the `halt_on` completion signal (`steptypes.py:182-187`); budget/timeout halts go through a different path (`_apply_budget_guard`, `orchestrator.py:304-332`).
- Schemas are Draft-07, `additionalProperties:false`, validated like `schemas/findings.json`. Identity fallback `agent@gauntlet.local` lives at `config.py:187-194` and must **not** be used for the audit field (FR-9).

---

## P1 — Manifest schema extensions + conflict-park discriminator

**Assumption validated:** an `UPSTREAM CONFLICT` halt is durably and unambiguously distinguishable from every other park (human_gate, budget/timeout halt, generic agent re-run), and the new manifest fields round-trip through `write_atomic`/`load` **without breaking existing manifests** (back-compat). If this fails, FR-1.1 scoping and the entire `--response` gate are unsound.

**Deliverables:**

- `manifest.py`: add to `StepRecord`
  - `parked_reason: str | None = None` (single enum-valued field; FR-2.1). Introduce a module constant/`Literal` for the only v1 value, `"upstream_conflict"`.
  - `human_responses: list[HumanResponse] = Field(default_factory=list)` (append-only; FR-2).
  - new `HumanResponse` pydantic model with fields exactly per FR-2: `response_id: str`, `response_text: str`, `timestamp: str`, `user: str`, `response_attempt: int`, `state: Literal["pending","consumed"]`.
- `orchestrator.py`: `parked_reason` is **current-state, not a latch** (FR-2.1
  lifecycle). `_finalize` (`:334-376`) **defaults `rec.parked_reason` to unset
  (`None`) on every terminal outcome it records** — `done`, `failed`, `halted`,
  `interrupted`, and any park — and re-sets it to `"upstream_conflict"` only when
  the just-finished execution carried the conflict signal on its `StepResult`.
  Because a `StepRecord` is reused across re-executions (`_execute` `:210-216`),
  this *clear-then-maybe-set on each finalize* is what guarantees a conflict park
  that is later resumed to a `done` / `failed` / non-conflict-park outcome ends
  with `parked_reason` unset — so a stale `upstream_conflict` can never cause a
  later generic park to be misclassified as a conflict park (which would wrongly
  require `--response`). This is FR-2.1's current-state acceptance.
- The conflict discriminator is set **specifically for the `UPSTREAM CONFLICT`
  halt, not for any `halt_on` completion signal.** Introduce the canonical
  marker as a module constant and have `_completion_signal` / `handle_agent_task`
  (`steptypes.py:151-157,175-194`) carry a typed `parked_reason="upstream_conflict"`
  on the returned `StepResult` **only** when the matched marker is that constant;
  a step configured with a *different* `halt_on` marker parks with
  `parked_reason` unset. The orchestrator reads that field off `StepResult` in
  `_finalize` and never re-parses marker text. Budget/timeout halts
  (`_apply_budget_guard`) and human_gate parks carry no `parked_reason` and so
  leave it unset.
- No CLI/recording/injection yet — those are explicit deferrals below.

**Test strategy (`tests/unit/test_human_response_manifest.py`, extend `test_manifest.py`):**

- Round-trip a `Manifest` containing a `StepRecord` with `parked_reason` and two `human_responses`; assert byte-stable JSON and full field fidelity through `write_atomic` → `load`.
- **Back-compat:** load a fixture manifest JSON that lacks both new fields; assert it loads and the fields default to `None` / `[]`.
- Drive a fixture pipeline (extend `test_orchestrator.py` harness, `FakeAdapter`) where the agent emits the `UPSTREAM CONFLICT` marker → assert `status == parked` **and** `parked_reason == "upstream_conflict"`.
- Drive a human_gate park and a budget/timeout halt → assert `parked_reason` stays unset in both.
- **Non-conflict `halt_on`:** a step configured with a *different* `halt_on`
  marker that signals → parks with `parked_reason` **unset** (only the canonical
  `UPSTREAM CONFLICT` marker sets it).
- **Lifecycle transitions (FR-2.1):** a step that parks on a conflict
  (`parked_reason == "upstream_conflict"`), then on its next finalize reaches
  (a) `done`, (b) `failed`, (c) a non-conflict park — assert `parked_reason` is
  **unset** at each of those outcomes, proving the field tracks current state and
  is not a latch.

**Exit criteria:** `uv run pytest` green; one commit `P1: Add conflict-park discriminator + human_responses schema`. Commit body cites FR-2, FR-2.1; notes deferrals.

**Deferrals:** CLI flag, identity, response recording/idempotency, prompt injection, builder logic — P2–P5.

---

## P2 — Operator identity resolution (fail-closed)

**Assumption validated:** the required `user` audit field can be resolved with a deterministic, **fail-closed** precedence (FR-9) before it is wired into the append path — so a malformed/empty environment can never produce a manifest entry with a blank `user`. Isolated as its own phase because it is pure, has no dependencies, and is a precondition for P3's append (the FR-2 append must not run if identity is unresolved).

**Deliverables:**

- A `resolve_operator_identity()` helper (in `run.py` or a small `engine/identity.py`):
  - Precedence: `GAUNTLET_USER_EMAIL` wins **only if** set and non-empty after trimming; else `git config user.email`.
  - Normalization: trim; whitespace-only is treated as unset (an exported-but-empty env var does not shadow git config).
  - Fail closed: if neither yields a non-empty value (env blank **and** git config missing or `git config` non-zero exit), raise a typed error carrying the exact FR-9 message; **append nothing**.
  - Validation: non-empty after trim only; **no** RFC-5322 check (deferred §7). Malformed-but-nonblank recorded verbatim.
  - Never calls `config.identity()` (would yield `…@gauntlet.local`).

**Test strategy (`tests/unit/test_operator_identity.py`):**

- env set with surrounding whitespace → returns trimmed value.
- env empty/whitespace-only + valid git config → returns git-config value.
- both unset/blank (monkeypatch env; stub `git config` to non-zero) → raises with the exact FR-9 message.
- git-config invocation fails (non-zero exit) with blank env → raises (not silent empty).

**Exit criteria:** `uv run pytest` green; one commit `P2: Resolve operator identity fail-closed (FR-9)`.

**Deferrals:** wiring into the append path — P3.

---

## P3 — CLI `--response` + idempotent recording + commit ownership + retry-budget decoupling

**Assumption validated (the determinism core):** a `gauntlet resume --response` is a **single idempotent state transition** — append-pending → re-execute → mark-consumed — that (a) the orchestrator persists atomically and commits via a new **orchestrator-owned manifest-checkpoint commit** (defined below — `_persist` does not commit today), (b) survives `kill -9` at any point in the append→launch→consume window — including after the write-ahead `RUNNING` checkpoint and after terminal finalize but before the consume persist — without duplicating an entry or double-counting, and (c) **never** advances the failure-retry budget on a conflict re-run (FR-6). This is the highest-risk plumbing; it is validated with `FakeAdapter`s before any real builder prompt exists.

**Deliverables:**

- `cli.py`: add `--response: str = typer.Option(None)` to `resume` (`:214-220`); pass through to `RunManager.resume`.
- `run.py` `RunManager.resume()` (`:768-821`), guard order (FR-1, FR-1.1, FR-8) — all errors use the exact PRD strings:
  - run not parked → error.
  - parked step not an `agent_task` (e.g. human_gate) → error directing to `approve`/`reject`.
  - `agent_task` with `parked_reason == "upstream_conflict"` and **no** `--response` → error requiring `--response`.
  - `agent_task` parked **without** `upstream_conflict` → existing response-less re-run unchanged; `--response` accepted+recorded but not required.
- **Orchestrator-owned response checkpoint commit (FR-2.2):** a new
  `Orchestrator._commit_manifest_checkpoint(message)` helper that, after
  `write_atomic`, stages **only the run-bookkeeping paths** (`manifest.json` and
  the regenerated `RUN.md` under the run dir) via `gitops.commit_paths`
  (`gitops.py:223`) under a fixed engine identity (`Gauntlet Engine
  <engine@gauntlet.local>` — the response's operator `user` is recorded *in* the
  manifest entry per FR-9; it is **not** the commit author). The message names
  the transition and `response_id`, e.g. `gauntlet: response <response_id>
  pending` / `… consumed`.
  - **Path selection / clean-worktree behavior:** the checkpoint commits the
    manifest and `RUN.md` only — never the implementation diff — so it composes
    with phase-commit discipline and the central clean-worktree invariant: a
    genuine `UPSTREAM CONFLICT` halt leaves the worktree clean, and a response
    checkpoint touches only run bookkeeping, so it can never smuggle agent edits
    into history. The helper uses path-scoped staging (not `commit -a`); if a
    non-bookkeeping path is unexpectedly dirty at checkpoint time, it is simply
    not staged (the dirty-base case is handled by the crash-recovery rules below,
    not by this commit).
  - **Crash reconciliation (commit lands before manifest bookkeeping, and vice
    versa):** the on-disk manifest is `write_atomic`-d *before* the commit, so
    the authoritative `state` is always the committed/on-disk manifest. Die after
    `write_atomic` but before the commit → the next resume loads the on-disk
    manifest (already showing the new `state`) and the next checkpoint commit
    captures it; die after the commit → the state is already in git. This reuses
    the same "branch tip slightly ahead of the manifest's last recorded commit is
    fine" reconciliation `resume()` already encodes (`run.py:806-821`) rather than
    inventing a second rule.
  - **Interaction with phase commit:** response checkpoints are independent of the
    `commit` step that later drafts the `PN:` phase commit; they record no
    `CommitRecord` phase and never trigger the message-agent drafting path, so
    phase-commit discipline is unaffected.
- **Append protocol (FR-2, FR-2.2, FR-7.1):**
  - If latest `human_responses` entry is `state=="pending"` (crash recovery): do **not** append; an identical `--response` (or none) re-launches the existing pending entry; a **different** `--response` errors with the exact FR-7.1 message.
  - Otherwise resolve identity (P2), assign `response_id = "<step_id>-resp-<ordinal>"`, set `response_attempt` = 1-based count, `timestamp = clock()` (injected `RunManager.clock`), `state="pending"`; append; `write_atomic`; then `_commit_manifest_checkpoint` (above) **before** the agent launches.
- **Re-execution + consume:** drive re-executes the parked agent_task (existing `_execute` path; `resuming` stays false for a clean PARKED re-entry). On terminal outcome, transition the latest pending entry to `state="consumed"`, `write_atomic`, then `_commit_manifest_checkpoint`. The orchestrator remains the sole committer; the builder gets no direct-write path.
- **Cross-state crash recovery (FR-7.1, the full append→consume window):** the
  pending→consumed transition keys on the **`state` field, independent of
  `StepRecord.status`**. `_execute` flips the record `PARKED → RUNNING` and
  write-aheads (`:225,234`) *before* invoking the adapter (`:238`), so a `kill -9`
  can leave a pending response attached to a `RUNNING` (or, after
  `_resume_disposition`, `INTERRUPTED`) record — not only a `PARKED` one. The
  recovery check in `RunManager.resume` (latest entry `pending` → re-launch the
  existing entry, do **not** append, reuse its `response_id`) therefore runs for
  `PARKED`, `RUNNING`, and `INTERRUPTED` alike, so a crash anywhere in the
  window re-enters with the same single pending entry — no duplicate, no
  double attempt-count.
  - **Dirty-worktree handling:** a genuine `UPSTREAM CONFLICT` halt leaves the
    worktree clean (the agent halted instead of writing), so the normal conflict
    re-launch is a clean re-entry (`_resume_disposition` returns `None` on a clean
    base, `:278-279`). If a crash left the record `RUNNING`/`INTERRUPTED` with a
    **dirty** base, the existing `interrupted_step` policy
    (`park | reset_to_base`, `_resume_disposition` `:254-302`) governs the
    worktree exactly as for any other interrupted agent write: `reset_to_base`
    snapshots a backup ref then re-launches the still-`pending` response cleanly;
    `park` leaves the step `INTERRUPTED` with the response still `pending` for a
    human to reconcile, and a later `gauntlet resume` (recovery path, **no** new
    `--response`) re-enters once reconciled. The pending response is never lost or
    double-consumed because its `state` is the single source of truth.
  - **Attempt accounting in these states:** consistent with the FR-6 relocation
    below — a `RUNNING`/`INTERRUPTED` re-entry that ultimately proceeds or
    re-parks does **not** advance `StepRecord.attempts`; only a `FAILED` outcome
    does, once.
- **Retry-budget decoupling (FR-6):** FR-6 redefines `StepRecord.attempts` as the
  **failure-retry counter — it increments only when a run ends in failure**, never
  on success, conflict park, halt, interruption, or human-driven response
  continuation. The fix is to **relocate** the `rec.attempts += 1` that today runs
  unconditionally at the top of `_execute` (`orchestrator.py:224`) onto the
  terminal/failure path: increment **exactly once, in `_finalize`, only when
  `result.status == FAILED`**. The seam is therefore **outcome-driven, not keyed on
  whether a response is pending** — which is what makes a pending-response
  invocation that then *fails* still record exactly one attempt (the rejected
  "skip when pending" seam would have recorded *zero* for that genuine failure).
  Exact behavior for every outcome:
  - `DONE` (ordinary success, incl. a response resume that proceeds) → **no** increment.
  - `PARKED` (a conflict park or any other park) → **no** increment, so arbitrarily
    many `--response`/conflict cycles never advance the counter.
  - `HALTED` (budget/timeout checkpoint) and `INTERRUPTED` (mid-edit park) → **no**
    increment — recoverable checkpoints, not failures.
  - `FAILED` (genuine agent/handler error, including a response resume that
    genuinely fails) → increment **once**.
  - **Persistence across restarts:** the increment is immediately followed by the
    existing write-ahead `_persist` (`:251`), so the failure count is durable before
    control returns; a crash after the `FAILED` finalize re-runs the step and
    re-reaches the same `FAILED` terminal state, incrementing once — never twice
    for one logical failure.
  - The in-memory `on_fail`/`retries` dict (`:149,170-175`) already triggers only on
    `FAILED`, so conflict re-parks never consumed `max_retries` regardless; this
    change aligns the *persisted* `StepRecord.attempts` audit counter with that same
    failure-only semantics. **Compatibility:** the only suite assertion on
    `attempts` is `test_orchestrator.py:151` (a `shell` step that fails twice →
    `attempts == 2`), which still holds under failure-only increment (two `FAILED`
    finalizes → two increments); `test_resume_crash.py` makes no `attempts`
    assertion, so nothing there changes.

**Test strategy (`tests/unit/test_resume_response.py`, extend `test_resume_crash.py`):**

- Append: one `--response` on a conflict park → exactly one entry, `state` goes `pending`→`consumed`; both states reachable in git history (assert via the two committed manifest SHAs, i.e. one checkpoint commit shows `pending`, a later one shows `consumed`).
- **Checkpoint-commit shape (FR-2.2):** each response checkpoint commit changes **only** `manifest.json` + `RUN.md` (assert the commit's name-only diff carries no other path — no implementation diff), and its author is the fixed engine identity, **not** the operator `user` recorded in the manifest entry.
- Idempotency / fault injection across the **full** append→consume window — kill
  at each checkpoint, then re-run resume, asserting exactly one entry (ending
  `consumed`), one logical re-execution, and `response_attempt`/`attempts`
  unchanged from a clean single run:
  - after the pending append (record still `PARKED`).
  - after the write-ahead `RUNNING` checkpoint (`:234`) but before the adapter returns.
  - during adapter execution.
  - after terminal finalization but **before** the consume persist/commit (entry
    still reads `pending` on disk → re-launch is idempotent to the same outcome).
  - dirty-base crash under each `interrupted_step` policy: `reset_to_base`
    re-launches the pending response cleanly to one `consumed` entry; `park`
    leaves it `INTERRUPTED` with the response `pending`, and the next
    response-less `gauntlet resume` consumes it once.
- Mismatched-response-over-pending → errors with the FR-7.1 string.
- Guards: not-parked / human_gate / conflict-park-without-`--response` / non-conflict-park-without-`--response` (still works) → each asserts the exact PRD message or unchanged behavior.
- **FR-6 (per-outcome attempt accounting), each driven independently:**
  - `>N` conflict resumes with `max_retries=N` → run never `FAILED`; `StepRecord.attempts` unchanged while `len(human_responses)` grows; then `N+1` genuine fake failures → budget exhausts.
  - response resume that **proceeds** (`DONE`) → `attempts` unchanged.
  - response resume that **genuinely fails** (`FAILED`) → `attempts` increments exactly once.
  - ordinary (non-response) success → `attempts` unchanged.
  - crash after a `FAILED` finalize, then resume → `attempts` reflects a single increment for that failure, not two.
- Identity fail-closed (P2 wired): both sources blank → resume errors and manifest is unchanged (no entry appended).

**Exit criteria:** `uv run pytest` green (incl. unchanged `test_resume_crash.py` failure-path assertions); one commit `P3: Record --response idempotently; decouple retry budget`.

**Deferrals:** prompt injection content (P4); builder classification + disposition schema (P5).

---

## P4 — Prompt injection: chronological `human-response.md` from the manifest

**Assumption validated:** the full ordered response history can be fed to the builder through the **existing** input-artifact path (no new `{{}}` interpolation) as a **single invocation-local** synthetic artifact, **without mutating** the approved pipeline definition or the step's persisted `inputs:` (FR-4, FR-4.1). If injection required mutating persisted inputs, it would violate the approved-artifact and audit invariants.

**Deliverables:**

- A renderer that rebuilds `human-response.md` from `steps[N].human_responses` (chronological, oldest first) using the exact FR-4 block format (`# Human decisions (chronological)`, per-response `## Response <response_id> — attempt <response_attempt>` with Response/Timestamp/User).
- Wire it into the resume re-execution so the file is added to an **invocation-local copy** of the step's inputs used only for this one `_render_prompt` call (`steptypes.py:217-233`); it surfaces as a `--- input artifact: human-response.md ---` block. The persisted `step.inputs`, the pipeline YAML, and `manifest.json` are untouched.
- Lifecycle exactly per FR-4.1: ephemeral, regenerated each resume, written to the step's prompt-render area, never committed as a step artifact, harmless if left stale (fully derived from the manifest array).

**Test strategy (extend `tests/unit/test_resume_response.py`):**

- After two recorded responses, assert the rendered prompt the adapter receives contains one `--- input artifact: human-response.md ---` block with **both** responses in chronological order and correct ids/attempts/user.
- Assert the pipeline definition file and persisted `step.inputs` are **byte-for-byte identical** before and after the resume; assert `manifest.json` has no `human-response.md` artifact entry.
- Crash/regenerate: after a re-park, the next resume regenerates the file from the array (no reliance on a prior on-disk copy).

**Exit criteria:** `uv run pytest` green; one commit `P4: Inject ordered response history via synthetic artifact (FR-4)`.

**Deferrals:** the builder's interpretation of the injected block — P5.

---

## P5 — Builder resume logic + `resume-disposition` schema + deterministic fixture

**Assumption validated:** the builder can classify a response **deterministically** via the FR-3.0 precedence and emit a **machine-checkable** disposition (FR-10), so the FR-3/FR-5 acceptance criteria are testable against structured fields with a **scripted/replayed adapter** — never against live-model prose. This kills the "tests depend on subjective NL judgment" risk.

**Deliverables:**

- `schemas/resume-disposition.json` (Draft-07, `additionalProperties:false`, mirroring `findings.json`): `disposition` enum (`proceed_in_place | amendment_required | proceed_with_deviation | new_conflict`), `responses_considered: [string]`, `action_summary: string`, optional `conflict` object (`summary`, `requested_input`, `artifact`); `conflict` **required** when disposition is `amendment_required` or `new_conflict`, else null/omitted.
- **Invocation-local schema binding (FR-10) — the schema must actually reach the
  adapter.** The approved `implement` step (`pipelines/standard.yaml:59-61`) has
  **no `schema:` field**, and `_load_schema` (`steptypes.py:236-240`) reads only
  the persisted step config — so merely adding `schemas/resume-disposition.json`
  and editing the prompt would leave the resume invocation unvalidated, and
  editing the approved pipeline snapshot is prohibited (FR-4.1 / approved-artifact
  rule). The fix mirrors P4's invocation-local inputs copy: during a `--response`
  resume re-execution **only**, the orchestrator binds
  `schemas/resume-disposition.json` as the step's schema for that one invocation
  (an invocation-local override passed into `handle_agent_task` / `_load_schema`,
  not a mutation of `step` or the YAML). The adapter then validates the
  disposition through the existing structured-output path
  (`steptypes.py:113,128,236-240`); the persisted step config and pipeline YAML
  are byte-for-byte unchanged.
- **Engine disposition→outcome mapping (FR-3/FR-5) — the disposition must drive
  the step status.** Today `handle_agent_task` returns `DONE` unless the textual
  `halt_on` marker fires (`steptypes.py:151-172`); nothing reads
  `result.structured`, so a schema-valid `new_conflict` would be marked `DONE`.
  On a `--response` resume, after the agent returns the orchestrator maps
  `result.structured["disposition"]`:
  - `amendment_required` or `new_conflict` → `PARKED` with
    `parked_reason="upstream_conflict"` (re-park for the human / FR-10.4 gate;
    matches FR-3(b)/FR-5). The mapping sets `parked_reason` on the `StepResult`
    so P1's `_finalize` clear/set logic records it correctly.
  - `proceed_in_place` / `proceed_with_deviation` → normal completion (`DONE` →
    commit path).
  Once a response is being consumed, this **structured** disposition is
  authoritative for the outcome — not just the textual `UPSTREAM CONFLICT` marker
  (which remains the *first*-conflict signal, before any response exists).
- Update the builder prompt asset (`prompts/implement-phase.md`, with an append-only `prompts/CHANGELOG.md` entry below the `<!-- gauntlet:changelog -->` marker): inject a `## Human decision` handling section encoding **FR-3.0 precedence** (step 1 artifact-contradiction → `amendment_required` even when asked to "proceed despite"; step 2 ambiguous → `new_conflict`; step 3 fully-consistent → `proceed_in_place`/`proceed_with_deviation`; tie → earlier/fail-closed), the FR-3(b) halt-and-regate message, the requirement to list consumed `response_id`(s) in `responses_considered`, and the FR-6 note ("conflicts don't consume the retry budget").
- A **deterministic adapter fixture** (scripted/replay adapter returning canned disposition objects) added to the test harness, per FR-10.

**Test strategy (`tests/unit/test_resume_disposition.py`):**

- Schema validity: every fixture disposition validates; `conflict` presence rule enforced (missing when required → reject; present when forbidden → reject).
- FR-3.0 mapping via scripted dispositions: artifact-contradicting (incl. "proceed despite") → `amendment_required`, no implementation lands; ambiguous → `new_conflict` whose `conflict.requested_input` names what the supplied response did **not** provide (asserted on the structured field, not prose); fully-consistent → `proceed_in_place` / `proceed_with_deviation`.
- **Engine outcome mapping (assert orchestration status, not just standalone
  fixtures):** driving a resume through the orchestrator with a scripted adapter,
  assert the resulting **step status** and `parked_reason`:
  `new_conflict` → step `PARKED`, `parked_reason == "upstream_conflict"`;
  `amendment_required` → step `PARKED`, `parked_reason == "upstream_conflict"`,
  and no implementation/commit lands; `proceed_in_place` /
  `proceed_with_deviation` → step `DONE`.
- **Invocation-local schema binding:** assert the resume invocation's adapter
  received the `resume-disposition` schema, while the persisted `step` config and
  `pipelines/standard.yaml` are byte-for-byte unchanged before/after the resume.
- FR-5 observable property: `responses_considered` lists the consumed `response_id`(s); a `new_conflict` carries a non-empty `requested_input`.
- Assertions are exclusively on structured fields (no substring matching of prose).

**Exit criteria:** `uv run pytest` green; one commit `P5: Builder resume taxonomy + resume-disposition schema (FR-3/FR-10)`.

**Deferrals:** full live-loop validation — P6.

---

## P6 — End-to-end synthetic test + one-time dogfood

**Assumption validated:** the assembled mechanism actually **un-sticks a parked run** and leaves a complete, auditable trail (§8 e2e, §11). This is last because it exercises every prior phase together.

**Deliverables:**

- **Synthetic e2e — the repeatable CI gate (`tests/unit/test_resume_response_e2e.py`, deterministic adapter fixture, FR-10):** per PRD §8, the repeatable gate runs on an **isolated, disposable run created fresh and torn down within the test** — it triggers an upstream conflict, then `resume --response` driving the scripted disposition, and asserts the full cycle on structured fields and the manifest audit trail (ids/state/user/timestamp, `attempts` vs `len(human_responses)`). Deterministic, no live creds, no real run mutated, safe to run before every handoff.
- **One-time dogfood — a human-gated procedure, NOT a pytest (PRD §8):** resuming the real parked `prd-authoring-aids` run with `--response` is a **one-time, human-authorized validation**, not an automated test. It mutates one real parked run and depends on live creds, branch state, and operator identity, so it **cannot satisfy its preconditions twice** — it is run **once**. It ships as a documented runbook (in the operator docs below), **not** as `tests/integration/test_dogfood_resume.py`, and is **not** part of the `pytest` gate that must pass before every handoff. Its evidence — the manifest audit trail (ids/state/user/timestamp), the commit SHA, and the run-proceeds (or re-parks-with-`new_conflict`-carrying-`requested_input`) outcome — is **captured in the run artifacts** under `runs/prd-authoring-aids/`.
- Operator docs: document `gauntlet resume <slug> --response "..."` as the standard conflict-resolution path (§11), that artifact amendments route through their own gate (FR-10.4), and the one-time dogfood runbook above (preconditions, the single authorized invocation, and where its evidence lands).

**Test strategy:** the **only** automated gate is the synthetic disposable-run e2e (deterministic, unit-marked) — there is **no** integration-marked dogfood pytest. It asserts the run transitions out of parked, the manifest shows responses with correct ids/state/user, and a commit lands referencing the consumed `response_id`. The one-time dogfood is performed by a human via the runbook; its evidence lives in `runs/prd-authoring-aids/`, not in a test assertion.

**Exit criteria:** `uv run pytest` green (includes the synthetic e2e); **no** `pytest -m integration` requirement is added by this phase (the dogfood is not a pytest). The one-time dogfood, when performed, leaves its evidence under `runs/prd-authoring-aids/`. One commit `P6: End-to-end synthetic gate + one-time dogfood runbook (FR-7/§8)`.

**Deferrals:** none — feature complete for v1.

---

## Global deferrals (carried from PRD §7; not implemented in any phase)

- Interactive resume prompt (CLI-only for v1).
- Response-text validation (humans decide validity).
- RFC-5322 email validation of operator identity (FR-9 enforces non-empty only).
- Structured conflict metadata / rich conflict bodies / fingerprinting / conflict-identity comparison — v1 adds only the single `parked_reason` discriminator (P1) and the observable structured-output guard (P5).

---

## Machine-readable phase list

```gauntlet-phases
- id: P1
  title: Manifest schema + conflict-park discriminator
  goal: Add parked_reason and human_responses to StepRecord. parked_reason is current-state (FR-2.1), not a latch — _finalize clears it on every non-conflict outcome (done/failed/non-conflict park) and sets "upstream_conflict" only on the specific UPSTREAM CONFLICT halt, not any halt_on marker. Validates that conflict parks are durably distinguishable from all other parks, the lifecycle prevents stale-reason misclassification, and the new fields round-trip without breaking existing manifests.
- id: P2
  title: Operator identity resolution (fail-closed)
  goal: Resolve the audit user via GAUNTLET_USER_EMAIL→git config with trimming and fail-closed errors (FR-9). Validates that a present, non-empty audit identity can be guaranteed before any response is appended.
- id: P3
  title: CLI --response + idempotent recording + retry-budget decoupling
  goal: Add --response, record entries idempotently (pending→consumed) with atomic persist plus a new orchestrator-owned manifest-checkpoint commit (_persist does not commit today) so both states reach git history, surviving kill -9 across the full append→launch→consume window (PARKED/RUNNING/INTERRUPTED, incl. dirty-base recovery), and relocate StepRecord.attempts to a failure-only increment so conflict/response resumes never consume the failure-retry budget while a genuine response failure still counts once (FR-1/FR-2/FR-6/FR-7.1). Validates the determinism and crash-recovery core with fake adapters.
- id: P4
  title: Prompt injection of chronological response history
  goal: Rebuild human-response.md from the manifest into an invocation-local inputs copy rendered via the existing artifact path, with no mutation of the pipeline definition or persisted inputs (FR-4/FR-4.1). Validates that history reaches the builder without touching approved artifacts.
- id: P5
  title: Builder resume logic + resume-disposition schema
  goal: Add schemas/resume-disposition.json and the FR-3.0 precedence prompt, bind the schema as an invocation-local override on the response resume (the approved implement step has no schema: field and the snapshot must not change), and map the structured disposition to the step outcome (amendment_required/new_conflict→PARKED with parked_reason=upstream_conflict; proceed_*→DONE) so a schema-valid new_conflict cannot be marked DONE. Tested via a deterministic adapter fixture asserting real orchestration status, not just standalone schema fixtures (FR-3/FR-5/FR-10). Validates deterministic classification without live-model judgment.
- id: P6
  title: End-to-end synthetic gate + one-time dogfood runbook
  goal: Drive a full conflict→resume→resolution cycle on an isolated, disposable run with deterministic fixtures as the repeatable CI gate, and document the one-time human-gated dogfood of the real prd-authoring-aids run as a runbook whose evidence lands in run artifacts — NOT a repeatable integration pytest and NOT part of the pytest gate (PRD §8). Validates the assembled mechanism end to end.
```
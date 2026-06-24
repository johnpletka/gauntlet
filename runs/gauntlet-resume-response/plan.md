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
- Atomic manifest write already exists: `Manifest.write_atomic` (`manifest.py:130-154`, tempfile + fsync + `os.replace`); the orchestrator commits manifest checkpoints on its existing path (`_persist`, `orchestrator.py:470-474`).
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
- `orchestrator.py`: when an **agent_task** parks via its `halt_on` completion signal, set `rec.parked_reason = "upstream_conflict"` in `_finalize` (`:334-376`). Carry the reason on `StepResult` from the steptypes handler (`steptypes.py:182-187`) so the orchestrator does not re-parse marker text. Budget/timeout halts (`_apply_budget_guard`) and human_gate parks leave `parked_reason` **unset**.
- No CLI/recording/injection yet — those are explicit deferrals below.

**Test strategy (`tests/unit/test_human_response_manifest.py`, extend `test_manifest.py`):**

- Round-trip a `Manifest` containing a `StepRecord` with `parked_reason` and two `human_responses`; assert byte-stable JSON and full field fidelity through `write_atomic` → `load`.
- **Back-compat:** load a fixture manifest JSON that lacks both new fields; assert it loads and the fields default to `None` / `[]`.
- Drive a fixture pipeline (extend `test_orchestrator.py` harness, `FakeAdapter`) where the agent emits the `halt_on` marker → assert `status == parked` **and** `parked_reason == "upstream_conflict"`.
- Drive a human_gate park and a budget/timeout halt → assert `parked_reason` stays unset in both.

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

**Assumption validated (the determinism core):** a `gauntlet resume --response` is a **single idempotent state transition** — append-pending → re-execute → mark-consumed — that (a) the orchestrator persists atomically and commits on its existing checkpoint path, (b) survives `kill -9` between any two steps without duplicating an entry or double-counting, and (c) **never** advances the failure-retry budget on a conflict re-run (FR-6). This is the highest-risk plumbing; it is validated with `FakeAdapter`s before any real builder prompt exists.

**Deliverables:**

- `cli.py`: add `--response: str = typer.Option(None)` to `resume` (`:214-220`); pass through to `RunManager.resume`.
- `run.py` `RunManager.resume()` (`:768-821`), guard order (FR-1, FR-1.1, FR-8) — all errors use the exact PRD strings:
  - run not parked → error.
  - parked step not an `agent_task` (e.g. human_gate) → error directing to `approve`/`reject`.
  - `agent_task` with `parked_reason == "upstream_conflict"` and **no** `--response` → error requiring `--response`.
  - `agent_task` parked **without** `upstream_conflict` → existing response-less re-run unchanged; `--response` accepted+recorded but not required.
- **Append protocol (FR-2, FR-2.2, FR-7.1):**
  - If latest `human_responses` entry is `state=="pending"` (crash recovery): do **not** append; an identical `--response` (or none) re-launches the existing pending entry; a **different** `--response` errors with the exact FR-7.1 message.
  - Otherwise resolve identity (P2), assign `response_id = "<step_id>-resp-<ordinal>"`, set `response_attempt` = 1-based count, `timestamp = clock()` (injected `RunManager.clock`), `state="pending"`; append; persist atomically; commit as an ordinary manifest checkpoint **before** the agent launches.
- **Re-execution + consume:** drive re-executes the parked agent_task (existing `_execute` path; `resuming` stays false for PARKED). On terminal outcome, transition the latest pending entry to `state="consumed"`, persist, commit. The orchestrator remains the sole committer; the builder gets no direct-write path.
- **Retry-budget decoupling (FR-6):** ensure a conflict-resume re-execution does **not** advance the failure-retry budget. Concretely: the `on_fail`/`retries` path already only triggers on `FAILED` (`:170`), so conflict re-parks never consume `max_retries`. Additionally decouple `StepRecord.attempts` so the **conflict-resume** re-execution does not increment the failure-retry counter (it is a human-driven continuation, not a failure retry), while genuine handler failures continue to increment as today. **Decision/risk to surface to review:** `StepRecord.attempts` increments unconditionally at `orchestrator.py:224` today and `test_resume_crash.py` asserts it grows on crash-resume — this phase must preserve that failure/interruption behavior while exempting only the conflict-resume continuation. The chosen seam is to skip the increment when re-executing a step whose latest `human_responses` entry is `pending` (a `--response` continuation), leaving all other `_execute` increments untouched.

**Test strategy (`tests/unit/test_resume_response.py`, extend `test_resume_crash.py`):**

- Append: one `--response` on a conflict park → exactly one entry, `state` goes `pending`→`consumed`; both states reachable in git history (assert via committed manifest SHAs).
- Idempotency: kill after the pending append, re-run resume → still exactly one entry (now `consumed`), one re-execution; `response_attempt` and `attempts` unchanged from a clean single run.
- Mismatched-response-over-pending → errors with the FR-7.1 string.
- Guards: not-parked / human_gate / conflict-park-without-`--response` / non-conflict-park-without-`--response` (still works) → each asserts the exact PRD message or unchanged behavior.
- **FR-6:** with `max_retries=N`, resume `>N` times where the fake adapter re-parks on conflict each time → run never FAILED; assert `StepRecord.attempts` is not advanced by conflict resumes while `len(human_responses)` grows. Then `N+1` genuine fake failures → budget exhausts.
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

- `schemas/resume-disposition.json` (Draft-07, `additionalProperties:false`, mirroring `findings.json`): `disposition` enum (`proceed_in_place | amendment_required | proceed_with_deviation | new_conflict`), `responses_considered: [string]`, `action_summary: string`, optional `conflict` object (`summary`, `requested_input`, `artifact`); `conflict` **required** when disposition is `amendment_required` or `new_conflict`, else null/omitted. Validated via the same structured-output path agent_task already uses (`steptypes.py:113,236-240`).
- Update the builder prompt asset (`prompts/implement-phase.md`, with an append-only `prompts/CHANGELOG.md` entry below the `<!-- gauntlet:changelog -->` marker): inject a `## Human decision` handling section encoding **FR-3.0 precedence** (step 1 artifact-contradiction → `amendment_required` even when asked to "proceed despite"; step 2 ambiguous → `new_conflict`; step 3 fully-consistent → `proceed_in_place`/`proceed_with_deviation`; tie → earlier/fail-closed), the FR-3(b) halt-and-regate message, the requirement to list consumed `response_id`(s) in `responses_considered`, and the FR-6 note ("conflicts don't consume the retry budget").
- A **deterministic adapter fixture** (scripted/replay adapter returning canned disposition objects) added to the test harness, per FR-10.

**Test strategy (`tests/unit/test_resume_disposition.py`):**

- Schema validity: every fixture disposition validates; `conflict` presence rule enforced (missing when required → reject; present when forbidden → reject).
- FR-3.0 mapping via scripted dispositions: artifact-contradicting (incl. "proceed despite") → `amendment_required`, no implementation lands; ambiguous → `new_conflict` whose `conflict.requested_input` names what the supplied response did **not** provide (asserted on the structured field, not prose); fully-consistent → `proceed_in_place` / `proceed_with_deviation`.
- FR-5 observable property: `responses_considered` lists the consumed `response_id`(s); a `new_conflict` carries a non-empty `requested_input`.
- Assertions are exclusively on structured fields (no substring matching of prose).

**Exit criteria:** `uv run pytest` green; one commit `P5: Builder resume taxonomy + resume-disposition schema (FR-3/FR-10)`.

**Deferrals:** full live-loop validation — P6.

---

## P6 — End-to-end synthetic test + dogfood integration

**Assumption validated:** the assembled mechanism actually **un-sticks a parked run** and leaves a complete, auditable trail (§8 e2e, §11). This is last because it exercises every prior phase together.

**Deliverables:**

- **Synthetic e2e (`tests/unit/test_resume_response_e2e.py`, deterministic adapter fixture, FR-10):** a fresh run that triggers an upstream conflict, then `resume --response` driving the scripted disposition; assert the full cycle on structured fields and the manifest audit trail (ids/state/user/timestamp, `attempts` vs `len(human_responses)`), reproducible in CI with no live model.
- **Dogfood integration (`tests/integration/test_dogfood_resume.py`, `@pytest.mark.integration`):** resume the parked `prd-authoring-aids` run with `--response`; verify the manifest audit trail, that the step commits and the run proceeds (or re-parks with a `new_conflict` carrying `requested_input`), and that `human_responses` persists across resumes. Run locally before review handoff (CI runs `-m "not integration"`).
- Operator docs: document `gauntlet resume <slug> --response "..."` as the standard conflict-resolution path (§11) and that artifact amendments route through their own gate (FR-10.4).

**Test strategy:** synthetic test is the CI gate (deterministic); dogfood test is the integration-marked manual gate. Assert: run transitions out of parked, manifest shows responses with correct ids/state/user, commit lands referencing the consumed `response_id`.

**Exit criteria:** `uv run pytest` green and `uv run pytest -m integration` green locally; one commit `P6: End-to-end + dogfood resume-with-response (FR-7/§8)`.

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
  goal: Add parked_reason and human_responses to StepRecord and set parked_reason="upstream_conflict" only on agent_task halt parks. Validates that conflict parks are durably distinguishable from all other parks and the new fields round-trip without breaking existing manifests.
- id: P2
  title: Operator identity resolution (fail-closed)
  goal: Resolve the audit user via GAUNTLET_USER_EMAIL→git config with trimming and fail-closed errors (FR-9). Validates that a present, non-empty audit identity can be guaranteed before any response is appended.
- id: P3
  title: CLI --response + idempotent recording + retry-budget decoupling
  goal: Add --response, record entries idempotently (pending→consumed) with atomic persist and orchestrator-owned commits, surviving kill -9, and ensure conflict resumes never consume the failure-retry budget (FR-1/FR-2/FR-6/FR-7.1). Validates the determinism and crash-recovery core with fake adapters.
- id: P4
  title: Prompt injection of chronological response history
  goal: Rebuild human-response.md from the manifest into an invocation-local inputs copy rendered via the existing artifact path, with no mutation of the pipeline definition or persisted inputs (FR-4/FR-4.1). Validates that history reaches the builder without touching approved artifacts.
- id: P5
  title: Builder resume logic + resume-disposition schema
  goal: Add schemas/resume-disposition.json and the FR-3.0 precedence prompt so the builder emits a machine-checkable disposition, tested via a deterministic adapter fixture (FR-3/FR-5/FR-10). Validates deterministic classification without live-model judgment.
- id: P6
  title: End-to-end synthetic test + dogfood integration
  goal: Drive a full conflict→resume→resolution cycle on deterministic fixtures and resume the real prd-authoring-aids run, asserting the manifest audit trail and that the run un-sticks (§8/§11). Validates the assembled mechanism end to end.
```
/# PRD: RUN-SUMMARY.md — a committed build-narrative artifact

**Status:** Draft v0.1
**Author:** John (with Claude)
**Date:** 2026-06-19
**Working name:** Run summary (every finished run now *explains itself* — what shipped, what was decided, what deviated)
**Relationship to the core spec:** This PRD adds a new committed run artifact alongside `prd.md` / `plan.md` / `PR.md`. It does **not** amend `PRD-gauntlet.md`; FR-9.8 (`PR.md` drafting) is unchanged and the new artifact is kept deliberately separate from it (§2.2). It builds on existing engine machinery (the `retro.py` run-summary collectors, the `commit` step type, the step-type registry).

---

## 1. Overview

### 1.1 Problem statement

When a `gauntlet run` finishes, the committed record of the run is three files in the slug dir: `prd.md` (human-authored seed), `plan.md` (agent-authored, gated), and `PR.md` (drafted at `RUN_DONE`). `PR.md` is a **PR description for the merge reviewer** — it carries the PRD summary, a flat per-phase commit list, and the last confirm pass's verdicts (`src/gauntlet/engine/pr.py`).

No committed artifact captures the **build narrative**: what was *actually* produced versus what the plan promised, the decisions and compromises made along the way, and the deviations a tester or a future reader needs in order to understand the implementation and where to scrutinize it.

That information exists — but only inside the **gitignored** `run-<timestamp>/` instance directory, scattered across the manifest and step logs:

- **Declined / deferred findings.** Triage verdicts with `action ∈ {defer, reject}` and their reasoning record real decisions to *not* fix something — the compromises.
- **Commit-body deferrals.** Builder commit bodies carry `Deferred to PN:` notes per the enforced commit format (CLAUDE.md §7).
- **Upstream conflicts & escalations.** `UPSTREAM CONFLICT` halts and escalation parks record where the plan/PRD and reality collided.
- **Test-failure loops & retries.** `on_fail` routings and `attempts > 1` show where the build fought to converge.
- **Reviewer mutations & warnings.** `manifest.warnings` and mutation records flag process anomalies.

Anyone who later tests or maintains the work — and does not replay the instance dir by hand — loses all of it. The audit trail is rich; it is just not *summarized* anywhere durable.

### 1.2 Solution summary

At run completion, Gauntlet generates and commits **`runs/<slug>/RUN-SUMMARY.md`**, a build-narrative record sourced from the run's own audit trail. Generation is **hybrid**:

- A **deterministic backbone** is harvested from persisted run data (manifest, step logs, commit metadata) — reproducible and invention-free. This is always produced.
- An **agent-authored narrative** is layered on top: a read-only `summarizer` agent reads the harvest plus transcripts and returns a *structured* narrative (executive summary, decisions, deviations, caveats), which the engine splices into the document.

The artifact is **auto-committed** by a final pipeline step (phase label `SUMMARY`), so every completed run leaves the record in git history with no human step. It is a *record*, not an approved artifact — it is not gated and not reviewed.

### 1.3 Why this is worth a feature (PRD assumption to validate)

**Assumption:** the decisions/compromises/deviations a future tester needs are already captured as structured data in the run record, so a faithful summary can be *harvested* rather than *inferred* — keeping the artifact accurate while still readable. If the deterministic backbone alone proves substantive enough that the narrative is mere polish, that validates "data over inference"; if not, it tells us the audit trail is missing data worth persisting.

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal | Addresses |
|---|------|-----------|
| G1 | A committed `runs/<slug>/RUN-SUMMARY.md`, sibling to `prd.md`/`plan.md`/`PR.md` | "the build narrative is lost in the gitignored instance dir" |
| G2 | Faithfully summarize what shipped, decisions, compromises, and deviations | the tester/future-reader need |
| G3 | Source facts from the audit trail deterministically — no invention | "data over inference", reproducibility |
| G4 | Layer an agent-authored narrative for readability, degrading gracefully | accuracy + prose without nondeterminism risk |
| G5 | Auto-commit it as part of the run so it is always in history | "every run explains itself, no human step" |
| G6 | Keep it distinct from `PR.md` (different audience, FR-9.8 untouched) | avoid conflating PR description with build record |

### 2.2 Non-Goals (v1)

- **Not a replacement for `PR.md`.** `PR.md` stays a PR description for the merge reviewer; FR-9.8 behavior is unchanged.
- **Not a gated/approved artifact.** It is descriptive and does not change behavior, so it is not run through an adversarial cycle or a human gate (contrast PRD/plan/policy, which humans ratify — CLAUDE.md §2).
- **Not cross-run trend reporting.** That is `gauntlet report --trend` (FR-6.6).
- **Not an editor or remediation surface.** It reads the record and writes one file.

## 3. Users and Personas

- **The tester** picking up a finished run: needs to know what to scrutinize, what was deferred, what deviated from plan.
- **The future maintainer** reading the implementation cold: needs the *why* behind compromises without replaying the run.
- **The operator** reviewing the merge: reads `PR.md` for the change list and `RUN-SUMMARY.md` for the story behind it.

## 4. System Architecture

### 4.1 Components

- **`src/gauntlet/engine/summary.py` (new).** Harvests the deterministic backbone, invokes the summarizer agent, assembles `RUN-SUMMARY.md`, writes it to the slug dir, and registers a `run_summary` step `SPEC`.
- **`prompts/run-summary.md` (new).** The summarizer prompt — read-only, accuracy-first ("cite the record; do not invent").
- **`schemas/run-summary.json` (new).** Structured narrative schema (`executive_summary`, `decisions[]`, `deviations[]`, `caveats[]`).
- **The `summary` pipeline stage (new).** Runs after `retro`: a `run_summary` step then a `commit` step (phase `SUMMARY`).
- **Reused:** `retro._common_summary` / `_collect_cycles` / `_test_failure_loops` (backbone collectors), `pr._prd_summary` (PRD goal extraction), the `commit` step type, the `RedactingWriter` (atomic writes), the step-type registry.

### 4.2 Key design decisions

- **Hybrid, backbone-first.** The deterministic harvest is the source of truth; the agent narrates over it and never writes files directly.
- **Fail-closed, calibrated to value.** Backbone failure **halts** (it is core data). Narrative failure (timeout / bad JSON / agent error) **degrades** to a backbone-only file plus a `manifest.warnings` entry, and the run continues. Halting an otherwise-complete run because the prose narrator failed would destroy the very record we want to preserve — the exception is justified by "data over inference".
- **A record, not a gate.** No cycle, no approval. It is committed automatically because it describes the run rather than changing behavior — the "humans ratify, agents propose" invariant governs *approved* artifacts, not descriptive ones.
- **Runs before `RUN_DONE`.** As a pipeline step it executes while status is `running`; like `PR.md` it cannot reference its own commit SHA. This is benign and matches the existing `PR.md` limitation. `PR.md` is still drafted afterward by `RunManager._maybe_draft_pr()` at `RUN_DONE`.

## 5. Functional Requirements

### FR-1: `run_summary` step type
A `run_summary` step type exists and is a registered built-in (registered in `steptypes._register_builtins()` exactly like `cycle`/`retro`). It is `needs_agent=True`, `requires_repo_write=False`, and touches the worktree only by writing `RUN-SUMMARY.md`.

### FR-2: Generation & commit
On a completing run, the `summary` stage writes `runs/<slug>/RUN-SUMMARY.md` with all §6 sections, then commits it with phase `SUMMARY`, recorded as a `CommitRecord` in the manifest like any other phase commit.

### FR-3: Deterministic backbone
The deterministic sections are sourced **only** from persisted run data (manifest, step logs, commit metadata) and are reproducible. No facts are invented.

### FR-4: Agent narrative
The narrative is produced by a read-only `summarizer` agent and validated against `schemas/run-summary.json`. The agent receives the harvest and transcripts; it never writes files itself. The engine splices the validated narrative into the document.

### FR-5: Fail-closed behavior
If the backbone cannot be built, the step halts. If the narrative agent fails or returns invalid output, the engine writes a backbone-only `RUN-SUMMARY.md`, appends a `manifest.warnings` entry, and the run continues to `RUN_DONE`.

### FR-6: Resumability
The step is resumable after `kill -9`: the backbone is deterministic from persisted data, the agent step re-issues idempotently, and the file write is atomic via the `RedactingWriter`.

### FR-7: Separation from `PR.md`
`RUN-SUMMARY.md` is a distinct file; FR-9.8 / `PR.md` drafting is unchanged.

### FR-8: Terminal non-`DONE` runs (open — see §11)
**Recommended:** `failed` / `aborted` runs also receive a **backbone-only** `RUN-SUMMARY.md` (written by `RunManager`, **not** auto-committed, since the tree may be dirty) so every terminal run leaves a record. If declined at approval, v1 ships happy-path only.

## 6. The artifact — sections

Slug-dir file, overwritten per run like `PR.md` (🤖 = agent narrative, 📊 = deterministic harvest):

| Section | Kind | Content |
|---|---|---|
| Header | 📊 | branch / base / run_id / status / pipeline |
| What was built | 🤖 | PRD goal vs. what actually shipped, in prose |
| Decisions & compromises | 🤖+📊 | declined/deferred findings + triage reasoning; `Deferred to PN:` notes from commit bodies; escalations |
| Deviations from plan | 🤖+📊 | plan phases vs. delivered phases; test-failure loops & `on_fail` routings; `UPSTREAM CONFLICT` halts; reviewer mutations; manifest warnings |
| Phases delivered | 📊 | per-phase commit list (`manifest.commits`) |
| Adversarial review outcome | 📊 | per cycle: rounds, findings/verdict/confirm tallies |
| Caveats for testers | 🤖 | known weak spots / areas to scrutinize |
| Provenance | 📊 | totals & cost, prompt-hash count, link to `RUN.md` |

## 7. Data sources (reuse — do not reinvent)

| Section | Source | Code to reuse |
|---|---|---|
| Steps / status / test loops / commits | manifest | `retro._common_summary`, `retro._test_failure_loops` |
| Per-round findings / triage / confirm | step logs | `retro._collect_cycles` |
| Decisions & compromises | triage verdicts (`action` defer/reject + reasoning) | `_collect_cycles` verdict data; `schemas/triage.json` |
| Deferrals | commit bodies (`Deferred to PN:`) | `git log` over `manifest.commits` SHAs |
| Cost / provenance | manifest totals, `prompt_hashes` | `manifest.json` |
| PRD goal | `prd.md` first heading + paragraph | `pr._prd_summary` |

## 8. Implementation Plan (phased, assumption-validating)

- **P1 — Deterministic backbone.** `summary.py` harvest (reusing/refactoring the `retro` collectors so behavior stays identical), the harvest of declined/deferred verdicts, commit-body deferrals, halts, and warnings, and the document assembler. Renders a backbone-only `RUN-SUMMARY.md`. *Validates: the audit trail is substantive enough to summarize without an LLM.*
- **P2 — `run_summary` step + pipeline wiring.** Register the step type; add the `summary` stage (step + `SUMMARY` commit) to `standard.yaml` and `bootstrap.yaml`; add the `summarizer` agent profile (read-only; cheap model; falls back to the `triage` profile if unconfigured). *Validates: the artifact lands committed without disturbing `PR.md`/FR-9.8.*
- **P3 — Agent narrative + fail-closed.** `schemas/run-summary.json`, `prompts/run-summary.md`, the splice, and the degrade-on-narrative-failure path with the warning. *Validates: the fail-closed calibration (halt vs. degrade) behaves as specified.*
- **P4 (only if FR-8 accepted) — Terminal non-`DONE` record.** Backbone-only write from `RunManager` for `failed`/`aborted`, no commit.

## 9. Success Metrics

- Every completed run on `standard`/`bootstrap` produces a committed `RUN-SUMMARY.md` with all §6 sections populated.
- Declined/deferred findings (with reasoning) and `Deferred to` commit-body notes appear in the artifact and match the run record.
- A narrative-agent failure yields a backbone-only file plus a `manifest.warnings` entry, and the run still reaches `RUN_DONE`.
- Backbone failure halts the step (fail closed).
- Re-running after `kill -9` reproduces an identical backbone.

## 10. Risks & Mitigations

- **Risk: the agent narrative drifts from the facts.** *Mitigation:* structured output validated against a schema; the engine, not the agent, composes the file; the deterministic sections stand alone.
- **Risk: cost creep from another agent call per run.** *Mitigation:* read-only summarizer on a cheap model; one call at the very end; degrade path means it is never on the critical correctness path.
- **Risk: overlap/confusion with `PR.md`.** *Mitigation:* explicit, distinct audiences documented in both artifacts; FR-9.8 untouched.
- **Risk: auto-committing a descriptive artifact conflicts with "humans ratify".** *Mitigation:* the invariant governs *approved* artifacts that change behavior; a descriptive record is committed like `plan.md` and code already are.

## 11. Open questions

- **OQ-1 (FR-8):** Should `failed`/`aborted` runs also emit a backbone-only `RUN-SUMMARY.md` (no auto-commit)? **Recommended: yes** — debugging value, "data over inference". Everything else in this PRD is settled.

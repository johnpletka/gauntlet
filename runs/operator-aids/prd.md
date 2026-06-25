# PRD: Operator & observability aids

**Status:** Draft v0.1
**Author:** John Pletka
**Date:** 2026-06-25
**Working name:** operator-aids
**Relationship to existing artifacts:** Does **not** amend any approved artifact
(`PRD-gauntlet.md`, `policy.yaml`, any approved `prd.md`/`plan.md`). Builds on
existing machinery: the per-worktree drive lock and `RunManager` (`engine/run.py`),
the PID-reuse-safe process identity (`procident.py`), the transcript logger
(`logging/transcript.py`), the committable-skill installer and provenance engine
(`engine/skill.py`, `engine/init.py`), and `doctor` (`engine/doctor.py`). It
extends the CLI surface and adds a second skill alongside `gauntlet-prd-author`.

---

## §1 Overview

### 1.1 Problem statement

When a human supervises a `gauntlet run` in an adopter repo (one that only ran
`gauntlet init`), the **routine pauses are the easy part** — a parked
`human_gate` has a known next action. The expensive part is everything else:

1. **Re-discovery tax.** A Claude Code session opened to check on a run has no
   resident knowledge of Gauntlet — the adopter repo carries no `CLAUDE.md`
   section about it. Every session re-derives, from scratch, what Gauntlet is,
   what its run lifecycle states mean, and which command resolves which state.
   The operator pays this discovery cost on every fresh session.
2. **Undesigned failures are opaque.** Steps time out, hit the budget/timeout
   guard (`HALTED`), get killed mid-step (`INTERRUPTED`), or the orchestrator
   process itself wedges or dies while the manifest still reads `running`. In
   that last case the on-disk status field actively *lies*, because failure
   recording is best-effort — a `kill -9` or power loss writes nothing. The
   operator (human or Claude) must then hand-diagnose: glob for the step dir,
   read `transcript.md`/`events.jsonl`, infer whether the driver is alive or
   dead, and — for a wedged process — guess a PID and `kill -9` it before
   resuming. This is slow, error-prone, and done by hand every time.

The cost is concentrated in exactly the moments that matter most: a run is stuck
and the operator needs to act correctly and quickly.

### 1.2 Solution summary

Make Gauntlet **self-describing about its own run state** and **discoverable to a
Claude session**, so neither a human nor an agent has to infer the next move:

- **Self-describing state (observability).** `gauntlet status` reports not just
  the manifest status but the *computed driver liveness* (alive / orphaned /
  none) and, for every state class, the **next action** as concrete command(s).
  A `--json` mode exposes the same computed state and `next_actions` as a stable
  machine contract so an agent reads structured state instead of scraping prose.
- **Evidence on demand.** `gauntlet logs <slug>` surfaces the failing step's
  transcript/events and its dir, replacing the by-hand glob-and-cat.
- **Guarded recovery (fail-closed).** `gauntlet recover <slug>` terminates a
  *verified* live-but-wedged driver using the existing process-identity check —
  it never kills a PID it cannot prove is ours — marks the in-flight step
  `INTERRUPTED`, and leaves the run resumable. This turns today's manual `kill -9`
  into a deterministic, identity-checked verb.
- **Discoverability (a sibling skill).** A committable `gauntlet-operator` skill,
  installed by `init` next to `gauntlet-prd-author`, is a thin pointer to a new
  operator playbook (`prompts/operator.md`) — a triage decision tree over the
  full run-state space. A Claude session that expresses operator intent ("is the
  run stuck", "approve the gate", "why did it fail") is routed there.

The two halves reinforce each other: self-describing `status`/`--json` gives the
*next step* to anyone (human, agent, script) with zero resident knowledge; the
skill gives a Claude session the *map* so it stops re-discovering.

### 1.3 The assumption this validates

The riskiest belief is: **a truthful driver-liveness signal can be computed
cheaply and correctly from the drive lock + process identity, and surfaced as a
reliable next-action — reliably enough that an operator acts on it instead of
hand-diagnosing.** If liveness is ever wrong (a false "alive" on a dead driver,
or a false "orphaned" on a live one), the aid is *worse* than nothing: it would
direct the operator to `resume` an orphan that is actually working, or to wait on
a driver that is actually dead. P1 attacks this assumption first and read-only,
before any mutating verb depends on it.

---

## §2 Goals and Non-Goals

### 2.1 Goals

| ID | Outcome | Need it serves |
|----|---------|----------------|
| G1 | A single `gauntlet status <slug>` call surfaces the correct next action for every run-state class (running, parked-gate, parked-for-response, failed, halted, interrupted, orphaned, done). | Kills the "infer the next move" tax for humans and agents alike. |
| G2 | Driver liveness (alive / orphaned / none) is reported truthfully, computed from the lock + process identity, never trusted from the manifest status field. | Resolves the lying-`running` case — the hardest failure to diagnose by hand. |
| G3 | An agent can obtain full run state + `next_actions` as schema-stable JSON in one call. | Lets a Claude/script operator read state instead of parsing prose. |
| G4 | The evidence behind a failed/halted/interrupted step is reachable with one command. | Replaces the manual glob-and-cat archaeology. |
| G5 | A wedged live driver can be terminated safely and deterministically, never killing an unverifiable or foreign PID. | Replaces the risky manual `kill -9`; fills the gap `resume` cannot (a live lock is never reclaimed). |
| G6 | A Claude session that expresses operator intent in an adopter repo is routed to the operator playbook without prior knowledge of Gauntlet. | Removes the per-session re-discovery cost. |

### 2.2 Non-Goals (v1)

- **No mutation of the adopter's `CLAUDE.md`/`AGENTS.md`.** `init` plants no
  guidance block in the adopter's agent-instruction files. Discovery rests on the
  skill's intent triggers plus the self-describing CLI (which works with no skill
  at all). *(Decided — Q3.)*
- **No web/console changes.** The supervisory console (`serve`, FR-11/FR-12) is
  out of scope; this PRD is the CLI + skill surface only. The `--json` contract
  may later feed the console, but that is not built here.
- **No detection of an alive-but-*semantically*-stuck agent.** Liveness proves a
  process is our process and running; it does not judge whether a running agent
  is making progress. A staleness heuristic (no transcript activity for N
  minutes) is at most advisory and its threshold is an Open Question (§11),
  not a gate.
- **`recover` does not auto-resume.** It terminates + marks `INTERRUPTED` and
  stops. Resuming is a separate, explicit operator step (separation of concerns).
- **No new safety bypass.** `recover` is an operator action, never an in-pipeline
  one; nothing here weakens the judge, and `--no-judge` is unchanged.
- **No Windows liveness support.** `procident.py` is fail-closed on unsupported
  platforms (always "unverifiable"); v1 inherits that contract and documents it
  rather than adding Windows process-identity reading.
- **No change to what gets logged.** `logs` reads existing transcript artifacts;
  it adds no new log content and changes no redaction behavior.

---

## §3 Users and Personas

Two reader-roles touch this; both are "the operator," differing only in surface:

- **Human operator** — supervises a run from a terminal. Reads `gauntlet status`,
  decides gates, and recovers stuck runs. Wants the next action stated, not
  inferred.
- **Agent operator (Claude Code session)** — invoked to monitor/respond/diagnose,
  often in an adopter repo with no resident Gauntlet knowledge. Consumes
  `status --json` and the operator skill/playbook; drives the same verbs.

---

## §4 System Architecture

### 4.1 Components

**New:**
- `src/gauntlet/engine/operator.py` — the pure, deterministic core. Two
  functions: `driver_liveness(run_root, slug) -> Liveness` (reads `.driving.lock`
  + `process_is_alive`) and `next_actions(manifest, liveness) -> list[Action]`.
  This is the **single** computation that both the human footer and `--json`
  render, so the two can never disagree.
- `src/gauntlet/scaffold/prompts/operator.md` (+ canonical `prompts/operator.md`
  for Gauntlet's own repo) — the operator playbook: the triage decision tree, the
  command surface grouped by intent, and the guardrails. Pointed at, never copied
  (mirrors the prd-author playbook).
- `src/gauntlet/scaffold/skills/gauntlet-operator/SKILL.md` (+ canonical
  `.claude/skills/gauntlet-operator/SKILL.md`) — a thin-pointer skill to the
  operator playbook.
- `schemas/status.json` — the normative JSON Schema for `status --json` (§6).

**Reused / extended:**
- `src/gauntlet/cli.py` — extend `status` output + add `--json`; add `logs` and
  `recover` commands.
- `src/gauntlet/engine/run.py` — add `RunManager.recover(slug)`; expose a
  liveness read built on the existing `_read_lock` + `procident.process_is_alive`.
  No change to lock acquisition/reclaim semantics.
- `src/gauntlet/procident.py` — reused unchanged (`process_is_alive`).
- `src/gauntlet/logging/transcript.py` — reused unchanged; `logs` reads its
  artifacts (`steps/<leaf>/transcript.md`, `events.jsonl`, `RUN.md`).
- `src/gauntlet/engine/skill.py` — generalize the single-skill constants
  (`SKILL_NAME`, `PLAYBOOK_REL`, `TRIGGER_PHRASES`) into a small **skill
  registry** (a list of skill specs). The prd-author skill's rendering,
  provenance, classification, and refresh behavior must remain byte-for-byte
  identical; the operator skill reuses the same machinery.
- `src/gauntlet/engine/init.py` — `_scaffold_skill` iterates the registry instead
  of installing one hard-coded skill.
- `src/gauntlet/engine/doctor.py` — validate the operator skill + playbook are
  present and frontmatter-valid, exactly as it does for prd-author.

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Source of truth for "is it stuck?" | Compute liveness from `.driving.lock` + `procident`; **never** read it from `manifest.status`. | The manifest can't be written on `kill -9`/power loss, so its `running` is untrustworthy in exactly the failure case. *Data over inference; fail closed.* |
| One computation, two renderings | `operator.py` computes state + `next_actions` once; the human footer and `--json` are renderings of it. | The two surfaces can never diverge; the agent contract and the human view stay in lockstep. *Determinism over cleverness.* |
| `recover` identity gate | Refuse to kill unless `proc_identity` verifies the live PID is *our* process; refuse (don't kill) when identity is `None`/unverifiable. | Never SIGKILL a recycled or foreign PID. The safety primitive already exists; v1 only exposes it. *Fail closed.* |
| `recover` vs `resume` boundary | `recover` handles the **alive-but-wedged** driver (a live lock `resume` won't reclaim, FR-10.5); the existing stale-lock reclaim on `resume` handles the **dead/orphaned** driver. | Disjoint, complementary cases; no overlap, no new reclaim logic. |
| `recover` scope | Terminate the recorded process group + mark step `INTERRUPTED`; do **not** auto-resume. | Recovery and re-drive are distinct decisions. *Separation of concerns.* |
| Skill shape | A **sibling** `gauntlet-operator` skill, not an umbrella `gauntlet` skill. | Narrow trigger surface triggers more reliably and keeps authoring prose out of context during an operate task; mirrors how prd-author was scoped. *(Decided — Q2.)* |
| Discovery anchor | Skill + self-describing CLI only; no `CLAUDE.md` mutation. | Smaller blast radius; the CLI's next-action output is the backstop when the skill doesn't trigger. *(Decided — Q3.)* |

---

## §5 Functional Requirements

### FR-1 — Next-action legibility in `status`

- **FR-1.1** `gauntlet status <slug>` retains its current per-step listing and
  adds a trailing **next-action block**: for the run's current state, a short
  human line stating what the state means and the concrete command(s) to act on
  it. *Acceptance:* for each state in the §2/G1 matrix, a unit test asserts the
  rendered output contains the expected command string (e.g. a parked
  `human_gate` renders both `gauntlet approve <slug>` and `gauntlet reject <slug>
  --notes`; a `done` run renders no action).
- **FR-1.2** The next-action block is computed by `operator.next_actions(...)`,
  the same function `--json` uses (FR-4). *Acceptance:* a test asserts the human
  footer's commands are exactly the `command` fields of the `--json`
  `next_actions` for the same manifest+liveness fixture.
- **FR-1.3** When state is ambiguous or unrecognized, the block states that and
  recommends the read-only inspection commands (`gauntlet logs`, `gauntlet
  status --json`) rather than a mutating verb. *Acceptance:* a test feeds a
  manifest with an unknown status and asserts no mutating verb is suggested.

### FR-2 — Driver liveness

- **FR-2.1** `gauntlet status <slug>` reports a driver-liveness line with exactly
  one of: `alive` (lock present for this slug, PID live and identity matches),
  `orphaned` (lock present for this slug, PID dead or identity mismatch), `none`
  (no lock, or lock is for a different slug). *Acceptance:* unit tests with
  fabricated lock records assert each classification, including the
  identity-mismatch (PID-reuse) case mapping to `orphaned`, not `alive`.
- **FR-2.2** Liveness is computed solely from `<run_root>/.driving.lock` and
  `procident.process_is_alive`; it never consults `manifest.status` to decide
  liveness. *Acceptance:* a test sets `manifest.status = running` with a dead
  recorded driver and asserts liveness is `orphaned`.
- **FR-2.3** An `orphaned` driver's next action is `gauntlet resume <slug>`
  (which reclaims the stale lock); an `alive` driver under a `running` run yields
  no mutating action ("in progress"). *Acceptance:* tests assert the
  `next_actions` for each case.
- **FR-2.4** Liveness reading is fail-closed: an unreadable/malformed lock, or an
  unobtainable process identity (incl. unsupported platform), is treated as
  *not a confirmed-alive driver* — never as `alive`. *Acceptance:* tests with a
  corrupt lock JSON and with `proc_identity: null` both assert the result is not
  `alive`.

### FR-3 — Evidence access (`gauntlet logs`)

- **FR-3.1** `gauntlet logs <slug>` prints the path to the run's latest
  run-instance dir and, for the most recent non-`done` step (or `--step <id>`),
  the tail of its `transcript.md` (and notes the `events.jsonl` path).
  *Acceptance:* an integration-style test over a fixture run dir asserts the
  failing step's dir and a transcript excerpt are printed.
- **FR-3.2** `--step <id>` selects a specific step; an unknown step id errors
  with the list of available step leaves. *Acceptance:* a test asserts the error
  message lists the real step ids.
- **FR-3.3** `logs` is strictly read-only and reads only within the run's dir
  under the resolved `run_root`; it writes nothing and follows no symlink out of
  the run tree. *Acceptance:* a test asserts no file under the run dir is created
  or modified by `logs`, and a path-traversal `--step` is rejected.

### FR-4 — Machine-readable status (`--json`)

- **FR-4.1** `gauntlet status <slug> --json` emits a single JSON object
  conforming to `schemas/status.json` (§6), carrying run status, current step,
  driver liveness, the parked/failure descriptor (if any), the step list, and
  `next_actions`. *Acceptance:* output validates against the schema for every
  state in the G1 matrix (parametrized test).
- **FR-4.2** Each `next_actions` entry is an object with `label`, `command`, and
  `kind` (`observe`/`decide`/`control`/`recover`). *Acceptance:* schema requires
  these fields; a test asserts every emitted action has a non-empty `command`.
- **FR-4.3** `--json` emits **only** the JSON object on stdout (no log lines
  interleaved) and exits non-zero only on an actual error (not on a parked/failed
  run, which are valid states). *Acceptance:* a test pipes `--json` through a
  JSON parser and asserts it parses, for a parked run and a failed run.

### FR-5 — Guarded recovery (`gauntlet recover`)

- **FR-5.1** `gauntlet recover <slug>` proceeds only when the drive lock is
  present, is for `<slug>`, and `process_is_alive` confirms the recorded PID is
  *our* process via identity match. *Acceptance:* tests assert it proceeds in the
  verified-alive case and **refuses** (no signal sent) when the lock is absent,
  for another slug, the PID is dead, or `proc_identity` is `None`/mismatched.
- **FR-5.2** On a verified target, `recover` signals the recorded **process
  group** (SIGTERM, then SIGKILL after a bounded grace), mirroring the
  timeout-kill path. *Acceptance:* a test with a spawned dummy process group
  asserts the group is terminated.
- **FR-5.3** After termination, `recover` marks the in-flight step `INTERRUPTED`,
  records the recovery in the manifest (who/when), releases the lock, and leaves
  the run resumable; it does not start a new step. *Acceptance:* a test asserts
  post-state is `INTERRUPTED` step + resumable run + manifest recovery record +
  no lock; and that a subsequent `gauntlet resume` is accepted.
- **FR-5.4** `recover` refuses fail-closed when it cannot verify the target and
  prints why and the safe alternatives (wait, or `gauntlet status`/`logs`).
  *Acceptance:* the refusal message in the unverifiable case names the reason and
  suggests no destructive action.
- **FR-5.5** `recover` is an operator action and must not be invocable as an
  in-pipeline step. *Acceptance:* a test/inspection confirms `recover` is not a
  pipeline step type and the judge policy treats process-killing in a pipeline
  context as denied (consistent with existing in-pipeline restrictions).

### FR-6 — Operator skill + playbook

- **FR-6.1** A committable `gauntlet-operator` skill installs at
  `.claude/skills/gauntlet-operator/SKILL.md` (project-level, travels with the
  repo) as a thin pointer to `<asset_root>/prompts/operator.md`; it copies none
  of the playbook prose. *Acceptance:* the installed skill contains the rendered
  playbook reference and no playbook body; a test asserts the reference resolves
  under the repo's `asset_root`.
- **FR-6.2** The skill's `description` carries documented operator trigger
  phrases (e.g. "check the gauntlet run", "is the run stuck/parked", "approve/
  reject the gate", "why did the step fail", "recover the run"). *Acceptance:* a
  test asserts each documented phrase is present in the description (presence
  proves discovery; FR-6.6 proves triggering).
- **FR-6.3** `prompts/operator.md` documents the triage decision tree over the
  full state space (the §2/G1 matrix → action) and the guardrails: never approve
  a gate unilaterally, never `--no-judge`, never work around a judge deny, never
  have the operator modify files a reviewer/builder owns. *Acceptance:* a test
  asserts the playbook references each run-state class and each guardrail by name.
- **FR-6.4** The skill carries the same normative frontmatter as prd-author
  (validated by `SKILL_FRONTMATTER_SCHEMA`) and the same provenance markers
  (`x-gauntlet-generated`, `x-gauntlet-template-version`). *Acceptance:* the
  installed operator skill validates against `schemas/skill-frontmatter.json`.
- **FR-6.5** `gauntlet doctor` validates the operator skill + playbook presence
  and frontmatter, mirroring the prd-author checks (warn-only where prd-author is
  warn-only). *Acceptance:* `doctor` reports a missing/invalid operator skill at
  the same severity it reports the prd-author skill.
- **FR-6.6** *(Recorded integration test — empirical, like prd-author's FR-1.6.)*
  On the pinned Claude Code version, the skill triggers on its documented phrases.
  *Acceptance:* a recorded `@pytest.mark.integration` test (or a documented
  manual transcript) shows the skill activating on at least the documented
  trigger phrases.

### FR-7 — Skill registry generalization (no regression)

- **FR-7.1** `engine/skill.py` is generalized from single-skill constants to a
  registry of skill specs, each `{name, playbook_rel, template_version,
  trigger_phrases, template_path}`. *Acceptance:* the registry contains both
  `gauntlet-prd-author` and `gauntlet-operator`.
- **FR-7.2** prd-author behavior is unchanged: rendering, classification
  (generated vs customization), refresh, and stale-warning produce byte-identical
  results to pre-change for the prd-author skill. *Acceptance:* existing
  prd-author skill tests pass unmodified; a golden-file test asserts the rendered
  prd-author skill is byte-identical to the current committed one.
- **FR-7.3** `init` installs every registry skill with the same per-file
  idempotent, never-clobber, fail-closed posture (create-if-absent, refresh only
  an unmodified generated file, warn-only on a stale customization). *Acceptance:*
  init tests assert the operator skill is created on a fresh repo, skipped when
  customized, and refreshed when an unmodified generated copy is stale —
  identical posture to prd-author.

---

## §6 Data & Schemas (normative excerpts)

**`status --json` (`schemas/status.json`) — shape:**

```json
{
  "slug": "operator-aids",
  "run_id": "operator-aids",
  "run_status": "running | parked | done | aborted | failed",
  "current_step": "impl-cycle.0 | null",
  "driver": {
    "state": "alive | orphaned | none",
    "pid": 48213,
    "since": "2026-06-25T14:02:11 | null",
    "host": "hostname | null"
  },
  "parked": {
    "step_id": "impl-cycle.0",
    "type": "human_gate | agent_task | adversarial_cycle",
    "reason": "upstream_conflict | cycle_escalation | null"
  },
  "failure": {
    "step_id": "implement.1",
    "status": "failed | halted | interrupted",
    "evidence_path": "runs/operator-aids/run-<ts>/steps/implement.1/"
  },
  "steps": [
    { "id": "prd-cycle", "iteration": null, "status": "done" },
    { "id": "impl-cycle", "iteration": 0, "status": "parked" }
  ],
  "next_actions": [
    { "label": "approve", "command": "gauntlet approve operator-aids", "kind": "decide" },
    { "label": "reject",  "command": "gauntlet reject operator-aids --notes \"…\"", "kind": "decide" }
  ]
}
```

`parked` and `failure` are each present **only** in the matching state, else
`null`. `next_actions` is always present (possibly empty, e.g. a `done` run).

**Drive-lock input (existing `<run_root>/.driving.lock`, read-only here):**
`{ nonce, slug, run_id, pid, pgid, started_at, host, proc_identity }`. Liveness
reads `slug` (ownership), `pid` + `proc_identity` (identity match), and surfaces
`pid`/`started_at`/`host`; it never writes this file.

**Operator skill frontmatter:** identical schema to prd-author
(`schemas/skill-frontmatter.json`), with `name: gauntlet-operator` and the
operator trigger phrases.

---

## §7 Security & Privacy

- **Fail-closed kill (FR-5).** `recover` never signals a process it cannot prove
  via `procident` is the same process it launched. Unverifiable identity → refuse
  and surface, never kill. Unsupported platform (`proc_identity` always `None`)
  → `recover` always refuses; the operator falls back to a manual decision.
- **Liveness fail-closed (FR-2.4).** A corrupt/missing lock or unobtainable
  identity is never reported `alive`; the worst-case misreport is `orphaned`/
  `none`, which directs the operator to read-only inspection or `resume` (itself
  fail-closed on a live lock), not to a destructive action.
- **No new secret exposure.** `logs` reads transcripts already written through
  `RedactingWriter` — content is already redacted on disk; `logs` adds no
  un-redacted path. `--json` emits run metadata (statuses, step ids, paths, pid/
  host), not transcript bodies.
- **Judge posture unchanged.** `recover` is an operator-only verb (FR-5.5);
  killing a process from inside a pipeline step stays denied. No path here adds a
  judge bypass; `--no-judge` semantics are untouched.
- **Path containment.** `logs`/`recover` resolve only within the run dir under
  the configured `run_root`; slug/step inputs are validated against path
  traversal (reusing the existing slug-validation discipline).

---

## §8 Implementation Plan (phased, assumption-validating)

Ordered riskiest-assumption-first; no phase depends on a later phase. Every phase
ends in passing tests and a commit.

| Phase | Deliverable | Assumption it validates |
|-------|-------------|--------------------------|
| **P1** | `operator.py` liveness + `next_actions` core; `status` gains the driver-liveness line and next-action block (read-only). (FR-1, FR-2) | **The load-bearing one (§1.3):** truthful liveness + correct next-action can be computed from lock + procident. Read-only, so it's safe to prove first. |
| **P2** | `gauntlet logs` (read-only evidence). (FR-3) | Evidence for a failed/halted/interrupted step is reachable in one command from the known dir layout. Depends only on P1's run-resolution + existing transcript layout. |
| **P3** | `status --json` rendering the P1 core; `schemas/status.json`. (FR-4) | The same computed state serializes to a stable machine contract an agent can consume. Depends on P1's `next_actions`; adds no new computation. |
| **P4** | `gauntlet recover` — guarded, identity-checked termination + `INTERRUPTED` mark. (FR-5) | A wedged live driver can be killed *safely* (never a foreign/reused PID) and left resumable. Reuses P1's verified lock+identity read; the only mutating verb, landed after liveness is proven. |
| **P5** | Skill-registry generalization (no regression) + `gauntlet-operator` skill + `prompts/operator.md` + `init`/`doctor` wiring + recorded trigger test. (FR-6, FR-7) | A second skill installs/refreshes with the prd-author posture and triggers on operator intent — documenting verbs that now all exist. Last because it points at P1–P4 and nothing depends on it. |

### Note on resequencing

P5 (the skill) is low technical risk and high *empirical* risk (does it trigger).
It is sequenced last so the playbook it points at documents verbs that already
exist — but it has no code dependency on P1–P4 and could move earlier if we
accept the playbook briefly referencing not-yet-shipped verbs. Default: last.

---

## §9 Success Metrics

- **G1/G2 correctness:** liveness classification is correct on 100% of the test
  matrix (live / SIGKILL'd / reused-PID / clean-idle), with **0** false `alive`
  on a dead driver and **0** false `orphaned` on a live one.
- **G1 coverage:** `gauntlet status` surfaces the correct next action for **8/8**
  run-state classes (parametrized test green).
- **G3:** `status --json` validates against `schemas/status.json` for **100%** of
  the state matrix and parses as a lone JSON object on stdout.
- **G4:** the failing-step evidence path + a transcript excerpt are returned by a
  single `gauntlet logs <slug>` call in the fixture run (test green).
- **G5 safety:** `recover` performs **0** kills across the unverifiable/foreign/
  dead/wrong-slug cases (refuses), and successfully terminates + marks
  `INTERRUPTED` + leaves-resumable in the verified-alive case.
- **G6 discovery:** the operator skill triggers on **100%** of its documented
  trigger phrases on the pinned Claude Code version (recorded FR-6.6 test), and
  the prd-author golden-file test stays byte-identical (FR-7.2 — zero regression).

---

## §10 Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| An alive-but-wedged driver reports `alive` and looks healthy, so the operator waits forever. | Liveness honestly reports `alive`; the per-step timeout/budget guard (`HALTED`) is the existing backstop, and `recover` lets the operator act on judgment. An advisory staleness signal is an Open Question (§11), explicitly *not* a gate (Non-Goal). |
| `--json` schema churn breaks agent/script consumers. | Treat `schemas/status.json` as a committed contract; version it; additive-only changes by default. |
| The operator skill fails to trigger reliably (same empirical risk as prd-author OQ-2). | Recorded FR-6.6 integration test; and the self-describing `status`/`--json` is the backstop — it works with no skill at all. |
| `recover` kills the wrong process. | `procident` exact-identity gate + fail-closed refuse on any unverifiable target (FR-5.1/5.4); signals only the recorded process group. |
| Generalizing `skill.py` regresses prd-author provenance/refresh. | FR-7.2 golden-file + the existing prd-author test suite must pass unmodified; registry change is mechanical, behavior-preserving. |
| Cross-platform liveness gaps (Windows). | Documented Non-Goal; `procident` already fail-closed (`None` → unverifiable); `recover` refuses, `status` reports non-`alive`. |

---

## §11 Open Questions

1. **Staleness heuristic.** Should `status` show an advisory "driver alive but no
   transcript activity for N minutes" signal, and if so what is N? Leaning:
   advisory-only, threshold deferred — liveness alone is the v1 contract. *(Record
   and move on; judgment call, cheap to defer.)*
2. **`gauntlet next` verb.** Is the one-line "what do I do" surface a separate
   `gauntlet next <slug>` verb, or just the `status` footer + `--json
   next_actions`? Leaning: footer + `--json` only in v1; `next` is optional sugar.
3. **`recover` → resume coupling.** Confirmed leaning: `recover` does **not**
   auto-resume (Non-Goal §2.2 / design §4.2). Open only if review argues the
   two-step flow is error-prone enough to fuse behind a flag.
4. **`logs` ergonomics.** Does v1 need `--follow`/`--tail N`/`events` flags, or is
   "dump the failing step's transcript tail + name the dir" enough? Leaning:
   minimal in v1; flags are additive later.
5. **Operator trigger-phrase set (empirical).** The exact `description` phrase
   list that reliably triggers `gauntlet-operator` on the pinned Claude Code
   version — to be ratified against FR-6.6, mirroring prd-author's OQ-2.

---

*Handoff: this is **Draft v0.1**. The riskiest assumption is §1.3 (truthful,
cheap liveness from lock + procident), attacked first and read-only in P1. Open
Questions 1, 2, 4, 5 remain live. Next step is `gauntlet run operator-aids`,
which begins with **adversarial review** — not implementation. I ratify; the
pipeline executes.*

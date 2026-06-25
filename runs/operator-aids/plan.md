# Implementation Plan: Operator & observability aids

**Plan version:** Draft v0.1
**Author:** builder agent
**Date:** 2026-06-25
**For PRD:** `operator-aids` (Draft v0.1)
**Relationship to existing code:** Extends `cli.py`, `engine/run.py`,
`engine/skill.py`, `engine/init.py`, `engine/doctor.py`; adds
`engine/operator.py`, `schemas/status.json`, the `gauntlet-operator` skill +
`prompts/operator.md`. Touches no approved artifact and changes no lock
acquisition/reclaim semantics, no judge policy, and no logged content.

---

## 1. Strategy & sequencing rationale

The plan follows the PRD's own §8 phasing exactly (P1–P5), because that ordering
already encodes the correct risk gradient and the strict-sequentiality
constraint (FR-10.3). I restate it here as a concrete contract and do not
re-derive a different decomposition.

The single load-bearing belief (§1.3) is **that a truthful driver-liveness
signal can be computed cheaply and correctly from the drive lock + process
identity, and rendered as a reliable next action.** Everything else either
consumes that computation (`--json` in P3, the playbook in P5) or mutates state
on the strength of it (`recover` in P4). So P1 builds and proves that
computation *read-only* — before any consumer trusts it and before any verb
kills a process based on it. If P1's liveness classification is wrong, we learn
it on the cheapest, safest phase.

The dependency chain is linear and shallow:

- **P1** establishes `operator.py` (`driver_liveness`, `next_actions`) and wires
  the read-only `status` footer + liveness line. Depends on nothing new.
- **P2** (`logs`) depends only on P1's run-instance/step resolution helpers and
  the *existing* transcript layout. It is read-only.
- **P3** (`status --json`) is a *second rendering* of P1's already-computed state
  + the committed schema. It adds no new computation.
- **P4** (`recover`) is the only mutating verb. It reuses P1's verified
  lock+identity read and the existing nonce-guarded release. It lands after
  liveness is proven and after the read-only diagnosis surfaces (`status`,
  `logs`, `--json`) exist, so an operator can already see state before any verb
  can change it.
- **P5** (skill + registry generalization) points at verbs that all now exist.
  It has no *code* dependency on P1–P4 (it is mechanical refactor + new assets),
  but is sequenced last so the playbook documents only shipped verbs (PRD §8
  resequencing note).

Each phase ends with `uv run pytest` green and exactly one commit (FR-9.2). No
phase implements another phase's deliverables; temptations are recorded as
deferrals in the commit body.

A note on a known forward-dependency risk the PRD already resolves: P4's
crash-reconciliation machinery (FR-5.6) is **not** required by P1's read-only
`status`. `status` only *detects and reports* a surviving `.recovery-intent.json`
(the `reconciliation` field); it never finalizes one. Reconciliation runs only in
P4's mutating contexts. P1 therefore ships the *detect-and-report* half (a stat of
a well-known path) without needing any P4 finalization code — the field is simply
always `null` until P4 can write intents. This keeps P1 free of a backward
dependency on P4.

---

## 2. Cross-phase design invariants

- **One computation, two renderings.** `operator.driver_liveness` and
  `operator.next_actions` are pure functions computed once. The P1 human footer
  and the P3 `--json` both render *the same* return values; a test pins them
  equal (FR-1.2). Neither surface re-derives state.
- **Liveness never reads `manifest.status`.** It is computed solely from
  `<run_root>/.driving.lock` + `procident` primitives (FR-2.2). It must *not*
  reuse `RunManager._lock_is_live` or `procident.process_is_alive` directly:
  both collapse "dead" and "identity-unverifiable" the wrong way for this purpose
  (`process_is_alive` returns `False` for an unverifiable-but-live driver, which
  would mislabel it `orphaned`). `operator.py` probes `os.kill(pid,0)` liveness
  and `read_process_identity` *separately* and distinguishes the three
  present-lock outcomes per the FR-2.4 table.
- **Fail closed on every unprovable datum.** Unparseable lock, unobtainable
  identity, foreign host → `indeterminate`, never `alive` and never `orphaned`.
  `recover` refuses on any `None`/mismatch. A wrong answer is always biased to
  the safe side (read-only inspection), never to a mutating verb.
- **Read-only verbs touch nothing.** `status` and `logs` resolve and read only
  within the run dir under the resolved `run_root`, with `realpath`-based
  containment on every path component, and write nothing. `status` does **not**
  reconcile.
- **Simplest design that satisfies the phase.** No speculative flags
  (`--tail N`, `gauntlet next`, `--follow`, Windows liveness) are built; they are
  named as deferrals. The skill registry is a plain list of specs, not a plugin
  system.

---

## 3. Phases

### P1 — Liveness + next-action core; read-only `status` (FR-1, FR-2)

**Assumption it validates (the load-bearing one, §1.3):** a truthful liveness
value (`alive`/`orphaned`/`indeterminate`/`none`) and the correct next action can
be computed cheaply and correctly from the drive lock + process identity, with
**zero** false `alive` on a dead driver and **zero** false `orphaned` on a
live-but-unverifiable one. Proven read-only, before any verb depends on it.

**Deliverables:**

- `src/gauntlet/engine/operator.py` — the pure deterministic core:
  - `Liveness` enum/literal: `alive` | `orphaned` | `indeterminate` | `none`.
  - `driver_liveness(run_root, slug) -> Liveness` — reads `.driving.lock` via the
    existing `_LockRecord.from_json`, then applies the FR-2.4 total table (rows
    a–h): no lock → `none`; foreign slug → `none`; dead PID → `orphaned`; live +
    identities both present and unequal → `orphaned`; live + identities equal +
    host match → `alive`; live + identity unobtainable → `indeterminate`;
    malformed/unreadable lock → `indeterminate`; live + identities equal + host
    mismatch → `indeterminate`. It probes `os.kill(pid,0)` and
    `read_process_identity(pid)` separately (not via `process_is_alive`).
  - The composite-state classifier implementing the §6.3 + §6.3a total decision
    table: `(run_status, liveness, descriptor) -> state` over the eleven classes
    (`in_progress`, `orphaned`, `indeterminate`, `parked_gate`,
    `parked_for_response`, `failed`, `halted`, `interrupted`, `done`, `aborted`,
    `unknown`), including the descriptor-selection and contradiction→`unknown`
    rules (zero/multiple parked steps, invalid `(type,reason)`, no failure step
    under `failed`, descriptor present under a `—` status).
  - `next_actions(manifest, liveness) -> list[Action]` — returns the structured
    actions (the FR-4.2 object shape: `label`, `kind`, `argv`, `required_inputs`,
    `executable`, `command`) per the §6.3 next-action column. `indeterminate`/
    `unknown` yield only read-only `observe` actions; `reject` carries
    `required_inputs: ["notes"]` and `executable: false`; no `executable: true`
    action's `argv` contains a placeholder. (The full structured object is built
    here so P3 serializes it directly with no new computation; the P1 footer uses
    only the `command` strings.)
- `cli.py` `status` (currently the one-line echo at `cli.py:196`) extended to
  append: a **driver-liveness line** (`driver: <state>` with pid/host/since when
  present) and a **next-action block** rendering `next_actions[].command` with the
  short human "what this state means" line. Existing per-step listing retained.
- A read-only detection of a surviving `<run_instance_dir>/.recovery-intent.json`
  rendered as a footer note (the `reconciliation` half `status` is responsible
  for) — detection only, never finalization. (The intent is never *written* until
  P4; this just means the detection code exists and reports `null`/"none" now.)
- Run-instance + step resolution helpers (FR-3.1a) needed by the footer and
  reused by P2: select the instance from `active-run.txt` else the
  lexicographically-greatest `run-<ts>`; select the default step from **manifest
  step order** (last status ∉ {`done`,`skipped`}, highest iteration). Placed where
  both `status` and (P2) `logs` can call them — in `operator.py` or a small
  resolution helper module.

**Design (simplest that satisfies P1):** pure functions over the already-parsed
`_LockRecord` and `Manifest`; no caching, no new lock I/O path, no new manifest
fields. Reuse `_LockRecord.from_json`, `ProcessIdentity.from_dict`,
`read_process_identity`. The decision table is a literal mapping/branch ladder
mirroring §6.3/§6.3a — boring and inspectable.

**Test strategy:**
- One unit test per FR-2.4 row (a–h) with fabricated `.driving.lock` records,
  asserting the `liveness` value; explicitly the PID-reuse case → `orphaned` (not
  `alive`), the identity-unobtainable case → `indeterminate` (not `orphaned`),
  malformed-lock → `indeterminate`, foreign-host → `indeterminate`; rows f/g/h
  assert no mutating `next_action`.
- A `manifest.status=running` + dead recorded driver fixture → `orphaned`; a live
  PID with recorded `proc_identity: null` → `indeterminate`.
- Parametrized composite-state test: one case per §6.3 class, asserting `state`
  and the expected command strings in the footer (e.g. parked `human_gate` renders
  both `gauntlet approve <slug>` and `gauntlet reject <slug> --notes`; `done`
  renders no action; `unknown`/`indeterminate` render only `logs`/`status --json`).
- §6.3a contradiction cases (zero/multiple parked steps; invalid `(type,reason)`;
  `failed` with no terminal step; descriptor under a `—` status) → `unknown` →
  read-only only.
- FR-1.2 lockstep: the footer's commands equal the `command` fields of
  `next_actions` for the same manifest+liveness fixture.
- FR-1.3: unknown `run_status` and unparseable-lock-under-`running` both suggest
  no mutating verb.
- `status` writes nothing (assert run dir unchanged after call).

**Exit criteria:** all the above green under `uv run pytest`; liveness correct on
100% of rows a–h; single commit `P1: ...`.

**Deferrals (recorded, not built):** `--json` (P3), `recover` and any intent
*writing* (P4), `gauntlet next` sugar (OQ-2), `--tail N` (OQ-4).

---

### P2 — `gauntlet logs` (read-only evidence) (FR-3)

**Assumption it validates:** the evidence behind a failed/halted/interrupted step
is reachable in one command from the known dir layout, deterministically and
without crashing on missing/malformed artifacts. Depends only on P1's
resolution helpers + the existing `logging/transcript.py` layout.

**Deliverables:**

- `gauntlet logs <slug> [--step <id>]` in `cli.py`:
  - Resolves the run-instance and default step via P1's deterministic helpers
    (FR-3.1a); `active-run.txt` naming a missing instance → error listing
    available instances.
  - Prints the resolved run-instance dir and the selected step dir, the **last
    200 lines** of `steps/<leaf>/transcript.md` (full file if shorter), and notes
    the `events.jsonl` path (never parses it).
  - `--step <id>` selects a specific step; unknown id → error listing the real
    step leaves.
  - Missing/unreadable `transcript.md` → prints the resolved step dir + an
    explicit "transcript absent/unreadable (step status: <status>)" notice + the
    `events.jsonl` path, exit `0`.
- Path containment (FR-3.3): every component (`run_root`, run dir, instance dir,
  step dir, transcript/events files) `realpath`-resolved and asserted a
  descendant of the run dir; slug/`--step` validated against traversal
  (reuse `safe_run_segment`); symlink escape refused with no read.

**Design (simplest):** read-only filesystem walk + slice; no new log content, no
events parsing, no `--tail`/`events`/`--follow` flags (deferred). Reuse the
existing run/step layout constants and `safe_run_segment`.

**Test strategy:**
- Fixture run dir with multiple steps and ≥2 iterations of one step: asserts the
  failing step's dir + a transcript excerpt are printed, and the selected leaf is
  the manifest's last non-`done` step at highest iteration *independent of dir
  mtime*.
- ≤200 lines emitted for a long transcript; full file for a short one.
- Absent transcript and unreadable transcript: notice printed, exit `0`.
- Unknown `--step` errors listing real step ids.
- Read-only: no file under the run dir created/modified.
- Path-traversal `--step` rejected; **symlink-escape** (step dir or
  `transcript.md` symlinked outside the run tree) refused with no out-of-tree read.

**Exit criteria:** tests green; single commit `P2: ...`.

**Deferrals:** `--tail N`, `events` sub-output, `--follow` (OQ-4 / belongs to the
follow-on streaming PRD).

---

### P3 — `status --json` + `schemas/status.json` (FR-4)

**Assumption it validates:** the same computed state (P1) serializes to a stable
machine contract an agent can consume as a lone JSON object. Adds no new
computation — a second rendering of P1.

**Deliverables:**

- `schemas/status.json` — committed JSON Schema (Draft 2020-12),
  `additionalProperties: false` top-level and on every nested object, exactly the
  §6.1 field contract (`schema_version`, `slug`, `run_id`, `run_status`, `state`,
  `current_step`, `driver{state,pid,since,host}`, `parked|null`, `failure|null`,
  `reconciliation|null`, `steps[]`, `next_actions[]`), with the §6.5 compatibility
  policy documented in a header comment / `$comment`.
- `gauntlet status <slug> --json` in `cli.py`: emits a single JSON object built
  from P1's `driver_liveness`/composite-state/`next_actions` plus manifest fields;
  `current_step` equals exactly one rendered `steps[]` id; `reconciliation` is the
  report-only object when a surviving intent is detected (still detection-only —
  never reconciles), else `null`.
- `--json` emits **only** the JSON on stdout (no interleaved log lines) and exits
  non-zero only on an actual error (parked/failed are valid states).

**Design (simplest):** a serializer over P1's return values + the manifest; the
schema is the single living contract for `schema_version: 1`. No private frozen
copies; consumer rules documented per §6.5.

**Test strategy:**
- Parametrized: output validates against `schemas/status.json` for **every**
  composite state in the §6.3 table (one case per class), including a case with a
  non-null `reconciliation`.
- FR-4.2: schema requires all six action fields; every action `argv` is a
  non-empty array; `reject` has `required_inputs: ["notes"]` + `executable:
  false`; no `executable: true` action's `argv` contains a placeholder.
- FR-4.3: piping `--json` through a JSON parser succeeds for a parked run and a
  failed run; stdout is a lone object.
- A drift guard asserting the rendered example (§6.2) validates.

**Exit criteria:** 100% of the state matrix validates and parses as a lone object;
single commit `P3: ...`.

**Deferrals:** any breaking schema change (would bump `schema_version`); console
consumption of the contract (Non-Goal).

---

### P4 — `gauntlet recover` (guarded, identity-checked termination) (FR-5)

**Assumption it validates:** a wedged *live* driver can be terminated **safely**
— never a foreign-host, recycled, or regrouped PID — left resumable, with a
crash-consistent, idempotent protocol. The only mutating verb; lands after
liveness is proven (P1) and the diagnosis surfaces (P1–P3) exist.

**Deliverables:**

- `RunManager.recover(slug)` in `engine/run.py` implementing the FR-5.6
  nonce-/state-guarded sequence:
  1. Capture lock once; run the full **FR-5.1 gate** (ownership; `host` ==
     `socket.gethostname()`; PID live + freshly-read `proc_identity` exact match;
     `os.getpgid(pid) == lock.pgid`). Any failed/unobtainable datum → fail-closed
     refuse, no signal.
  2. State guard: target in-flight step still `running`, else abort
     "transitioned concurrently".
  3. Re-read lock immediately before signalling; nonce changed/gone → abort
     without signalling.
  4. **Durably persist** `<run_instance_dir>/.recovery-intent.json` (§6.4 intent
     schema) via temp-file → `flush`+`fsync` → `rename` → `fsync` containing dir,
     capturing the frozen verified facts.
  5. Re-verify identity (exact `proc_identity` match + `getpgid == intent.pgid`)
     then signal the recorded **process group** (SIGTERM, then SIGKILL after a
     bounded grace, mirroring the timeout-kill path); on reused/absent PID send no
     signal, `signal_outcome: already_dead`.
  6. Atomic manifest update (temp → `fsync` → `rename` → `fsync` dir): mark step
     `INTERRUPTED`, **append** the §6.4 recovery record to `recoveries: []`
     (append-only).
  7. Unlink the intent (then `fsync` dir).
  8. Release the lock only under the recorded-nonce guard (reuse
     `_release_worktree_lock`'s discipline).
- **Crash reconciliation** keyed on the surviving intent (not a fresh liveness
  gate): runs at the start of every `recover` and on the run-startup/`resume`
  path (both already mutating). Stale (lock present, nonce differs) → discard, no
  signal, no manifest mutation. Live (lock absent, or present with matching
  nonce) + step still `running` → re-run FR-5.1 identity gate against the frozen
  intent; exact match may re-signal (no-op SIGKILL), mismatch/absent → no signal +
  `already_dead`; then finalize (steps 6–8). Read-only `status` never reconciles.
- Manifest support for the append-only `recoveries: []` list (§6.4 record schema)
  and the `INTERRUPTED` step transition (the `INTERRUPTED` status already exists).
- `recover` CLI command, plus the **operator-only boundary** (FR-5.5): `recover`
  is *not* registered in the step-type registry (`steptypes.SPECS` /
  `execution.BUILTIN_STEP_TYPES`), and it refuses fail-closed when
  `GAUNTLET_STEP_ID` is set in its environment. No `policy.yaml` change.
- Refusal messaging (FR-5.4): names the reason and suggests only safe
  alternatives (wait, `gauntlet status`/`logs`).

**Design (simplest):** compose the *existing* primitives — `_LockRecord`,
`read_process_identity`, the nonce-guarded release, the SIGTERM→SIGKILL group-kill
pattern, atomic manifest writes — into the total gate + the fixed crash-consistent
sequence. No new reclaim logic (the `resume` stale-lock path already handles
dead/orphaned drivers); `recover` handles only the alive-but-wedged case. Does
**not** auto-resume.

**Test strategy:**
- FR-5.1: proceeds only fully-verified; refuses (no signal) on absent lock, wrong
  slug, host mismatch, dead PID, `proc_identity` `None`/mismatch, and `getpgid` ≠
  recorded `pgid`.
- FR-5.2: spawned dummy process group is terminated.
- FR-5.3: post-state = `INTERRUPTED` step + resumable run + a §6.4 record
  (actor, ts, nonce, pid/pgid/identity, signal outcome, prior→resulting states) +
  no lock; a subsequent `resume` is accepted; after a resume re-wedges a newly
  `running` driver, a second `recover` *appends* a second record.
- FR-5.5: `recover` absent from the step-type registry; invocation with
  `GAUNTLET_STEP_ID` set refuses fail-closed (no signal), message names the
  operator-only boundary.
- FR-5.6 crash-consistency: (a) nonce changed between capture and signal → no-signal
  abort; (b) step no longer `running` → no-mutation abort; (c) `recover` run twice
  converges (no torn manifest, no double-kill) — idempotent; (d) crash injected
  between step 5 and step 6 (intent persisted, manifest still `running`, no record)
  → reconciled by the next `recover`/run-startup (never by `status`) into
  `INTERRUPTED` + §6.4 record (`already_dead`) + cleared intent + released lock; (e)
  intent with lock present + different nonce → discarded (no signal, no mutation),
  whereas absent lock → finalized; (f) reused-PGID injection → finalizes with no
  signal + `already_dead`, never strands the manifest `running`.

**Exit criteria:** 0 kills across unverifiable/foreign/dead/wrong-slug cases;
verified-alive case terminates + marks `INTERRUPTED` + leaves resumable;
reconciliation cases converge. Single commit `P4: ...`.

**Deferrals:** auto-resume (Non-Goal); a `pipeline_step_only` judge deny for
signalling verbs (explicitly a retro follow-up, not this PRD); Windows liveness
(Non-Goal — `procident` already fail-closed → `recover` refuses).

---

### P5 — Skill registry generalization + `gauntlet-operator` skill (FR-6, FR-7)

**Assumption it validates:** a second skill installs/refreshes with the *exact*
prd-author posture (byte-for-byte unchanged prd-author behavior) and triggers on
operator intent — documenting verbs that, by now, all exist. Last because it
points at P1–P4 and nothing depends on it.

**Deliverables:**

- Generalize `engine/skill.py` from single-skill constants (`SKILL_NAME`,
  `PLAYBOOK_REL`, `TRIGGER_PHRASES`, `CURRENT_TEMPLATE_VERSION`) into a small
  **registry**: a list of skill specs `{name, playbook_rel, template_version,
  trigger_phrases, template_path}`. The rendering, provenance/classification
  (`classify_skill`, `_is_generated_rendering`), refresh, and stale-warning
  machinery become per-spec but otherwise unchanged.
- `engine/init.py` `_scaffold_skill` iterates the registry instead of installing
  one hard-coded skill — same per-file idempotent, never-clobber, fail-closed
  posture for every spec.
- `engine/doctor.py` validates the operator skill + playbook presence and
  frontmatter, mirroring the prd-author checks at the same severity.
- New assets:
  - `src/gauntlet/scaffold/skills/gauntlet-operator/SKILL.md` (+ canonical
    `.claude/skills/gauntlet-operator/SKILL.md`) — thin-pointer skill to
    `<asset_root>/prompts/operator.md`, normative prd-author frontmatter
    (`name: gauntlet-operator`, provenance markers), and the **seven** FR-6.2
    trigger phrases verbatim in `description`.
  - `src/gauntlet/scaffold/prompts/operator.md` (+ canonical
    `prompts/operator.md`) — the triage decision tree over the full §2/G1
    state space → action, the command surface grouped by intent, and the
    guardrails (never approve a gate unilaterally, never `--no-judge`, never work
    around a judge deny, never modify reviewer/builder-owned files).

**Design (simplest):** the registry is a plain module-level list of dataclasses/
dicts — not a plugin/entry-point system. The operator skill reuses the *same*
machinery; only the data differs. No `CLAUDE.md`/`AGENTS.md` mutation (Non-Goal).

**Test strategy:**
- FR-7.1: registry contains both `gauntlet-prd-author` and `gauntlet-operator`.
- FR-7.2 (no regression): existing prd-author skill tests pass unmodified; a
  golden-file test asserts the rendered prd-author skill is byte-identical to the
  currently committed one.
- FR-7.3: init creates the operator skill on a fresh repo, skips it when
  customized, refreshes an unmodified-generated stale copy — identical posture to
  prd-author.
- FR-6.1: installed operator skill contains the rendered playbook reference and no
  playbook body; the reference resolves under the repo's `asset_root`.
- FR-6.2: each of the seven phrases present verbatim in `description`.
- FR-6.3: playbook references each run-state class and each guardrail by name.
- FR-6.4: installed operator skill validates against
  `schemas/skill-frontmatter.json`.
- FR-6.5: `doctor` reports a missing/invalid operator skill at the same severity
  as prd-author.
- FR-6.6 (`@pytest.mark.integration`, **not** in `pytest -m "not integration"`,
  **not** a CI gate): a recorded release-qualification check enumerating the seven
  phrases, recording model id, configuration, invocation protocol, activation
  oracle, retry policy, and Claude Code CLI version, reporting the activation count
  (target 7/7).

**Exit criteria:** unit suite green (incl. the byte-identical prd-author
golden-file); the FR-6.6 integration check exists and runs on demand. Single
commit `P5: ...`.

**Deferrals:** Windows liveness docs aside, none new; the FR-6.6 7/7 result is a
recorded empirical qualification at acceptance time, not a frozen CI pass.

---

## 4. Global deferrals (named, not smuggled)

- **Streamed/live output & a freshness "is it progressing" signal** → follow-on
  `live-run-observability` PRD (OQ-1). v1 is binary liveness + `recover` on
  operator judgment.
- **`gauntlet next` verb** → OQ-2; footer + `--json next_actions` only in v1.
- **`logs --tail N` / `events` / `--follow`** → OQ-4 (additive; `--follow` belongs
  to the streaming PRD).
- **`recover` auto-resume** → Non-Goal §2.2.
- **`pipeline_step_only` judge deny for signalling verbs** → retro proposal
  follow-up (CLAUDE.md §8), not this PRD; `policy.yaml` unchanged.
- **Windows process-identity liveness** → Non-Goal; `procident` fail-closed
  contract inherited and documented.
- **`CLAUDE.md`/`AGENTS.md` adopter mutation** → Non-Goal §2.2.

---

## 5. Machine-readable phase list

```gauntlet-phases
- id: P1
  title: Liveness + next-action core; read-only status
  goal: Add operator.py (driver_liveness + composite-state + next_actions) and the read-only status driver-liveness line and next-action block. Validates the load-bearing assumption (§1.3) that truthful liveness and a correct next action can be computed cheaply from the drive lock + process identity, with zero false alive/orphaned.
- id: P2
  title: gauntlet logs (read-only evidence)
  goal: Add `gauntlet logs <slug>` surfacing the deterministically-selected run-instance/step dir and transcript tail, never crashing on missing/malformed artifacts. Validates that failed/halted/interrupted-step evidence is reachable in one read-only command from the known layout.
- id: P3
  title: status --json + schemas/status.json
  goal: Add `gauntlet status --json` and the committed JSON Schema, rendering P1's computed state as a lone, schema-valid JSON object. Validates that the same computation serializes to a stable machine contract an agent can consume.
- id: P4
  title: gauntlet recover (guarded, identity-checked termination)
  goal: Add `RunManager.recover(slug)` and the `gauntlet recover` verb — the full FR-5.1 identity gate, the FR-5.6 crash-consistent idempotent protocol, INTERRUPTED mark, append-only recovery record, and the operator-only boundary. Validates that a wedged live driver can be killed safely (never a foreign/reused/regrouped PID) and left resumable.
- id: P5
  title: Skill registry generalization + gauntlet-operator skill
  goal: Generalize engine/skill.py into a skill registry (prd-author byte-for-byte unchanged), add the gauntlet-operator thin-pointer skill + prompts/operator.md, and wire init/doctor + the recorded trigger qualification. Validates that a second skill installs/refreshes with the prd-author posture and triggers on operator intent.
```
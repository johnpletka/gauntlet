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
A follow-on PRD, `live-run-observability` (live/streamed step output), is
sequenced **after** this one and builds *on* its `logs` and `--json` surfaces;
this PRD does not depend on it.

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
  the manifest status but the *computed driver liveness* (`alive` / `orphaned` /
  `indeterminate` / `none`) and, for every composite state class (§6.3), the
  **next action** as concrete, structured command(s).
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
| G1 | A single `gauntlet status <slug>` call surfaces the correct next action for every **composite run-state class** defined in the §6.3 normative decision table — the *total* function of (manifest `run_status` × driver `liveness` × parked/failure descriptor). The class set is exactly: `in_progress`, `orphaned`, `indeterminate`, `parked_gate`, `parked_for_response`, `failed`, `halted`, `interrupted`, `done`, `aborted`, `unknown`. | Kills the "infer the next move" tax for humans and agents alike. |
| G2 | Driver liveness (`alive` / `orphaned` / `indeterminate` / `none`) is reported truthfully, computed from the lock + process identity, never trusted from the manifest status field. | Resolves the lying-`running` case — the hardest failure to diagnose by hand. |
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
  is making *progress* — and v1 has no mid-step freshness artifact to judge from:
  agent output is buffered until step end (`run_with_timeout` → `communicate()`),
  so neither the transcript nor the orchestrator log grows during a step. A real
  progress signal needs the streamed-output work in the follow-on
  `live-run-observability` PRD; until then the wedged case is handled by `recover`
  (FR-5) on operator judgment, not by an automatic signal.
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
| `recover` identity gate | Refuse to kill unless ownership + **host equality** + exact `proc_identity` match + **PID-in-PGID** (`getpgid(pid)==lock.pgid`) all hold immediately before signalling; refuse on any `None`/unverifiable/mismatched datum (FR-5.1). | Never SIGKILL a recycled, foreign-host, or regrouped PID. The safety primitives already exist; v1 composes them into a total gate. *Fail closed.* |
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
- **FR-1.3** When the composite state is `unknown` or `indeterminate` (per the
  §6.3 decision table — an unrecognized `run_status`, an internally
  contradictory manifest, or a lock that cannot be parsed/identity-verified), the
  block states that and recommends **only** the read-only inspection commands
  (`gauntlet logs`, `gauntlet status --json`) — never a mutating verb.
  *Acceptance:* a test feeds a manifest with an unknown status, and another with
  an unparseable lock under a `running` manifest, and asserts no mutating verb is
  suggested in either case.

### FR-2 — Driver liveness

- **FR-2.1** `gauntlet status <slug>` reports a driver-liveness line with exactly
  one of four values, assigned by the **total failure-mode table** in FR-2.4:
  `alive`, `orphaned`, `indeterminate`, or `none`. Every possible lock condition
  maps to exactly one value. *Acceptance:* unit tests with fabricated lock
  records assert each of the four values, including the identity-mismatch
  (PID-reuse) case mapping to `orphaned`, not `alive`, and the
  identity-unobtainable case mapping to `indeterminate`, not `orphaned`.
- **FR-2.2** Liveness is computed solely from `<run_root>/.driving.lock` plus the
  `procident` primitives (`os.kill(pid, 0)` liveness probe **and**
  `read_process_identity` exact-match), and **never** from `manifest.status`. It
  must **not** collapse "dead" and "identity-unverifiable" the way the
  `process_is_alive` convenience helper does (that helper returns `False` for
  both, which would mislabel a live-but-unverifiable driver as `orphaned` and
  steer the operator to `resume` it — the exact false-`orphaned`-on-a-live-driver
  failure §1.3 forbids). It therefore probes liveness and identity separately and
  distinguishes the three present-lock outcomes per FR-2.4. *Acceptance:* a test
  sets `manifest.status = running` with a dead recorded driver and asserts
  liveness is `orphaned`; a second sets a **live** PID whose recorded
  `proc_identity` is `null` and asserts liveness is `indeterminate`, **not**
  `orphaned`.
- **FR-2.3** Next-action mapping by liveness, under a `running` manifest, is
  governed by §6.3 and summarised here: `orphaned` (and `none` under `running`) →
  `gauntlet resume <slug>` (reclaims the stale/absent lock); `alive` → no
  mutating action ("in progress", observe only); `indeterminate` → read-only
  inspection only, never a mutating verb (FR-1.3). *Acceptance:* tests assert the
  `next_actions` for each of the three cases.
- **FR-2.4 (normative failure-mode table).** Liveness reading is fail-closed.
  Each lock condition maps to **exactly one** value and next-action class:

  | # | Lock condition | `liveness` | Next-action class |
  |---|----------------|-----------|-------------------|
  | a | No lock file present | `none` | per `run_status` (resume if `running`) |
  | b | Lock present, parses, all required fields, `slug` ≠ target | `none` (foreign) | per `run_status` |
  | c | Lock for this slug; `os.kill(pid,0)` fails (PID dead) | `orphaned` | `resume` |
  | d | Lock for this slug; PID live; recorded & fresh identities **both present and unequal** (PID reuse) | `orphaned` | `resume` |
  | e | Lock for this slug; PID live; recorded & fresh identities **both present and equal**; `host` == this host | `alive` | observe (no mutation) |
  | f | Lock for this slug; PID live; identity **unobtainable** (recorded `null`, fresh-read `null`, or unsupported platform) | `indeterminate` | read-only inspection only |
  | g | Lock present but **malformed JSON / unreadable / missing required field** | `indeterminate` | read-only inspection only |
  | h | Lock for this slug; PID live; identities equal but `host` ≠ this host (foreign-host lock in a shared run root) | `indeterminate` | read-only inspection only |

  The defining principle: a driver is reported `alive` **only** when ownership,
  PID liveness, exact identity match, **and** host equality all hold; it is
  reported `orphaned` **only** when we can *prove* the original process is gone
  (dead PID or definitive identity mismatch); every "cannot prove either way"
  condition is `indeterminate`, which is never paired with a mutating verb.
  *Acceptance:* one test per row (a–h) asserts the `liveness` value and that rows
  f/g/h yield no mutating `next_action`.

### FR-3 — Evidence access (`gauntlet logs`)

- **FR-3.1** `gauntlet logs <slug>` prints the path to the run's selected
  run-instance dir and, for the selected step (default, or `--step <id>`), the
  tail of its `transcript.md` (and notes the `events.jsonl` path). *Acceptance:*
  an integration-style test over a fixture run dir asserts the failing step's dir
  and a transcript excerpt are printed.
- **FR-3.1a (deterministic selection — authoritative source).** Run-instance and
  step selection are deterministic and driven by run metadata, never by
  filesystem `mtime`:
  - **Run-instance:** the instance named in `<run_dir>/active-run.txt` if present
    and it exists; otherwise the lexicographically-greatest `run-<ts>` directory
    (the `%Y-%m-%dT%H-%M-%S` UTC stamp sorts chronologically). Ties cannot occur
    (stamps are unique per run); if `active-run.txt` names a missing instance,
    `logs` errors and lists the available instances rather than guessing.
  - **Default step:** from the selected instance's **manifest step order**
    (authoritative — not directory order), the last record whose status ∉
    {`done`, `skipped`}; for an iterated step (a cycle), its highest-numbered
    iteration. If every step is `done`/`skipped`, the last `done` step is shown.
    *Acceptance:* a fixture with multiple steps and ≥2 iterations of one step
    asserts the selected leaf is the manifest's last non-`done` step at its
    highest iteration, independent of dir mtime ordering.
- **FR-3.1b (default tail policy).** The default tail is the **last 200 lines** of
  `transcript.md`. The number is normative for v1; `--tail N` is a deferred
  additive flag (OQ-4). *Acceptance:* a test asserts ≤200 transcript lines are
  emitted by default for a longer transcript, and the full file for a shorter one.
- **FR-3.1c (missing / malformed artifact behavior).** `logs` never crashes on
  absent or unreadable evidence: a missing or unreadable `transcript.md` prints
  the resolved step dir plus an explicit "transcript absent/unreadable (step
  status: <status>)" notice and the `events.jsonl` path (whether or not it
  exists); it does **not** parse `events.jsonl`, so a malformed events file
  cannot break it. It exits `0` (read-only inspection succeeded) in all these
  cases. *Acceptance:* tests with an absent transcript and with an unreadable
  transcript both assert the notice is printed and exit code is `0`.
- **FR-3.2** `--step <id>` selects a specific step; an unknown step id errors
  with the list of available step leaves. *Acceptance:* a test asserts the error
  message lists the real step ids.
- **FR-3.3** `logs` is strictly read-only and reads only within the run's dir
  under the resolved `run_root`; it writes nothing. **Containment is checked on
  every path component:** `run_root`, the run dir, the run-instance dir, the step
  dir, and the `transcript.md`/`events.jsonl` files are each resolved
  (`realpath`) and must remain descendants of the run dir; any component that is
  a symlink escaping the run tree, or a `--step`/slug containing path traversal
  (`..`, absolute, or separators), is rejected with no read. *Acceptance:* a test
  asserts no file under the run dir is created or modified by `logs`; a
  path-traversal `--step` is rejected; and an explicit **symlink-escape** test —
  a step dir (or a `transcript.md`) symlinked to a target outside the run tree —
  asserts `logs` refuses to follow it and reads nothing outside the run dir.

### FR-4 — Machine-readable status (`--json`)

- **FR-4.1** `gauntlet status <slug> --json` emits a single JSON object
  conforming to `schemas/status.json` (§6), carrying `schema_version`, raw
  manifest `run_status`, the **computed composite `state`** (§6.3), `current_step`,
  driver liveness, the parked/failure descriptor (if any), the step list, and
  `next_actions`. *Acceptance:* output validates against the schema for every
  composite state in the §6.3 decision table (parametrized test, one case per
  class).
- **FR-4.2 (structured, safely-executable actions).** Each `next_actions` entry
  is an object with `label`, `kind` (`observe`/`decide`/`control`/`recover`),
  `argv` (a JSON **array** of already-split, fully-resolved argument tokens — no
  shell quoting, no interpolation), `required_inputs` (array of named
  operator-supplied inputs the action needs before it is runnable, e.g.
  `["notes"]` for `reject`; empty when none), `executable` (boolean: `true` only
  when `required_inputs` is empty and `argv` is complete and safe to run as-is;
  `false` when an operator must supply an input first), and `command` (a rendered
  string **for human display only**, never for execution — it may contain
  placeholder text such as `--notes "<your reason>"`). A consumer executes
  `argv` only when `executable` is `true`; it must never execute the rendered
  `command` string, and an action with non-empty `required_inputs` is `executable:
  false` so a script cannot run a literal placeholder. *Acceptance:* the schema
  requires all six fields; a test asserts every action's `argv` is a non-empty
  array, that the `reject` action carries `required_inputs: ["notes"]` with
  `executable: false`, and that no `executable: true` action's `argv` contains a
  placeholder token.
- **FR-4.3** `--json` emits **only** the JSON object on stdout (no log lines
  interleaved) and exits non-zero only on an actual error (not on a parked/failed
  run, which are valid states). *Acceptance:* a test pipes `--json` through a
  JSON parser and asserts it parses, for a parked run and a failed run.

### FR-5 — Guarded recovery (`gauntlet recover`)

- **FR-5.1 (recovery identity gate — all of these, ANDed).** `gauntlet recover
  <slug>` proceeds only when **every** condition holds; any failed or
  unobtainable datum is a fail-closed refusal (no signal sent):
  1. the drive lock is present and `slug` == `<slug>` (ownership);
  2. the lock's `host` **equals** the current host (`socket.gethostname()`) — a
     lock from a *different* host in a shared run root is never actioned, since a
     local PID could otherwise be checked against an unrelated foreign record;
  3. the recorded PID is live and its freshly-read `proc_identity` **exactly
     matches** the recorded one (PID-reuse-safe — equivalent to liveness `alive`,
     never `orphaned`/`indeterminate`);
  4. **immediately before signalling**, the verified PID still belongs to the
     recorded process group — `os.getpgid(pid) == lock.pgid` — so `recover`
     never signals a PGID the proven-ours PID has since left (or that was never
     its group). If `getpgid` is unobtainable, refuse.

  *Acceptance:* tests assert it proceeds only in the fully-verified case and
  **refuses** (no signal sent) when the lock is absent, is for another slug, the
  `host` differs, the PID is dead, `proc_identity` is `None`/mismatched, **or**
  `os.getpgid(pid)` ≠ the recorded `pgid`.
- **FR-5.2** On a verified target, `recover` signals the recorded **process
  group** (SIGTERM, then SIGKILL after a bounded grace), mirroring the
  timeout-kill path. *Acceptance:* a test with a spawned dummy process group
  asserts the group is terminated.
- **FR-5.3** After termination, `recover` marks the in-flight step `INTERRUPTED`,
  **appends** a recovery-event record to the manifest (schema in §6.4), releases
  the lock (only if it still carries the recorded `nonce` — FR-5.6), and leaves
  the run resumable; it does not start a new step. The recovery record is
  **append-only** — repeated recoveries accumulate, none is overwritten — so the
  audit trail is complete. *Acceptance:* a test asserts post-state is
  `INTERRUPTED` step + resumable run + a recovery record matching §6.4 (actor,
  timestamp, lock nonce, pid/pgid/identity, signal outcome, prior→resulting
  states) + no lock; that a subsequent `gauntlet resume` is accepted; and that a
  second `recover` appends a second record rather than replacing the first.
- **FR-5.4** `recover` refuses fail-closed when it cannot verify the target and
  prints why and the safe alternatives (wait, or `gauntlet status`/`logs`).
  *Acceptance:* the refusal message in the unverifiable case names the reason and
  suggests no destructive action.
- **FR-5.5 (operator-only enforcement — concrete boundary).** `recover` is an
  operator action, enforced by two independent mechanisms, neither of which
  relies on an unspecified policy rule:
  1. **Not a pipeline step type.** `recover` is not registered in the step-type
     registry, so no pipeline YAML can dispatch it — the orchestrator only runs
     known step types. This blocks *declarative* in-pipeline use.
  2. **In-process pipeline-context guard (authoritative for ad-hoc invocation).**
     `recover` refuses fail-closed when it detects it is running inside a
     pipeline-agent context — i.e. when `GAUNTLET_STEP_ID` is set in its
     environment (the per-step marker the orchestrator exports to every in-run
     agent, the same signal the judge's `pipeline_step_only` rules key on). An
     in-pipeline agent that shells out to `gauntlet recover` is therefore
     refused by `recover` *itself*, independent of `policy.yaml`. This keeps the
     §2.2 "policy.yaml unchanged" promise true: the boundary is in `recover`, not
     in a new judge rule. (Hardening the judge fast-path with a
     `pipeline_step_only` deny for process-signalling verbs is a *recommended
     follow-up* through the retro proposal process — CLAUDE.md §8 — and is
     explicitly **not** done in this PRD.)

  *Acceptance:* a test confirms `recover` is absent from the pipeline step-type
  registry, and a test asserts that invoking `gauntlet recover` with
  `GAUNTLET_STEP_ID` set in the environment refuses fail-closed (no signal sent)
  with a message naming the operator-only boundary.

- **FR-5.6 (concurrency & crash-consistency protocol).** `recover` may run
  concurrently with a driver that is finishing normally, and may itself be killed
  mid-operation. It executes a fixed, nonce- and state-guarded sequence that is
  safe to interrupt at every boundary and safe to re-run (idempotent):
  1. **Capture & verify** the lock once: record its `nonce` and run the full
     FR-5.1 gate (ownership, host, identity, PID-in-PGID).
  2. **State guard:** read the manifest and confirm the target in-flight step is
     still `running` (not already `done`/`failed`/`parked`). If it is not, abort
     with "step transitioned concurrently; no action taken" — never overwrite a
     completed/terminal step status with `INTERRUPTED`.
  3. **Re-read the lock immediately before signalling.** If the `nonce` changed
     or the lock is gone, the driver finished or relaunched between step 1 and
     now: abort **without signalling** and report "run completed/relaunched
     concurrently; re-run `gauntlet status`." This is the race against a normally
     completing driver, closed.
  4. **Persist the recovery intent durably *before* any signal.** Atomically
     write `<run_instance_dir>/.recovery-intent.json` (write-temp-then-`rename`)
     capturing the just-verified, now-frozen facts: the lock `nonce`, `pid`,
     `pgid`, `proc_identity`, `host`, the target `step_id`, the `prior_step_status`
     / `prior_run_status`, and the invoking `actor`/`actor_source`/`ts` (schema in
     §6.4). This intent is the durable record that "a kill of *this* verified
     process is in progress," written while the gate's identity proof is still
     valid — i.e. before the PID can become dead. Every crash from here on is
     finalizable from the intent alone, without re-running the FR-5.1 liveness
     gate (which would now fail precisely *because* the kill succeeded).
  5. **Signal** the recorded process group (FR-5.2: SIGTERM, then SIGKILL after a
     bounded grace).
  6. **Atomic manifest update:** mark the step `INTERRUPTED` and append the
     recovery record (§6.4), built from the intent plus the observed
     `signal_outcome`, in a single write-temp-then-`rename` so a crash leaves
     either the old manifest or the fully-updated one — never a torn write.
  7. **Clear the intent:** unlink `.recovery-intent.json` only after step 6's
     rename is durable — its content is now folded into the persisted recovery
     record, so a surviving intent always means "manifest not yet finalized."
  8. **Release the lock** only if it still carries the recorded `nonce`
     (ownership release, mirroring the engine's existing nonce-guarded release —
     never unlink a new owner's lock).

  **Crash reconciliation (deterministic restart).** On *every* `recover` (and the
  read-only `status`) invocation, before anything else, reconcile any surviving
  `.recovery-intent.json` for the selected run instance. Its presence means a
  prior `recover` reached step 4 but did not durably complete step 6 — including
  the otherwise-unreconcilable window where the process group was already killed
  (step 5 done) but the manifest is still `running` with no recovery record. The
  reconciliation is keyed on the intent, **not** on a fresh liveness gate, so a
  now-dead target does not strand the run:
  - **Stale intent (superseded run):** if the lock is gone or its `nonce` ≠ the
    intent's `nonce`, the run was relaunched since the intent was written; the
    intent is stale. Discard it (unlink) **without signalling and without mutating
    the manifest**, and report "stale recovery intent discarded; run
    relaunched — re-run `gauntlet status`."
  - **Live intent (finalize the interrupted recovery):** otherwise (lock absent or
    its `nonce` still matches, and the target step is still `running`), finalize
    idempotently. The target PID being dead is the **expected** post-signal
    outcome, not a gate failure, so finalization trusts the intent's frozen
    identity rather than re-verifying liveness. It (re-)signals the recorded group
    only if it is still alive — a no-op SIGKILL when already dead, safe because the
    intent's identity still pins the group — then performs steps 6–8: writes the
    `INTERRUPTED` transition + the §6.4 recovery record (`signal_outcome:
    already_dead` when the group was already gone), clears the intent, and releases
    the lock under the nonce guard. The FR-5.3 audit record is therefore **always**
    written for any recovery that passed step 4; `resume` is never relied on to
    create it.

  A crash *before* step 4 leaves no intent and no signal, so a fresh `recover`
  simply re-runs the full FR-5.1 gate (the PID is still live) — no special path
  needed. Every boundary is thus either a clean fresh start or a finalizable
  intent; no interrupted state is contradictory or unrecoverable. *Acceptance:*
  tests assert (a) a lock whose `nonce` changed between capture and signal causes
  a no-signal abort; (b) a step that is no longer `running` causes a no-mutation
  abort; (c) `recover` run twice over the same interrupted run converges (no torn
  manifest, no double-kill of a reused PID) — i.e. the operation is idempotent;
  **(d) crash injected between step 5 (signal sent, group dead) and step 6
  (manifest write) — intent persisted, manifest still `running`, no recovery
  record — is reconciled by the next `recover`/`status` into an `INTERRUPTED` step
  + a §6.4 recovery record (`signal_outcome: already_dead`) + cleared intent +
  released lock, with the manifest never left stranded `running` and the audit
  record present; (e) an intent whose lock `nonce` no longer matches (relaunched
  run) is discarded with no signal and no manifest mutation.**

### FR-6 — Operator skill + playbook

- **FR-6.1** A committable `gauntlet-operator` skill installs at
  `.claude/skills/gauntlet-operator/SKILL.md` (project-level, travels with the
  repo) as a thin pointer to `<asset_root>/prompts/operator.md`; it copies none
  of the playbook prose. *Acceptance:* the installed skill contains the rendered
  playbook reference and no playbook body; a test asserts the reference resolves
  under the repo's `asset_root`.
- **FR-6.2 (normative trigger corpus).** The skill's `description` carries a
  **finite, closed** set of exactly seven documented operator trigger phrases —
  the normative corpus that fixes the FR-6.6 denominator (resolves OQ-5):
  1. "check the gauntlet run"
  2. "is the run stuck"
  3. "is the run parked"
  4. "approve the gate"
  5. "reject the gate"
  6. "why did the step fail"
  7. "recover the run"

  This list is the v1 contract; additions are an explicit PRD revision, not an
  open-ended example set. *Acceptance:* a test asserts each of the seven phrases
  is present verbatim in the `description` (presence proves discovery; FR-6.6
  proves triggering).
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
- **FR-6.6 (empirical triggering — fixed denominator, deterministic pass).**
  *(Recorded integration test, like prd-author's FR-1.6.)* The acceptance
  denominator is exactly the **seven** FR-6.2 phrases. The pinned environment is
  the Claude Code version recorded in the run's environment manifest / `gauntlet
  doctor` output at acceptance time, and the recorded test names that version so
  the result is reproducible. **Automated acceptance** is a recorded
  `@pytest.mark.integration` test that asserts the skill activates on **all 7/7**
  phrases on that pinned version (matching G6's 100%); a phrase that fails to
  activate is a failing test, not a soft miss. **Manual evidence is separate:** a
  documented manual transcript may *supplement* but does **not** satisfy automated
  acceptance — the two are recorded distinctly so ratification rests on the
  deterministic 7/7 automated result. *Acceptance:* the integration test
  enumerates the seven phrases, records the pinned Claude Code version, and
  passes only when all seven activate the skill.

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

## §6 Data & Schemas (normative)

### §6.1 `status --json` (`schemas/status.json`) — normative field contract

`schemas/status.json` is a committed JSON Schema (Draft 2020-12) and the stable
machine contract. It sets `additionalProperties: false` at the top level and on
every nested object, so an unknown field is a validation failure, not silently
accepted. All fields below are **required** unless marked nullable; a nullable
field is always present and explicitly `null` when not applicable (never omitted).

| Field | Type | Null? | Constraint |
|-------|------|-------|------------|
| `schema_version` | integer | no | `1` for v1; see §6.5 compatibility policy. |
| `slug` | string | no | matches the slug-validation pattern `^[a-z0-9][a-z0-9-]*$`. |
| `run_id` | string | no | the run-instance id. |
| `run_status` | string | no | raw manifest enum: `running`\|`parked`\|`done`\|`aborted`\|`failed`. An unrecognized value does not fail the schema (it is a string) but maps to composite `state: unknown` (§6.3). |
| `state` | string | no | the **computed composite class** (§6.3): `in_progress`\|`orphaned`\|`indeterminate`\|`parked_gate`\|`parked_for_response`\|`failed`\|`halted`\|`interrupted`\|`done`\|`aborted`\|`unknown`. |
| `current_step` | string | yes | rendered id of the run's active/most-recent non-terminal step, or `null`. When non-null it **must** equal the rendered id of exactly one `steps[]` entry (`"<id>"`, or `"<id>.<iteration>"` for an iterated step). It is a derived convenience; `steps[]` is authoritative. |
| `driver` | object | no | always present; see below. |
| `driver.state` | string | no | liveness enum: `alive`\|`orphaned`\|`indeterminate`\|`none` (FR-2.4). |
| `driver.pid` | integer | yes | recorded pid, or `null` when liveness is `none`. |
| `driver.since` | string | yes | the lock's `started_at` **verbatim**, format `%Y-%m-%dT%H-%M-%S` in **UTC** (hyphen-delimited time, no offset suffix); treated as an opaque stamp, never reformatted; `null` when `none`. |
| `driver.host` | string | yes | the lock's recorded host, or `null` when `none`. |
| `parked` | object\|null | yes | present **iff** `state ∈ {parked_gate, parked_for_response}`, else `null`. |
| `parked.step_id` | string | no | (within `parked`) the parked step's rendered id. |
| `parked.type` | string | no | `human_gate`\|`agent_task`\|`adversarial_cycle`. |
| `parked.reason` | string\|null | yes | `upstream_conflict`\|`cycle_escalation`\|`null` (null for a plain `human_gate`). |
| `failure` | object\|null | yes | present **iff** `state ∈ {failed, halted, interrupted}`, else `null`. |
| `failure.step_id` | string | no | the failing step's rendered id. |
| `failure.status` | string | no | `failed`\|`halted`\|`interrupted`. |
| `failure.evidence_path` | string | no | POSIX-relative path under `run_root`, no `..`, the failing step's dir. |
| `steps` | array | no | authoritative ordered step list; each item below. |
| `steps[].id` | string | no | step id. |
| `steps[].iteration` | integer\|null | yes | iteration index for a cycle step, else `null`. |
| `steps[].status` | string | no | step enum: `pending`\|`running`\|`done`\|`failed`\|`interrupted`\|`parked`\|`halted`\|`skipped`. |
| `next_actions` | array | no | always present, possibly empty (e.g. a `done` run); each entry per FR-4.2. |

`next_actions[]` entries are the structured-action objects defined in FR-4.2:
`{ label: string, kind: "observe"|"decide"|"control"|"recover", argv: string[]
(non-empty), required_inputs: string[], executable: boolean, command: string }`.

### §6.2 Example object

```json
{
  "schema_version": 1,
  "slug": "operator-aids",
  "run_id": "run-2026-06-25T16-41-22",
  "run_status": "parked",
  "state": "parked_gate",
  "current_step": "impl-cycle.0",
  "driver": { "state": "none", "pid": null, "since": null, "host": null },
  "parked": { "step_id": "impl-cycle.0", "type": "human_gate", "reason": null },
  "failure": null,
  "steps": [
    { "id": "prd-cycle", "iteration": null, "status": "done" },
    { "id": "impl-cycle", "iteration": 0, "status": "parked" }
  ],
  "next_actions": [
    { "label": "approve", "kind": "decide",
      "argv": ["gauntlet", "approve", "operator-aids"],
      "required_inputs": [], "executable": true,
      "command": "gauntlet approve operator-aids" },
    { "label": "reject", "kind": "decide",
      "argv": ["gauntlet", "reject", "operator-aids", "--notes"],
      "required_inputs": ["notes"], "executable": false,
      "command": "gauntlet reject operator-aids --notes \"<your reason>\"" }
  ]
}
```

### §6.3 Composite run-state decision table (normative, total)

The composite `state` and `next_actions` are a **total function** of three
inputs — manifest `run_status`, computed driver `liveness` (FR-2.4), and the
parked/failure descriptor — under this **precedence**:

- **P1.** Liveness is computed independently of `run_status` (FR-2.2) and is
  always reported in `driver.state`.
- **P2.** `run_status ∈ {done, aborted, failed, parked}` is engine-/operator-
  written and trustworthy; for these the **manifest is authoritative** for `state`
  and `next_actions`, and liveness is informational. A lingering lock under a
  terminal/parked run is surfaced as a note, never overriding the action.
- **P3.** `run_status == running` is the **only** status the manifest cannot
  guarantee (a `kill -9`/power loss leaves it stale-`running`); for it, liveness
  governs.
- **P4.** Any unrecognized `run_status`, or an internally contradictory manifest
  (e.g. `run_status: parked` with no parked step), → `state: unknown` →
  read-only inspection only.

| `run_status` | liveness | descriptor | `state` | Meaning | `next_actions` (kind) |
|--------------|----------|------------|---------|---------|------------------------|
| running | alive | — | `in_progress` | driver alive & working | `status`/`logs` (observe) — no mutation |
| running | orphaned | — | `orphaned` | manifest says running but driver dead/recycled; lock reclaimable | `resume` (control) |
| running | none | — | `orphaned` | running but no lock; driver gone | `resume` (control) |
| running | indeterminate | — | `indeterminate` | cannot prove alive or dead (unparseable/unverifiable/foreign-host lock) | `logs`, `status --json` (observe) only |
| parked | (any) | type=human_gate, reason=null | `parked_gate` | awaiting a human decision | `approve` (decide), `reject` (decide, needs `notes`) |
| parked | (any) | reason ∈ {upstream_conflict, cycle_escalation} | `parked_for_response` | awaiting `resume --response` | `resume --response` (decide, needs `response`) |
| failed | (any) | step.status=failed | `failed` | a step failed | `logs` (observe), `resume` (control) |
| failed | (any) | step.status=halted | `halted` | budget/timeout guard tripped | `logs` (observe), `resume` (control) |
| failed | (any) | step.status=interrupted | `interrupted` | killed mid-step | `logs` (observe), `resume` (control) |
| done | (any) | — | `done` | run complete (lingering lock, if any, is harmless residue, noted) | (empty) |
| aborted | (any) | — | `aborted` | operator-aborted | (empty) |
| *any other / contradictory* | (any) | — | `unknown` | unrecognized state | `logs`, `status --json` (observe) only |

This table is the canonical taxonomy that G1, FR-1, FR-2.3 and FR-4 all reference;
the human footer (FR-1) and `--json` (FR-4) are two renderings of this one
computation (§4.2).

### §6.4 Recovery-event record (`recovery` append-only list in the manifest)

`recover` (FR-5.3) **appends** one record to a manifest `recoveries: []` list
(append-only; never overwrites a prior record):

```json
{
  "ts": "2026-06-25T16-44-03",
  "actor": "jdoe",
  "actor_source": "os_user",
  "reason": "wedged on model timeout (operator note) | null",
  "lock_nonce": "a1b2…",
  "pid": 48213,
  "pgid": 48213,
  "proc_identity": { "platform": "darwin", "value": 1750000000, "unit": "epoch_seconds" },
  "host": "hostname",
  "signal_outcome": "terminated_sigterm | terminated_sigkill | already_dead",
  "prior_step_id": "implement.1",
  "prior_step_status": "running",
  "prior_run_status": "running",
  "resulting_step_status": "interrupted",
  "resulting_run_status": "failed"
}
```

- `ts` — UTC, `%Y-%m-%dT%H-%M-%S` (same stamp as the lock's `started_at`).
- `actor` / `actor_source` — the invoking OS user (`getpass.getuser()`), tagged
  with its derivation source (`os_user`) so the identity provenance is explicit.
- `reason` — optional operator-supplied note, else `null`.
- `lock_nonce`, `pid`, `pgid`, `proc_identity`, `host` — the verified prior-lock
  identity (the FR-5.1 datums), so the record proves *which* process was killed.
- `signal_outcome` — which signal terminated the group (or `already_dead` if it
  exited during the grace window).
- `prior_*` / `resulting_*` — the step/run statuses before and after, so the
  audit trail records the exact transition.

**Recovery-intent file (`<run_instance_dir>/.recovery-intent.json`, transient).**
The durable pre-signal companion to the recovery record (FR-5.6 step 4). It is
written atomically *before* `recover` signals the process group and unlinked only
after the recovery record is durably appended (FR-5.6 step 7), so its presence on
a later invocation means exactly "a kill of this verified process began but the
manifest was not finalized" — the signal it gives crash reconciliation:

```json
{
  "ts": "2026-06-25T16-44-03",
  "actor": "jdoe",
  "actor_source": "os_user",
  "lock_nonce": "a1b2…",
  "pid": 48213,
  "pgid": 48213,
  "proc_identity": { "platform": "darwin", "value": 1750000000, "unit": "epoch_seconds" },
  "host": "hostname",
  "step_id": "implement.1",
  "prior_step_status": "running",
  "prior_run_status": "running"
}
```

It carries the FR-5.1-verified identity datums (so finalization trusts them
instead of re-running a liveness gate against a now-dead PID) and the prior
states needed to compose the §6.4 record. Reconciliation matches its `lock_nonce`
against the current lock: equal (or absent lock) ⇒ finalize; differing ⇒ stale,
discard. It is never appended to and never long-lived — at most one exists per run
instance, only during an in-flight or interrupted `recover`.

### §6.5 Schema compatibility policy

`schemas/status.json` is a committed contract. `schema_version` starts at `1`.
Changes are **additive-only within a major version** (new optional fields with
`null`/empty defaults, new enum members appended); any field removal, type
change, or required-field addition is a **breaking** change that bumps
`schema_version`. Consumers must tolerate unknown enum members defensively (a
new `state`/`kind` value is not a parse error). This is the concrete realisation
of the §10 schema-churn mitigation.

**Drive-lock input (existing `<run_root>/.driving.lock`, read-only here):**
`{ nonce, slug, run_id, pid, pgid, started_at, host, proc_identity }`. Liveness
reads `slug` (ownership), `pid` + `proc_identity` (identity), `host` (host
equality, FR-2.4/FR-5.1), `pgid` (PID-in-PGID check, FR-5.1), and `nonce`
(concurrency guard, FR-5.6); it surfaces `pid`/`started_at`/`host` in `--json`
and **never writes** this file.

**Operator skill frontmatter:** identical schema to prd-author
(`schemas/skill-frontmatter.json`), with `name: gauntlet-operator` and the
seven operator trigger phrases (FR-6.2).

---

## §7 Security & Privacy

- **Fail-closed kill (FR-5).** `recover` never signals a process it cannot prove
  via `procident` is the same process it launched, **on this host**, **still in
  the recorded process group**. The full gate (FR-5.1) is ownership + host
  equality + exact identity match + PID-in-PGID (`os.getpgid(pid) == lock.pgid`),
  checked immediately before signalling; any failed or unobtainable datum → refuse
  and surface, never kill. A foreign-host lock in a shared run root is never
  actioned against a local PID. Unsupported platform (`proc_identity` always
  `None`) → liveness is `indeterminate` and `recover` always refuses; the
  operator falls back to a manual decision.
- **Liveness fail-closed (FR-2.4).** A corrupt/missing/foreign-host lock or an
  unobtainable identity is never reported `alive`, and — critically — is never
  reported `orphaned` either (which would steer the operator to `resume` and
  reclaim a possibly-live lock). Such cases report `indeterminate`, whose only
  next action is read-only inspection; `orphaned` is reserved for a *provably*
  dead/recycled driver. The worst-case misreport is therefore `indeterminate`/
  `none`, never a destructive or lock-reclaiming action.
- **No new secret exposure.** `logs` reads transcripts already written through
  `RedactingWriter` — content is already redacted on disk; `logs` adds no
  un-redacted path. `--json` emits run metadata (statuses, step ids, paths, pid/
  host), not transcript bodies.
- **Judge posture unchanged.** `recover` is an operator-only verb enforced by two
  concrete, self-contained mechanisms (FR-5.5): it is not a registered pipeline
  step type, and it refuses fail-closed when `GAUNTLET_STEP_ID` is set in its
  environment (the in-pipeline-agent marker). This boundary lives in `recover`
  itself, so `policy.yaml` is genuinely unchanged — no new judge rule is required
  or added. (A `pipeline_step_only` judge deny for process-signalling verbs is a
  recommended retro follow-up, CLAUDE.md §8, not part of this PRD.) `--no-judge`
  semantics are untouched.
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

- **G1/G2 correctness:** liveness classification is correct on 100% of the
  FR-2.4 failure-mode matrix (rows a–h: no-lock / foreign-slug / SIGKILL'd /
  reused-PID / live-verified / identity-unobtainable / malformed-lock /
  foreign-host), with **0** false `alive` on a dead driver, **0** false
  `orphaned` on a live-but-unverifiable one (it must be `indeterminate`), and
  every `indeterminate` case yielding no mutating action.
- **G1 coverage:** `gauntlet status` surfaces the correct next action for
  **every composite state class** in the §6.3 decision table (parametrized test
  green, one case per class).
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
| An alive-but-wedged driver reports `alive` and looks healthy, so the operator waits forever. | Liveness honestly reports `alive`; the per-step timeout/budget guard (`HALTED`) is the existing backstop, and `recover` (FR-5) lets the operator act on judgment. A true progress signal needs streamed output and is deferred to the `live-run-observability` PRD (§11 OQ-1); it is explicitly *not* a v1 gate (Non-Goal §2.2). |
| `--json` schema churn breaks agent/script consumers. | `schemas/status.json` is a committed JSON Schema with a `schema_version` field and an explicit compatibility policy (§6.5): additive-only within a major, breaking changes bump the version, consumers tolerate unknown enum members. |
| The operator skill fails to trigger reliably (same empirical risk as prd-author OQ-2). | Recorded FR-6.6 integration test; and the self-describing `status`/`--json` is the backstop — it works with no skill at all. |
| `recover` kills the wrong process. | `procident` exact-identity gate + fail-closed refuse on any unverifiable target (FR-5.1/5.4); signals only the recorded process group. |
| Generalizing `skill.py` regresses prd-author provenance/refresh. | FR-7.2 golden-file + the existing prd-author test suite must pass unmodified; registry change is mechanical, behavior-preserving. |
| Cross-platform liveness gaps (Windows). | Documented Non-Goal; `procident` already fail-closed (`None` → unverifiable); `recover` refuses, `status` reports non-`alive`. |

---

## §11 Open Questions

1. **Alive-but-wedged freshness — deferred to streaming.** A truthful "is the live
   step actually *progressing*?" signal is **not buildable in v1**: the agent
   subprocess output is buffered until the step exits (`run_with_timeout` →
   `communicate()`), so neither `transcript.md`/`events.jsonl` nor the orchestrator
   log grows mid-step — there is no freshness artifact to read. (The proxies that
   *do* move are too weak to surface: process CPU is noisy — a healthy agent
   waiting on the model API is ~0% CPU — and worktree churn appears only for steps
   that write files.) v1's contract is therefore **binary liveness (FR-2) +
   `recover` (FR-5)** on operator judgment. The real progress signal — a live
   last-event timestamp — requires streaming agent output to disk, which is the
   follow-on `live-run-observability` PRD; the staleness threshold (N) is *its*
   open question, not this one.
2. **`gauntlet next` verb.** Is the one-line "what do I do" surface a separate
   `gauntlet next <slug>` verb, or just the `status` footer + `--json
   next_actions`? Leaning: footer + `--json` only in v1; `next` is optional sugar.
3. **`recover` → resume coupling.** Confirmed leaning: `recover` does **not**
   auto-resume (Non-Goal §2.2 / design §4.2). Open only if review argues the
   two-step flow is error-prone enough to fuse behind a flag.
4. **`logs` ergonomics.** Does v1 need `--tail N`/`events` flags, or is "dump the
   failing step's transcript tail + name the dir" enough? Leaning: minimal in v1;
   flags are additive later. (`--follow` is **not** in scope here — a live tail
   needs streamed output and belongs to the `live-run-observability` PRD; `logs`
   is designed to leave room for it.)
5. **Operator trigger-phrase set — RESOLVED.** The `description` phrase list is
   now fixed normatively as the seven-phrase corpus in FR-6.2, which sets the
   FR-6.6 acceptance denominator (7/7 on the pinned Claude Code version). It is no
   longer open: changes are an explicit PRD revision. (Empirical *reliability* of
   triggering is still proven, not assumed, by the recorded FR-6.6 integration
   test — but the corpus and the deterministic pass criterion are ratified.)

---

*Handoff: this is **Draft v0.1**. The riskiest assumption is §1.3 (truthful,
cheap liveness from lock + procident), attacked first and read-only in P1. Open
Questions 2 and 4 remain live (OQ-1 is deferred to the follow-on
`live-run-observability` PRD; OQ-5 is resolved — the trigger corpus is ratified
in FR-6.2). Next step is `gauntlet run operator-aids`,
which begins with **adversarial review** — not implementation. I ratify; the
pipeline executes.*

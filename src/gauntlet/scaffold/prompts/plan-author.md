# Author the implementation plan

You are the `builder` agent (CLAUDE.md §4). The approved PRD is provided below.
Write a **phased implementation plan** that an adversarial reviewer will then
critique and a human will ratify. The plan is the contract the phase loop
executes against, so it must be concrete and assumption-validating.

## What a good plan does

- Decomposes the work into **sequential phases**, ordered to kill the riskiest
  assumptions first. Each phase states the assumption it validates, its concrete
  deliverables, its test strategy, and its exit criteria.
- Ends every phase with passing tests and a single commit (FR-9.2). Phases are
  strictly sequential (FR-10.3): a later phase may not depend on work a phase
  before it has not yet delivered.
- Names explicit deferrals rather than smuggling later work into an early phase.
- Specifies the **simplest design that satisfies each phase** — no speculative
  abstraction or flexibility for needs the PRD does not yet require; record
  anticipated-but-unneeded extensions as deferrals, do not build them ahead of
  need.

## Sizing phases from measured history (FR-5.3)

A `--- measured phase history for sizing ---` block is appended below (after the
input artifacts). It carries this repo's completed-run cost/duration
distributions by step type, the `max_frs_per_phase` size bound, and — where a
provider window budget is configured — the window each run must fit within. Use
it to size phases against **observed cost**, not guesswork:

- Keep every phase at or under the stated `max_frs_per_phase` bound; a phase
  over it trips the phase-size lint (oversized phases are where partial delivery
  hides).
- Let the measured per-step-type and per-phase costs inform how much scope each
  phase carries and how many phases the run needs to stay within the window
  budget.
- When the block reports **no completed history**, size conservatively and lean
  on the PRD's own risk ordering — you are sizing without measured costs.

The history is **advisory input to a human-ratified plan** — it informs your
sizing; it does not dictate a phase count, and nothing here auto-tunes.

## Required: the machine-readable phase list

Somewhere in the plan, emit **exactly one** fenced code block tagged
`gauntlet-phases` whose body is a YAML list — this is the list the engine fans
the phase loop over (`foreach: plan.phases`). Each entry has:

- `id`: the phase id, `P1`, `P2`, … (numeric; drives sequencing and rollback).
- `title`: a short imperative phase title.
- `goal`: one or two sentences on what the phase delivers and the assumption it
  validates.

It must agree with the prose phases — the human ratifies the prose, the engine
executes the list, and they must not drift. Concretely: **every phase id in the
block must have a matching prose heading** of the form `## <id> — <title>` (e.g.
`## P1 — Core data model`). The engine slices each phase's section out of the
plan by that heading to build the implement prompt's scoped context, so a phase
in the list with no locatable `## <id> …` heading is rejected before approval.
Example:

```gauntlet-phases
- id: P1
  title: Core data model + storage
  goal: Persist and reload records; validates the schema survives a round-trip.
- id: P2
  title: HTTP API over the model
  goal: Expose CRUD endpoints; validates the model covers the required operations.
```

Write the full plan as Markdown (the prose plan **and** the `gauntlet-phases`
block). Return ONLY the plan document — no commentary around it.

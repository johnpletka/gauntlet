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

## Required: the machine-readable phase list

Somewhere in the plan, emit **exactly one** fenced code block tagged
`gauntlet-phases` whose body is a YAML list — this is the list the engine fans
the phase loop over (`foreach: plan.phases`). Each entry has:

- `id`: the phase id, `P1`, `P2`, … (numeric; drives sequencing and rollback).
- `title`: a short imperative phase title.
- `goal`: one or two sentences on what the phase delivers and the assumption it
  validates.

It must agree with the prose phases — the human ratifies the prose, the engine
executes the list, and they must not drift. Example:

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

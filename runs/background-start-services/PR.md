# PR draft — `background-start-services`

> Drafted by Gauntlet at the final gate (FR-9.8). **Not opened, not pushed** — opening the PR and pushing remain human actions (PRD §2.2). Edit freely before use.

- branch: `gauntlet/background-start-services` (base `main`)
- run: `run-2026-06-26T16-42-42` — status **done**
- pipeline: `standard` v1

## Summary

**PRD: Background service startup & the interactive run monitor** — **Status:** Draft v0.1 **Author:** John Pletka **Date:** 2026-06-25 **Working name:** background-start-services **Relationship to existing artifacts:** Does **not** amend any approved artifact in place. It **builds on** two predecessors and **supersedes one narrow, named clause** of a third:

## Phases & commits

### PRD
- `7252912ea6` **PRD.1** (step `prd-cycle`)

### PLAN
- `592a4e5a87` **PLAN** (step `plan-cycle`)
- `3e21da0ab9` **PLAN.1** (step `plan-cycle`)

### P1
- `b2fbab269f` **P1** (step `phase-commit`)
- `e251404dc7` **P1.1** (step `impl-cycle`)

### P2
- `05d17ba1de` **P2** (step `phase-commit`)
- `99360d875d` **P2.1** (step `impl-cycle`)

### P3
- `29ce3841de` **P3** (step `phase-commit`)
- `5155f74270` **P3.1** (step `impl-cycle`)

### P4
- `e0d47dfa6a` **P4** (step `phase-commit`)
- `fcbd938074` **P4.1** (step `impl-cycle`)

### P5
- `6ed0cf8ef3` **P5** (step `phase-commit`)
- `773a77cc59` **P5.1** (step `impl-cycle`)

## Final per-finding verdicts (last confirm pass)

- `F-001`: **resolved** — The diff now sets `token=token` in `build_record()` and returns `existing.token` on the reuse path. The added round-trip and reuse tests pin the previously missing persistence behavior.
- `F-002`: **resolved** — The diff adds `select_console_port()`, validates loopback, scans the requested port through the bounded window, falls back to an ephemeral bind, and wires it into `ensure_console()`. The new tests cover requested-free, requested-taken, fallback, and non-loopback cases.
- `F-003`: **resolved** — `runner` now imports and re-exports the registry loopback symbols instead of defining its own copies, and `registry.__all__` includes them. The added identity test guards against future drift.

## Transcripts

Full review→triage→fix→confirm record: [`run-2026-06-26T16-42-42/RUN.md`](run-2026-06-26T16-42-42/RUN.md).

_Plan: see `plan.md` in this directory._

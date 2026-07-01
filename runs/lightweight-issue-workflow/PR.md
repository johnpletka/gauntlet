# PR draft — `lightweight-issue-workflow`

> Drafted by Gauntlet at the final gate (FR-9.8). **Not opened, not pushed** — opening the PR and pushing remain human actions (PRD §2.2). Edit freely before use.

- branch: `gauntlet/lightweight-issue-workflow` (base `docs/lightweight-issue-workflow-prd`)
- run: `run-2026-06-30T19-01-19` — status **done**
- pipeline: `standard` v1

## Summary

**PRD: Lightweight Issue Workflow — `gauntlet review`** — **Status:** Draft v0.2 **Author:** John (with Claude) **Date:** 2026-06-30 **Working name:** Lightweight Issue Workflow (`gauntlet review`) **Relationship to existing artifacts:** Does **not** amend `PRD-gauntlet.md`, `policy.yaml`, or any approved `prd.md`/`plan.md`. It is an additive feature that builds on existing machinery: the `adversarial_cycle` primitive (FR-5.2), its `code_review` mode and diff-scoped confirm (FR-9.5), the run lifecycle / manifest

## Phases & commits

### PRD
- `108b4b5750` **PRD.1** (step `prd-cycle`)
- `409708b4fc` **PRD.1** (step `prd-cycle`)

### PLAN
- `ba2e757bf6` **PLAN** (step `plan-author`)
- `f711da97bc` **PLAN.1** (step `plan-cycle`)

### P1
- `55ec6ad0b0` **P1** (step `phase-commit`)
- `aa59df8d96` **P1.1** (step `impl-cycle`)

### P2
- `e66caacef8` **P2** (step `phase-commit`)
- `a33296d049` **P2.1** (step `impl-cycle`)

### P3
- `2be86beb5a` **P3** (step `phase-commit`)
- `1d53fd337d` **P3.1** (step `impl-cycle`)

### P4
- `2e001bd838` **P4** (step `phase-commit`)
- `b662529827` **P4.1** (step `impl-cycle`)

### P5
- `8023444c73` **P5** (step `phase-commit`)
- `4802b122b0` **P5.1** (step `impl-cycle`)

## Final per-finding verdicts (last confirm pass)

- `F-001`: **resolved** — The diff preserves owner/repo from PR URLs and fails closed before any PR read/fetch/checkout if the URL repo cannot be confirmed against local origin. It also keeps bare-number behavior unchanged and adds mismatch/no-origin coverage.
- `F-002`: **resolved** — The diff adds lightweight review-run ownership scanning across the review state root and invokes it for both branch mode and PR mode. PR mode now resolves the candidate branch from metadata and refuses competing review runs before checkout touches the worktree.
- `F-003`: **resolved** — The diff persists PR summary facts in the manifest, rehydrates them during resume, and passes the rehydrated resolution into `_outcome()`. The added resume test covers chosen/ignored refs, fork state, and PR URL survival across a parked review resume.

## Transcripts

Full review→triage→fix→confirm record: [`run-2026-06-30T19-01-19/RUN.md`](run-2026-06-30T19-01-19/RUN.md).

_Plan: see `plan.md` in this directory._

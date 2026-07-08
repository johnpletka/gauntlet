# PR draft — `pipeline-effectiveness`

> Drafted by Gauntlet at the final gate (FR-9.8). **Not opened, not pushed** — opening the PR and pushing remain human actions (PRD §2.2). Edit freely before use.

- branch: `gauntlet/pipeline-effectiveness` (base `main`)
- run: `run-2026-07-05T16-46-45` — status **done**
- pipeline: `standard` v1

## Summary

**PRD: Pipeline Effectiveness — catch more, gate smarter, learn across runs** — **Status:** Draft v0.3 **Author:** John Pletka (drafted with Claude from a goals-first analysis of the pipeline, 2026-07-02; v0.2 resolves Q1/Q2/Q5; v0.3 folds in issue #49's convergence-honesty cluster as FR-6/P7, 2026-07-05) **Date:** 2026-07-05 **Working name:** pipeline-effectiveness **Relationship to existing artifacts:** Does **not** amend `PRD-gauntlet.md` or any approved artifact. FR-4 (evidence-tiered gates) was checked against the spec's gate requirements: phase-gate policy is pipeline configuration, not spec mandate (evidence recorded at §11 Q1); PRD/plan gates and blocker escalations stay unconditionally human. FR-6 adopts the four upstreaming requests of [issue #49](https://github.com/johnpletka/gauntlet/issues/49) (convergence + confirm-pass semantics, surfaced by a real adopting-repo run). Builds on: the adversarial cycle (`engine/cycle.py`), plan phase machinery (`engine/planphases.py`, `phase_lint`), the retro/proposals loop (the spec's FR-6 machinery: `prompts/retro.md`, `gauntlet proposals`), `gauntlet trend` data, and the manifest metrics. Companion to `runs/harness-efficiency/prd.md` (plumbing hardening); this PRD changes what the pipeline *does*, that one changes how reliably it runs. Neither depends on the other.

## Phases & commits

### PRD
- `10ddfd19b4` **PRD.1** (step `prd-cycle`)

### PLAN
- `74a2861b68` **PLAN** (step `plan-author`)
- `2c19a59a56` **PLAN.1** (step `plan-cycle`)

### P1
- `e435467880` **P1** (step `phase-commit`)
- `dc7bd493a1` **P1.1** (step `impl-cycle`)

### P2
- `dedab97d65` **P2** (step `phase-commit`)
- `b73da9bab9` **P2.1** (step `impl-cycle`)

### P3
- `8e00e47173` **P3** (step `phase-commit`)
- `1a51cc9431` **P3.1** (step `impl-cycle`)

### P4
- `6c41d626d4` **P4** (step `phase-commit`)
- `bc13ee46e0` **P4.1** (step `impl-cycle`)

### P5
- `2ffc57b60e` **P5** (step `phase-commit`)
- `1d58d95d85` **P5.1** (step `impl-cycle`)
- `f60d3e8122` **P5.2** (step `impl-cycle`)

### P6
- `b3c666672e` **P6** (step `phase-commit`)
- `5eecd99c56` **P6.1** (step `impl-cycle`)

### P7
- `373963aec1` **P7** (step `phase-commit`)
- `555f73c720` **P7.1** (step `impl-cycle`)

### P8
- `60ac36b3d5` **P8** (step `phase-commit`)
- `47eff7176a` **P8.1** (step `impl-cycle`)

### P9
- `a2267085ff` **P9** (step `phase-commit`)
- `475636c87b` **P9.1** (step `impl-cycle`)

## Final per-finding verdicts (last confirm pass)

- `F-001`: **resolved** — The diff removes `carried_from` from the persisted confirm schema's `new_findings.items.required` list in both root and scaffold copies, while deriving a strict confirmer-only schema that promotes it back to required for native output. It also adds explicit validation coverage for a non-empty pre-migration `new_findings` entry without `carried_from`, so the compatibility concern is addressed.

## Transcripts

Full review→triage→fix→confirm record: [`run-2026-07-05T16-46-45/RUN.md`](run-2026-07-05T16-46-45/RUN.md).

_Plan: see `plan.md` in this directory._

# PR draft — `live-run-observability`

> Drafted by Gauntlet at the final gate (FR-9.8). **Not opened, not pushed** — opening the PR and pushing remain human actions (PRD §2.2). Edit freely before use.

- branch: `gauntlet/live-run-observability` (base `main`)
- run: `run-2026-06-26T03-33-24` — status **done**
- pipeline: `standard` v1

## Summary

**PRD: Live run observability (streamed step output)** — **Status:** Draft v0.2 **Author:** John Pletka **Date:** 2026-06-25 **Working name:** live-run-observability **Relationship to existing artifacts:** Does **not** amend any approved artifact (`PRD-gauntlet.md`, `policy.yaml`, any approved `prd.md`/`plan.md`). Builds on: the subprocess wrapper (`adapters/process.py`) and the CLI agent adapters — Claude (`adapters/claude_code.py`) and Codex (`adapters/codex.py`); the transcript

## Phases & commits

### PRD
- `fa4552d82d` **PRD.1** (step `prd-cycle`)

### PLAN
- `dab3217ec2` **PLAN** (step `plan-cycle`)
- `5553de2eaa` **PLAN.1** (step `plan-cycle`)

### P1
- `6246867793` **P1** (step `phase-commit`)
- `cf21dfd1a1` **P1.1** (step `impl-cycle`)

### P2
- `79751c4bdf` **P2** (step `phase-commit`)
- `84933069f3` **P2.1** (step `impl-cycle`)

### P3
- `417e4b5fdb` **P3** (step `phase-commit`)
- `f0b66160e0` **P3.1** (step `impl-cycle`)
- `595e4043a1` **P3.2** (step `impl-cycle`)

### P4
- `b89d478aed` **P4** (step `phase-commit`)
- `c103a495f7` **P4.1** (step `impl-cycle`)

## Final per-finding verdicts (last confirm pass)

- `F-001`: **resolved** — The diff validates the rendered step id with `safe_run_segment` before path construction and converts unsafe ids into `StatusContractError`. It also adds a resolved-path containment check under the run instance, covering symlink escape cases called out in the finding.
- `F-002`: **partially_resolved** — The diff prevents buffered adapter output and normally closed prior attempts from being reported as live progress by requiring an open-stream marker plus a non-empty file. However, the marker is only removed on `StepStream.close()`, so a killed attempt after `open_stream()` can leave both marker and events behind and still be misreported as current progress.

## Transcripts

Full review→triage→fix→confirm record: [`run-2026-06-26T03-33-24/RUN.md`](run-2026-06-26T03-33-24/RUN.md).

_Plan: see `plan.md` in this directory._

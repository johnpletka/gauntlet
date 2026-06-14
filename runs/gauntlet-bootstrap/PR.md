# PR draft — `gauntlet-bootstrap`

> Drafted by Gauntlet at the final gate (FR-9.8). **Not opened, not pushed** — opening the PR and pushing remain human actions (PRD §2.2). Edit freely before use.

- branch: `gauntlet/gauntlet-bootstrap` (base `main`)
- run: `run-2026-06-12T15-15-27` — status **done**
- pipeline: `bootstrap` v1

## Summary

**PRD: Gauntlet — Adversarial Multi-Agent Development Harness (pointer)** — The canonical, human-authored PRD for this run is `PRD-gauntlet.md` at the repository root (v1.3, authored by John). It is the spec; agents must not modify it (CLAUDE.md §8). The approved implementation plan is `runs/gauntlet/plan.md`.

## Phases & commits

### P5
- `f8de7a935b` **P5** (step `p5-commit`)
- `471bccf613` **P5.1** (step `p5-cycle`)

### P6
- `a1f606a408` **P6** (step `p6-commit`)
- `4cea8393a1` **P6.1** (step `p6-cycle`)

### P7
- `f3b03976d2` **P7** (step `p7-commit`)
- `8857c497ea` **P7.1** (step `p7-cycle`)

## Final per-finding verdicts (last confirm pass)

- `F-001`: **resolved** — The diff adds `RunManager.regenerate_proposals` and wires `gauntlet feedback` to call it after saving feedback, so feedback entered after a completed run can now reach proposal synthesis and create pending proposals. The added unit and integration coverage exercise completed run -> save feedback -> regenerate -> valid proposal.
- `F-002`: **resolved** — Proposal synthesis exceptions now persist an error file and mark the retrospective step `FAILED` by default, with continuation only behind an explicit `proposals_optional` flag. The previous fail-open test was replaced with fail-closed and explicit optional-path coverage.
- `F-003`: **resolved** — The retrospective flow now builds per-agent summaries with role-specific reviewer/fixer slices, and the synthesis summary walks per-round cycle logs instead of only latest top-level artifacts. The added test covers multiple rounds, joined triage/confirm outcomes, and differing reviewer versus fixer summaries.
- `F-004`: **resolved** — Cycle metrics now record `accepted_resolved_total` by joining confirm verdicts to the accepted finding ids for the round, and trend calculation uses accepted resolved fixes over accepted fixes. The new trend test verifies declined findings' expected unresolved confirmations no longer depress fix survival.
- `F-005`: **resolved** — `_validate_diff` now rejects diffs whose deduplicated target set is not exactly one path, closing the multi-file allowlisted diff loophole. The new unit test covers a diff touching both `prompts/triage.md` and `policy.yaml`.
- `F-006`: **resolved** — The integration test now extends the standard pipeline run with late feedback, regeneration, valid pending proposal detection, human approval, applied asset change, changelog update, and prompt hash verification when applicable. That addresses the missing real-data FR-6 acceptance path at the test level.

## Transcripts

Full review→triage→fix→confirm record: [`run-2026-06-12T15-15-27/RUN.md`](run-2026-06-12T15-15-27/RUN.md).

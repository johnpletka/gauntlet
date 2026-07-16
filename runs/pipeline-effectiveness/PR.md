# PR draft — `pipeline-effectiveness`

> Drafted by Gauntlet at the final gate (FR-9.8). **Not opened, not pushed** — opening the PR and pushing remain human actions (PRD §2.2). Edit freely before use.

- branch: `gauntlet/pipeline-effectiveness` (base `main`)
- run: `run-2026-07-05T16-46-45` — status **done**
- pipeline: `standard` v1

> ### ⚠ The run is not the whole PR — read this before the commit list
>
> **The Gauntlet run ended at `475636c` (P9.1). Twelve further commits landed
> afterward and are part of this PR.** Everything from `## Summary` to
> `## Transcripts` below is engine-rendered from the run manifest and describes
> *only* the run; it cannot describe the post-run work, because the manifest has
> no record of it. Read [Post-run commits](#post-run-commits-outside-the-run-audit-trail)
> for the rest — it includes most of the highest-risk behavior in this PR
> (verifier confinement, collector execution, the FR-4 predicate, ensemble dedup
> and metrics).
>
> This section is a **human/agent addition to the draft**, not engine output.
> It exists because review F-008 found that PR.md "presents a completed Gauntlet
> run while most high-risk final behavior was added afterward" — a true and
> material gap in the audit boundary. Re-running the generator does not close it
> (the regenerated file is byte-identical but for the PRD version), so the
> post-run record is written here by hand and marked as such.

## Summary

**PRD: Pipeline Effectiveness — catch more, gate smarter, learn across runs** — **Status:** Draft v0.5 **Author:** John Pletka (drafted with Claude from a goals-first analysis of the pipeline, 2026-07-02; v0.2 resolves Q1/Q2/Q5; v0.3 folds in issue #49's convergence-honesty cluster as FR-6/P7, 2026-07-05; v0.4 reconciles the spec with the system as built and hardened by the PR #59 review fixes, 2026-07-16; v0.5 recalibrates FR-4 against measured evidence after the shipped predicate was found to fire 0/9 on a real run, 2026-07-16)

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

## Post-run commits (outside the run audit trail)

_Human/agent addition — not engine output. The run manifest ends at `475636c`;
these twelve commits are on the branch and in this PR, and the sections above do
not cover them._

**None of these went through an adversarial fix/confirm cycle.** They are manual
review-fix commits made on the run branch after the pipeline closed, in response
to two rounds of adversarial review *of PR #59 itself*. So they carry no
`findings.json` → `triage.json` → `confirm.json` trail, no per-finding triage
verdicts, and no reviewer confirmation — the evidence this document records for
P1–P9 does not exist for them. They use this repo's `<type>:` commit convention
(CLAUDE.md §3) rather than the in-run `PN.x` format, which is correct: `PN.x`
governs phase fix commits *inside* a run, and these are outside one.

### Round 1 — 2026-07-08 (nine commits)
Addressing an adversarial review of PR #59.

| Commit | Change |
|---|---|
| `a7f949a` | **sec:** validate `carried_from` parentage (all three legs) and block remainder re-litigation — an unvalidated `carried_from` minted a triage-exempt obligation from nothing |
| `67c21bd` | **fix:** make FR-4 clean gates able to fire — zero-findings convergence parked forever on a missing triage artifact; added the evidence-freshness conjunct |
| `79146cf` | **fix:** re-verify the acceptance map after fix rounds (`acceptance-recheck`); repair a stale P9 citation |
| `e4126b1` | **fix:** sole-source ensemble yield, PRD-conformant dedup, lensed single member |
| `c0eec4a` | **sec:** server-authoritative verifier boundary — reads, network, and git refs confined; engine-held lease key |
| `5c3d62c` | **fix:** enumerate acceptance ids as an engine subprocess (no LLM in the evidence path); project-resolved command |
| `1c0269f` | **fix:** working supersession path, enforced governed-asset guard, schema compat, resource caps |
| `c9339a3` | **fix:** seed the verifier scratch HOME with the claude login surface only |
| `94ec65a` | **test:** give the live-toy e2e fixture the profiles `standard.yaml` requires |

### Round 2 — 2026-07-16 (three commits)
Addressing a second adversarial review of PR #59 (eight findings; four assessed
legitimate, the rest misdiagnosed, over-claimed, or resolved as PRD conflicts).

| Commit | Change |
|---|---|
| `5a3b0d0` | **fix:** complete-linkage dedup + member-keyed ensemble yield (review F-004, F-005). F-004 was reproduced before fixing: single linkage suppressed a finding against a primary it did not match, losing the claim before triage |
| `b139c82` | **docs:** ratify PRD **v0.4** — fold the v0.4 proposal's §§A–F into `prd.md`, absorbing the divergences a plan-level amendment could not waive (review F-001, F-002, F-006, F-007) |
| `5c740ea` | **feat:** recalibrate FR-4 and ship the phase gate (review F-003) → PRD **v0.5**. The predicate was measured against this repo's own nine-phase run and would have fired **0/9**; it is now open-based and clears 8/9 |

### Evidence at this head, stated honestly

- **Tests:** 2372 passed, 0 failed (`uv run pytest -m "not integration"`). The integration suite is **not** run here; it needs live CLI credentials.
- **CI:** none. This is not specific to this PR — the repo ships only `release.yml` and has **no test workflow**, so no PR head has ever been CI-verified. Worth fixing, separately.
- **Review status:** the round-2 findings were assessed and dispositioned, but the resulting commits are themselves **unreviewed** — the same gap this section documents, one round later.
- **Artifact of record:** `prd.md` is now **v0.5**. The `## Summary` above reflects it; `prd-v0.4-proposal.md` is retained as a ratified, historical audit trail.

### What is still open

- **F-008's full remedy is not met.** Its suggested fix asks that the post-run changes go through an adversarial fix/confirm cycle with per-finding triage and confirmation. That has not happened; this section records the gap rather than closing it. Closing it means running these commits through a review cycle — a human call, since the alternative reading is that a PR-level review (which is what produced them) is the appropriate audit boundary for post-run work.
- **FR-4 has never fired on a real run.** The recalibrated predicate is verified by replay against P1–P9 and by unit fixtures, but no live run has yet exercised the shipped `phase-gate`.

## Final per-finding verdicts (last confirm pass)

- `F-001`: **resolved** — The diff removes `carried_from` from the persisted confirm schema's `new_findings.items.required` list in both root and scaffold copies, while deriving a strict confirmer-only schema that promotes it back to required for native output. It also adds explicit validation coverage for a non-empty pre-migration `new_findings` entry without `carried_from`, so the compatibility concern is addressed.

## Transcripts

Full review→triage→fix→confirm record: [`run-2026-07-05T16-46-45/RUN.md`](run-2026-07-05T16-46-45/RUN.md).

_Plan: see `plan.md` in this directory._

---

_The verdict and transcript sections above are the **run's** record and end at
`475636c` (P9.1). The twelve
[post-run commits](#post-run-commits-outside-the-run-audit-trail) have no
equivalent trail — that is the point of that section, and the reason this file
no longer ends here._

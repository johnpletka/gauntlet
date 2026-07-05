# Run run-2026-07-05T16-46-45 — `pipeline-effectiveness`

- branch: `gauntlet/pipeline-effectiveness` (base `main`)
- pipeline: `standard` v1 (`sha256:639c398d43c4…`)
- status: **running** (at `plan-cycle`)
- totals: 449965in/173060out $16.7391

| step | type | status | duration | usage | notes |
|---|---|---|---|---|---|
| [prd-cycle](steps/prd-cycle/) | adversarial_cycle | done | 6434s | 156813in/63312out $5.5994 | converged in round 1 (blocking policy): no open blocking; 7 fixed, 0 non-blocking item(s) surfaced for the gate resume: reset round-1 worktree to the handoff (backed up at refs/gauntlet/backup/run-2026-07-05T16-46-45/prd-cycle-r1-fix-resume) before re-running the fix sub-step (FR-4.1) |
| prd-approve | human_gate | done | 7110s | 0in/0out (tokens only) | approved |
| [plan-author](steps/plan-author/transcript.md) | agent_task | done | 247s | 3228in/19232out $0.7923 | agent 'builder' completed |
| [plan-cycle](steps/plan-cycle/) | adversarial_cycle | done | 8795s | 219384in/78660out $10.3060 | converged in round 1 (blocking policy): no open blocking; 2 fixed, 0 non-blocking item(s) surfaced for the gate resume: reset round-1 worktree to the handoff (backed up at refs/gauntlet/backup/run-2026-07-05T16-46-45/plan-cycle-r1-fix-resume) before re-running the fix sub-step (FR-4.1) |
| plan-lint | phase_lint | pending | — | 0in/0out (tokens only) | phase lint: 9 phase(s) valid (P1, P2, P3, P4, P5, P6, P7, P8, P9) |
| plan-approve | human_gate | pending | — | 0in/0out (tokens only) | awaiting human decision; review: plan.md, findings.json, triage.json, confirm.json |

## Commits

- `10ddfd19b4` PRD.1 (step `prd-cycle`)
- `74a2861b68` PLAN (step `plan-author`)
- `2c19a59a56` PLAN.1 (step `plan-cycle`)

# Issue #175 — Preserve the reviewed base across cycle re-entry

Source: https://github.com/johnpletka/gauntlet/issues/175

## Cause

Fresh review invokes agents in the current checkout, but the cycle derives its
handoff from the latest manifest commit. Operator commits and bookkeeping flushes
can advance HEAD beyond that SHA. The fix step independently searches all past
commits by phase/round label; a prior attempt's `P5.1` therefore forces a reset
even after checkpoint reuse was disabled. The snapshot saves the discarded work,
but review, fix, and downstream checks no longer share the same baseline.

## P1 — Bind recovery to the actual review handoff

1. Capture the clean checkout's HEAD after bookkeeping is committed for every
   fresh review. Use that SHA for checkpoints, mutation recovery, fix recovery,
   and confirmation. Reused reviews retain their recorded handoff.
2. Keep the phase review's diff base separate from its handoff. Bookkeeping and
   operator commits must not narrow either full-phase or regression review scope.
3. Scope forced stale-fix recovery to a reused review's interrupted round.
   Historical round labels alone cannot authorize discarding committed work.
   Preserve snapshot-backed recovery of partial fixer edits and invalidated
   ordered-prefix fixes, including a kill before manifest finalization.
4. Test response, gate-reject, and plain re-entry with operator commits, tracked
   bookkeeping, and dedicated worktrees; verify exact handoff identity, prompt
   scope, ancestry, and continued checkpoint reuse after a second interruption.
5. Run unit and full integration suites, commit, run Gauntlet's independent
   review, address accepted findings, and create a PR for human review.

The user authorized one focused repair outside `gauntlet run` and use of existing
credentials for live integration and Gauntlet review. No approved PRD/plan or
judge policy changes are required. This validates FR-8.2, FR-9.3, FR-9.5, and
FR-10.4: resumability, truthful review boundaries, scoped confirmation, and human
authority over approved artifacts. Recovery snapshots remain mandatory; no
global change to the recovery planner's governed-artifact policy is proposed.

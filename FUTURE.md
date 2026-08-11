# FUTURE.md — deferred work, surfaced at gates

Items the adversarial cycle confirmed as legitimate but only *partially* resolved,
then surfaced to the human at a phase gate (convergence policy A: a major finding
gets one fix, then the human decides rather than the cycle looping). Recorded here
so a partial fix accepted at a gate is tracked, not forgotten.

## From #31 review (resume-with-response) — deferred 2026-06-24

- **F-002 [latent, deferred] — `on_fail` on a response-consuming step can strip
  the disposition gate / drop the route.** A `--response` failure is finalized
  FAILED *and* the pending response is flipped to `consumed` in the same atomic
  transaction; `_is_terminal_failure` then makes that record terminal on
  recovery (the P3-era F-002 fix, to avoid double-counting). Two interactions
  follow IF a response-consuming `agent_task` *also* carries `on_fail`: (a) an
  in-invocation retry re-runs with the response already consumed, so
  `_consuming_response` is false and the resume-disposition schema is not re-bound;
  (b) a crash between FAILED-finalize and `on_fail` routing terminates as FAILED on
  recovery instead of routing. **Not reachable in the shipped `standard` pipeline:**
  the only response-consuming step (`implement`, which parks on UPSTREAM CONFLICT)
  has no `on_fail`; `on_fail` lives on `tests` (a shell step that carries no
  response), and its route-back to `implement` happens only *after* `implement`
  already proceeded with the response consumed — so not re-binding the schema there
  is correct, not a bypass. Deferred as a latent trap for user-authored pipelines.
  Follow-up: reject (or warn on) `on_fail` attached to a step that can consume a
  `--response`, or re-bind the disposition schema whenever a still-relevant
  response is present on a retry — whichever is cheaper once a non-standard
  pipeline actually needs that shape.

## From P6 (init / doctor / packaging) — accepted at p6-gate 2026-06-12

- **F-004 [major, partially_resolved] — `doctor` CLI-auth false positive.**
  `doctor` now runs real CLI auth probes and emits FAIL/WARN rows, so a logged-out
  CLI no longer silently passes. Residual: `_real_cli_authenticated` treats any
  exit code 0 as authenticated and does not inspect Claude's in-band `is_error`
  JSON field — a CLI that returns 0 while reporting an auth error in-band is a
  false-positive path. Follow-up: parse Claude's JSON `is_error`/error envelope in
  the auth probe rather than trusting the return code alone.

- **F-005 [major, partially_resolved] — Codex hook wiring only WARNs.**
  Claude hook validation is now structural (parses JSON, requires a `*` PreToolUse
  matcher, verifies the hook command + executable, fails malformed/unwired cases).
  Residual: Codex config still only WARNs for absent / malformed / unwired hook
  config, where the original finding asked required wiring to FAIL. Partly justified
  by the pinned-Codex inert-hook note, but the required-wiring aspect is not fully
  met. Follow-up: decide whether Codex hook wiring should be a hard FAIL once the
  Codex hook surface is no longer inert, and tighten the check accordingly.

## From #8 review (`.gauntlet/` asset_root consolidation) — deferred 2026-06-14

- **F-003 [major, deferred] — `init` does not migrate a pre-existing root-layout
  repo.** `gauntlet init` unconditionally scaffolds asset targets under
  `.gauntlet/`, but a repo init'd under the previous root layout keeps
  `asset_root: "."` (its committed config is skipped as idempotent). Plain
  `init` then creates duplicate, INACTIVE `.gauntlet/` assets alongside the
  active root ones, and `init --from-repo` reports the active root assets as
  MISSING. Low real-world impact pre-1.0 (no deployed adopters on the old
  layout), so deferred rather than blocking the consolidation PR (#8). Follow-up:
  load an existing config before selecting asset targets and honour its
  `asset_root`; treat a root→`.gauntlet` migration as an explicit, atomic
  operation with legacy-layout tests.

## From run-branch-lifecycle (0.2.0) — deferred 2026-06-15

- **Worktree isolation for runs.** `gauntlet run` operates in the user's primary
  worktree (in-place `git checkout` of `gauntlet/<slug>`). Running each run in
  its own git worktree (separate directory) would mean branch switching never
  touches the user's working copy, and would enable concurrent same-repo runs.
  Deferred to its own PR: it's a larger architectural change (run cwd, adapter
  working dirs, judge `repo_root`, run-dir path resolution), and the
  stale-branch guard shipped in 0.2.0 already closes the worktree-clobber bug
  class, so isolation is defense-in-depth rather than a fix. See
  [proposals/run-branch-lifecycle.md](proposals/run-branch-lifecycle.md) §5.

## From prd-authoring-aids run (P1) — deferred 2026-06-24

- **Upstream-conflict decision mechanism — SHIPPED 2026-06-24 (PR #31).** This
  P1 park surfaced the gap: when a builder halts with an `UPSTREAM CONFLICT`
  (FR-10.4), there was no formalized way to signal the human's decision to
  `gauntlet resume` — the only workaround was manually editing the artifact and
  re-running (which the builder re-surfaced unchanged). It was specced and built
  as its own run (`runs/gauntlet-resume-response/`) and merged via PR #31:
  `gauntlet resume <slug> --response "<decision>"` records the response
  (timestamped, audited, `pending`→`consumed`) in the manifest and injects it
  into the builder's prompt so it re-evaluates rather than re-surfaces. This
  prd-authoring-aids run was the first real consumer of that mechanism. No
  follow-up remains; entry kept as the provenance trail for why the feature
  exists.

**From the harness-efficiency run (0.5.0) — recorded 2026-07-05:**

**Self-hosting driver staleness** — when a run's phases modify
`src/gauntlet/` (the engine's own source), the long-lived driver keeps its
stale imported module graph and fails on the next lazy import or behavior
seam (~once per driver lifetime; see BOOTSTRAP-NOTES 2026-07-02..03). 0.5.0
made every *consequence* recoverable (usage parks, respondable commit steps,
checkpoints) but the hazard itself needs one of: phase-boundary driver
restarts when target repo == engine repo; running the driver from an
installed snapshot; or a source-tree-hash park as a detection floor.
Candidate for a small PRD or a `pipeline-effectiveness` follow-up — an engine
lifecycle change, so it wants adversarial review, not a quick patch.

**From the recovery-redesign run (P6 review) — recorded 2026-08-04:**

**Journal rebuild for out-of-repository state dirs** — P6 makes the
append-only journal authoritative for every run whose manifest goes through
`Manifest.write_atomic`, including lightweight `gauntlet review` runs. Review
runs now reconcile (genesis, catch-up, out-of-band preserve/restore,
torn/duplicate quarantine) on their mutating resume path and read the
authoritative head during discovery. The remaining half is the
missing/corrupt-projection **rebuild**: `RebuildProjectionAction` requires
repo-relative contained `journal_path`/`projection_path` (the P1 containment
validator), so a review state dir outside the repository cannot express one
and `projection_rebuild_assessment` returns `None` there. Such a run degrades
to exactly its pre-P6 behavior (an unloadable manifest is as unrecoverable as
it was before the journal existed) — never worse. Closing it means either a
contained non-repo-relative action payload or a separate file-plane rebuild
verb; both change a P1 model or add a verb, so they want the recovery plan's
own review loop rather than an in-phase patch.

**From PR #87 review (P7 verification gate) — recorded 2026-08-07:**

**Ref restoration is a guarded git command, not a driven Gauntlet
transaction** — the review's third P1 asked for two things, and the fix
delivered one of them. Both branch-relation restore forms now carry the
observed ref value and render a compare-and-swap (`git update-ref <ref> <new>
<expected>`, empty expected for the create-only forms), so a ref that moved
between the assessment and the operator running the printed command makes git
refuse the stale update atomically instead of rewinding a tip and orphaning
its commits. `ContinueOnRecoveryBranchAction.requires_snapshot` was corrected
to `False` to match: with the guard, every form either creates a ref that did
not exist or fast-forwards one that is provably still where it was observed,
so there is nothing for a pre-mutation snapshot to preserve.

What is deferred is the other half — rendering a *Gauntlet verb* that
re-observes under the run lock and applies the restoration through
`RecoveryExecutor`, with an intent, a journal audit record and the §6.4
evidence every other mutating verb leaves. That is a new mutating verb and a
new executor site (the existing `apply`/`apply_rebuild` sites are the Git
rewind and file planes; a ref plane is a third), so it wants the recovery
plan's own review loop rather than an in-phase patch. Until it exists, the
restoration's audit trail is the command in the `status` output plus git's own
reflog, and its safety is structural rather than archival. Natural home: the
§10 closing tranche alongside the fault-injection matrix.

## From #103 (agent-liveness watchdog) — deferred 2026-08-10

- **Post-recover/watchdog resume restarts the whole cycle round instead of the
  interrupted sub-step.** When an impl-cycle fix sub-step is interrupted (by §4
  `recover` or the new agent-liveness watchdog), a plain `resume` re-drives the
  cycle from the round's first incomplete checkpoint — the r1 reviewers and
  triage re-run from scratch even though their verdicts are already journaled,
  costing a full review round. Resuming at the interrupted sub-step (r1-fix,
  seeded with the prior triage verdicts) would save that round. Deferred from
  the #103 fix because it changes the cycle's checkpoint/re-entry model
  (`cycle.py` `_Resume` granularity plus the FR-9.3 clean-handoff interaction
  with the dead agent's uncommitted paths) — a state-machine redesign that
  wants its own review loop, not a rider on a liveness-watchdog patch.

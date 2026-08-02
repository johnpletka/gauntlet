# Gauntlet Recovery Redesign Plan

**Status:** Proposed implementation plan; not an approved PRD or run artifact.

**Prepared from:**

- GitHub issues #61, #62, #63, #64, #65, and #72.
- PR #76 (merged) and PR #77 at `177d721` (open when this plan was written).
- The earlier three-PR plan at
  `/Users/johnpletka/.claude/plans/pure-floating-reddy.md`.
- A static audit of the recovery, rollback, resume, adversarial-cycle, Git, and
  operator-classification paths on `fix/recover-rollback-reconcile`.

**Primary goal:** A Gauntlet run must be recoverable from ordinary operational
failures without silent data loss, forbidden manual Git surgery, placebo human
responses, or successful no-op resume loops.

The core pipeline remains:

```text
implement phase
  -> review
  -> fix accepted findings
  -> confirm
  -> repeat fix/confirm as needed
  -> next phase
```

Failures in infrastructure, generated artifacts, local process lifetime, Git
bookkeeping, or state persistence must interrupt that loop safely, not wedge it.

---

## 1. Disposition of the current work

Do not merge PR #77 in its current form. Keep it available as implementation
history, but replace its lossy preservation mechanism before relying on any new
rewind path.

Retain these ideas from PRs #76/#77:

- Engine-bookkeeping-only commits are not partial implementation work.
- `base_sha` belongs to one step attempt, not the whole phase.
- Recovery and rewinds create durable backup references first.
- Dirty/interrupted diagnostics name the exact evidence.
- `resume --reset-interrupted` is a valid operator action.
- Rollback targets the run branch explicitly and accepts a strictly-ahead run
  branch when the operator explicitly requests a rollback.
- Recovery finalization must complete even if optional Git evidence collection
  fails after the target process is dead.

Replace or redesign:

- `worktree_overlay` and `restore_overlay`.
- `backup_dirty_worktree` mutating the real index with `git add -A`.
- Direct checkout/reset/clean sequences distributed across `run.py`,
  `orchestrator.py`, and `cycle.py`.
- Separate recovery decisions in `operator.py` and the mutating resume path.
- The assumption that one backup tree can represent HEAD, index, and worktree.
- The assumption that every adapter error that is not a pinned quota marker is
  semantically terminal.

Do not close, rewrite, or supersede PR #77 automatically. The human operator
chooses whether the new implementation lands as additional commits on PR #77
or as a replacement PR stack.

---

## 2. Recovery principles

### R1. Fail closed without wedging

Fail closed means that Gauntlet does not perform an irreversible or unbacked
mutation while state is uncertain. It does not mean withdrawing every mutating
verb indefinitely.

Every persisted nonterminal state with no live driver must expose at least one
safe executable action:

- retry the same side-effect-free operation;
- resume an existing agent session;
- continue from a committed checkpoint;
- adopt observed work as a new attempt boundary;
- snapshot and restart an attempt;
- restore a prior snapshot;
- continue on a recovery branch/fork; or
- abort while retaining all snapshots and evidence.

### R2. Preserve before mutation

Before checkout, reset, clean, branch movement, index replacement, or any other
operation that can discard or obscure work, create a durable recovery snapshot
covering every affected state plane.

### R3. Snapshot creation is observational

Creating a snapshot must not change HEAD, a branch ref, the real index, the
working tree, staging state, or file types.

### R4. One recovery assessment

`status`, `resume`, `recover`, and `rollback` must consume the same recovery
assessment. The read-only operator view cannot call a state unsafe while the
mutating path silently proceeds, or recommend a resume that the mutating path
will reject or no-op.

### R5. No successful no-op loops

A mutating command may not exit successfully when it returns to the same
progress fingerprint without entering a legitimate live wait. It must either
make progress or return a nonzero actionable explanation.

### R6. Branch/manifest disagreement is evidence

A linear branch-ahead state, a human commit, a killed process, or an incomplete
manifest flush is a reconciliation input, not automatically corruption.

### R7. Human decisions are semantic

`--response` is reserved for decisions such as resolving a finding,
ratification conflict, or upstream ambiguity. Network retries, provider
outages, timeout retries, and session recreation never require invented human
decision text.

### R8. Active control state is not controlled history

The authoritative execution state must eventually live outside the run branch
that rollback/reset manipulates. Committed manifests may remain audit exports,
but resetting implementation history must not reset the state machine itself.

### R9. Approved artifacts remain governed

Recovery may preserve or detect edits to approved PRDs/plans, but it may not
silently adopt them. Existing FR-10.4 governance still applies: surface an
upstream conflict and require the artifact's own review loop and gate.

---

## 3. Why the current model is insufficient

The current preservation model reduces an affected path to:

```text
path -> working-tree bytes | deleted
```

Git actually has independent state for:

- checked-out branch and HEAD;
- run-branch ref;
- real index, including staged changes and unmerged conflict stages;
- working-tree bytes;
- tracked and untracked paths;
- deletions and renames;
- executable modes;
- regular files versus symbolic links;
- ignored but deliberately protected human files;
- commits ahead of, behind, or forked from the recorded boundary.

A single tree also cannot represent staged version B and working-tree version C
at the same path. Refusing that state would be a safe temporary guard but would
not satisfy the recovery goal. Both states must be preserved separately.

Recovery policy is also distributed across five modules:

- `run.py`: resume, recover, rollback, locks, branch checks;
- `orchestrator.py`: attempt state, resume disposition, finalization;
- `cycle.py`: fix-resume and reviewer-mutation rewinds;
- `gitops.py`: dirty checks, backup, checkout, reset, clean;
- `operator.py`: composite classification and next actions.

This distribution produced the reported contradictions and should be replaced
with a shared recovery observation/planning layer.

---

## 4. Target architecture

### 4.1 Recovery observations

Add immutable observation models, likely in a new
`src/gauntlet/engine/recovery.py` module:

```python
class GitObservation:
    checked_out_branch: str | None
    head_sha: str
    run_branch: str
    run_branch_sha: str | None
    recorded_sha: str | None
    branch_relation: str
    index_fingerprint: str
    worktree_fingerprint: str
    dirty_entries: list[GitEntryObservation]

class StateObservation:
    run_status: str
    step_status: str | None
    attempt_id: str | None
    liveness: str
    pending_response_id: str | None
    last_snapshot_id: str | None
    artifact_fingerprint: str | None

class RecoveryAssessment:
    cause: str
    disposition: str
    evidence: list[str]
    safe_actions: list[RecoveryAction]
    recommended_action: str | None
    progress_fingerprint: str
```

`branch_relation` should be a closed enum such as:

```text
equal
engine_bookkeeping_ahead
checkpoint_ahead
operator_ahead
mixed_ahead
behind
forked
missing
```

`cause` and `disposition` are orthogonal. Suggested causes:

```text
none
provider_unavailable
quota_exhausted
process_lost
artifact_invalid
precondition_unsatisfied
worktree_partial
branch_ahead
branch_diverged
state_inconsistent
policy_denied
internal_error
```

Suggested dispositions:

```text
continue
retry
resume_session
edit_then_retry
restart_from_checkpoint
adopt_commits
snapshot_and_restart
restore_snapshot
continue_on_recovery_branch
human_decision
abort_only
```

### 4.2 Recovery planner

Implement a pure or read-only `RecoveryPlanner.assess(...)`. It receives the
manifest/state observation, liveness, pipeline metadata, and Git observation.
It performs no checkout, staging, reset, cleanup, or file writes.

All four public workflows use it:

```text
status   -> assess -> render
resume   -> assess -> choose/default action -> apply
recover  -> stop/finalize process -> assess -> snapshot/normalize
rollback -> assess target and preconditions -> snapshot -> apply
```

`operator.py` renders `assessment.safe_actions`; it must not maintain an
independent table that can drift from resume behavior.

### 4.3 Recovery executor

Implement a single mutation gateway:

```python
class RecoveryExecutor:
    def apply(self, assessment, action) -> RecoveryResult:
        ...
```

The executor follows this order:

1. Acquire the run/worktree lock.
2. Re-observe and confirm the assessment fingerprint still matches.
3. Resolve and validate all refs, target phases, paths, and policies.
4. Create and durably record a complete recovery snapshot when the action can
   mutate Git state.
5. Persist a recovery intent containing action, snapshot ID, preconditions,
   and idempotency key.
6. Apply checkout/reset/restore/adopt operations.
7. Persist the resulting state transition.
8. Clear the intent only after the result is durable.

A surviving intent is replayed idempotently on the next mutating command.

### 4.4 Complete Git recovery snapshots

Introduce a `GitRecoverySnapshot` abstraction in `gitops.py` or a dedicated
`git_snapshot.py` module. A snapshot record includes:

```text
snapshot_id
run_id
attempt_id
reason
created_at
checked_out_branch
head_sha
run_branch
run_branch_sha
raw_index_blob_oid
index_tree_or_null
worktree_tree
snapshot_commit
protected_ignored_paths
```

Snapshot construction:

1. Resolve HEAD and all relevant refs without checkout.
2. Hash and store the raw real-index bytes as a Git blob. This preserves
   unmerged index stages that cannot be represented by `write-tree`.
3. When `git write-tree` succeeds, record the normal index tree too.
4. Create a temporary index using `GIT_INDEX_FILE`; never use the real index.
5. Seed the temporary index from HEAD.
6. Stage the current working tree into the temporary index using Git itself.
   Git records symlinks as symlinks and preserves executable modes.
7. Explicitly force-include protected ignored paths that a subsequent operation
   can affect, while excluding active engine state.
8. Write the worktree tree.
9. Create a snapshot commit whose metadata references the raw index blob and
   whose parent chain keeps the original HEAD/index/worktree objects reachable.
10. Atomically create `refs/gauntlet/recovery/<run>/<snapshot>` only after all
    required objects exist.

The exact object layout may vary, but one root ref must retain every object
needed for exact restoration.

Restoration must use Git plumbing/materialization, not `Path.write_bytes()`:

- restore working-tree entries from the snapshot's temporary index/tree;
- let Git recreate symlinks and modes;
- restore the raw index bytes last when exact staging/conflict state is wanted;
- validate every destination is inside the repository;
- never write through a pre-existing symlink;
- make restoration idempotent.

### 4.5 Progress fingerprint

Before and after every mutating command, compute a stable fingerprint from at
least:

```text
run id
current step and iteration
attempt id
run/step status
run-branch SHA
index fingerprint
worktree fingerprint
artifact fingerprint
pending response id/state
latest completed cycle substep
```

If a command returns to the same fingerprint and is not deliberately waiting
for a quota/dependency deadline, raise a `NoProgressError`. The error must name
what is unchanged and list executable next actions. Never print only
`run status: parked` and exit zero.

### 4.6 Append-only attempt journal

Add an append-only journal for authoritative state transitions. Use atomic
individual event files or another inspectable atomic format; do not depend on a
single rewritable JSON document for crash recovery.

Suggested events:

```text
AttemptStarted
AgentCallStarted
AgentCallFinished
CheckpointObserved
ArtifactValidationFailed
DependencyUnavailable
AttemptInterrupted
RecoverySnapshotCreated
RecoveryActionPlanned
RecoveryActionApplied
StepCompleted
RunStatusChanged
```

Each event includes:

```text
schema version
monotonic sequence
event id
run id
step/iteration
attempt id
timestamp
observed branch SHA
idempotency key
event-specific payload
```

`manifest.json` becomes a regenerated projection of the journal plus current Git
evidence. Keep backward-compatible manifest fields during migration.

Initially the journal can live under the existing ignored run-instance state
directory so reset/clean operations do not touch it. A later phase may anchor
state snapshots under a separate `refs/gauntlet/state/<run>` ref.

Moving authoritative state off the tracked run branch changes the present PRD
interpretation. Do not edit `PRD-gauntlet.md` automatically. Stop for explicit
human ratification before implementing that migration phase.

### 4.7 Dedicated run worktree

The target design gives each active run its own Git worktree and branch. The
operator's current checkout, staged changes, and branch selection then cannot be
altered by resume or rollback.

Before implementation, perform a design spike to choose a contained configurable
worktree root and verify nested/adopter repository behavior. This phase also
requires explicit human approval because it changes machine/worktree layout.

---

## 5. Required behavior by incident class

### 5.1 Invalid YAML, schema errors, and `Phase 1` versus `P1`

- Classify as `artifact_invalid`, not terminal adapter failure.
- Record the responsible artifact path, validator, exact diagnostic, and content
  fingerprint.
- If the artifact is unchanged, plain resume returns nonzero and explains that
  rerunning would produce the same result.
- If the artifact changed, rerun only the validator and then continue.
- Before approval, route validator diagnostics back into the artifact's existing
  author/review/fix loop where possible.
- After approval, an edit is an upstream invalidation and must go through the
  existing human decision and artifact-review gate.
- Do not leave `RUN_RUNNING` after a parser exception. Parser/validator failures
  must produce one coherent step/run transition.

### 5.2 Network loss, provider outage, timeout, 429, and 5xx

- Treat transport/dependency failures separately from semantic agent failures.
- Recognize typed timeout, connection, DNS, rate-limit, service-unavailable, and
  server-error envelopes for API adapters.
- For CLI adapters, preserve captured evidence and classify known dependency
  failures from pinned structured events.
- Use bounded retries with persisted attempt counts, exponential backoff, jitter,
  and structured `Retry-After` when present.
- After retry exhaustion, park as `provider_unavailable` or
  `quota_exhausted`; plain resume retries without `--response`.
- For fan-out, persist successful leaves and retry only incomplete leaves.
- Write the failure event in the failing leaf's directory so `logs` cannot point
  at a successful sibling.
- For unknown adapter failures, do not decide retryability from the exception
  name alone. Assess whether Git/worktree side effects occurred. Side-effect-free
  calls may retry; side-effecting calls require snapshot/reconciliation first.

### 5.3 Laptop sleep, process death, SIGTERM, and SIGKILL

- A dead driver with an attempt-start event but no terminal event is interrupted,
  not unknown.
- Persist step outcome and run outcome as one logical transition.
- Keep the classifier fallback for historical/incomplete manifests:
  `running + exactly one ended interrupted/halted/failed step + dead driver`
  maps to the corresponding recoverable state.
- Reconcile the attempt from Git evidence:
  - no changes: retry at a newly stamped attempt boundary;
  - known checkpoint commits: continue from the latest valid checkpoint;
  - dirty worktree/index: snapshot, then offer session continuation or clean
    restart;
  - committed but unmanifested phase/fix work: adopt or explicitly rollback;
  - unknown human commits: classify as operator work and preserve/adopt subject
    to approved-artifact governance.

### 5.4 Manual repairs and branch/manifest mismatch

- A linear descendant range is inventoried by commit identity, subject, changed
  paths, and known attempt/checkpoint metadata.
- Recognized `P<N> wip`, phase, or fix commits can be adopted automatically when
  they match the active phase/attempt.
- Unknown commits are treated as operator work, never engine bookkeeping.
- Resume may adopt an operator commit as the next attempt's base when it does not
  modify an approved artifact.
- Explicit rollback may discard descendant commits only after a complete
  snapshot and a loud audit record, because rollback itself authorizes rewind.
- Internal cycle resets may not silently discard operator commits merely because
  they are descendants of a handoff.
- Forked/behind/missing branches must offer a recovery ref/branch workflow, not
  only “reconcile manually.” Preserve every observable tip, then allow restore or
  continuation on a recovery branch.

### 5.5 Manifest inconsistency or corruption

- Rebuild the manifest projection from the append-only journal and Git evidence.
- Never require hand-editing `manifest.json` as the primary repair mechanism.
- Preserve the malformed/original manifest as recovery evidence.
- If journal and Git evidence remain genuinely ambiguous, snapshot both and park
  with explicit adopt/restore/fork choices.

---

## 6. Phased implementation

Each phase ends with the full required test gate and a review handoff. Do not
start the next phase until the current phase is reviewed and accepted. Follow
the repository's commit-message and review-fix conventions in `AGENTS.md`.

### P1 — Establish recovery invariants and test harness

Deliverables:

- Add `recovery.py` observation/assessment/action models with closed enums.
- Add the progress-fingerprint model and `NoProgressError` contract without yet
  changing all public behavior.
- Create fixture helpers capable of constructing Git states across HEAD, index,
  worktree, untracked paths, deletions, modes, symlinks, and branch relations.
- Add table-driven tests for the invariants in Section 2.
- Add a regression test proving staged B plus worktree C is distinct state.
- Add a regression test proving an outside-target symlink is never followed.
- Add a regression test proving prevalidation failure leaves the checked-out
  branch unchanged.

Acceptance:

- The models can represent every incident in Section 5.
- Tests fail against the current overlay/checkout behavior where expected.
- No production rewind behavior changes yet.

### P2 — Implement lossless Git recovery snapshots

Deliverables:

- Implement `GitRecoverySnapshot` with temporary-index support.
- Extend the Git runner narrowly to accept a controlled environment containing
  `GIT_INDEX_FILE`; do not expose arbitrary environment injection.
- Preserve raw index bytes/blob plus normal index tree when available.
- Preserve worktree tree, untracked affected paths, deletions, modes, symlinks,
  and protected ignored paths.
- Implement exact/idempotent restoration.
- Replace `backup_dirty_worktree`, `worktree_overlay`, and `restore_overlay` at
  one pilot rewind path.
- Never mutate the real index during snapshot creation.

Acceptance:

- Snapshot creation leaves `git status --porcelain=v2` and current branch
  byte-for-byte/semantically unchanged.
- Restore reproduces staged B and worktree C separately.
- Restore reproduces file deletion, untracked file, executable bit, and symlink.
- A crash after snapshot ref creation retains all data needed to restore.
- A snapshot failure occurs before any destructive mutation.

### P3 — Centralize every rewind behind the recovery executor

Deliverables:

- Implement `RecoveryPlanner` and `RecoveryExecutor` for Git mutation paths.
- Convert rollback, interrupted restart, conflict-park cleanup, fix-resume reset,
  and reviewer-mutation revert to the shared transaction.
- Validate target phase, branch ref, ancestry, artifact governance, and snapshot
  viability before checkout.
- Re-observe under the lock before applying a planned action.
- Persist and reconcile recovery intents idempotently.
- Remove direct reset/checkout/clean sequences from callers where the executor
  owns the operation.
- Remove the overlay helpers after the last caller migrates.

Acceptance:

- F-002, F-003, and F-004 from the post-`177d721` review are resolved by design.
- All rewind sites have the same precondition/snapshot/apply ordering.
- Killing the process at every transaction boundary either leaves the original
  state untouched or leaves a durable snapshot and replayable intent.

### P4 — Unify status, resume, and kill-window reconciliation

Deliverables:

- Make step terminalization and run-status change one logical durable transition.
- Fix the short-circuit finalization hole.
- Recognize the historical `RUN_RUNNING + interrupted/failed step + dead driver`
  shape as recoverable.
- Make `operator.py` render the planner's safe actions.
- Use progress fingerprints to prevent exit-zero re-park/no-op loops.
- Reconcile linear branch-ahead states into checkpoint, operator, mixed, or
  bookkeeping categories.
- Complete issue #62 bug 2 and the reconciliation portion of #72.

Acceptance:

- Real subprocess SIGTERM/SIGKILL tests at each persist boundary recover.
- `status` and `resume` agree on every table row.
- Every dead-driver row has a safe mutating action.
- Repeating resume against an unchanged deterministic failure returns actionable
  nonzero output instead of success with unchanged state.

### P5 — Recover artifact and infrastructure failures

Deliverables:

- Add orthogonal failure cause/disposition fields with backward-compatible
  manifest loading.
- Make plan/YAML/schema/lint failures uniformly `artifact_invalid` with content
  fingerprints.
- Route pre-approval artifact defects back into their author/fix loop.
- Add dependency retry policies for timeout, network, 429, and 5xx failures.
- Persist fan-out leaf completion and retry only incomplete leaves.
- Correct logs selection to point to failing evidence.
- Keep `--response` only for semantic decisions.

Acceptance:

- Replay issues #63 and #64 end-to-end.
- Simulated provider outage, network loss, timeout, and quota exhaustion all park
  or retry appropriately and resume without synthetic human text.
- Invalid YAML and invalid phase IDs recover after a file edit; unchanged files
  do not enter a no-op loop.

### P6 — Add append-only authoritative state journal

This phase requires explicit human ratification before implementation because it
changes how the PRD's manifest requirement is realized.

Deliverables:

- Persist append-only attempt and recovery events atomically.
- Regenerate manifest projection from the journal.
- Reconcile or finalize a partially written event through idempotency keys.
- Migrate existing manifests into a journal genesis event without rewriting
  approved artifacts.
- Stop relying on engine bookkeeping commits as the sole durable state source.

Acceptance:

- Delete or corrupt the manifest projection and rebuild it exactly.
- Reset the run branch without losing authoritative execution state.
- Crash between every event/manifest projection boundary and recover.
- Existing manifests remain loadable and migratable.

### P7 — Isolate runs in dedicated Git worktrees

This phase requires an approved design spike and explicit human permission for
the chosen worktree/state-root layout.

Deliverables:

- One dedicated worktree and run branch per active run.
- Resume/rollback operate on the run worktree regardless of the operator's
  current checkout.
- Lifecycle management for creation, discovery, stale cleanup, and recovery.
- Migration path for existing same-worktree runs.

Acceptance:

- Starting, resuming, recovering, and rolling back a run never changes the
  operator's checked-out branch, index, or worktree.
- Concurrent different-run operations cannot target the same worktree.
- A missing run worktree can be recreated from refs plus journal state.

---

## 7. Test matrix

Do not add only one regression test per latest report. Use pairwise or targeted
cross-product parametrization over these dimensions.

### Branch state

- run branch checked out;
- another branch checked out;
- detached HEAD;
- run branch equal to manifest;
- engine bookkeeping ahead;
- wip/checkpoint ahead;
- phase/fix commit ahead;
- operator commit ahead;
- mixed ahead;
- behind;
- forked;
- missing.

### Index/worktree state

- clean;
- staged only;
- unstaged only;
- staged and unstaged same path with different content;
- unmerged/conflict index;
- tracked deletion;
- untracked file and directory;
- rename;
- executable-bit change;
- regular file changed to symlink;
- symlink changed to regular file;
- symlink pointing inside repository;
- symlink pointing outside repository;
- ignored protected PR.md.

### Failure/interruption state

- invalid YAML;
- invalid plan phase ID;
- missing artifact/schema key;
- provider timeout;
- DNS/connection failure;
- 429/quota;
- 500/503 overload;
- session missing on resume;
- SIGTERM/SIGKILL before and after every durable boundary;
- laptop-sleep/heartbeat orphan simulation;
- commit landed before manifest/event flush;
- manifest/event landed before branch mutation;
- human manual commit/edit while parked.

### Transaction fault injection points

- before observation;
- after observation, before lock revalidation;
- before and after each snapshot object;
- before and after snapshot ref creation;
- before and after recovery-intent persist;
- before and after checkout;
- before and after reset/clean;
- before and after worktree/index restoration;
- before and after action-applied event;
- before and after manifest projection.

### Global acceptance property

For every supported combination and injected crash point:

> Repeated `recover`, `status`, and `resume` either complete the next safe
> transition or return a specific executable recovery action. No observable Git
> state is silently lost, no outside-repository path is written, and no mutating
> command exits successfully with an unchanged progress fingerprint.

---

## 8. Migration and compatibility

- Add manifest fields only as optional/backward-compatible until a ratified schema
  migration says otherwise.
- Old manifests without attempt IDs get deterministic legacy attempt IDs during
  observation/projection; do not rewrite them during read-only status.
- Existing `refs/gauntlet/backup/...` remain valid recovery inputs.
- New snapshots use `refs/gauntlet/recovery/...` and include explicit metadata
  distinguishing the complete snapshot format.
- `resume --reset-interrupted` remains supported but becomes a thin selection of
  `snapshot_and_restart` through the shared executor.
- Existing CLI output can gain fields/actions additively; scripts using
  `status --json` must receive a schema-compatible migration.
- Keep explicit support for historical bookkeeping commits until all active runs
  have migrated away from branch-coupled control state.

---

## 9. Implementation cautions

- Never use the real index as scratch space.
- Never assume `write-tree` succeeds; an unmerged index is valid recoverable
  state and requires the raw-index snapshot.
- Never use `Path.is_file`, `read_bytes`, or `write_bytes` to preserve a Git
  entry without first handling symlinks and entry kind explicitly.
- Never checkout merely to inspect a ref. Accept explicit refs in Git helpers.
- Never infer engine ownership from subject text alone; retain identity and
  changed-path validation while historical bookkeeping exists.
- Never treat a backup ref as sufficient if it represents only HEAD and not the
  index/worktree state that a subsequent command can destroy.
- Never let optional evidence gathering prevent process-death finalization, but
  never permit a destructive follow-up action unless its required snapshot is
  durable.
- Never auto-adopt changes to an approved PRD/plan.
- Never use `--response` to encode retry intent.
- Never clean or reset paths outside the recovery executor after P3.

---

## 10. Definition of done

The recovery redesign is complete when all of the following are true:

1. Issues #62, #63, and #72 have end-to-end regression coverage and are closed.
2. The scenarios in issues #61, #64, and #65 remain covered and do not regress.
3. F-002, F-003, and F-004 from the final PR #77 review are structurally
   impossible under the shared snapshot/executor design.
4. Every public recovery recommendation is generated from the same assessment
   used to authorize the action.
5. No direct destructive Git operation remains outside the audited transaction
   layer, except narrowly documented initialization/maintenance operations.
6. Full unit and integration suites pass at every phase handoff.
7. Fault-injection tests demonstrate recovery across all durable boundaries.
8. A real dogfood run survives at least:
   - provider disconnection during review/triage;
   - process kill during builder work after a wip commit;
   - staged-plus-unstaged manual repair;
   - invalid plan YAML repaired by a human;
   - branch-ahead-of-manifest reconciliation;
   - rollback from a different operator checkout;
   without manual `git reset --hard`, lost work, synthetic response text, or a
   successful no-progress loop.

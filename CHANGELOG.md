# Changelog

All notable changes to Gauntlet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `gauntlet report` now ends with a clock-time section answering "where did
  the time go?": the run's overall wall-clock span split into disjoint agent
  time (the union of adapter-call intervals), parked (by park reason — gate,
  usage limit, response,
  provider unavailable — replayed from the run's state journal, never
  estimated), host-suspended, and the remaining engine/git/test/gap time; plus
  clock time per step (with a review/triage/fix/confirm/verify breakdown for
  each cycle), per agent profile (mapped to its adapter/model) and per
  activity pooled across cycles. Per-profile/activity rows retain
  **agent-seconds**, so concurrent work stays visible without corrupting the
  overall remainder. The evidence is a new append-only
  `invocations` list on each manifest step record — one engine-measured
  entry (UTC start/end, wall seconds, agent, label, outcome, attempt) per
  adapter call — so a CLI that exports no timing of its own (Codex) is
  measured exactly like one that does (Claude Code). Failed, malformed and
  retried calls are recorded too: their time is real time. Runs recorded
  before this change show `—` for agent time; runs without a journal show `—`
  for parked time.

- Each invocation also freezes the **adapter, model and effort** that actually
  ran (from the profile and the built adapter, with any step-/cycle-level
  `effort:` override), so a later `config.yaml` edit can never re-attribute a
  past run; the time report shows what ran rather than what is configured
  today. The raw provider token counters that the dollar figure hides under
  subscription auth are now carried through every accumulator into the
  manifest (per step, per profile, run totals) and the cost report:
  prompt-cache **writes** (Claude Code `cache_creation_input_tokens`) and
  **reasoning output** (Codex `reasoning_output_tokens`, API
  `completion_tokens_details.reasoning_tokens`; the API adapter also picks up
  `prompt_tokens_details.cached_tokens`). All additive; older manifests load
  with zeros.

- `gauntlet run` snapshots the **effective run configuration** into the run
  dir as `config.yaml` beside `pipeline.yaml` — every profile's adapter, model,
  effort, timeouts, tool allowlists and sandbox mode with defaults made
  explicit, written through the redacting writer. Evidence only: the engine
  never reads it back.

### Changed

- `keep_awake` now defaults to **true** (#134). Host sleep was measured as a
  leading source of park latency (58 minutes of one run, and a driver wedge on
  another), and the `caffeinate -i -w <pid>` assertion is scoped to the
  driver's lifetime so nothing lingers. Set `keep_awake: false` to opt out.
  Off darwin the knob is a no-op; the "ignored on this platform" warning now
  fires only when it was set explicitly. `gauntlet doctor` gains a
  `keep-awake` check that warns (never fails) when the knob is off on darwin
  or `caffeinate` is not on PATH.
- Scheduled auto-resume now covers **provider-unavailable parks**, not only
  usage-limit ones (#134, rec. 1a). A new `resume_on_provider_unavailable`
  knob (`notify` default | `auto`, validated exactly like `resume_on_quota`)
  arms the same in-process wait loop on a `provider_unavailable` park, using
  the backoff / Retry-After deadline the park already records; the two knobs
  share `max_auto_resume_attempts` and the keep-awake / external-scheduler
  survival warning, and each governs only its own park reason (a knob flipped
  back to `notify` stops the loop at its next decision). Exhaustion leaves a
  plain `provider_unavailable` park with the usual note. The manifest's
  `scheduled_resume` gains an additive `reason` field recording which park
  armed it, `status --json` exposes the armed schedule as a new always-present
  nullable `scheduled_resume` object (`attempt_at`, `attempts`,
  `max_attempts`, `reason`; schema_version stays 1), and the human `status`
  footer prints `auto-resume scheduled at <iso> (attempt n/m, <reason>)`. No
  provider health probe is attempted — the recorded deadline is the only
  signal (fail-closed).
- **Notifications are pushed by the driver itself** (#134, recs 6 and 10).
  Every run transition that needs a human — gate reached, escalation, decision
  park, usage-limit / provider-unavailable / usage-window / invalid-artifact
  park, halt, failure, completion — is emitted the instant the driver persists
  it, on every driving verb (`run`, `resume`, the in-process auto-resume,
  `approve`, `reject`), to macOS desktop, a Slack incoming webhook and/or a new
  generic JSON webhook (`notify.webhook_url` / `GAUNTLET_NOTIFY_WEBHOOK`).
  Detection latency no longer depends on a resident `gauntlet serve`. The
  channel primitives, kind table and classifier moved to
  `gauntlet.engine.notify` (the console re-exports them); the classifier now
  maps every park class by its persisted `parked_reason` and a halt to its own
  `run-halted` kind. A gate-reached notification carries a pre-built review
  bundle — `git diff --stat` of the reviewed range, finding/triage counts,
  spend and elapsed time, and the exact next command — assembled read-only by
  the new `gauntlet.engine.gate_evidence`. Each emission is appended to the
  run's `notifications.jsonl` ledger; the console notifier consults the same
  ledger, so driver and console never double-fire and a restarted emitter never
  re-announces. New engine `notify:` config block (`desktop`, `slack`,
  `webhook`, `slack_webhook`, `webhook_url`, optional `kinds` allowlist;
  defaults are safe no-ops); an absent `web.notify` inherits it.
  `GAUNTLET_NOTIFY_DISABLED=1` is the driver-side kill switch.
- **`gauntlet sweep`** — the unattended, judgment-free resume sweep (#134,
  rec. 1b). A dead driver cannot self-resume, so a resident process (the
  console's timer, or cron/launchd with `external_scheduler: true`) runs a
  sweep that takes only the two actions the operator playbook already classes
  as no-decision: reclaim an **orphaned** run whose drive lock proves the
  driver dead or PID-reused, and fire a parked step's armed, **due**
  `scheduled_resume` under the config knob that armed it. The decision is a
  pure function of the same composite state `status` renders plus the lock
  proof, the schedule and the config (table-tested, reason-agnostic); gates,
  response parks, failures, indeterminate liveness, malformed locks, live
  drivers and terminal runs are skipped with a one-line reason. Each action
  re-verifies and stamps `unattended sweep resumed (<reason>) at <iso>` under
  the drive lock (a schedule attempt is counted write-ahead), then goes
  through the real `RunManager.resume` — in the foreground for one slug, or
  as a detached `gauntlet resume <slug>` child (log in
  `<run_dir>/sweep-resume.log`) with `--all`. `--json` emits one object per
  run; exit 0 whether or not anything was resumed. `gauntlet serve` runs the
  same sweep on `web.sweep_interval_s` (default 120 s; 0 disables),
  launching console-owned drivers. README documents the cron/launchd recipe.
### Fixed

- `phase-commit` no longer fails "clean worktree, nothing to commit" when the
  builder's `P<N> wip:` checkpoints sit beneath an adopted commit (#134,
  rec. 4). The trailing-run checkpoint discovery stops at the first
  non-checkpoint commit, so an operator pre-commit that `resume` adopted (or
  an adopted fix round) hid every checkpoint beneath it; adoption had also
  re-anchored `base_sha` at or past them. The operator's ritual was a
  hand-made empty `P<N>:` marker plus `resume --response`. The step now takes
  that path itself: with a clean tree and no trailing checkpoints it walks the
  run's own history (`HEAD ^base_branch`, stopping at the previous phase's
  handoff commit) for this phase's scoped checkpoints, lands the same empty
  `P<N>:` marker listing them, notes how many adopted commits it sits over,
  and restores `base_sha` to the oldest checkpoint's parent so the review range
  is the cumulative phase diff. Fail closed as before on a wrong-phase
  checkpoint, an unbounded walk (missing base ref), or a range with no
  checkpoints at all; adopted commits are never squashed.
### Fixed

- The commit-message drafter no longer inlines an unbounded phase diff. It
  resolves the `message_agent` adapter's declared input cap through the same
  capability path the review and confirm prompts use and, when the assembled
  prompt would exceed it (or, with no declared cap, when the diff alone exceeds
  a fixed 400,000-char ceiling — fail closed against the unknown), hands the
  change by reference for repository-capable drafters. Tool-less API drafters
  receive bounded inline excerpts with explicit omission notices. Redraft
  feedback echoes the offending header and its exact character count. The
  validator allows 100 characters (the prompt targets 72); invalid drafts
  fail after bounded retries without synthesizing a replacement title.
  The failure names the verbatim `resume --response` override (#134).

## [1.2.0] — 2026-08-18

### Fixed

- A `phase-commit` step no longer fails terminally when `resume` commit
  adoption re-anchored its `base_sha` past the phase's own `P<N>:` commit: the
  step walks back from HEAD — bounded to the run branch's own commits
  (`HEAD ^base_branch`), in one batched `git log` — adopts the phase commit,
  and repairs the record's `base_sha` to that commit's parent so review-diff
  consumers see a forward, non-empty range. A same-prefix commit from the base
  branch's pre-run history is never adopted, a phase with `P<N> wip:`
  checkpoints still lands its empty marker commit at the tip, and a genuinely
  empty phase still fails loud (#124).

- Ensemble-member Codex capacity failures and the pinned shared-model-cache
  startup fatal now consume bounded persisted dependency retries and park
  `provider_unavailable` on exhaustion instead of requiring a human response.
  `doctor` also reports Codex pin drift and incompatible/shared cache metadata
  without exposing cached model content (#119).
- A failed `shell` step (e.g. a flaky test suite) whose attempt provably left
  no Git/worktree side effects is no longer terminal-with-abort-as-the-only-exit:
  the failure gets the plan §5.2 side-effect assessment and records
  `failure_kind=side_effect_free_unknown`, so a plain `gauntlet resume` retries
  it and a deterministic repeat trips the R5 no-progress guard. Pre-existing
  FAILED shell records (stamped before this fix) are assessed at the resume
  boundary and upgraded with an audited manifest warning when the tree is
  provably clean against the attempt's `base_sha`; side-effecting or unprovable
  failures stay terminal exactly as before (#121).
- A failed `shell` step that declares an `on_fail` route (the standard
  pipeline's `tests`) no longer strands the run once its retry budget is spent.
  A plain `gauntlet resume` used to re-execute the same failing command, fail
  identically, and trip the R5 no-progress guard naming only `abort` — so
  operators reached for git surgery instead of the route the pipeline already
  declares. A plain resume is a human action: it now re-arms exactly one more
  route through the same reset the in-budget path uses (the route target and
  everything after it go pending, `base_sha` and stale cycle checkpoints are
  cleared), records an audited manifest warning (`operator resume re-armed
  on_fail for <step> (route_to=<target>, re-arm #k)`), and drives; each later
  plain resume after another exhaustion re-arms again and increments `k`. Shell
  steps without `on_fail` keep today's refusals (including the #121 dirty-tree
  case), and the no-progress refusal now names each safe action's executable
  form, including the re-arm when a route exists. `tests-recheck` in the
  standard pipeline gains `on_fail: {route_to: impl-cycle, max_retries: 1}`, so
  a post-cycle test failure routes back to the cycle that changed the tree
  instead of terminating the run (#134).

## [1.1.1] — 2026-08-12

### Changed

- Promote `pytest>=8.0` to a runtime dependency so it is available in standard
  Gauntlet installations.

## [1.1.0] — 2026-08-11

This release hardens long-running agent recovery and closes the remaining
release-blocking failures found by the `job-platform-base` dogfood run. Vanished
agents, interrupted cycles, governed-artifact publication, commit-message
sanitization, and human-authorized FR-10.4 resumes now converge through explicit,
auditable recovery paths instead of wedging or silently losing state.

### Fixed

- A vanished CLI agent now exits the adapter wait through the existing
  interrupted/resume path, with proof-gated process-group checks, agent-silence
  status evidence, and truthful `recover` state output (#103 / PR #113).
- Recovery now preserves coherent foreach state, re-drives rejected phase gates,
  resumes killed adversarial cycles without losing completed rounds, and fails
  closed when a mutating implementation attempt was entirely denied (PRs #102,
  #108, #111, and #112).
- Governed-artifact publish-back uses a three-way comparison, while provider
  outage markers and engine bookkeeping dirt are classified without masking real
  failures (PRs #107 and #109).
- Agent-authored commit messages are sanitized at the shared `git commit -F -`
  stdin boundary; NUL/control bytes no longer strand completed work (#105 /
  PR #114).
- A human-approved FR-10.4 response now consumes the same prior finding-root and
  upstream-target question after the response-disposition gate says to proceed;
  a different root or artifact still re-parks fail-closed (#106).

### Tests and documentation

- The full live-pipeline test now resolves bounded FR-10.4 variance through the
  public response workflow and treats a proven plain-resumable provider/quota
  park as environment variance rather than an engine failure (#116).
- Removed the stray closing code fence from the README (PR #115).

## [1.0.8] — 2026-08-08

**Recover, rollback, and the interrupted park now reconcile a branch left
ahead of the manifest (#72).** `gauntlet recover` on a builder that had
committed wip but never flushed the manifest left the branch ahead of the
manifest's last recorded commit — then every sanctioned path deadlocked:
`resume` re-parked, `rollback` refused on the FR-9.9 divergence guard, and the
only escape was the exact `git reset --hard` the docs and harness forbid (the
`scheduled-restart` run died this way). Three coordinated changes: `recover`
now always snapshots the killed branch tip to a backup ref
(`refs/gauntlet/backup/<run_id>/recover-…`) and records the tip + unmanifested
range on the §6.4 audit record with a warning naming the ways out; `rollback`
absorbs a tip that is a strict *descendant* of the last recorded commit
(backed up first, absorption recorded as a warning) while still refusing
genuine forks; and `gauntlet resume --reset-interrupted` gives the
interrupted-park a native one-shot resolution — back up the partial work,
rewind only to the latest committed `P<N> wip:` checkpoint, re-run cleanly —
which the park message and `status` next-actions now name. Also:
`interrupted_step` is finally validated (`park|reset_to_base`; a typo used to
silently mean `park`), and the operator playbook documents rollback for the
first time. Hardened per PR #77 review: rollback now takes the worktree lock
and verifies + checks out the run branch explicitly before any guard reads a
SHA (bare HEAD under the absorb tier would have hard-reset a checked-out
merged `main`); every rewind site (interrupted reset, conflict-park restore,
rollback, the cycle's fix-resume and mutation reverts) carries uncommitted
edits and deletions to human-owned excluded files (`PR.md`) across the reset,
and includes that state in the durable backup ref — they are hidden from the
dirty checks by policy, but `reset --hard` is not policy-scoped. A cross-branch
rollback now refuses with an actionable guard before checkout when such local
state exists; and the recover reconciliation is best-effort end-to-end (a
failing `update-ref`/`log` degrades to a warning, never blocks the kill/audit
finalization). Regression tests replay the incident at the recover, rollback,
and full-stack resume level.

**Plan reviews now sweep the PRD requirement by requirement, and can actually
see it (#80).** A plan-cycle review of a 12-phase plan produced nine genuine
findings and converged; a human then found four majors it had missed, all the
same shape — a PRD requirement with no delivering phase (one FR's key event
name had *zero* occurrences in the plan). An LLM reviewer finds contradictions
in text it reads and misses requirements with no counterpart to collide with:
absences generate no attention. Two coordinated changes. The document-mode
review prompt now carries a **mandatory coverage sweep** — walk the spec clause
by clause, name the delivering phase and acceptance clause for each, and raise
anything unmappable as a `spec-gap` finding of at least `major`, with the
resulting clause→phase map persisted in `summary` so the sweep is auditable;
a plan's own FR→phase traceability table is an assertion to check, not
evidence. And the sweep is now *executable*: artifact mode inlined only the
artifact under review, so a plan reviewer never had the PRD in context at all —
the codex reviewer was never told the path and the `api`-adapter panel member
cannot read the repo — so the new `review_against:` step key (set to `prd.md`
on `plan-cycle` in `pipelines/standard.yaml`) inlines the approved spec beside
the plan for round 1, failing closed if it is unreadable. Because the key names
a file whose whole body lands in a reviewer prompt and the transcripts, it is
validated like any other path-bearing artifact reference: pipeline load rejects
a dangling name, an absolute or `../` value, a path that resolves outside the
repo root, and a seed that is not on disk — and the cycle re-proves containment
at the read. Rounds 2+ are unaffected (regression-scoped, FR-1.2), as is the
PRD cycle, which has no upstream spec. **Upgrade note:** the prompt half ships with the scaffold, but
the sweep is keyed on the spec block's presence and stays inert without it — if
your `pipelines/standard.yaml` is customized, `gauntlet upgrade` will not add
the key for you; add `review_against: prd.md` to your `plan-cycle` step.

**The engine's own bookkeeping commits no longer wedge resume and rollback
(#62, #65).** `is_dirty_vs`'s HEAD leg demanded `HEAD == base_sha` exactly,
but the engine itself advances HEAD during every drive — response checkpoints
and run-bookkeeping flushes — and every plain resume commits a fresh pending
checkpoint *before* the dirty check runs. An interrupted worktree-writing step
therefore re-parked INTERRUPTED in under a second, forever, exit 0, with
`status` recommending the same `resume` right back into the loop (and
`rollback`'s divergence guard was unsatisfiable for any run that ever took a
checkpoint). The dirty check now tolerates a `base_sha..HEAD` range iff every
commit carries both engine markers (`ENGINE_IDENTITY` author and the
`gauntlet:` subject) *and* — the authoritative leg — every changed path is in
the exact allowlist of engine-committed bookkeeping files (the run's
`manifest.json`/`RUN.md`), so an engine-labelled commit that moves anything
else — implementation, or a human-owned excluded file like `PR.md` — still
reads dirty; rollback's guard shares the same tolerance. The transaction boundary is now re-armed per step *attempt*: a
cleanly re-entering interrupted step and an `on_fail` retry both re-stamp
`base_sha` at their own entry HEAD, so `interrupted_step: reset_to_base` can
no longer rewind to a stale phase boundary predating the artifact under review
(#65's destructive-escape hazard). And the park is loud: the INTERRUPTED notes
now carry the dirty verdict (porcelain paths + the offending commit range),
and `resume` prints them instead of a bare `run status: parked`. Regression
tests replay the incident end-to-end, including the resume that must drive
past its own freshly committed pending checkpoint.

**Dirty-worktree preflight before the run branch exists (#61).** `gauntlet
run` on a dirty worktree created `gauntlet/<slug>`, checked it out (carrying
the uncommitted changes), and then failed — stranding the operator on a
half-born branch they had to hand-delete after stashing/committing on the
original branch. `start()` now refuses up front, while still on the
operator's own branch, naming the offending paths in a friendly one-line CLI
error. The preflight applies the same exclusion policy as the drive itself
(PR #75 review, both rounds): exactly this slug's `prd.md` is exempt — the
one artifact legitimately uncommitted at start, which the first cycle
baseline-commits (FR-5.1) — and every slug's human-owned `PR.md` is now
excluded engine-wide (preflight, clean-handoff checks, and the engine's
commits), so a finished sibling run's pending `PR.md` neither blocks the
next start nor gets swept into a machine commit. Everything else — an extra
file beside the PRD, a stale `plan.md`, a sibling slug's artifacts — is
refused up front, exactly because the baseline commit fires only when the
single dirty path is the artifact itself, so any of it would fail the
handoff guard after the branch existed.

**Fix the plan-author↔plan-lint contract (#64).** The shipped `plan-author.md`
specified `gauntlet-phases` entries with only `id`/`title`/`goal`, but
`phase_lint` and the `plan_phases` validator require every phase to carry an
`acceptance:` list of `{id, clause}` entries — the planner followed its prompt
faithfully and the gate rejected the result, wedging every fresh run at
plan-lint. The prompt now specifies (and its example models) the full schema the
gate enforces, and a new `tests/unit/test_prompt_contract.py` round-trips the
prompt's own embedded example through the parser, the lint, and the in-step
validator so contract drift fails the build instead of a user's run.

**Size-lint counts declared scope, not prose mentions (#66).** The FR-3.4
phase-size lint regex-swept each phase's whole prose section, so incidental
cross-references and parent-vs-child FRs (`FR-4` + `FR-4.1` = 2) inflated counts
past `max_frs_per_phase` — a false positive that would fail closed at the plan
gate under `size_lint: park`. Phases now declare their scope in an `frs:` list
in the `gauntlet-phases` block (shape-validated when present; the prompt always
emits it) and the lint counts those declared refs; pre-`frs` plans keep the
prose-sweep fallback.

**Honest halted-status meaning (#64).** `gauntlet status` rendered every halted
run as "the budget/timeout guard tripped" — the meaning line was keyed on run
state, collapsing all seven `halt_reason` values (a `precondition` halt read as
a budget problem). The footer now names the guard that actually fired, with an
explicit "reason unrecorded" fallback for pre-P3 manifests; `status --json` is
unchanged (`steps[].halt_reason` already carried it).

## [0.7.0] — 2026-07-17

**Fix oversize review diffs killing the round.** A `code_review` round
inlined the full commit-range diff into the review prompt unconditionally.
The codex app-server rejects any turn over 1 MiB of input wholesale
(`input_too_large`), so a phase whose diff exceeded the cap (clerk-auth P3:
1.23M chars, observed live) killed the round with a terminal `adapter_error`
and no recovery lever.

`AdapterCapabilities` gains `max_input_chars` (declared: codex 1 MiB, others
`None`). `_review_prompt` consults the tightest cap across the review panel
and, when the inline diff would exceed it and every member's adapter
`reads_repo` (FR-1.3), swaps the inline section for a by-reference block
naming the exact range and the git commands to read it. Never truncates: a
clipped diff would silently narrow review scope (fail closed). Unknown cap
or a non-repo-reading panel behaves exactly as before.

## [0.6.0] — 2026-07-16

**Pipeline effectiveness: catch more, gate smarter, learn across runs** (PRD
`runs/pipeline-effectiveness/prd.md`, P1–P9; PR #59). The organizing principle:
Gauntlet's pitch is adversarial multi-model review, but every cycle ran exactly
one reviewer, one pass, reading a diff — so this release widens what the
pipeline can detect, then uses the widened evidence to narrow ceremony. Built by
a `gauntlet run` dogfood on this repo. All new machinery defaults to prior
behavior except the `standard.yaml` phase gate (see **Changed**).

Two of this release's headline claims were falsified by measuring them, and the
spec was corrected rather than the measurement: the evidence-tiered gate
predicate would have fired **0/9** on this repo's own run, and the PRD premise
it was built on described a pipeline that did not exist. Both are recorded in
the PRD's v0.5 changelog.

### Added — detection (catch more)

- **Ensemble review panels (FR-1).** An adversarial cycle accepts multiple
  reviewers, each with a distinct lens (`reviewers: [{profile, lens}, …]`,
  panel capped at 3). Members run sequentially against one worktree, each
  followed by the mutation guard, and are content-addressed so a resumed panel
  re-pays only unfinished members. Findings merge deterministically before
  triage — same file, overlapping location per the §6 normalized-location
  model, same category, and a compatible claim fingerprint — with the
  highest-severity phrasing kept as primary and every source recorded. Only
  primaries reach triage. `standard.yaml` ships a two-member panel (codex
  `correctness` + gemini `spec-coverage`): three distinct providers across the
  pipeline, zero builder-window contention.
- **Per-member yield metrics (FR-1.3).** `metrics.ensemble.unique_legit_by_member`
  answers "unique legitimate findings per panel member" from the manifest, with
  no transcript access. Yield is **sole-source**: a finding two members raised
  is shared coverage and counts toward neither. This exists to make the tool's
  founding premise — that reviewer diversity pays — a measured quantity with an
  explicit kill criterion, rather than an assertion.
- **Behavioral verifier sub-step (FR-2).** An optional `verifier:` between
  review and triage *executes* the phase deliverable in a disposable worktree
  copy and reports `category: behavioral` findings with the executed commands as
  evidence — a signal class no diff reader produces. Its findings join the merged
  panel and flow through the same triage/fix/confirm machinery. Fail-closed
  throughout: an unhooked or absent backend, an unproven boundary, a copy or
  launch failure, or a wall-clock expiry parks the cycle; it never degrades to
  "skipped, proceed".
- **Verifier sandbox contract (FR-2.5).** Confinement is a server-authoritative
  per-step judge boundary: the engine registers the copy root against the
  verifier's step id before launch (one-shot; clearing needs an engine-held
  lease key the sandbox never sees), proves on the live judge that an
  outside-copy read is denied, and parks if it is not. Plus network default-deny
  at the boundary, a rebuilt-allowlist environment (no credential-shaped var
  survives by construction), a scratch HOME, inherited rlimits, and a git
  ref-mutation deny. **Enforcement is hook-mediated, not OS-kernel-level** — a
  forked child is not independently gated. That residual is named in the PRD
  with its compensating controls rather than papered over; kernel-level
  isolation awaits the post-v1 codex `[permissions]` backend.
- **Acceptance mapping + `acceptance_gate` (FR-3.2).** The implement contract
  produces `artifacts/acceptance-map.json` mapping every plan acceptance clause
  to ≥1 collector-enumerated test id; a deterministic gate proves citation and
  existence and parks the phase naming any unmapped clause. This structurally
  closes the silent-partial-delivery class (BOOTSTRAP-NOTES #54: a phase shipped
  25% of its planned FRs and diff review had no mechanism to notice). Scope is
  deliberate — the gate proves *existence*, not sufficiency; whether a cited test
  meaningfully exercises its clause stays the spec-coverage lens's job.
- **Deferral reconciliation + phase-size lint (FR-3.3/3.4).** "Deferred to P<N>"
  references in commit bodies and mapping artifacts are validated against the
  plan's real phases (a deferral to a phantom phase parks); open deferrals are
  injected into the target phase's implement prompt. `phase_lint` flags phases
  carrying more than `max_frs_per_phase` (default 3) distinct FR refs.
- **Declined-findings registry (FR-5.2).** Reasoned declines are recorded with
  provenance (`repo`, `prd_family`, prompt/lens/schema versions, run id) and
  surface as **advisory** precedent to a future run's triage — but only while
  that provenance is current. A decline recorded under a superseded prompt,
  lens, or schema, or a different PRD family, is retained for audit and never
  injected. The triager keeps authority; a match never decides.
- **Trend-informed plan authoring (FR-5.3).** Measured per-phase cost/duration
  distributions and the `max_frs_per_phase` bound are injected into the
  plan-author prompt, so phase sizing is grounded in observed cost. An empty
  history renders a stated "no history" block, not silence.

### Added — ceremony (gate smarter)

- **Evidence-tiered phase gates (FR-4).** A per-phase code gate accepts
  `policy: auto_when_clean`: a strict conjunction over evidence the pipeline
  already records auto-approves the gate and writes a durable `auto_approval`
  manifest record with its full evidence snapshot; any miss parks for a human
  and names *why*. Document gates (PRD/plan) reject the policy at load, as does
  a code phase with no verifier configured. Auto-approved gates are enumerated
  in `PR.md` for collective ratification, and a single recorded reversal
  disables auto-approval for the rest of the run.
- **Convergence honesty (FR-6).** An accepted (`fix_now`) finding confirmed
  `partially_resolved` is **non-converged by definition** — the engine predicate
  says so regardless of severity, and the confirm pass emits the specific
  unresolved remainder as a carryable finding (`carried_from`) so the next round
  has a concrete target. `max_rounds` rises 2 → 3 so a remainder has a round to
  land. Carry parentage is validated on all three legs; an entry failing any is
  demoted to an ordinary regression rather than minting a triage-exempt
  obligation. Closes issue #49's silent-closure class, found by a real adopting
  repo.

### Changed

- **`standard.yaml` gains a per-phase gate.** This is the release's one
  behavior change for existing pipelines, and it is worth reading twice: the
  phases stage previously had **no gate at all**, so every phase proceeded
  automatically after a converged cycle. The new `phase-gate` (`auto_when_clean`)
  does not remove a human step — it **adds a fail-closed stop** for phases whose
  evidence is ambiguous, while clean phases keep flowing. A `tests-recheck` step
  is added after the cycle alongside `acceptance-recheck` to keep both evidence
  signals fresh. Pipelines that want the prior behavior omit the gate.
- **The FR-4 clean predicate is open-based, not raised-based.** A blocking/major
  legitimate finding blocks the gate unless it was accepted `fix_now` *and*
  confirmed `resolved`. The original form counted findings *raised* and,
  replayed against this repo's nine-phase run, would have auto-approved **0/9** —
  it asked whether review had found nothing serious, which is the one state an
  adversarial panel exists to make unlikely, and ensemble review pushes further
  out of reach. Deferred, rejected, unconfirmed, and non-`resolved` findings all
  still park. Snapshots record findings raised *and* still-open, so an
  auto-approval cannot hide that anything was found.
- **The acceptance-gate runs twice per phase**, before and after the cycle: a
  fix round can rename a cited test after the first pass blessed the map.
- **Collector enumeration is a bounded engine subprocess** in a disposable copy
  with a stripped environment, and its command resolves from the project's own
  `test_command`. Deterministic by design — no LLM in the evidence path, since a
  model asked to echo collector output can truncate it (chronic false park) or
  fabricate ids (false pass), either of which defeats the gate's whole premise.
- **Findings schema** gains `source`, `lens`, `duplicate_of`, `sources`,
  `source_members`, and `carried_from`; `category` gains `behavioral`. All
  additive and engine-stamped — legacy and single-reviewer artifacts validate
  unchanged, and the reviewer's pinned strict-output shape is untouched.
- **Governed learning assets are enforced, not conventional.** An in-pipeline
  agent write to `prompts/lenses/*` or `registry/*.jsonl` is judge-denied.

### Fixed (post-review hardening, from the PR-59 adversarial reviews)

- **Dedup could silently lose a defect.** Grouping used single linkage, but both
  legs of the merge predicate are non-transitive: A can overlap B and B overlap C
  while A and C are disjoint. The transitive closure marked a finding
  `duplicate_of` a primary it did not match — and only primaries reach triage, so
  its distinct claim vanished. Grouping is now complete linkage: every group is a
  clique, so a duplicate always matches its primary.
- **Per-lens yield was miscounted when one profile carried two lenses.** Merge
  provenance aggregated by profile, so two members raising one finding collapsed
  to a single source and read as unique yield — inflating the metric in the
  loosening direction and masking the full-overlap case the kill criterion exists
  to detect. Provenance is now tracked by panel-member identity.
- **The archetypal clean gate could never fire:** zero-findings convergence left
  no triage artifact, so the predicate parked forever on a missing file.
- **Stale evidence could vouch for a tree that no longer existed** — tests and
  acceptance records predating a fix commit are now a predicate miss unless
  re-proved after the cycle.
- **A stale `confirm.json` could leak across phases.** `artifacts/` is per-run,
  and the two convergence paths that fix nothing returned without writing it,
  leaving the previous phase's verdicts on disk. Harmless until the gate began
  reading them; both paths now invalidate it (absent > stale).
- Verifier scratch HOME seeded with the CLI login surface only; enumeration
  env stripped of the credentials it previously inherited; carried-remainder
  re-litigation blocked; supersession path made functional; assorted schema
  compatibility and resource-cap fixes.

### Notes

- **No new runtime dependency.** The orchestrator stays thin by design.
- **PRD as artifact of record.** `runs/pipeline-effectiveness/prd.md` reached
  **v0.5** during this release: v0.4 reconciled the spec with the built system
  (the hook-mediated verifier backend a plan-level amendment could not
  legitimately waive), and v0.5 recalibrated FR-4 against measured evidence.
  Both are ratified and their reasoning is in the document's changelog — the
  project's rule is that approved artifacts change only through their own loop
  and gate, and these did.
- **Known gap:** the shipped `phase-gate` has not yet fired on a live run. The
  recalibrated predicate is verified by replay against P1–P9 and by unit
  fixtures. The first real multi-phase run is its first live exercise.

## [0.5.0] — 2026-07-05

**Harness efficiency & resilience** (PRD `runs/harness-efficiency/prd.md`,
P1–P11; PR #52). The organizing principle: a run's scarce resource is the
builder's provider window, so this release makes interruptions survivable
instead of destructive, spends each step more deliberately, and makes an
in-flight run explainable from `status` alone. Built by a `gauntlet run`
dogfood on this repo — and battle-tested by it: the run survived a live codex
usage-limit hit, host sleep, and a malformed plan block using the machinery it
was building. All new knobs default to prior behavior.

### Added — resilience (protect the window)

- **Usage-limit parks with session-preserving resume (FR-3).** Adapters
  classify failures transient-vs-terminal against a pinned, fixture-backed
  marker allowlist (`adapters/failure_markers.py`); a quota/429/overload hit —
  including inside an adversarial cycle's reviewer/triager/fixer/confirmer
  sub-agents — parks the run (`parked_usage_limit`) with the worktree untouched
  and the agent session preserved. Plain `gauntlet resume` continues the same
  session with a short continuation prompt; an expired session falls back to a
  full re-run with an audit note. Unrecognized errors stay `terminal`
  (fail-closed). Opt-in `resume_on_quota: auto` self-resumes at the provider's
  hinted reset time with bounded, restart-safe attempts.
- **Suspend/sleep resilience (FR-5).** A driver heartbeat
  (`engine/heartbeat.py`) detects host suspension (dual detectors, pinned by a
  darwin contract test) and credits slept time back to the running step's
  deadline up to `suspend_credit_cap_s`; stalls classify as `host_suspended` /
  `driver_orphaned` / `agent_silent`. Opt-in `keep_awake: true` wraps the
  driver in `caffeinate -i` on darwin.
- **In-step artifact validation with self-repair (FR-2).** `agent_task`
  supports `validate:` (e.g. `plan_phases`): the authoring agent gets its own
  parse error back for a bounded repair loop; exhaustion parks
  (`parked_artifact_invalid`) for a sanctioned hand-edit — `resume` re-runs
  only the validator and records a content-hash audit of the edit.
- **Adversarial-cycle sub-step checkpointing (FR-4).** Review/triage/fix/
  confirm checkpoint write-ahead per round; a killed cycle re-enters at the
  first incomplete sub-step, guarded by the round handoff SHA and fail-closed
  reloading of corrupt fragments.
- **Intra-phase checkpoint commits (FR-11).** Builders commit `P<N> wip:`
  milestones (`checkpoint_commits: keep | squash`); the phase always terminates
  in a `PN:` commit (empty marker if needed) that is the review handoff, and
  reviewers always see the cumulative range diff. Interrupted-step recovery
  rewinds to the latest checkpoint instead of the phase base, bounding lost
  work to one milestone.

### Added — efficiency (sharpen the spend)

- **Scoped context input modes (FR-1).** Per-input `mode: inline | reference |
  phase` lets large artifacts travel by path (agents read them in-session,
  where turns hit the provider prompt cache) or as the current phase's plan
  excerpt; artifact-mode re-review rounds get a diff since the last reviewed
  snapshot instead of the full document. Gated on a probe-verified `reads_repo`
  capability; pipeline load and `doctor` both fail closed on a blind profile.
- **Effort tiering plumbing (FR-6).** Canonical `effort: minimal | low |
  medium | high` on any profile or step, mapped per adapter (unsupported value
  = config-load error); triage escalation is severity-gated (blocking/major
  only); mechanical emissions (commit messages, resume dispositions) run on a
  cheap `mechanic` profile; `doctor` probes every profile's model resolution
  and effort-flag acceptance.
- **Machine-global usage ledger + window admission (FR-10).**
  `~/.gauntlet/usage-ledger.jsonl` accumulates content-free per-step usage
  across runs (`gauntlet ledger backfill` seeds it from existing manifests,
  idempotently); a configured `providers.<name>` window warns — or with
  `enforce: true` parks pre-step (`parked_usage_window`) — before launching a
  step predicted not to fit. Advisory by design.
- **Concurrent triage + judge decision cache (FR-9, FR-12).** Per-finding
  triage calls run in a bounded pool (`triage_concurrency`, byte-identical
  `triage.json` on all-success rounds; deterministic checkpoint fragment on
  failure); the judge caches **allow** decisions per run keyed on canonical
  payload + policy content hash (deny/ask never cached), eliminating repeated
  LLM-rung evaluations of identical tool calls.

### Added — observability (explain the run)

- **Engine-stamped reason enums (FR-7).** Disjoint `halt_reason` (timeout /
  budget / judge_deny / signal_kill / adapter_error / precondition /
  operator_recover) and `parked_reason` (usage_limit / usage_window /
  artifact_invalid / response / gate) on every non-DONE step; legacy manifests
  normalize read-side (`upstream_conflict`/`cycle_escalation` → `response`),
  never rewritten.
- **Enriched `status --json` (additive; `schema_version` stays 1):** run
  elapsed, token/cost totals and per-profile `agent_usage`, per-step
  duration/notes/reasons, heartbeat age + suspension intervals, quota reset
  time, and a `gate` context block (convergence summary, prior responses,
  per-finding triage reasoning — FR-8). `gauntlet report` gains
  cache-effectiveness columns. Consumers pinning an older strict schema copy
  must re-pin on upgrade.

### Changed

- **Custom `halt_on` marker parks now stamp `parked_reason=response`** and
  receive the same clean-tree restoration as the canonical UPSTREAM CONFLICT
  marker (the dirty tree is snapshotted to a backup ref first — nothing is
  lost, but edits no longer remain in the worktree for in-place inspection).
- **Failed `commit` steps are `--response`-recoverable** (`commit` joined
  `RESPONDABLE_STEP_TYPES`), closing an operator deadlock where neither plain
  `resume` nor `--response` could advance a crashed commit step.
- **Code-review base spans the whole phase**: cycle review diffs cover
  `base_sha..PN:` (the cumulative phase range), not the final commit alone —
  required for multi-commit (`wip:`) phases and empty marker commits.

### Fixed (post-review hardening, from the PR-52 adversarial review)

- Codex NDJSON decode and `classify_codex_failure` now fail closed on
  valid-JSON-but-non-object event lines instead of crashing the classifier
  with `AttributeError`.
- `Retry-After: 0` is treated as a real "retry now" hint (reset time = now),
  not an absent one — auto-resume can fire immediately.
- The post-disposition session-expiry fallback now writes the same
  `session-expired` audit record as the quota-resume path (FR-3.3 parity).
- Ledger appends no longer re-parse the entire machine-global ledger per step:
  de-dup keys are cached per process and refreshed by scanning only new tail
  bytes (truncation/rotation triggers a full reload).
- Cycle-role usage is recorded in the ledger per-role (previously dropped
  ~92% of a cycle's tokens from admission estimates); `StepRecord` gains
  `agent_usage`.

### Docs

- Operator playbook (+ scaffold twin): the three new park states and their
  recovery verbs, the sanctioned hand-edit exception, suspend-aware stall
  classification. README: run-lifecycle resilience, the enriched status
  contract, `ledger backfill`, corrected canonical `effort` reference, and a
  full resilience/window/scoped-context configuration section. Scaffold
  `config.yaml`: commented opt-in reference block for every new knob.

## [0.4.0] — 2026-07-01

A new **lightweight review** surface. `gauntlet review` brings the adversarial
review cycle (review → triage → fix → confirm) to small changes — bug fixes,
one-off patches — **without** the PRD → plan → phase ceremony. The engineer fixes
the bug however they like (plain Claude Code, by hand) on a branch or PR;
`gauntlet review` reviews that change against its originating ticket, lands
accepted fixes as `REVIEW.x` commits **in place**, and stops — the resulting
branch/PR *is* the human review. It runs with zero routine human gates while
keeping the cycle's fail-closed escalation park as the safety stop. Everything is
additive; no existing CLI workflow changes, and no approved artifact
(`PRD-gauntlet.md`, `policy.yaml`, any `prd.md`/`plan.md`) is amended.

### Added — `gauntlet review`

- **New `gauntlet review [<branch>]` command.** Reviews an already-implemented
  change on a local branch (default: the current branch) against a three-dot diff
  from its base, reusing the trusted `adversarial_cycle` machinery verbatim — same
  reviewer, triage rubric, severity-aware escalation, reviewer-mutation guard, and
  audit-trail fix commits. Only the PRD/plan/phase ceremony is removed. (FR-1, FR-3)
- **Solution-correctness review against the originating ticket.** The reviewer is
  given both the diff **and** a provenance-tagged problem statement (`intent.md`,
  the lightweight analog of `prd.md`) and asked whether the change actually
  *resolves the stated problem* — not just whether the diff is internally sound.
  Intent comes, in precedence order, from `--issue <ref|url>` (tracker fetch),
  `--intent <path>`, `-m <text>`, or an `$EDITOR` template; `--code-only` skips
  intent entirely. (FR-2, G2)
- **Pluggable issue trackers; v1 ships Linear.** A new `gauntlet.issue_trackers`
  entry-point registry (mirroring the adapter registry) resolves a Linear ref
  (`ENG-1234` or a `linear.app/.../issue/<KEY>` URL) into a normalized problem
  statement via the Linear GraphQL API. Auth is by env-var name
  (`config.issue_tracker.api_key_env`, default `LINEAR_API_KEY`) — never a token
  in config — and every fetch fails closed on auth / not-found / unavailable /
  timeout with a typed, actionable error. GitHub Issues / Jira are a registry
  seam, not built code. (FR-6)
- **GitHub PR mode (`--pr <N|url>`).** Checks out a PR locally, resolves its base
  and linked ticket (auto-derived from the PR body in textual order; the first
  resolved ref supplies the intent, secondary refs are reported as ignored), and
  reviews the head branch against its base — including the fork / cross-repository
  case. Fixes land locally; the harness never pushes (FR-9.8 boundary unchanged).
  A non-fast-forward update to a diverged local branch is refused **before** the
  branch is touched, so a checkout failure never leaves a destructive partial
  state. (FR-4)
- **Zero Git-status footprint.** A review run adds nothing — tracked or untracked
  — to the target repo's `git status`, at every point including completion. Run
  state (intent, findings, manifest) lives out-of-repo by default under
  `${XDG_STATE_HOME:-~/.local/state}/gauntlet/reviews/`; an in-repo
  `review.state_dir` override is permitted only when git-ignored (verified via
  `git check-ignore`, else fail closed). A user-supplied in-repo `--intent` file is
  excluded from the entry contract and never swept into a fix commit. (FR-8, G3)
- **`--rounds`, `--test` / `--no-test` controls.** `--rounds` is validated at parse
  time to the closed range `[1, 10]` (default 1) as a deterministic runaway guard;
  `--test` runs the configured `test_command` as an optional baseline step.
- **Terminal-severity contract for gate-less runs.** An unresolved legitimate
  **blocking** finding parks the run (resumable via `resume --response`); an
  unresolved legitimate **non-blocking** finding (`major`/`minor`/`nit`) *completes*
  but is recorded in the run summary as **residual risk**; a not-legitimate finding
  is recorded with its triage reasoning. The summary is a pure, deterministic
  function of the cycle's persisted findings/triage/confirm records. (FR-3.4)

### Added — supporting surface

- **`gauntlet doctor` tracker health check.** When an `issue_tracker` block is
  configured, `doctor` validates the provider is supported, the named env var is
  set, and `verify_auth` succeeds — with fail-closed, actionable messages. (FR-10.1)
- **`gauntlet init` scaffolding.** Ships `pipelines/review.yaml` and the
  `review-code-intent.md` prompt in the standard asset set, and scaffolds a
  commented-out `issue_tracker` block (Linear example + env-var name) and the
  `gauntlet-cli` / `pr_read_commands` policy examples. (FR-10.2)
- **Deterministic PR-read policy preflight.** PR mode is gated by a machine-checkable
  read of `policy.yaml` that verifies the `pr_read_commands@v1` rule is present,
  ratified, and version-`v1` — no network, no agent — and halts with the exact
  FR-7.4 message on absent / unratified / version-mismatch. `PolicyRule` now carries
  `id` / `version` / `ratified` governance markers. Branch-mode reviews skip the
  preflight entirely. The rule is *proposed*, never silently applied; ratification
  is the human gate. (FR-7)

### Notes

- **Adopters:** run `gauntlet init` (new repos) or `gauntlet upgrade` (existing) to
  pick up `pipelines/review.yaml`, the review prompt, and the commented
  `issue_tracker` / policy examples. PR mode additionally requires the
  `pr_read_commands@v1` rule to be ratified in `policy.yaml` via the policy-change
  process; branch mode needs no policy change.
- **Engine surface:** the review lifecycle composes existing `RunManager`
  primitives (manifest, writer/redactor, worktree lock, `resume`/`status`/`abort`)
  and reaches the `adversarial_cycle` through configuration only — `engine/cycle.py`
  loop logic is unchanged.

## [0.3.3] — 2026-06-30

A patch release that stops two operator dead-ends where a run could brick itself,
plus a commit-isolation correctness fix. All bug fixes; existing workflows are
unchanged. (#47)

### Fixed

- **`gauntlet reject` is no longer a dead end.** A `human_gate` rejection marked
  the run terminally `failed`, and a plain `resume` then no-op'd — so a rejection
  with an actionable note went nowhere. `reject_gate` now re-drives: when the gate
  sits downstream of an `adversarial_cycle` (the PRD/plan loops), the rejection
  note is injected into that cycle as a pending `--response` (audited and
  checkpoint-committed), the cycle and everything after it in the stage is reset,
  and the loop re-runs with the note as authoritative reviewer/triager guidance
  before re-parking the gate for a fresh decision. A gate with no upstream cycle to
  iterate still fails terminally with a clear note — reject is never a silent
  no-op. `reject` now takes the worktree lock and honors the judge like `approve`.
- **A clean-handoff precondition failure no longer bricks the run.** Re-running a
  failed clean-handoff precondition kept a stale `base_sha` (stamped on the failed
  run and never refreshed), so a later interrupt would diff/rewind against a SHA
  behind the operator's cleanup commit. Re-arming a re-runnable precondition
  failure now clears `base_sha` so the fresh attempt re-stamps the boundary at the
  current HEAD.
- **Producer-commit isolation.** `commit_paths` ran `git commit -F -` with no
  pathspec, so any file already staged when a producer commit (or the FR-6.4
  proposal apply) ran was swept in — defeating the "stages only its output"
  isolation both callers depend on. The commit is now pathspec-limited; a
  pre-staged file is left staged and uncommitted.
- **Fail-closed on artifact-commit git errors.** `_commit_output_artifact`
  promised fail-closed on git errors but let them bubble out as a generic "handler
  error"; it now catches `GitError` and returns an actionable `StepResult` naming
  the path and phase.
- **Rejecting an already-run cycle guard.** `reject_gate` now iterates only when
  the upstream cycle ran to `DONE`, else terminal-rejects with a clear reason —
  re-arming a non-`DONE` upstream cycle re-skips and orphans the note.
- **Operator-facing message polish.** The terminal-failure resume refusal now
  names the run by slug (operators act by slug, not `run_id`), and `--no-judge`
  gained a help string for discoverability.

## [0.3.2] — 2026-06-27

A patch release that fixes two ways the interactive operator/monitor session
(`gauntlet run --interactive`) could be left unable to act. Both are bug fixes;
existing workflows are unchanged.

### Fixed

- **The interactive operator session no longer bricks when its run ends.** The
  monitor is wired to the run's judge, which is reaped the instant the run exits
  — cleanly or, as seen live, on an early-step failure seconds in. The operator
  session was wired in the judge's default `unattended` mode, so once the judge
  was gone every operator tool call failed closed and was denied — even
  read-only diagnostics like `gauntlet status` — with a misleading "judge
  unreachable" error. `operator_session_env` now marks the session
  `interactive`, so an unreachable judge degrades to a permission prompt (the
  human operator is the backstop) instead of a total deny. A live judge's *deny*
  still denies in both modes, so this never loosens policy on a reachable judge.
- **The judge no longer denies the `gauntlet` CLI in the operator session.**
  `policy.yaml` had no fast-path rule for `gauntlet`, so the operator's own
  verbs (`status`/`logs`/`approve`/`reject`/`resume`/`abort`/`recover`/…)
  escalated to the LLM classifier, which denied them as an "untrusted external
  tool outside the repository" — blocking the operator at the commands it exists
  to run. A new `gauntlet-cli` fast-path allow rule covers the first-party CLI;
  the deny-first rules still gate the destructive git/network primitives in every
  context. Because allow rules are skipped on chained/piped/redirected commands
  (a benign prefix must not bless a dangerous trailing segment), the monitor's
  starter prompt now steers the operator to run gauntlet verbs as a single bare
  command — their output is already bounded (`logs` tails 200 lines,
  `status --json` is small), so piping is never needed.

### Notes

- **Adopters:** the `gauntlet-cli` allow rule ships in the scaffolded
  `.gauntlet/policy.yaml`; existing repos pick it up via `gauntlet upgrade` (or
  by adding the rule to their `policy.yaml`).

## [0.3.1] — 2026-06-27

A patch release that completes and hardens the run-supervision surface shipped
in 0.3.0. Both changes are bug fixes; existing workflows are unchanged.

### Fixed

- **`gauntlet run --watch` now opens the console in your browser.** The
  background-start-services phase (P5) had specified an authenticated
  browser-open for `--watch` but shipped only a subset of its scope — the
  browser-open (FR-1), `?p=` loopback query auth (FR-2), and `serve --resume`
  (FR-4) were never implemented. This release builds the dropped scope: `--watch`
  opens the authenticated loopback URL (degrading fail-soft to `/login`, never
  aborting the run), `serve --resume` reuses or boots a console and opens the
  browser without a foreground bind, and a new `--no-browser` flag opts out.
  Loopback `?p=<token>` query auth bootstraps the HttpOnly session cookie and is
  then stripped from the URL. (#43)
- **The interactive monitor now loads this repo's operator skill.**
  `run --interactive` / `status --interactive` launched a bare `claude` with no
  flags, so the spawned session never loaded the repo's `.claude/` project
  config and reported it could not run the `gauntlet-operator` skill. The
  monitor command now passes `--setting-sources project` for the `claude` agent
  (matching the builder/reviewer adapter profiles), bringing `.claude/skills/`
  into scope. (#44)

## [0.3.0] — 2026-06-27

A run-observability and supervision release. It makes an in-flight run
answerable — live log streaming, machine-readable status, a guarded recovery
path, and a one-command bridge into the console — and adds per-agent reasoning
effort control. Everything is additive; existing CLI workflows are unchanged.

### Added — operator observability & supervision (`status` / `logs` / `recover`)

- **`gauntlet status`** now reports **driver liveness**, the **computed
  run-state**, and the **next action / recovery hint**, so a glance answers
  "where is it, and does it need me?".
- **`gauntlet status --json`** emits the same state as one machine-readable
  object (schema `schemas/status.json`) for scripts and CI.
- **`gauntlet logs <slug>`** is read-only evidence-on-demand — a step's dir plus
  its transcript tail.
- **`gauntlet recover <slug>`** terminates a driver **only after verifying it is
  genuinely wedged** (identity-checked) and marks its step `INTERRUPTED` so a
  plain `resume` re-enters cleanly — it never kills a healthy run.
- **`gauntlet-operator` skill + playbook.** `gauntlet init` installs a
  project-level Claude Code skill (`prompts/operator.md`) that routes a
  supervising session to this repo's run-state triage and recovery playbook,
  propagated like every other asset.
- **Engine hardening.** A response-less terminal cycle failure now surfaces
  instead of being silently re-executed/rewritten on `resume` (with a regression
  test).

### Added — live run observability (streamed step output)

- **Streamed step output.** The CLI agents' line-delimited JSON events are now
  written to disk incrementally as they arrive (replacing the buffered drain in
  `run_with_timeout`), so an in-flight step has a live, redacted, tailable log.
  Claude + Codex adapters; the API/LiteLLM adapter is a durable non-goal.
  Streaming ships behind a **default-off** flag.
- **`gauntlet logs --follow`** tails a running step's `events.jsonl` live.
- **Advisory freshness signal.** `status` / `status --json` expose
  `current_step_freshness.last_event_age_s` so a stalled step is visible.

### Added — background service startup & the interactive run monitor

- **`gauntlet run --watch`** ensures the supervisory console is running
  (boot/reuse) and prints its URL before running in the foreground;
  `--console-host` / `--console-port` override the bind.
- **`gauntlet run --interactive[=claude|codex]`** launches the run **detached**
  and hands the terminal to an interactive monitoring agent, wired to the run's
  judge as the **operator's own session** (judge-gated without prompt spam);
  **`gauntlet status --interactive`** attaches the same monitor to an
  already-running run.
- **Per-run `judge.json`** (gitignored — endpoint + process identity) lets
  `abort` / `finish` / `clean` reap an **orphaned per-run judge** by verified
  identity; the shared console is never killed.

### Added — per-agent reasoning effort

- **`claude-code`** profiles accept an optional `effort`
  (`low`/`medium`/`high`/`xhigh`/`max`, passed as `--effort`), and **`codex`**
  profiles accept `reasoning_effort` (passed as `-c model_reasoning_effort=…`).
  Both are optional and no-op when absent — existing configs are unaffected — so
  a cheaper `fixer:` role can run review-fix rounds while `builder` runs higher.

### Changed — the wired judge hook tolerates a missing install

`gauntlet init` now wires the PreToolUse judge hook as an **install-tolerant
launcher** instead of the bare `gauntlet-judge-hook` console script, so a repo can
mix Gauntlet and non-Gauntlet developers without the latter seeing hook errors. The
launcher:

- **execs the real hook when it's installed** — the permission decision and exit
  code (including the exit-2 deny) and the `GAUNTLET_RUN_ID` gating pass through
  unchanged, so gating is byte-identical to before;
- **stays silent (exit 0) for a teammate who never installed Gauntlet**, instead of
  emitting a per-tool-call `command not found` hook-error notice on every call; and
- **fails closed (exit 2) only when the hook is missing during an active run**
  (`GAUNTLET_RUN_ID` set), so a broken install can never let a run proceed ungated.

A re-run upgrades an existing bare-command wiring in place (idempotent; a
hand-customized wrapping is left untouched), and `gauntlet doctor` recognizes both
forms while still FAILing when the console script is absent on a Gauntlet user's
PATH. The launcher is POSIX sh (macOS/Linux/WSL2 — native-Windows users follow the
README's WSL2 path).

### Added — PRD-authoring aids (the repo teaches its own PRD conventions)

Two committable aids make a fresh session — human or Claude — start PRD authoring
from the right shape instead of from tribal knowledge. Both propagate via
`gauntlet init` like every other asset, so a teammate who clones the repo
inherits them (FR-1.2).

- **PRD-authoring skill.** `gauntlet init` installs a project-level Claude Code
  skill at `.claude/skills/gauntlet-prd-author/SKILL.md`. It triggers on
  natural-language PRD intent ("write/draft/author a PRD", "start a Gauntlet
  run") and routes the session to this repo's playbook and conventions. It is a
  **thin pointer** to `prompts/prd-author.md` (resolved under the repo's
  `asset_root`) — never a copy — so the single instruction source can't drift.
  The playbook reference is repository-relative, so the committed skill keeps
  resolving after a clone or copy to a different absolute path.
- **Structured `gauntlet new` stub.** The PRD stub is now one committable
  template (`<asset_root>/prd-stub.md`) carrying the playbook's full section
  skeleton plus one-line guidance per section. Both `gauntlet new` and the
  `gauntlet run` entry-contract gate resolve the *same* template (repo copy if
  present, else the packaged default), so they can never disagree about what an
  unfilled stub is.
- **The human-author gate is unchanged and hardened.** The richer stub keeps the
  FR-10.1 marker, and a deterministic authored-content predicate rejects every
  trivial edit (whitespace-, comment-, or heading-only). Because the stub
  template is now a gate input, both consumers validate it against template
  invariants (exactly one marker, every mandatory section, required metadata
  labels) and **fail closed** — a malformed customization can't silently disable
  the gate.
- **Idempotent, never-clobber propagation.** A re-run creates whichever aid is
  missing, refreshes an *unmodified generated* file to the current template after
  a version bump, and never overwrites a customization. `gauntlet init
  --from-repo` reports each aid as present / missing / customized without
  writing. Malformed pre-existing state (a non-regular or symlinked destination)
  fails the run closed before any write.
- **Committability + `doctor` check.** `init` warns (without editing the rule) if
  a foreign ignore source — repo/parent `.gitignore`, `.git/info/exclude`, or the
  global `core.excludesFile` — would exclude the skill from git. `gauntlet doctor`
  gains a **warn-only** skill check (the skill gates nothing, so it never FAILs):
  it warns when the skill is missing, malformed against the pinned frontmatter
  schema, or its provenance looks stale.
- **`gauntlet new` pointer (OQ-4).** `gauntlet new` now prints a CLI-agnostic
  pointer to the playbook and skill, reinforcing the convention outside a
  skill-aware Claude session.

## [0.2.0] — 2026-06-19

A significant feature release. The headline is the **Gauntlet Console** — a
local-first web UI that makes every run visible, answerable, and recoverable —
alongside a hardened judge security posture, the run-branch lifecycle, and
smarter project setup. Everything is additive; existing CLI workflows are
unchanged.

### Added — Gauntlet Console (supervisory web UI)

`gauntlet serve` starts a loopback-only, token-authenticated console that runs
strictly *above* the orchestrator — every control action launches the same
sanctioned `gauntlet` CLI verb a human would type, so it inherits every existing
safety invariant rather than being able to weaken one.

- **Run list & detail.** Lists every run across all slugs (sortable, filterable,
  searchable) with live status, current step, and cost; per-run detail renders
  the full step tree, per-step status/duration/cost, and an owned/observed badge.
- **Live updates.** A ~1 s manifest poll emits edge-triggered transitions over
  SSE — no manual refresh — via a ~30-line vendored vanilla-JS shim (no HTMX, no
  build step, no new dependency).
- **Step drill-down.** Open any step's `prompt.md`, `transcript.md` (rendered
  markdown), and `events.jsonl`, including artifacts nested in round / sub-step
  dirs (cycle review/triage/confirm, retrospective builder/reviewer/synthesis,
  per-finding triage verdicts). Live log tailing for running steps.
- **Human-gate review.** When a run parks at a gate, the console assembles the
  decision's evidence — findings/triage as readable tables, rendered artifacts,
  and a deterministic phase diff — and offers **Approve / Reject** in one place.
- **Cycle-escalation reconciliation.** Parks *inside* an adversarial cycle
  (upstream-invalidation, open-blocker, max-rounds) are surfaced with their
  escalated findings, triage verdicts, and the named upstream artifact, framed
  as a reconcile-then-resume decision — previously invisible and un-notified.
- **Failure diagnosis & recovery.** A pure classifier maps each parked/halted/
  failed state to the action that actually applies — Resume where it helps, and
  an honest "resume won't fix this" with guidance (raise the timeout/budget,
  reconcile the artifact) where it won't.
- **Supervised runs.** Launch and abort runs as managed subprocess children of
  the CLI, with captured logs and crash survival: a server restart re-discovers
  owned runs and re-attaches to live PIDs (PID-reuse-safe), and an orphaned run
  is offered for resume exactly like a `kill -9`'d one.
- **Notifications.** Fire on the four moments that need a human — gate reached,
  escalation parked, run failed, run completed — to macOS desktop, Slack, and
  in-tab, edge-triggered and fail-soft (a notification error can never affect a
  run).
- **Durable auth & ergonomics.** A one-time `/login` token exchange sets an
  `HttpOnly; SameSite=Strict` session cookie with per-session CSRF on every
  state-changing POST; full run-history browsing per slug; cost report and
  judge-audit views; `gauntlet run --watch` boots/reuses a console for the run.
- **Read-only proposals view** for a run's retrospective improvement proposals
  (review/apply stays the `gauntlet proposals review` CLI verb).
- **Opt-in analysis hand-off** (`gauntlet serve --enable-handoff`): assembles a
  copy-pasteable, read-only prompt for a parked decision — the console itself
  makes no model call and spawns nothing.

### Added — engine & tooling

- **Run-branch lifecycle:** `base:current` resolution, a stale-branch guard,
  `gauntlet clean`, and `gauntlet finish` (merge a completed run via PR), with
  fail-closed resume / clean / base resolution.
- **Worktree-scoped active-run lock (FR-10.5):** a repo/worktree advisory lock
  fail-closes `start` / `resume` / `approve` across *all* slugs in a worktree,
  so two orchestrators can never drive one worktree. Parallel runs across
  *different* repos are unaffected.
- **`gauntlet init`** now detects the per-project test command.
- **Run-id handshake** (`gauntlet run --run-id`) lets a supervisor pre-allocate
  a run's id; the env-var form was dropped to avoid colliding with the judge's
  `GAUNTLET_RUN_ID`.

### Security

- **Context-aware push/PR policy.** The operator may `git push` and
  `gh pr create`/read; in-run agents are denied. Force-pushing and direct merges
  to `main`/`master` remain denied for everyone.
- **Judge gated on run context** (an active `RUN_ID`), not mere token presence,
  so an ambient token can't pull an unrelated session under judge control.
- Engine-managed judge **avoids port clashes** and reuses an existing judge
  rather than failing to bind.
- **Warns loudly** when the judge LLM classifier is disabled.
- Stale `triage.json` is cleared between rounds to prevent phantom escalations.

### Fixed

- Baseline-commit guard missed an artifact under the nested run layout.
- Numerous review-hardening fixes across the judge, resume/clean/base paths, and
  the console (path containment, fail-closed gate evidence, the active-run lock's
  unverifiable-identity handling, console registry startup race, and FR-5.3
  control gating).

### Notes

- **Dependencies:** `httpx` and `jinja2` promoted from transitive to explicit
  `pyproject.toml` dependencies; no new heavy runtime dependency.
- **Engine surface:** the console adds exactly one sanctioned engine change (the
  worktree active-run lock); everything else reads on-disk state or shells out to
  CLI verbs.

[0.6.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.6.0
[0.5.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.5.0
[0.4.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.4.0
[0.3.3]: https://github.com/johnpletka/gauntlet/releases/tag/v0.3.3
[0.3.2]: https://github.com/johnpletka/gauntlet/releases/tag/v0.3.2
[0.3.1]: https://github.com/johnpletka/gauntlet/releases/tag/v0.3.1
[0.3.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.3.0
[0.2.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.2.0

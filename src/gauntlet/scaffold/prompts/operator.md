# Operating a Gauntlet run — triage instructions for Claude

**What this is.** A reusable playbook for the *operator* role: a human or a
Claude session supervising a `gauntlet run` and deciding the next move when it
pauses, fails, or wedges. It ships in every Gauntlet install, so "this project"
is whatever repo you are operating in — Gauntlet's own repo, or any project that
adopted Gauntlet via `gauntlet init`. You may have opened a fresh session with no
resident knowledge of this run; that is fine — everything you need to act is
either here or printed by the CLI.

## 0. Your role and the one rule that dominates

You are the **operator**, not a participant in the pipeline. You read the run's
state, decide gates, fetch evidence, and recover a stuck run. You do **not** do
the builder's or the reviewer's job, and you never weaken a safety boundary to
make a run move.

The single most important habit: **let the tool tell you the state — never
infer it.** `gauntlet status <slug>` computes the truth (including whether the
driver process is actually alive) and prints the exact command(s) to run next.
`gauntlet status <slug> --json` is the same computation as a machine contract.
When you are unsure, that output is the authority, not your memory of what the
run was doing last time you looked.

## 1. The state space you are triaging

A run is always in exactly one **composite state**, a total function of the
manifest status, the *computed* driver liveness, and any parked/failure
descriptor. `status` reports the state name and its next action; this is the map
behind that output. Drive every decision off the reported state class:

- **`in_progress`** — the driver is provably alive and working. Action: observe
  only (`status`, `logs`). Do **not** resume or recover a healthy run.
- **`orphaned`** — the manifest says running but the driver is dead or its PID was
  recycled; the drive lock is reclaimable. Action: `gauntlet resume <slug>`.
- **`indeterminate`** — liveness cannot be proven either way (an unparseable,
  unverifiable, or foreign-host lock; an unsupported platform). Action:
  **read-only inspection only** (`logs`, `status --json`) — never a mutating verb.
  This is the deliberately safe verdict; treat it as "look, do not touch."
- **`parked_gate`** — the run is awaiting a human decision at a `human_gate`.
  Action: `gauntlet approve <slug>` or `gauntlet reject <slug> --notes "<reason>"`
  (reject feeds the note back into the upstream review cycle as a new round — see §3).
- **`parked_for_response`** — the run is awaiting `resume --response`: a builder
  `UPSTREAM CONFLICT` or a review-cycle escalation its own loop could not settle.
  Action: `gauntlet resume <slug> --response "<decision>"` for a genuine
  decision, or `gauntlet resume <slug> --accept-artifacts` when the decision is
  "the PRD/plan as they stand are approved — proceed" (#134): the structured
  form records the artifact digests and re-drives as proceed_in_place with no
  prose classification, so it can never be re-parked as an amendment request.
- **`parked_usage_limit`** — a provider usage limit interrupted an agent (or a
  cycle sub-agent) mid-step. This is a pause, not a failure: the worktree is
  untouched and the agent's session id is preserved. Action: plain `gauntlet
  resume <slug>` once the window replenishes — it **continues the same session**
  with a short continuation prompt (FR-3.3); resuming too early harmlessly
  re-parks. `status` prints the reset time when the provider reported one. With
  `resume_on_quota: auto` configured, the live driver self-resumes at the hinted
  time (bounded attempts) — check `status` before assuming you must act.
- **`parked_usage_window`** — the pre-step admission check (a configured
  `providers.<name>` window with `enforce: true`) parked *before* launching a
  step predicted not to fit the remaining window. Nothing is in flight; zero
  work is lost. Action: `gauntlet resume <slug>` when headroom returns.
- **`parked_provider_unavailable`** — a transport/dependency failure (provider
  timeout, connection/DNS failure, 5xx/overload) exhausted the engine's bounded
  in-process retries (the consumed budget is persisted on the step, so crashes
  never reset it). This is infrastructure, not content: no decision is at
  stake. Action: plain `gauntlet resume <slug>` after the retry deadline
  `status` prints — **never** `--response` (retry intent is not a human
  decision). For a cycle fan-out, completed sub-steps and per-finding leaves
  are checkpointed; the resume retries only the incomplete work, and
  `gauntlet logs <slug>` points at the failing leaf's own evidence.
- **`parked_artifact_invalid`** — an agent-authored structured artifact (e.g.
  the plan's `gauntlet-phases` block) failed validation after bounded in-session
  repair attempts; the exact validator error is in the step `notes`. Action:
  this is the **one sanctioned hand-edit** in the whole state space — fix the
  named artifact file directly, then plain `gauntlet resume <slug>` re-runs
  **only the validator** (no agent re-run). The edit is audited: content hashes
  at park and at resume are recorded in the manifest.
  **Edit the copy in YOUR OWN checkout**, always — never one inside a run
  worktree. Your checkout is the authoring surface for `prd.md`/`plan.md`: it
  is what the engine reads, validates and hashes, and the resume publishes your
  bytes into the run's tree for you. A dedicated run's tree also holds those
  files, but editing them there edits a disposable copy the next sync
  overwrites.
- **`failed`** — a step failed. Action: read the evidence with `gauntlet logs
  <slug>` (and the failed step's `notes` + engine-stamped `halt_reason`), then
  recover by failure kind:
  - **A re-runnable precondition failure** (e.g. the FR-9.3 clean-handoff guard:
    "worktree dirty at round-1 review handoff") fired *before* any agent ran. The
    step `notes` name the offending uncommitted paths. Commit or stash them, then
    `gauntlet resume <slug>` re-runs the guard and continues. `status` recommends
    exactly this.
  - **A terminal failure** (a fixer that made no changes, a genuine agent error)
    cannot be advanced by a plain `resume` — it would only repeat. If a human
    decision can unblock it, inject one: `gauntlet resume <slug> --response
    "<decision>"`; otherwise `gauntlet abort`. A plain `resume` here refuses with
    that guidance instead of silently no-op'ing. A `commit` step that failed on
    the enforced message format is `--response`-recoverable too: the response
    text guides the redraft.
- **`halted`** — a guard tripped; the step record's `halt_reason` names which
  (`timeout`, `budget`, …). Note that deadlines are suspend-aware: time the host
  spent asleep is credited back (up to `suspend_credit_cap_s`), so a `timeout`
  halt means the *agent* genuinely exceeded its budget, not that the laptop lid
  closed. Action: `logs`, then `resume`.
- **`interrupted`** — a step was killed mid-run. Action: `logs`, then `resume`.
  A plain `resume` first reconciles a run branch left *ahead* of the manifest
  by proven commit class: engine bookkeeping is tolerated, a committed
  `P<N> wip:` checkpoint or phase/fix commit is **adopted** into the manifest
  (the audit warning names the range) and the run continues from it, and an
  operator commit becomes the next attempt's base — a hand-committed
  `prd.md`/`plan.md` edit is surfaced loudly through the artifact's own gate,
  never refused or discarded. It re-parks (fast, zero agent work) only when
  the killed attempt left *uncommitted* work vs the step's recorded base — the
  park message shows the dirty verdict (uncommitted paths). **Check which tree
  those paths are in.** For a dedicated run they are in the run worktree, not
  your checkout — so `git status` where you are standing can read *clean* while
  the verb refuses on dirtiness, and the two are not in conflict. `status
  --json` prints `worktree.path`; inspect with `git -C <that path> status`.
  Repeating a resume
  that changes nothing exits nonzero, naming the unchanged state and the
  executable safe actions — never a silent re-park loop. The
  sanctioned exit is `gauntlet resume <slug> --reset-interrupted`: it preserves
  the partial work as a complete recovery snapshot under
  `refs/gauntlet/recovery/`, rewinds only to the latest
  committed `P<N> wip:` checkpoint (committed milestones survive), and re-runs
  the step cleanly. One-shot — the configured `interrupted_step` policy is
  unchanged. Never reach for `git reset` on a run branch instead.
- **`worktree_unavailable`** — the run drives a dedicated worktree (the
  default since P7g; `worktree.mode: same_tree` opts back out) and that tree
  could not be created, locked, or verified: the path was taken, the branch is checked out somewhere else, the
  disk filled, or the repo has uninitialized submodules. **Nothing was moved
  or modified** — the run is exactly where it was, and the engine did *not*
  quietly fall back to your checkout. `status` names the reason and the git
  error verbatim. Action: fix what the message names and `gauntlet resume
  <slug>`, or take the operator-chosen fallback `gauntlet resume <slug>
  --same-tree`, which drives THIS resume in your own checkout. `--same-tree` is
  one-shot: it is never persisted and never applied automatically.
- **a dedicated run whose tree is at the OLD location** — only on repositories
  that opted into `dedicated` before the run worktree moved out of the git
  directory. The refusal names the tree it found, the new path, and the verb:
  `gauntlet migrate-worktree <slug>` relocates it. Take that action rather than
  `--same-tree` here — the tree is fine, it is simply somewhere the `claude` CLI
  refuses to write, which is why the run could not be driven. Relocating rebuilds
  the tree at the new root from the branch, so **commit or discard uncommitted
  work in the old tree first**; the verb refuses while any exists rather than
  sweeping it into a recovery ref you would have to know to look for. Nothing is
  moved until you run it, and a live driver blocks it.
- **`worktree missing`** (a dedicated run whose tree is gone) — `status --json`
  shows `worktree.registered: true` with `present: false`; plain `status`
  prints `worktree: MISSING at <path>`. Note `prunable` is usually **null**
  here, not a reason string: a live run's tree is held under `git worktree
  lock` for its whole life, and git does not report a locked worktree as
  prunable. Registered-and-absent is the signal, not `prunable`.
  The tree was swept (a reboot, a `/tmp` clean, an `rm -rf`)
  while the branch ref and the journal — the authoritative state — survived.
  This is recoverable by construction: action is plain `gauntlet resume
  <slug>`, which recreates the worktree from the branch plus journal state and
  verifies the recreated HEAD matches the journal head before driving. Do
  **not** hand-run `git worktree add`; the engine also re-establishes the
  anti-prune lock that stops another run's cleanup from removing it again.
- **a `same_tree` run offered migration** — not a park and not a problem, and
  since P7g it is the *exception* rather than the norm: a run that started
  before the dedicated layout became the default, or one an adopter deliberately
  pinned to `worktree.mode: same_tree`, drives your checkout, and `status`
  offers the **optional** action `gauntlet migrate-worktree <slug>` to give it a
  tree of its own. Nothing moves a run for you — the config `mode` only decides
  what *new* runs are born as, so an existing run keeps driving `same_tree`
  until you run this by name. That asymmetry is deliberate: a default flip must
  never relocate a run that is already under way.
  Copy, never move: the branch, its commits, the journal, the manifest and the
  run dir all stay exactly where they are; only the tree the agents edit
  changes. Undo with `gauntlet migrate-worktree <slug> --rollback`, which
  removes the tree and returns the run to `same_tree` with everything else
  intact.
  Two preconditions, both checked before anything is touched — so `status` only
  offers the action once it would actually run. **Step off the run branch
  first**: a `same_tree` run leaves `gauntlet/<slug>` checked out in your tree,
  and git refuses a second worktree for a checked-out branch, so
  `git checkout <base>` and then migrate. The engine will not check out or move
  a branch in your checkout to make this succeed, by design. **Commit or stash
  uncommitted work first**: a `same_tree` run's work-in-progress lives in your
  checkout, and migration builds the run's new tree from the committed branch
  tip — so anything uncommitted would be stranded with you while the agents
  carried on elsewhere. Your `prd.md`/`plan.md` are not affected; they are
  republished into the run tree.
  `gauntlet finish` then handles your local untracked `prd.md` for you when it
  is byte-identical to what the run branch committed: it clears the duplicate,
  the merge restores the same bytes as a tracked file, and the result line says
  which paths it replaced. If your copy has **diverged** from the branch's, it
  refuses instead and names both resolutions — a disagreement about an approved
  artifact is yours to settle, not the engine's.
  Migration is refused, with the blocker named, while a driver is `alive` or
  `indeterminate`, and for a terminal run. **A refusal never wedges anything**:
  the run is left exactly as it was and stays fully drivable in `same_tree`. If
  a failure ever leaves the tree behind, the refusal says so explicitly and
  names the mode the run is actually in — it never claims `same_tree` without
  having verified it.
- **`done`** — the run completed. No action; a lingering lock is harmless residue.
- **`aborted`** — an operator aborted the run. No action.
- **`unknown`** — an unrecognized or internally contradictory manifest. Action:
  **read-only inspection only** — never a mutating verb. Surface it; do not guess.

The two states that never pair with a mutating verb — `indeterminate` and
`unknown` — are where fail-closed thinking matters most: when the tool cannot
prove what is safe, it withholds the destructive option, and so must you.

## 2. The triage decision tree

Work top-down; stop at the first branch that matches.

1. **Run `gauntlet status <slug>`** (or `--json` if you are scripting). Read the
   `state` line and the driver-liveness line. Everything below keys off them.
2. **Is it parked?** `parked_gate` → decide the gate (§3). `parked_for_response`
   → supply the response (§3). A gate decision is the only routine pause; make it
   deliberately, never reflexively.
3. **Did it fail?** `failed` / `halted` / `interrupted` → `gauntlet logs <slug>`
   to see the failing step's transcript and dir, diagnose, then `resume`. An
   `interrupted` step that re-parks on a dirty base has a sanctioned exit:
   `resume --reset-interrupted` (§1); a branch left ahead of the manifest by a
   killed builder is adopted by a plain `resume` (§1), or rewound instead by
   `rollback` / that same verb (§4/§4a).
4. **Does the manifest say running?** Then trust *liveness*, not the manifest:
   - `in_progress` → it is genuinely working; wait and observe. If it looks
     stalled, read the stall classification before acting: the driver heartbeat
     distinguishes **`host_suspended`** (the machine slept — the run resumes
     itself on wake; deadlines are credited, do nothing) from
     **`driver_orphaned`** (process dead → `resume`) from **`agent_silent`**
     (driver alive, no recent step events → keep watching; `recover` only per
     §4). `status` prints the heartbeat age and any detected suspension
     intervals.
   - `orphaned` → the driver is gone; `gauntlet resume <slug>` reclaims it.
   - `indeterminate` → you cannot prove it is alive *or* dead. Inspect
     read-only (`logs`, `status --json`) and escalate; **never** a mutating verb
     — not even with out-of-band proof. `recover` is reserved for a state the
     tool itself can prove is the verified live target (§4); `indeterminate` is
     by definition not that. When liveness cannot be proven, you look, you do
     not touch.
5. **Terminal?** `done` / `aborted` → nothing to do. `unknown` → inspect
   read-only and escalate; never apply a mutating verb to a state the tool itself
   could not classify.

## 3. Gates and responses (the routine pauses)

**Reading the diff a gate is gating.** Do this from your own checkout, without
checking anything out:

```
git log --oneline <base>..gauntlet/<slug>
git diff <base>...gauntlet/<slug>
git show gauntlet/<slug>
```

All three are read-only and disturb nothing — this is the better habit in every
mode. For a **dedicated** run it is the only one that works: the run branch is
checked out in the run's own worktree, and git refuses a second checkout of the
same branch (`fatal: 'gauntlet/<slug>' is already used by worktree at ...`).
Never `git checkout gauntlet/<slug>`. If you want a browsable copy, make your
own worktree off the branch's tip — never adopt or edit the run's.


- **Approve** a parked `human_gate` only after you have actually reviewed what it
  is gating: `gauntlet approve <slug>`. Approval is a human ratification, not a
  formality — see the guardrails.
- **Reject** with a reason the builder can act on: `gauntlet reject <slug>
  --notes "<why>"`. The note is required and consequential: when the gate sits
  downstream of an adversarial_cycle (the PRD/plan loops, and the phase loop's
  `phase-gate` over its own iteration's `impl-cycle` — #98), reject injects your
  note into that cycle as a new fix round and re-drives, then re-parks the gate
  for a fresh decision — so a bare rejection wastes a cycle. A gate with no
  upstream cycle to iterate ends the run permanently; that terminal reject is
  refused unless you add the explicit `--terminal` flag, so a flag-less reject
  can never end a run by surprise. Reject re-drives agents, so it honors
  the judge like `approve`.
- **Respond** to a `parked_for_response` park with the human's decision:
  `gauntlet resume <slug> --response "<decision>"`. The text is passed verbatim
  to the agent that re-evaluates the conflict; be specific. **Phrasing trap:**
  a response that *states* acceptance but contains imperative verbs ("record
  these digests…", "implement it as written") is routinely classified
  `amendment_required` and re-parked. For pure acceptance use
  `--accept-artifacts` instead; keep `--response` for decisions that change
  something.

## 4. Recovery (the wedged live driver)

`gauntlet recover <slug>` exists for one narrow case: a driver that is *alive*
(so `resume` will not reclaim its live lock) but wedged, on your operator
judgment. It is fail-closed by construction — it terminates only a process it can
prove is the one Gauntlet launched, on this host, still in its recorded process
group, and it refuses on any unverifiable datum. It does **not** auto-resume:
after it marks the step `interrupted`, run `gauntlet resume <slug>` as a separate,
deliberate step. Never reach for `recover` on an `orphaned` run (that is
`resume`'s job) or an `indeterminate`/`unknown` one (inspect first).

Recover also reconciles the branch↔manifest pair it leaves behind: it snapshots
the killed branch tip to a backup ref (`refs/gauntlet/backup/<run_id>/recover-…`)
and, when the killed driver had committed work the manifest never recorded (a
builder killed before a flush), records that unmanifested range on the §6.4
audit record and as a manifest warning naming the ways out: a plain `resume`
(adopts the recognized range into the manifest and continues), `rollback`
(absorbs the unmanifested commits to a phase boundary, after backup), or
`resume --reset-interrupted` (discards the interrupted attempt,
checkpoint-preserving). All are native verbs; none needs git surgery.

## 4a. Rollback (rewinding to a phase boundary)

`gauntlet rollback <slug> --phase N` rewinds the run branch AND the manifest to
the end-of-phase-N boundary together (FR-9.9) — they never disagree afterward.
Before any rewind it writes a backup ref and a manifest snapshot, so every
rollback is reversible. The guards, in order:

- **Dirty worktree** → refuses; commit or discard first. The refusal names the
  tree it inspected — for a dedicated run that is the run worktree
  (`status --json` → `worktree.path`), not your checkout (only engine
  bookkeeping and `PR.md` are exempt).
- **Branch tip vs last recorded commit:** equal, or ahead by only engine
  bookkeeping commits → proceeds. Ahead by *real* unmanifested commits that
  descend from the last recorded commit (the recover-left-ahead shape) →
  proceeds by **absorbing** them: they are captured in the backup ref and the
  absorption is recorded as a manifest warning naming the ref. A tip that has
  **forked** from (or lost) the recorded history → refuses; restore the branch
  before rewinding.
- Rolling back past an auto-approved gate reverses it and disables
  auto-approval for the rest of the run (FR-4.2).

## 5. Evidence on demand

`gauntlet logs <slug>` prints the resolved run-instance and step dirs and the
tail of the failing step's transcript, and names the `events.jsonl` path. Use
`--step <id>` to target a specific step or a composite role sub-leaf. It is
strictly read-only and never crashes on a missing or unreadable artifact — it
tells you what is absent instead. Reach for it before every `resume` of a
`failed`/`halted`/`interrupted` run: resume blind and you may just re-hit the
same wall.

`gauntlet status` is itself evidence-rich now: run elapsed time, cost so far,
per-step `halt_reason`/`parked_reason`, heartbeat age, detected suspensions,
and the quota reset time on a usage-limit park all render without opening a
transcript. `gauntlet report <slug>` adds the per-profile cost split and
cache-effectiveness columns (cache-read share per step type) for judging where
a run's budget actually went.

## 6. Guardrails — the lines you do not cross

These hold regardless of how stuck the run is. Each exists because crossing it
defeats the safety the pipeline is built on.

- **Never approve a gate unilaterally.** A human owns every ratification. If you
  are the agent operator, surface the decision and its evidence; do not approve on
  the human's behalf to keep things moving.
- **Never `--no-judge`.** That flag disables the safety judge. It is not an
  operator convenience; using it to get past a deny is exactly the failure the
  judge exists to prevent.
- **Never work around a judge deny.** A denied action is a stop, not an obstacle.
  Surface it and ask; do not retry it by another route, re-word it, or disable the
  hook.
- **Never modify files a reviewer or builder owns.** The operator reads state and
  drives verbs. Editing the worktree, the transcripts, the manifest, or a review
  artifact by hand breaks the clean-worktree invariant that makes review diffs and
  recovery meaningful. If something needs changing, it changes through a step, not
  your editor. **The one sanctioned exception:** a `parked_artifact_invalid` park
  explicitly invites you to hand-fix the named artifact — that path is designed
  for it, and the resume revalidates and records the edit (content-hash audit)
  rather than trusting it blindly. No other state licenses an edit.

  Since P7g this guardrail is **structural rather than behavioural for every
  new run**, with no configuration: the builder's tree is a separate directory —
  `.gauntlet/worktrees/<slug>/<run-id>` inside your repo, gitignored by an
  engine-owned marker — that you have no reason to open, rather than files
  sitting in your own editor. That is the clearest operator-facing win of the
  dedicated layout, and it now applies by default. The trade, stated plainly: the agent's work no longer
  appears in the files you are already editing. It is still browsable when you
  want it (`status --json` → `worktree.path` names the exact directory), but
  reach for `gauntlet logs <slug>` for the live transcript and `git diff
  <base>...gauntlet/<slug>` for committed progress first — both answer the
  question without disturbing anything.

  Two things about that directory are worth knowing before they surprise you.
  It is **gitignored, not hidden**: `git status` stays clean, but `git status
  --ignored` lists it. And `git clean -xdff` **will** delete it — double-force
  ignores the "skip repositories" rule that `-xdf` respects. That is recoverable
  rather than fatal: the branch and the journal both survive, so `gauntlet
  resume <slug>` rebuilds the tree and verifies its HEAD against the state the
  run recorded. Uncommitted work in the tree at that instant is the one thing
  that does not survive.

## 7. Operating a `gauntlet review` run (the lightweight surface)

`gauntlet review` runs the adversarial cycle *alone* against an
already-implemented change — no PRD/plan/phase ceremony and **no routine gates**.
Everything above is the heavyweight `gauntlet run` state machine; a review run is
operated differently, and the difference matters before you reach for a familiar
verb.

- **The generic slug verbs do not see it.** `gauntlet status` / `logs` /
  `resume` / `abort <slug>` resolve heavyweight run instances under the run root.
  A review run's state lives *out-of-repo* under
  `${XDG_STATE_HOME:-~/.local/state}/gauntlet/reviews/<repo-id>/<slug>/` (`<slug>`
  is the sanitized branch name, or `pr-<N>`), so those verbs will not find it. You
  operate a review run only by re-invoking `gauntlet review` against the **same
  target** — the same branch, or the same `--pr <N>` — which re-resolves the same
  state dir. Its evidence lives in that state dir and is printed at run end, not
  through `gauntlet logs`.
- **Its routine outcome is a summary, not a gate.** With zero gates a review run
  either **completes** — printing its `REVIEW.x` fix commits and a residual-risk /
  declined-findings summary — or **parks** on an unresolved legitimate *blocking*
  finding (the cycle's fail-closed escalation, preserved unchanged). A legitimate
  *non-blocking* finding does **not** park: it completes and is surfaced as
  residual risk in that summary. Read the summary — it is the run's whole result,
  and there is no gate to approve. A review run can also pause on a provider
  **usage limit** (or a configured provider window's pre-step admission park):
  that is a pause, not a finding — re-invoke the same target **without**
  `--response` once the window replenishes, and the cycle resumes its preserved
  sub-agent session at the first incomplete sub-step. Do not inject a
  `--response` for a usage pause; there is no decision to record.
- **Resume a parked review by re-running it with the decision.** `gauntlet review
  <same-target> --response "<decision>"` re-drives the parked cycle with your note
  injected as authoritative reviewer/triager guidance (the same FR-10.4 mechanism
  as `resume --response` on a heavyweight run). Re-invoking the same target
  **without** `--response` also resumes an existing non-terminal run rather than
  starting a fresh one — it never clobbers a parked run. `--response` with no
  resumable run refuses ("nothing to resume") rather than silently starting one.
- **The §6 guardrails hold.** A review run mints no branch and never
  pushes; accepted fixes land as `REVIEW.x` commits in place on the branch under
  review (or the PR's head branch) and the human pushes. The judge boundary is
  unchanged. Nothing here relaxes §6 — you still never approve on a human's
  behalf, never `--no-judge`, and never hand-edit what a step owns (§6's one
  sanctioned hand-edit exception, `parked_artifact_invalid`, cannot arise in a
  review run — its pipeline has no artifact-validation steps).

## 8. Handoff

When you have acted, say plainly what state the run is now in and what the next
human decision is, if any. Leave the run in a state the next operator — or the
next fresh session — can read off `gauntlet status` without re-deriving anything.
That legibility is the whole point.

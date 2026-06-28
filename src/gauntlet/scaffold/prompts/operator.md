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
  Action: `gauntlet resume <slug> --response "<decision>"`.
- **`failed`** — a step failed. Action: read the evidence with `gauntlet logs
  <slug>` (and the failed step's `notes`), then recover by failure kind:
  - **A re-runnable precondition failure** (e.g. the FR-9.3 clean-handoff guard:
    "worktree dirty at round-1 review handoff") fired *before* any agent ran. The
    step `notes` name the offending uncommitted paths. Commit or stash them, then
    `gauntlet resume <slug>` re-runs the guard and continues. `status` recommends
    exactly this.
  - **A terminal failure** (a fixer that made no changes, a genuine agent error)
    cannot be advanced by a plain `resume` — it would only repeat. If a human
    decision can unblock it, inject one: `gauntlet resume <slug> --response
    "<decision>"`; otherwise `gauntlet abort`. A plain `resume` here refuses with
    that guidance instead of silently no-op'ing.
- **`halted`** — the budget/timeout guard tripped. Action: `logs`, then `resume`.
- **`interrupted`** — a step was killed mid-run. Action: `logs`, then `resume`.
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
   to see the failing step's transcript and dir, diagnose, then `resume`.
4. **Does the manifest say running?** Then trust *liveness*, not the manifest:
   - `in_progress` → it is genuinely working; wait and observe.
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

- **Approve** a parked `human_gate` only after you have actually reviewed what it
  is gating: `gauntlet approve <slug>`. Approval is a human ratification, not a
  formality — see the guardrails.
- **Reject** with a reason the builder can act on: `gauntlet reject <slug>
  --notes "<why>"`. The note is required and consequential: when the gate sits
  downstream of an adversarial_cycle (the PRD/plan loops), reject injects your
  note into that cycle as a new fix round and re-drives, then re-parks the gate
  for a fresh decision — so a bare rejection wastes a cycle. (A gate with no
  upstream cycle to iterate ends the run.) Reject re-drives agents, so it honors
  the judge like `approve`.
- **Respond** to a `parked_for_response` park with the human's decision:
  `gauntlet resume <slug> --response "<decision>"`. The text is passed verbatim
  to the agent that re-evaluates the conflict; be specific.

## 4. Recovery (the wedged live driver)

`gauntlet recover <slug>` exists for one narrow case: a driver that is *alive*
(so `resume` will not reclaim its live lock) but wedged, on your operator
judgment. It is fail-closed by construction — it terminates only a process it can
prove is the one Gauntlet launched, on this host, still in its recorded process
group, and it refuses on any unverifiable datum. It does **not** auto-resume:
after it marks the step `interrupted`, run `gauntlet resume <slug>` as a separate,
deliberate step. Never reach for `recover` on an `orphaned` run (that is
`resume`'s job) or an `indeterminate`/`unknown` one (inspect first).

## 5. Evidence on demand

`gauntlet logs <slug>` prints the resolved run-instance and step dirs and the
tail of the failing step's transcript, and names the `events.jsonl` path. Use
`--step <id>` to target a specific step or a composite role sub-leaf. It is
strictly read-only and never crashes on a missing or unreadable artifact — it
tells you what is absent instead. Reach for it before every `resume` of a
`failed`/`halted`/`interrupted` run: resume blind and you may just re-hit the
same wall.

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
  your editor.

## 7. Handoff

When you have acted, say plainly what state the run is now in and what the next
human decision is, if any. Leave the run in a state the next operator — or the
next fresh session — can read off `gauntlet status` without re-deriving anything.
That legibility is the whole point.

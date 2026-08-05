# P7c split: the seam between P7c-1 and P7c-2

> **Status: proposed by the P7c builder, ratified by the maintainer
> (2026-08-05) as a phasing change.** It splits the ratified §13 stage P7c into
> two commits. It changes **no** ratified recommendation, adds no deferral, and
> moves no work out of P7 — every P7c deliverable still lands, in one of the two
> commits named here.
>
> This document exists because the second commit must be implementable from
> repository evidence alone. It records the design of BOTH commits, decided
> together before either was written, so P7c-1 cannot box in P7c-2.

---

## 1. Why P7c splits, and where the seam is NOT

P7c as scoped is the largest stage in the plan: the worktree lifecycle, the
`RunPaths` carrier, the export dir, migration, the additive `status --json`
block, the §14.4 refusal, the §18 operator surface, and a test matrix whose
centrepiece is an autouse fixture asserting acceptance A1 across **every**
existing verb test. Landing it as one commit risks shipping a half-built
lifecycle, which the phase prompt explicitly forbids.

**The seam is not "by layer."** An initial proposal put the mechanism
(lifecycle module + carrier) in one commit and the verbs + operator surface in
the other. That is the half-built lifecycle: a tree nothing can drive. Rejected.

**The seam is not "migration vs everything else" either**, which was the second
proposal and is the one this document corrects. It fails on a safety coupling,
described next.

---

## 2. The coupling that decides the seam

Migration has two halves, and only one of them is separable.

* **The migration DECISION** — "this run predates the dedicated layout, so it
  keeps driving `same_tree`, and nothing may move it implicitly." This is a
  *safety boundary*, and it is **not optional in P7c-1**.
* **The migration ACTION** — "the operator explicitly asked to move it, so move
  it, journalled, copy-never-move." This is an added capability and it *is*
  separable.

Why the decision cannot wait. The moment `worktree.mode: dedicated` exists, an
operator can set it on a repository that already has runs. Those runs were born
under the pre-P7c layout. If P7c-1 resolved a run's mode from **config**, the
next `resume` of every existing run would create a worktree and move the run
into it — silently, with no operator action, at a moment the operator believes
they only changed a default. That is precisely the auto-migration spike §10
forbids ("a pre-P7 run is never auto-migrated, and never wedged"), and it would
be introduced *by the commit that does not mention migration*.

So P7c-1 must implement mode resolution correctly — evidence first, config only
for **new** runs — or it is not safe standing alone.

**Seam, final:** P7c-1 carries the decision and the refusal. P7c-2 carries the
action.

---

## 3. Mode resolution — the shared contract both commits obey

One function, `RunManager._effective_worktree_mode(man)`, is the single
authority. It resolves in this order, and **config is consulted last and only
for a run that does not exist yet**:

| # | evidence | resolves to | why it wins |
|---|---|---|---|
| 1 | `git worktree list --porcelain` registers a worktree for `man.branch` | `dedicated` | the tree is observable and is the ground truth; available with a dead driver (spike §10) |
| 2 | the journal carries a `WorktreeAdopted` with no later `WorktreeReleased` | `dedicated` | the tree is *missing* but was adopted — this is §11 row 2, and the answer must be "dedicated, recreate it", never "same_tree" |
| 3 | `man.worktree_mode` is recorded | that value | what the run was BORN as; additive optional manifest field (§16 permits it, no schema bump) |
| 4 | nothing above | `same_tree` | a pre-P7c run. The legacy population, forever (§16) |

`config.worktree.mode` appears in exactly one place: `start()`, choosing what a
**new** run is born as, which it then records under rule 3.

Rules 1 and 2 are §10's detection rule verbatim, in both directions. A run is
`same_tree` iff it has no registered worktree AND no unreleased
`WorktreeAdopted` — which is the same predicate P7c-2 uses to decide a run is
*eligible* for migration.

### What P7c-1 does when config and the run disagree

Config `dedicated`, run born `same_tree`: the run **keeps driving
`same_tree`**, unchanged, and `status --json` reports
`worktree: {mode: "same_tree", ...}` honestly. P7c-1 advertises no migration
action, because the command does not exist yet — surfacing a command an
operator cannot run is worse than silence. P7c-2 adds the action and the
`next_actions` entry in the same commit.

---

## 4. What each commit contains

### P7c-1 — the dedicated run worktree, for runs that start under it

1. `worktree.mode: same_tree | dedicated` in `RunConfig`, **default
   `same_tree`** (§13). `WorktreeConfig` refuses any other value at load.
2. The derived root `<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>`, no
   knob (§6.2/§6.4).
3. `engine/worktree.py`: create, `git worktree lock --reason`, discover,
   recreate, teardown; the §11 rows mapped to R1 safe actions; the submodule
   fail-closed park (§7); explicit `--expire` on every prune (§11 row 7).
4. `RunPaths` as the runtime **carrier** (P7b F-001, deferred here by name),
   threaded through RunManager, Orchestrator, StepContext, RecoveryExecutor,
   the verifier and the judge boundary.
5. The two-file bookkeeping export dir (§4.4) with the authority question
   answered in code: journal authoritative, projection derived, export
   write-only.
6. **Mode resolution per §3, including the refusal to auto-migrate**, and
   `Manifest.worktree_mode` (additive, optional).
7. `WorktreeAdopted` / `WorktreeReleased` journal kinds (additive, state-less
   audit events) and `recreate_worktree` for §11 row 2.
8. The `worktree_unavailable` park and `gauntlet resume <slug> --same-tree`.
9. The additive `status --json` `worktree` object at `schema_version: 1`,
   byte-identical in `schemas/status.json` and the embedded mirror.
10. The §14.4 "you are inside a run worktree" refusal.
11. The §18 operator-surface delta in all three files, **minus** the
    migration row (addition 4's `worktree_unavailable` and missing-worktree
    rows land here; the migrate row lands in P7c-2).
12. Tests: the §12.1 autouse A1 invariance fixture over every verb test; the
    snapshot matrix over `tree_kind ∈ {main, linked}`; A2 in both modes and the
    mixed pairing; A3 (recreate, HEAD matches the journal head's `branch_sha`);
    §11 rows 2/5/10 end to end; new `_crash_child` worktree boundaries; **the
    no-auto-migration test** (config `dedicated` + a run born `same_tree` →
    still `same_tree` after resume, operator checkout untouched).

### P7c-2 — the migration action

1. `gauntlet migrate-worktree <slug>`: explicit, copy-never-move, journalled
   (§10). Steps 1–6 of §10 verbatim.
2. The §10 refusal matrix: refused under a live **and** under an indeterminate
   driver; terminal runs never migrated; a blocked migration leaves the run
   fully resumable in `same_tree` with the blocker named.
3. Rollback of a migration: `worktree unlock` + `remove` + `WorktreeReleased`.
4. The `status --json` `next_actions` entry offering migration, and the
   §18 addition-4 playbook row for it, in all three operator-surface files.
5. Tests: refused-under-live, refused-under-indeterminate, blocked-stays-
   resumable, migrate-then-rollback round trip.

---

## 5. What P7c-1 must NOT do, so P7c-2 stays open

* **Do not** consult `config.worktree.mode` anywhere except `start()`. Any
  other reader re-introduces auto-migration.
* **Do not** treat "no worktree registered" as "needs a worktree." It means
  `same_tree`, which is a valid terminal answer, not a repair target.
* **Do not** spend the `status --json` `worktree` object's shape on P7c-1's
  needs alone — it must already be able to express a `same_tree` run that is
  *eligible* for migration, so P7c-2 adds a `next_actions` entry rather than a
  schema field. The object therefore reports observed facts (mode, path,
  present, locked, prunable), never a recommendation.
* **Do not** write `WorktreeAdopted` from any path except a genuine
  create/recreate transition. P7c-2's migration appends the same kind, and a
  steady-state re-adoption event would make the two indistinguishable.

---

## 6. What neither commit does

Unchanged from the ratified spike: no `PRD-gauntlet.md` or
`RECOVERY-REDESIGN-PLAN.md` edit; none of the §15 deferrals (D1–D6), most
importantly **D1** (`refs/gauntlet/state/<run>` anchoring), which P7 must not
absorb; no change to `gauntlet review` (§14.3); and **P7d — flipping the default
to `dedicated` — remains a separate stage** gated on a dogfood run that
exercises §11 rows 2, 5 and 10.

Until P7d, P7 acceptance A1/A2/A3 are met **only for runs a human has
explicitly opted into `dedicated` mode**. Neither commit here changes that.

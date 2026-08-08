# P7 completion: the split across P7e–P7h

> **Status: proposed by the P7-completion builder.** The maintainer ratified
> the *location* (§1); this document proposes the *phasing* of the work that
> follows from it. It changes no ratified recommendation beyond the one the
> maintainer already ruled on, adds no deferral, and moves no work out of P7 —
> every remaining P7 deliverable lands in one of the four commits named here.
>
> It exists because the maintainer asked for one run, not one commit, and
> because the later commits must be implementable from repository evidence
> alone. Both commits of `proposals/P7c-split-seam.md` were designed together
> for the same reason; this follows that shape. All four are designed here,
> before any is written, so P7e cannot box in P7h.

---

## 1. The ratification this rests on

`proposals/P7d-gate-blocker.md` recorded an UPSTREAM CONFLICT: spike §6.2's
ratified root, `<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>`, is not
writable by the `claude` CLI, which drives every `builder` and `verifier` step.
§5 recommended sub-option **1A**.

**The maintainer ratified 1A — `<repo>/.gauntlet/worktrees/<slug>/<run-id>` —
on 2026-08-06**, in the session that opened this tranche, in response to a
direct question naming the four options and their costs. That decision is
recorded in `BOOTSTRAP-NOTES.md`'s 2026-08-06 entry and in P7e's commit body,
which is the authority this tranche deviates from §6.2 under. Nothing here
amends `proposals/P7-worktree-spike.md`; §6.2 still reads as ratified and this
document is the proposal against it, exactly as P7d's was.

---

## 2. Why it splits, and where the seam is NOT

Four things remain: the relocation, a per-adapter writability preflight, the
default flip, and the tree-guard retirement. They are not four independent
edits — three of them are ordered by hard preconditions.

**The seam is not "mechanism vs policy."** Landing the relocation and the flip
together would put a ~101-test suite migration in the same diff as a change to
where every run worktree lives, and a reviewer could not tell a relocation
defect from a test-assumption breakage. Rejected.

**The seam is NOT between the relocation and the migration of runs already at
the old root.** This is the coupling that decides the first commit, and it is
the same shape as P7c's. The moment the derived root moves, an adopter's
existing `dedicated` run has its tree at a path the new engine's `observe` no
longer recognises — `is_inside_worktrees_root` filters it out. Resolver rule 1
then fails. Rule 2 (an unreleased `WorktreeAdopted` in the journal) still
answers `dedicated`, so the run does *not* silently fall back to the operator's
checkout — but only because rule 2 exists. A relocation shipped without the
legacy-root case handled explicitly would rest that safety property on a
backstop rather than on a decision, and would then fail closed with
`worktree add`'s E2-A refusal offering `--same-tree`, which is precisely the
action that *would* drop the run onto the operator's checkout. So the
relocation and the legacy-root handling are one commit.

**The seam is NOT between the flip and the test-suite migration.** Every
fixture that does not name a mode inherits the new default in the same instant.
They are one commit by construction.

**The seam IS between the flip and the guard retirement**, even though the
retirement's precondition is the flip. Between them the machine is in the
*safe* intermediate state — default `dedicated` and the tree guard still
written — which is strictly more exclusion, not less. That makes the boundary
reviewable and independently revertible.

---

## 3. The ordering, and the precondition behind each arrow

```
P7e  relocation + legacy-root migration      (no precondition)
  │   default still same_tree
  ▼
P7f  per-adapter writability preflight       (independent of the root;
  │                                           must precede the flip)
  ▼
P7g  flip the default + suite migration      (requires P7e: without it every
  │                                           new run is born broken)
  ▼
P7h  retire the tree guard to read-only      (requires P7g: P7c-1's
                                              CORRECTION 1 states it)
```

* **P7f before P7g, not after.** The preflight is the detector that makes a
  writability refusal legible (`P7d-gate-blocker.md` §5 Option 4). Its value is
  highest at the moment `dedicated` becomes the default for adopters whose
  layout this tranche has not measured. Shipping the flip first would reproduce
  P7d's failure shape — a silent, model-dependent refusal — for exactly one
  release.
* **P7f is genuinely independent of P7e.** Option 4 says so, and it is why it
  is a separate commit rather than folded in: it is the one deliverable whose
  correctness does not depend on where the root is.
* **P7h last.** P7c-1's CORRECTION 1: the guard "could not retire while
  `same_tree` was the default, because a dedicated run that stopped writing it
  would stop excluding a concurrent `same_tree` run of another slug on the
  operator's checkout." P7g is what makes that precondition true; P7h verifies
  it holds before retiring anything.

---

## 4. What each commit contains

### P7e — relocate the run worktree root out of `.git/`

1. `worktrees_root` derives from the **main worktree root**, not the git common
   dir and not the invoking checkout: `<main-worktree>/.gauntlet/worktrees/`.
   Measured (E13): `git worktree list --porcelain` reports the main worktree
   first from every vantage point — the main checkout, an adopter's linked
   worktree, and a run worktree — while `rev-parse --show-toplevel` does not.
   That vantage-independence is the one property §6.2 got for free from the
   shared common dir, and it is what keeps `observe`'s scoping, the §14.4
   refusal and `_run_tree_excludes` coherent (design problem A).
2. The self-ignoring marker at `<main>/.gauntlet/worktrees/.gitignore`,
   re-established on **every** drive via the `_ensure_run_root_gitignore`
   pattern. It goes at `worktrees/`, never at `.gauntlet/`, because
   `.gauntlet/config.yaml` is a *tracked* adopter file.
3. The legacy-root case, explicit: detect a run whose tree is registered under
   `<git-common-dir>/gauntlet/worktrees/`, refuse to drive it with a message
   naming the relocation command rather than `--same-tree`, and extend
   `migrate-worktree` to relocate it. Never silently `same_tree`.
4. The `cli.py` §14.4 refusal and its `operator_checkout` hint, both re-derived
   from the main worktree root (today's `common.parent if common.name == ".git"`
   heuristic does not survive the move).
5. The operator surface, in **four** files (§18.1 says three and is wrong), plus
   every non-playbook operator-facing string naming the worktree path.
6. Tests: the E11 properties as unit-level assertions where they are assertable;
   legacy-root detection, refusal and relocation; vantage-independence of the
   derived root.

### P7f — the per-adapter writability preflight

1. A deterministic probe exercising each write mechanism **by name** — the
   `Write` tool, the `Edit` tool, and a shell redirection — with no model
   choice, reading the **post-tool** outcome (did the file land?) rather than
   the PreToolUse hook's verdict, which is the specific blindness §2.4 names.
2. **Per adapter, never generalized** (§2.5 probe 2: `codex` is unaffected,
   `claude` is). A codex-only check would call the tree healthy while every
   claude step failed silently — the dogfood's exact shape.
3. Two surfaces: a `gauntlet doctor` check, and a start-time preflight that
   parks with a named reason before any agent step, in the fail-closed shape of
   the §7 submodule park.

### P7g — flip `worktree.mode` to `dedicated`

1. One line in `RunConfig`; the blast radius is the suite. P7d measured it on a
   throwaway worktree: `101 failed, 2874 passed, 3 skipped … 13 errors`.
2. Re-base the A1 fixture's baseline **per run** rather than once at first tree
   creation (design problem D), keeping the property strict. P7d spot-checked
   `test_lock_released_on_done_and_park` as a test that legitimately commits in
   the operator's checkout between two runs; under the flip that shape is
   common, and a strict-but-imprecise property gets suppressed the first time it
   cries wolf.
3. Triage every failure into *vacuous* (the test's precondition no longer
   exists — `test_worktree_migrate_p7c.py` is mostly this, since migration
   applies only to `same_tree` runs), *wrong* (the assertion encoded a
   `same_tree` truth), and *genuine A1 violation*. No mass edits; a rewritten
   assertion must still assert something and must actually enter the path it
   names.
4. Every new `operator_tree_verb` use justified in the commit body by the
   verb's contract, never by the failure it quiets (design problem C).

### P7h — retire the worktree-global tree guard to read-only

1. Verify the precondition holds before retiring anything.
2. The guard is **read** for one release so a half-migrated machine cannot
   double-drive (§10 step 6, design problem F).
3. `finish` must take the per-run lock — it passes `run_dir=None` today and
   would stop excluding a live driver of its own run once the guard retires.
   `clean`'s drive lock (P7d.1 review F-005) must still hold under the
   retirement.
4. The four readers, each asked what it is actually querying — "is anything
   driving this tree?" vs "is this run being driven?" — and whether the per-run
   lock answers it: `supervisor.driving_lock`, `store.worktree_lock`,
   `store._ownership`, `service._refuse_if_worktree_locked`, plus
   `views._lock_context`. A surface that silently starts answering "no driver"
   for a live run is worse than one that refuses (design problem E).
5. The dogfood (§5) and its `BOOTSTRAP-NOTES.md` entry.

---

## 5. The dogfood, and what it must prove

P7d's dogfood proved §11 rows 2, 5 and 10 against a real run's real tree; those
need not be re-proven. What it could **not** prove, and what this tranche must:
a real `gauntlet run` in `dedicated` mode reaching a phase commit with a live
**claude** builder actually writing in the run tree.

E12 already measured the underlying property in isolation — all three write
mechanisms land at the 1A root — but an isolated probe is not a run, and P7d is
the standing evidence that a green component check can coexist with a run that
cannot write a file.

---

## 6. What none of these commits does

Unchanged from the ratified spike and from P7c's seam: no `PRD-gauntlet.md`,
`RECOVERY-REDESIGN-PLAN.md` or `policy.yaml` edit; no approved run artifact
touched; the ratified spike and the P7c seam document **unamended**; none of the
§15 deferrals **D1–D6**, most importantly **D1** (`refs/gauntlet/state/<run>`
anchoring), which P7 must not absorb; and no change to `gauntlet review`
(§14.3).

Also explicitly out of scope, because finishing P7 is not the same as finishing
the PRD: `RECOVERY-REDESIGN-PLAN.md` §10's closing criteria — the §8
five-incident dogfood matrix, issues #62/#63/#72 regression coverage, and fault
injection across all durable boundaries. Those are a separate tranche.

**Acceptance.** Every P7c and P7d commit body ends by noting that P7 acceptance
A1/A2/A3 holds only for runs a human explicitly opted into `dedicated`. **P7g is
where that stops being true**, and its commit body says so directly rather than
repeating the caveat.

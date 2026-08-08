Implement P7b (lock and liveness relocation) of the Gauntlet recovery redesign in:

/Users/johnpletka/projects/gauntlet

SCOPE GATE: This prompt authorizes **P7b ONLY** — stage 2 of the four ratified
P7 stages (proposals/P7-worktree-spike.md §13). It does NOT authorize P7c
(dedicated worktree mode, lifecycle, `git worktree lock`, migration,
`--same-tree` fallback, `recreate_worktree`) or P7d (flipping the default). The
run still drives the operator's checkout when you are done, and all three §6 P7
acceptance criteria remain UNMET after this phase. Do not describe your output
as "P7" — a prior handoff did, and the reviewer correctly rejected it (F-001).

Read AGENTS.md and CLAUDE.md completely before acting. Then read:

- proposals/P7-worktree-spike.md — RATIFIED 2026-08-04. Read the header
  (what was and was not authorized), §8 (lock scope, the decision you are
  implementing), §10 (migration), §13 (phasing), §18 (operator surface).
  **Treat the spike as ratified but fallible**: §9.3 was already found wrong
  during P7a (it recommended a fail-closed raise that would have broken
  `gauntlet review`). If you find another such conflict, surface it as an
  UPSTREAM CONFLICT and record the correction in your commit body — do not
  implement a recommendation you can demonstrate is wrong, and do not silently
  amend the ratified document.
- RECOVERY-REDESIGN-PLAN.md §2 (R1, R4, R5, R8), §5.3, §6 P7, §7, §8.
- src/gauntlet/engine/run.py — the whole lock machinery: `DRIVING_LOCK_NAME`
  (line 62), `_LockRecord` (295), `_LockHandle` (369), `_run_root_dir` (853),
  `_lock_path` (856), `_ensure_run_root_gitignore` (860), `_read_lock` (884),
  `_lock_is_live` (892 — note the DELIBERATE fail-closed asymmetry: an
  unverifiable live pid blocks, the opposite of `procident.process_is_alive`),
  `_lock_busy_message` (927), `_new_lock_record` (934), `_link_into_place`
  (953), `_try_reclaim` (975), `_acquire_worktree_lock` (999), `_take_handle`
  (1031), `_release_worktree_lock` (1037), `_release_lock_if_nonce` (2350),
  and the design note at 816-826 explaining why the lock is worktree-GLOBAL
  today and how it complements `_refuse_if_active_run` (814).
- The 8 acquisition sites: run.py 1131, 1299, 1589, 1609, 1908, 1942, 2622, 3143.
- src/gauntlet/engine/operator.py — `_lock_state` (288), `driver_info` (340),
  `driver_liveness` (354), the LIVENESS_* constants (59-62), the FR-2.4 row
  comments (317-330), and the second `_lock_state` consumer at 1211.
- The 5 liveness consumers: cli.py:728, interactive.py:250, run.py:2025,
  run.py:2267, operator.py:1211.
- src/gauntlet/engine/recovery_exec.py — `WorktreeLockGuard` (1850) and its
  `lock_path` derivation (1863), plus how `RecoveryExecutor` receives
  `run_root`.
- src/gauntlet/engine/execution.py — `RunPaths`, `StateDirNotContained`,
  `state_outside_worktree` / `artifacts_outside_worktree` (all added in P7a).
- src/gauntlet/engine/gitops.py — `ROOT_SCOPE` and `git_common_dir` (both
  added in P7a).
- tests/unit/test_root_scope.py — the audit that must stay green.
- tests/unit/test_operator.py, test_status_json.py, test_recovery_executor.py,
  test_resume_crash.py, tests/unit/_crash_child.py.
- schemas/status.json — additive changes only; `schema_version` stays 1 (its
  own `$comment` records the P5/P6 precedents).

Repository state:

- Branch: gauntlet/recovery-redesign, at ddd25cf (pushed).
- P7.0 spike e5364fd + 59f9e6d + 8537f70 (ratified).
- P7a: b2441f8 (containment defects) + b9fd104 (RunPaths / work_root) +
  ddd25cf (P7a.1, review fixes: ROOT_SCOPE enforcement, governance decoupling).
- Suite at ddd25cf: 2842 passed, 3 skipped, 76 deselected.

WHAT P7b DELIVERS (ratified §13 + §8.3):

1. The driving lock moves from the worktree-global path
   `<run_root>/.driving.lock` to the per-run path
   `<run_root>/<slug>/<run-id>/.driving.lock`, keeping the existing
   `_LockRecord` PID + `ProcessIdentity` reclaim semantics UNCHANGED.
2. A repo-global lock at `<git-common-dir>/gauntlet/.repo.lock`, held ONLY for
   short shared-git critical sections (branch create/delete, snapshot ref
   creation) — never for a whole drive, or concurrent runs serialize.
3. `driver_info` learns to read both the new and the legacy path.

`git worktree lock` is the THIRD layer in §8.3 and belongs to P7c: there is no
run worktree to lock yet. Do not implement it here.

DESIGN PROBLEMS YOU MUST RESOLVE OR SURFACE (do not skip this section):

A. **The double-driving regression.** This is the load-bearing one. Today's
   lock is worktree-global BY DESIGN (run.py:816-826): holding it for slug A
   blocks every driving verb for every slug B, because they share one tree.
   Demoting it to a per-run path while the tree is still shared means slug A
   and slug B can drive the same worktree concurrently — and two concurrent
   `gauntlet run <same-slug>` invocations mint DIFFERENT run ids, so they take
   different lock paths and both proceed, with only the racy `active-run.txt`
   check between them. Git's one-branch-one-worktree rule that heals this
   (spike E2-A/E2-B) does not exist until P7c gives each run its own tree.
   The spike's §8.3 lock model is correct for the END state and does not
   address the transition. Resolve this explicitly. The obvious answer is to
   ADD the per-run lock while RETAINING the worktree-global one until P7c
   retires it, but if you choose otherwise, justify it against R1 and the
   FR-10.5 mutual-exclusion guarantee. Whatever you choose, a test must prove
   two concurrent drives of different slugs cannot both proceed.

B. **Lock-path bootstrapping in `start()`.** The lock is acquired at run.py:1131
   BEFORE the run dir is created (~1205) and before `active-run.txt` is
   written. A per-run lock path does not exist yet at acquisition time.
   Whatever creates it must also ensure the run dir's self-ignoring
   `.gitignore` (`*`) exists, because `_ignore_run_dir` is the Orchestrator's
   job and runs later — otherwise the lock file is briefly visible to
   `git status` and dirties the tree before the first clean-handoff guard.

C. **The FR-2.4 liveness table.** `driver_info(run_root, slug)` reads ONE lock
   and has a "foreign lock" row (`rec.slug != slug` → `LIVENESS_NONE`, row b)
   that becomes unreachable for per-run locks. It must remain correct for
   legacy locks still at the old path. `driver_info` must also now resolve
   which run instance to inspect; reuse the existing safe resolution
   (`operator.resolve_run_instance` + the containment chain in
   `cli._resolve_run_instance_dir`), never an unvalidated path join.

D. **Legacy locks are READ, never WRITTEN** (spike §10). A P7b engine must
   refuse when a legacy `<run_root>/.driving.lock` is held by a live process,
   so a half-migrated machine (two Gauntlet versions, or an in-flight run
   started by the old engine) cannot double-drive. It must never create one.

E. **`WorktreeLockGuard`** (recovery_exec.py:1850) derives its path from
   `repo_root / run_root / DRIVING_LOCK_NAME` and verifies "the lock names THIS
   process". It must follow the lock to its new home without weakening that
   check or its refusal to reclaim a foreign stale lock (reclaim policy stays
   in RunManager).

F. **What the repo-global lock actually protects in P7b.** The worktree
   add/remove/prune sections it exists for do not land until P7c. If it has no
   live critical section yet, say so plainly rather than inventing one — and
   make sure it is exercised by a test rather than dead code.

DELIVERABLES:

- The per-run driving lock, the retained/renamed worktree-global guard per
  decision A, and the repo-global lock, all with unchanged reclaim semantics.
- `driver_info` / `driver_liveness` reading both paths, with the FR-2.4 table
  still total and every row still reachable or documented as legacy-only.
- `WorktreeLockGuard` following the lock.
- `status --json`: any new field is additive at `schema_version: 1`; the
  byte-identical `schemas/status.json` guard must pass.
- Tests, at minimum:
  * two concurrent drives of DIFFERENT slugs cannot both proceed (decision A);
  * two concurrent `start`s of the SAME slug cannot both proceed;
  * a live legacy lock at the old path refuses a new-engine driving verb;
  * a stale (proven-dead) lock at either path is reclaimed, and an
    unverifiable-live one is NOT (the `_lock_is_live` asymmetry);
  * a run whose lock file is deleted mid-drive behaves per R1;
  * `_crash_child` boundaries around lock acquire/release still recover;
  * `tests/unit/test_root_scope.py` stays green — new git calls must name
    `work_root` or `operator_root`, never the ambiguous `repo_root`.

PROCESS REQUIREMENTS:

- Work on gauntlet/recovery-redesign. Nothing lands on main except via PR.
- Do NOT modify PRD-gauntlet.md, RECOVERY-REDESIGN-PLAN.md, policy.yaml, or any
  approved run artifact. Do not amend the ratified spike; corrections go in the
  commit body as an explicit "CORRECTION TO THE RATIFIED SPIKE" note.
- Do not weaken or delete a passing test to make this phase pass. The suite only
  grows.
- `uv run pytest` takes ~13 minutes. Read the SUMMARY LINE, not a piped exit
  code (a piped exit code lies here). Run the integration suite locally before
  the handoff.
- Commit as `P7b: <imperative summary>` with a body naming: what moved, decision
  A's resolution and why, which FR/R references apply, the migration behaviour
  for legacy locks, and an explicit "NOT DONE, and not claimed" paragraph
  listing P7c/P7d and the three still-unmet acceptance criteria.
- Do not self-review; hand off to the reviewer.

At completion, stop and report:

PHASE COMPLETE
Phase: P7b — lock and liveness relocation
SHA: <commit>
Tests: <N passed, M skipped, K deselected> (from the summary line)
Decision A: <how concurrent-drive exclusion is preserved while the tree is
  still shared, and what P7c will retire>
Migration: <what a legacy lock does on first contact with a P7b engine>
Still unmet: P7 acceptance A1/A2/A3 — <one line each on what remains>
Deferrals: <anything pushed to P7c, explicitly including `git worktree lock`>
Upstream conflicts: <any ratified-spike recommendation you found to be wrong,
  and what you did instead>

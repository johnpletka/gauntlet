Implement P7c (dedicated run worktree behind a flag) of the Gauntlet recovery
redesign in:

/Users/johnpletka/projects/gauntlet

PRECONDITION: P7b (7f9787e) + P7b.1 (9da3189) are awaiting the reviewer's
confirm pass. Do not start P7c until that pass is recorded and the phase is
accepted. If it is not, stop and say so.

SCOPE GATE: This prompt authorizes **P7c ONLY** — stage 3 of the four ratified
P7 stages (proposals/P7-worktree-spike.md §13). It does NOT authorize P7d
(flipping the default to `dedicated`). `worktree.mode` ships defaulting to
`same_tree`, and P7 acceptance A1/A2/A3 are met only for runs a human has
explicitly opted into `dedicated` mode — say so plainly rather than claiming
P7 is done. It also does NOT authorize the §15 deferrals (D1–D6), most
importantly D1 (`refs/gauntlet/state/<run>` anchoring), which P7 must not
absorb, nor any change to `gauntlet review` (§14.3 ratified it out of scope).

Read AGENTS.md and CLAUDE.md completely before acting. Then read:

- proposals/P7-worktree-spike.md — RATIFIED 2026-08-04. Read the header (what
  was and was not authorized), §4 (state-root split and §4.4's table of what
  moves and what does not), §6 (worktree root location, §6.4 on why there is no
  knob and what a containment validator would have to do), §7 (nested/adopter
  repos — submodules need an explicit `submodule update --init` or a refusal),
  §8.1 (git's own one-branch-one-worktree rule — the guarantee that REPLACES
  P7b's tree guard), §8.3 (layer 3, `git worktree lock --reason`), §9 (the
  full catalogue of path assumptions, with the **W/O/G/!** legend), §10
  (migration), §11 (the nine failure classes and their R1 safe actions), §12
  (test strategy — the autouse A1 invariance fixture), §18 (operator surface).
  **Treat the spike as ratified but fallible.** Two of its recommendations have
  already been found wrong when implemented: §9.3 during P7a, and §8.3/§10's
  lock model during P7b (see that commit's CORRECTION section). If you find a
  third, surface it as an UPSTREAM CONFLICT and record the correction in your
  commit body — do not implement a recommendation you can demonstrate is wrong,
  and do not silently amend the ratified document.
- RECOVERY-REDESIGN-PLAN.md §2 (R1, R2, R4, R6, R8, R9), §4.7, §5.3, §6 P7,
  §7 (the test matrix and the global acceptance property), §8, §9.
- The P7b commit bodies: `git show 7f9787e --stat` and `git log -1 9da3189`.
  They record the decisions P7c inherits and the two the reviewer deferred to
  P7c by name (F-001's RunPaths threading, F-003's paired-lock evidence).
- src/gauntlet/engine/execution.py — `RunPaths` (line 116) and its
  `git_common_dir()` (now resolved from `repo_root`, P7b.1/F-001); `StepContext`
  (182) and its `work_root` field + `paths` property (234) — **which nothing
  currently calls**; `StateDirNotContained` (319); `engine_bookkeeping_candidates`
  (451); `run_bookkeeping_paths` (528); `governed_artifact_paths`.
- src/gauntlet/engine/run.py — `RunLayout` (611), `RunManager.operator_root`
  (~672) and `work_root` (~682, currently `repo_root` and run-INdependent);
  the P7b lock block: the design note above `_tree_lock_path` (842),
  `_run_lock_path` (852), `_ensure_run_dir_gitignore` (888), `_read_lock` (907),
  `_acquire_one` (979), `_acquire_worktree_lock` (~1010), `_attach_run_lock`
  (1057), `_release_worktree_lock`, `_lock_paths_for` (~2440); the verbs
  `start` (1113), `resume` (1309), `approve` (1962), `reject` (1994), `abort`
  (2121), `clean` (2864), `finish` (2933), `rollback` (3214);
  `_prepare_run_branch` (740), `_resolve_base_branch` (722).
- src/gauntlet/engine/locking.py + repolock.py (both new in P7b) — the shared
  tri-state read `read_lock_state`, the single `record_is_live` reclaim rule,
  and repolock's module docstring, which states exactly what the repo-global
  lock does and does NOT cover today.
- src/gauntlet/engine/gitops.py — `ROOT_SCOPE` (105), `git_common_dir` (266),
  `add_worktree` (549), `remove_worktree` (557), `prune_worktrees` (564),
  `delete_branch` (602).
- src/gauntlet/engine/verify.py — `make_disposable_copy` (~783) and
  `discard_disposable_copy` (~820), which already take the repo-global lock.
- src/gauntlet/engine/operator.py — `_lock_state` / `_lock_state_scoped` /
  `driver_info` and the FR-2.4 row comments; `resolve_run_instance` (~903);
  `SCHEMA_VERSION` and the embedded `_STATUS_SCHEMA_JSON` mirror.
- src/gauntlet/cli.py — `Path.cwd()` at 169/188/203/261/364 and the
  `_resolve_run_instance_dir` containment chain (206).
- schemas/status.json — read the `$comment` compatibility policy in full.
  Additive only; `schema_version` stays 1.
- src/gauntlet/engine/config.py — `RunConfig` (493), `run_root` (513),
  `asset_root` (520), `_validate_repo_relative` (~171).
- Operator surface, BOTH copies plus the playbook:
  `.claude/skills/gauntlet-operator/SKILL.md`,
  `src/gauntlet/scaffold/skills/gauntlet-operator/SKILL.md`,
  `src/gauntlet/scaffold/prompts/operator.md`.
- tests/unit/test_root_scope.py (the audit that must stay green),
  test_drive_lock_p7b.py, test_operator.py, test_status_json.py,
  test_recovery_executor.py, test_resume_crash.py, test_recovery_unification.py,
  tests/unit/_crash_child.py (note its `lock:<point>:<sig>` mode, added in P7b).

Repository state:

- Branch: gauntlet/recovery-redesign, at 9da3189 (push before you start if it
  is not already pushed).
- P7.0 spike e5364fd + 59f9e6d + 8537f70 (ratified).
- P7a: b2441f8 + b9fd104 + ddd25cf. P7b: 7f9787e + 9da3189.
- Suite at 9da3189: 2899 passed, 3 skipped, 76 deselected.
- Integration suite at 7f9787e: 70 passed, 6 skipped (~54 min, real CLIs).

WHAT P7c DELIVERS (ratified §13):

1. `worktree.mode: same_tree | dedicated` in RunConfig, **default `same_tree`**.
2. The run worktree at `<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>`,
   derived, no config knob (§6.2/§6.4).
3. Lifecycle: create, `git worktree lock --reason`, discover, recreate,
   teardown — with the §11 rows mapped to R1 safe executable actions.
4. The two-file bookkeeping export dir inside the run worktree (§4.4).
5. Migration for existing `same_tree` runs (§10): explicit, copy-never-move,
   journaled, and never automatic.
6. The additive `status --json` `worktree` object at `schema_version: 1`.
7. The "you are inside a run worktree" refusal (§14.4).
8. The §18 operator-surface delta, in the same commit series.

DESIGN PROBLEMS YOU MUST RESOLVE OR SURFACE (do not skip this section):

A. **Retiring P7b's tree guard without re-opening double-driving.** P7b
   retained the worktree-global lock at `<run_root>/.driving.lock` and writes
   it on every driving verb, because every run shares the operator's checkout.
   §10 says P7c retires it to read-only. But P7c ships `same_tree` as the
   DEFAULT, so both modes coexist on one machine indefinitely. If a `dedicated`
   run stops writing the tree guard, it no longer excludes a concurrent
   `same_tree` run of another slug on the same checkout — a double-driving
   vector P7b does not have. Git's one-branch-one-worktree rule (§8.1) does not
   help: the two runs have different branches and the `same_tree` run is not in
   a linked worktree at all. Resolve this explicitly. Note also that
   `web/supervisor.driving_lock`, `web/store.worktree_lock` and
   `web/service._refuse_if_worktree_locked` read that exact path for the
   console's FR-10.5 surface and its 409 refusal; whatever you decide, they
   must not go blind for `dedicated` runs.

B. **`RunPaths` must actually become the runtime carrier.** The P7b reviewer's
   F-001 was deferred here by name: `RunPaths` is currently constructed only by
   `StepContext.paths`, which nothing calls, and `RunManager.work_root` is a
   run-INDEPENDENT property. P7c is the phase where `work_root` becomes
   per-run, so this is no longer optional plumbing — it is the mechanism.
   Thread one immutable `RunPaths` through RunManager, Orchestrator,
   StepContext, RecoveryExecutor, the verifier and the judge boundary. The
   `test_root_scope.py` audit is a static name check; it cannot prove the
   independently-supplied values describe one coherent layout, so the carrier
   is what must.

C. **Two manifests.** §4.4 keeps the live `manifest.json` + journal in the
   operator's checkout and exports `manifest.json` + `RUN.md` into
   `<run worktree>/<run_root>/<slug>/<run-id>/` purely so the FR-2.2 checkpoint
   commit can stage a path that exists in the branch's tree. Decide and state:
   which is authoritative (R8 says the journal is), how the export cannot be
   mistaken for state, what `engine_bookkeeping_candidates` /
   `run_bookkeeping_paths` return and relative to WHICH root, and what happens
   when the export and the live projection disagree. `StateDirNotContained`
   exists precisely so an empty result here is loud rather than silent.

D. **Branch delete now hits git's refusal (E2-D).** `clean` (run.py:2928,
   2980) and `finish` (2995) delete the run branch. With a live worktree on
   that branch, `branch -D` fails. The worktree must be unlocked and removed
   FIRST, in that order, and R2 says a dirty run worktree is snapshotted before
   any `--force` removal (§11 row 10). Do not `remove -f -f` or `add -f`
   automatically anywhere (§11 rows 1 and 6).

E. **The verifier's disposable copy and prune cross-talk.** `verify.py` already
   takes the repo-global lock around `add_worktree` / `remove_worktree` /
   `prune_worktrees`. Serialization is not the whole answer: E8-C shows any
   prune anywhere removes another run's *prunable* entry, and the only thing
   that stops it is `git worktree lock --reason` held for the life of the run
   worktree. §11 row 7 also requires an explicit `--expire` on every prune —
   never rely on adopter-configurable `gc.worktreePruneExpire`. Make the
   disposable copy come from the right root, and keep it out of the run
   worktree's own tree.

F. **Reclaim must also unlock.** §8.3: when reclaiming a lock whose holder is
   PROVEN dead, also `git worktree unlock` that run's worktree, so the
   subsequent lifecycle actions are not blocked by a lock whose owner no longer
   exists. Gate it on the same proof — an `indeterminate` holder blocks,
   exactly as `locking.record_is_live` does today. P7b.1 made "cannot read the
   lock" a fail-closed refusal at every scope; do not weaken that to make the
   unlock path convenient.

G. **The fail-closed fallback is operator-chosen, never automatic.** §13: if a
   run worktree cannot be created, locked or verified, the run PARKS with
   reason `worktree_unavailable` and the assessment offers
   `gauntlet resume <slug> --same-tree`. An automatic fallback to the
   operator's tree would do the exact thing P7 exists to prevent, precisely
   when the machine is already in an unexpected state.

H. **`status --json` additivity.** The `worktree` object is always present and
   nullable. Read the schema's own `$comment` policy before touching it; the
   committed file and the embedded `_STATUS_SCHEMA_JSON` mirror are
   drift-guarded byte-for-byte by tests/unit/test_status_json.py. Two new
   states need rows in the operator playbook's *total* state list:
   `worktree_unavailable` and a run whose worktree is missing (§18 addition 4).
   The P7b reviewer's F-003 also parked one item here: if you add paired
   lock-evidence classification, this block is where it belongs — but it is a
   composite-state change, so justify it or leave it.

I. **The §14.4 refusal.** `cli.py:203` builds `RunManager(Path.cwd())`. A verb
   invoked from inside a run worktree must refuse with a message naming the
   operator checkout to run it from. Watch the adopter cases in §7: nested
   repos, submodules, bare/mirror, and worktree-of-worktree.

J. **Migration is never automatic (§10).** A run is `same_tree` iff its journal
   carries no `WorktreeAdopted` event AND `worktree list --porcelain` registers
   no worktree for `man.branch`. Terminal runs are never migrated. A run with a
   live or INDETERMINATE driver is refused. A run that cannot migrate stays
   fully resumable in `same_tree` mode — that is its R1 safe action, and the
   refusal must name the blocker.

DELIVERABLES:

- The config knob, the derived worktree root, and the full lifecycle.
- `RunPaths` as the real carrier (problem B).
- The export dir, with the authority question answered in code and comments.
- Migration + `WorktreeAdopted` / `WorktreeReleased` journal events, and
  `recreate_worktree` for §11 row 2.
- `resume --same-tree`, and the `worktree_unavailable` park.
- The additive `status --json` `worktree` block; the byte-identical
  `schemas/status.json` guard must pass.
- The §18 operator-surface delta in ALL THREE files (repo skill, scaffold
  skill, scaffold playbook) — additions 1–5 plus §18.3's structural-guardrail
  note. The playbook must never describe a tree the engine no longer uses.
- Tests, at minimum:
  * §12.1's autouse operator-checkout invariance fixture, asserting A1 (the
    operator's branch, index and worktree are unchanged) across EVERY existing
    verb test — this is the acceptance criterion as a property, not a case;
  * the snapshot matrix parametrized over `tree_kind ∈ {main, linked}`;
  * concurrent different-run operations cannot target the same worktree (A2),
    in BOTH modes and in the mixed pairing that problem A is about;
  * a missing run worktree is recreated from refs plus journal state, and the
    recreated HEAD matches the journal head's `branch_sha` (A3, the E4-B shape);
  * the §11 rows, with 2, 5 and 10 exercised end to end;
  * new `_crash_child` boundaries at worktree create / lock / teardown;
  * migration: refused under a live AND under an indeterminate driver; a
    blocked migration leaves the run resumable in `same_tree`;
  * a `dedicated` run and a `same_tree` run cannot drive concurrently
    (problem A);
  * `tests/unit/test_root_scope.py` stays green — new git calls name
    `work_root` or `operator_root`, never the ambiguous `repo_root`.

PROCESS REQUIREMENTS:

- Work on gauntlet/recovery-redesign. Nothing lands on main except via PR.
- Do NOT modify PRD-gauntlet.md, RECOVERY-REDESIGN-PLAN.md, policy.yaml, or any
  approved run artifact. Do not amend the ratified spike; corrections go in the
  commit body as an explicit "CORRECTION TO THE RATIFIED SPIKE" note naming
  where the authority to deviate came from.
- Do not weaken, delete or skip a passing test. If a test encodes behaviour
  this phase proves wrong, rewrite it with the reasoning in its docstring and
  call that out in the commit body.
- `uv run pytest` takes ~14 minutes. Read the SUMMARY LINE, not a piped exit
  code (a piped exit code lies here). Freeze the tree before the authoritative
  run — do not edit source while it is running. Run the integration suite
  (~55 min, real CLIs, burns quota) before the handoff, and revert the report
  file `runs/gauntlet-bootstrap/manual/p4-triage-accuracy.md` that
  test_triage_accuracy.py rewrites as a side effect.
- This phase is large. If it becomes clear it cannot land as one coherent
  commit, STOP and propose a split with a named seam rather than shipping a
  half-built lifecycle — a phasing change is a plan deviation and is the
  human's call.
- Commit as `P7c: <imperative summary, ≤72 chars>`. Do not self-review; hand
  off to the reviewer.

At completion, stop and report:

PHASE COMPLETE
Phase: P7c — dedicated run worktree behind a flag
SHA: <commit>
Tests: <N passed, M skipped, K deselected> (from the summary line)
       <integration summary line>
Problem A: <how same_tree and dedicated runs exclude each other, and what the
  web console's FR-10.5 surface reads now>
RunPaths: <what now carries it, and what still supplies roots independently>
Export dir: <which manifest is authoritative and how a disagreement surfaces>
Acceptance: A1/A2/A3 — <one line each: met for `dedicated` runs only, with the
  evidence that proves it>
Default: <confirm `worktree.mode` still defaults to `same_tree`, and what P7d
  needs to see from a dogfood run before flipping it>
Migration: <what a pre-P7c run does on first contact, and what refuses>
Deferrals: <anything pushed to P7d or to the §15 deferrals>
Upstream conflicts: <any ratified-spike recommendation found wrong, and what
  you did instead>

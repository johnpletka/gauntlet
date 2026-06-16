# Proposal: Run-branch lifecycle (`base: current`, guard, `clean`, `finish`)

> **Status:** Ratified by the maintainer (2026-06-15) and implemented in the
> same change set (version 0.2.0). This doc is the spec record for the
> run/branch-management delta adjacent to FR-9.1, written alongside the
> implementation per the maintainer's approved "approach (a)".

## 1. Motivation

Two real incidents drove this:

- **A run silently rewound a worktree.** `gauntlet run <slug>` adopted a
  pre-existing `gauntlet/<slug>` branch that pointed at an *older* base (before
  the repo's `.gauntlet/` scaffolding was committed). The bare `git checkout`
  updated the working tree to that old commit, removing the tracked scaffolding
  from disk (recoverable from history, but alarming and run-breaking).
- **The single-PR workflow needed per-run fiddling.** Implementing a PRD on a
  branch other than `main` and landing PRD + implementation in one PR required
  either merging the PRD to `main` first (two PRs) or hand-editing
  `base_branch`. Teams wanted a low-mental-load, hard-to-misuse flow.

## 2. The target workflow (integration branch)

A feature may bundle several PRDs plus manual QA before it merges to `main`:

```
main
 └── feat/<feature>                         integration branch (human-QA'd whole)
        ├── gauntlet/<prd-a>  ──finish──┐   PRD a + impl
        ├── gauntlet/<prd-b>  ──finish──┤   PRD b + impl
        ├── (interactive QA fixes)      │
        └── ...                          │
              feat/<feature> ── one PR ──┴──► main
```

Vocabulary stays four obvious verbs: `new` → run → `finish` (with `clean` as
the primitive underneath). The `<feature>` integration-branch name and each
per-PRD `<prd-slug>` are **separate namespaces** — a feature holds many PRDs.

## 3. Changes (all implemented in 0.2.0)

### 3.1 `base_branch: current` sentinel
`config.base_branch` accepts the sentinel `current` (also `@current`), meaning
"branch the run from whatever branch is checked out now." Set once in
`.gauntlet/config.yaml`, every run stacks on the current integration branch — no
per-run `--base` flag to remember. The **resolved** branch name (never the
sentinel) is recorded in the manifest, so resume, the PR draft, and `finish` all
act on a concrete branch. Fails closed on a detached HEAD (no branch to record
or merge back into). — `run.py:_resolve_base_branch`.

### 3.2 Stale-branch guard in `gauntlet run`
`start()` no longer blindly checks out an existing run branch. The lifecycle is
fail-closed (`run.py:_prepare_run_branch`):

| Run branch state | Action |
|---|---|
| absent | create off base (normal path) |
| fully merged into base (ancestor of base, incl. equal) | **spent** → discard + recreate fresh off base (`checkout -B`) |
| has commits not in base (unmerged / divergent / stale) | **refuse** (`StaleRunBranchError`) — never adopt, never rewind |

This makes "forgot to clean up" *safe*: the run can never silently rewind onto a
stale branch. After `finish` (or any merge into base), re-running the slug
self-heals via the "merged → discard" arm.

### 3.3 `gauntlet clean <slug>`
Deletes a *merged* run branch and clears the live `active-run.txt` pointer.
Refuses (`RunBranchNotMergedError`) unless `gauntlet/<slug>` is fully merged into
its recorded base; `--force` overrides. **Never** touches the committed run dir
(`prd.md`, `manifest.json`, transcripts are the audit trail). Steps off the
branch first if you are on it. — `run.py:clean`.

### 3.4 `gauntlet finish <slug>`
One-verb land: requires the run to be `done` and the worktree clean, merges
`gauntlet/<slug>` into its recorded base with a `--no-ff` merge commit (using the
human's git identity, not an engine identity), deletes the branch, and clears the
pointer. A merge conflict is **aborted** (never left half-applied) and surfaced
for a manual merge, returning the human to the run branch. Wraps `clean`'s
cleanup; `clean` stays the primitive for teams whose gauntlet→base merge is
itself a reviewed PR. Idempotent when the run is already merged. — `run.py:finish`.

## 4. Why these preserve the invariants

- **Fail closed.** Every new branch decision defaults to refuse/halt on
  ambiguity (unmerged branch, detached HEAD, dirty tree, merge conflict, unknown
  base) rather than mutating state — CLAUDE.md §2.
- **No silent worktree mutation.** The guard removes the one path that rewound a
  tree without consent; resets remain anchored to recorded SHAs on the run
  branch, never `base_branch`'s tip.
- **`gauntlet/<slug>` stays machine-owned.** Humans author on `feat/<feature>`;
  the engine creates, lands, and tears down the run branch.
- **Data over inference.** `finish`/`clean` delete only the ephemeral branch +
  pointer; the committed run record is never destroyed.

## 5. Deferred: worktree isolation (follow-up)

The original change list included running each run in its own **git worktree**
(separate directory) so branch switching never touches the user's working copy.
This is deliberately **deferred to a follow-up PR** because:

- It is a substantially larger architectural change — it touches run cwd,
  adapter working directories, the judge `repo_root` boundary, and run-dir path
  resolution — and deserves its own reviewable PR.
- The **stale-branch guard (§3.2) already closes the reported bug class**: a run
  can no longer silently rewind or clobber a worktree. Worktree isolation
  becomes defense-in-depth (and enables *concurrent* same-repo runs), not a
  fix — so shipping it separately costs no safety.

Tracked in [FUTURE.md](../FUTURE.md).

## 6. Test coverage

`tests/unit/test_run_branch_lifecycle.py` — base:current resolution + recording,
detached-HEAD refusal, stale-branch refusal (worktree not rewound), spent-branch
discard/recreate, `clean` refuse/force/merged paths (run dir preserved), `finish`
merge + delete, refuse-when-not-done, refuse-dirty, and conflict-abort-and-restore.

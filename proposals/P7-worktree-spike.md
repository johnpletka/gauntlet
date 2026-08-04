# P7 design spike: the dedicated run worktree

> **Status: PROPOSED — awaiting human ratification.** This document is the
> design spike that `RECOVERY-REDESIGN-PLAN.md` §4.7 and §6 P7 require *before*
> any implementation. It changes no engine code, no run/worktree layout, and no
> public verb. Every experiment below ran in throwaway temp directories; the
> only tracked change in this commit is this file.
>
> **What ratification is being asked for:** the state-root/worktree-root layout
> in §4 and §6, the lock model in §8, and the acknowledgement of the two
> upstream conflicts in §14 (FR-4.1/FR-4.5 evidence location, and the governed
> artifact authoring surface). P7 implementation may not begin until those are
> ratified.

---

## 1. Executive summary

| # | Decision | Recommendation | Reversal cost |
|---|---|---|---|
| 1 | State root vs worktree root | **Do not move the run-instance dir.** It stays in the operator's checkout at `<repo>/<run_root>/<slug>/<run-id>/` (journal, projection, transcripts, heartbeat, intent, lock). The run worktree holds only the tree agents edit, plus a two-file bookkeeping *export* dir for the manifest checkpoint commit. | Low — no data migration; the split is a code-level path parameter |
| 2 | `refs/gauntlet/state/<run>` anchoring | **Not required for P7.** Proven: a filesystem state root outside the worktree fully satisfies "recreated from refs plus journal state" (E4-B). Anchoring remains a **separate ratification** and must not be absorbed into P7. | n/a (deferred) |
| 3 | Worktree root location | `<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>` — fixed, not configurable in P7. Invisible to `status`/`clean -xdff`, survives operator hygiene, cannot escape the repository, works identically for bare/submodule/linked-worktree repos. | Low — the path is derived, and a stale worktree is a `remove`+`prune` away |
| 4 | Nested / adopter repos | Works unchanged for nested, bare, mirror, submodule and worktree-of-worktree layouts **because** the root is under the git common dir. Submodules need an explicit `submodule update --init` in the run worktree (P7 must add it or refuse). | Low |
| 5 | Lock scope | **Three layers.** (a) per-run `.driving.lock` in the run-instance dir (existing `_LockRecord` machinery, unchanged reclaim semantics); (b) a short-held repo-global lock under the git common dir for shared-git critical sections; (c) `git worktree lock --reason` as the git-native anti-prune marker for a live run. Git's own one-branch-one-worktree rule supplies the strongest guarantee for free. | Medium — lock path is persisted state; a mixed-version machine needs both paths read |
| 6 | Path assumptions that break | 24 concrete sites catalogued in §9 with file:line. Three are silent-failure hazards today (`except ValueError: pass` in the excludes builders) and one is a containment regression (`validate_temp_index_path` accepts the shared `.git` when called from a linked worktree). | n/a |
| 7 | Migration | Legacy runs keep driving in `same_tree` mode forever; migration is explicit, copy-never-move, and journaled. A run that cannot migrate stays resumable in `same_tree` mode — that is its R1 safe action. | Low |
| 8 | Failure/cleanup lifecycle | Nine failure classes mapped to R1 safe executable actions in §11, each backed by an experiment. | n/a |
| 9 | Test strategy | An autouse *operator-checkout invariance* fixture converts acceptance criterion A1 into a property asserted on every existing verb test, plus a `tree_kind ∈ {main, linked}` parametrization of the snapshot matrix and five new `_crash_child` worktree-lifecycle boundaries. | n/a |
| 10 | Phasing | P7a plumbing (pure refactor) → P7b lock/state relocation → P7c dedicated worktree behind `worktree.mode`, default `same_tree` → P7d flip the default after a dogfood run. The fallback is an operator-chosen `--same-tree`, never a silent automatic fallback. | Each stage independently revertible; P7c is a config flip |

**Blocking unknowns are in §14. Two of them are upstream conflicts that P7 may
not resolve on its own authority.**

---

## 2. Method

Every claim about Git in this document is backed by a transcript from a
runnable experiment. The experiments are reproduced verbatim in Appendix A and
were run in `$TMPDIR`-scoped throwaway repositories with
`GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null` so no machine or user
git configuration influenced the result. No worktree of *this* repository was
created, moved, or deleted.

```
$ git --version
git version 2.54.0
$ sw_vers -productVersion
26.5.2
```

Experiment index (§ references point at where each transcript is quoted):

| id | what it proves | quoted in |
|----|----------------|-----------|
| E1 | worktree anatomy: pointer files, per-worktree vs common git dir, shared refs | §3, §9.1 |
| E2 | git's own one-branch-one-worktree rule; what `checkout`/`branch -D`/`branch -f`/`merge` do across worktrees | §8, §9.4, §10 |
| E3 | an in-repo worktree root vs `git status` / `git add -A` / `git clean -xdf` / `-xdff` | §6.1 |
| E4 | **the load-bearing one**: state inside the worktree dies with it; state outside recreates exactly | §4 |
| E5 | nested repo, bare, mirror, submodule, worktree-of-worktree | §7 |
| E6 | lifecycle failures: dirty removal, `worktree lock`, creation failure, stale admin entry, prune expiry, `worktree repair` | §11 |
| E7 | Gauntlet's *own* `gitops` helpers executed against a linked worktree | §9.1, §9.2 |
| E8 | candidate state roots vs status/clean/removal; reset isolation; cross-run prune cross-talk | §5, §6, §11.6 |
| E9 | existing target paths, symlinked roots, concurrent `worktree add` | §6.3, §11.1 |
| E10 | the recommended layout end-to-end under the git common dir | §6.2 |

---

## 3. What a linked worktree actually is (E1)

```
$ git -C repo worktree add --quiet -b gauntlet/slug ../wt-outside HEAD
$ cat wt-outside/.git
gitdir: .../e1/repo/.git/worktrees/wt-outside
$ git -C wt-outside rev-parse --absolute-git-dir
.../e1/repo/.git/worktrees/wt-outside
$ git -C wt-outside rev-parse --git-common-dir
.../e1/repo/.git
$ git -C repo rev-parse --absolute-git-dir
.../e1/repo/.git
$ ls repo/.git/worktrees/wt-outside
HEAD
ORIG_HEAD
commondir
gitdir
index
logs
refs
$ test -d wt-outside/.git && echo DIR || echo "FILE (pointer)"
FILE (pointer)
```

Per-worktree: `HEAD`, the index, `ORIG_HEAD`, reflogs, and `refs/bisect` etc.
Shared: the object database and all ordinary refs.

```
$ git -C repo rev-parse --abbrev-ref HEAD
main
$ git -C wt-outside rev-parse --abbrev-ref HEAD
gauntlet/slug
$ git -C wt-outside rev-parse --git-path index
.../repo/.git/worktrees/wt-outside/index
$ git -C repo rev-parse --git-path index
.git/index
$ git -C repo update-ref refs/gauntlet/state/run-1 HEAD
$ git -C wt-outside rev-parse refs/gauntlet/state/run-1
608a4c81be36ff0e9ddc1da69bd3003cc9ce7003
```

Three consequences that drive the rest of this document:

1. Both linkage directions are stored as **absolute paths** (the worktree's
   `.git` file and the admin dir's `gitdir` file). Nothing is hardlinked, so a
   worktree works on any filesystem; moving either side breaks the pair until
   `git worktree repair` (E6-F).
2. `--absolute-git-dir` and `--git-common-dir` **differ** inside a linked
   worktree. Gauntlet's containment validator reads only the former (§9.2).
3. `refs/gauntlet/recovery/...` and `refs/gauntlet/backup/...` snapshots are
   visible identically from every worktree. The P2 snapshot design needs no
   namespacing change.

---

## 4. Decision 1 — state root vs worktree root

### 4.1 The failure this decision must avoid

Today the run-instance dir is `<repo>/<run_root>/<slug>/<run-id>/`
([run.py:618](src/gauntlet/engine/run.py:618),
[run.py:628](src/gauntlet/engine/run.py:628)) and the P6 journal is
`<run_dir>/journal/` ([journal.py:247](src/gauntlet/engine/journal.py:247)).
`repo_root` is the operator's checkout, so the journal is inside the tree today.

If P7 naively re-points `repo_root` at a per-run worktree, the run-instance dir
— and with it the authoritative journal — lands *inside* the disposable
worktree. Removing that worktree then destroys the authority, which directly
contradicts the P7 acceptance criterion "a missing run worktree can be
recreated from refs plus journal state".

**E4-A proves the destruction is real, and that being gitignored does not save
you:**

```
$ git -C repo worktree add --quiet -b gauntlet/slug $E4/wt HEAD
$ find $E4/wt/runs -type f
wt/runs/slug/run-1/.gitignore
wt/runs/slug/run-1/journal/evt-00000001.json

--- lifecycle teardown / stale cleanup: git worktree remove --force ---
$ git -C repo worktree remove --force $E4/wt
[exit 0]
$ ls $E4/wt
ls: .../e4/wt: No such file or directory
$ find $E4 -name "evt-*.json" | wc -l
       0
>>> the ignored, authoritative journal was deleted WITH the worktree
```

`git worktree remove --force` is not an exotic path: it is exactly what
`gitops.remove_worktree` ([gitops.py:431](src/gauntlet/engine/gitops.py:431))
does, and it is the normal end-of-run teardown.

### 4.2 The recreate path, proven (E4-B)

With the journal kept outside the worktree, destruction of the tree is fully
recoverable:

```
$ git -C repo worktree add --quiet $E4/wt gauntlet/slug
   (state written to $E4/state/slug/run-1/journal/, agent commits to the branch)
$ git -C repo rev-parse gauntlet/slug
0221208f9f0be4e2209609479ea01589be3bf7cd

--- the run worktree is destroyed out from under the run (rm -rf, driver dead) ---
$ rm -rf $E4/wt
$ git -C repo worktree list --porcelain
worktree .../e4/repo
HEAD 43413c333d2b1885e38d19fb4c299a0f6994ee94
branch refs/heads/main

worktree .../e4/wt
HEAD 0221208f9f0be4e2209609479ea01589be3bf7cd
branch refs/heads/gauntlet/slug
prunable gitdir file points to non-existent location

$ find $E4/state -type f
state/slug/run-1/journal/evt-00000001.json
state/slug/run-1/journal/evt-00000002.json

--- recreate from refs + journal state ---
$ git -C repo worktree prune
$ git -C repo worktree add --quiet $E4/wt gauntlet/slug
$ git -C wt log --oneline
0221208 P1: work
43413c3 init
$ git -C wt status --porcelain
[exit 0]

journal head branch_sha = 0221208f9f0be4e2209609479ea01589be3bf7cd
recreated worktree HEAD  = 0221208f9f0be4e2209609479ea01589be3bf7cd
RESULT: recreate-from-refs+journal RECONSTRUCTS the exact state
```

Note the porcelain discovery signal: `prunable gitdir file points to
non-existent location`. That is the machine-readable evidence a recovery
observation should consume (§11).

### 4.3 What is actually in a run-instance dir

Only two files are tracked. Everything else dies with the tree it lives in:

```
$ git ls-files runs/background-start-services/
runs/background-start-services/PR.md
runs/background-start-services/plan.md
runs/background-start-services/prd.md
runs/background-start-services/run-2026-06-26T16-42-42/RUN.md
runs/background-start-services/run-2026-06-26T16-42-42/manifest.json
```

against an on-disk run dir of:

```
.gitignore  RUN.md  artifacts/  judge-audit.jsonl  manifest.json
pipeline.yaml  retro/  steps/          (+ live: journal/, heartbeat.json,
                                         suspensions.jsonl, .recovery-intent.json,
                                         .serve/)
```

So the destruction surface is much larger than the journal: `pipeline.yaml`
(which `_resume_once` reloads —
[run.py:1284](src/gauntlet/engine/run.py:1284)), every transcript under
`steps/`, `judge-audit.jsonl`, `artifacts/`, `retro/`, the heartbeat and the
recovery intent would all be lost by a worktree teardown if the run dir moved
into the worktree. A resume after teardown would fail at pipeline reload, not
at the journal.

### 4.4 Recommendation

**Do not move anything. Keep the run-instance dir where P6 put it — in the
operator's checkout under `run_root` — and keep the run worktree code-only.**

| Item | Location under P7 | Why |
|---|---|---|
| `journal/` (authoritative) | operator checkout, `<run_root>/<slug>/<run-id>/journal/` — **unchanged** | already outside any run worktree by construction; P6 is untouched |
| `manifest.json` (live projection) | same dir — **unchanged** | it is the journal's projection; every reader already resolves it through `run_dir` |
| `manifest.json` + `RUN.md` (committed export) | **new**: `<run worktree>/<run_root>/<slug>/<run-id>/` | the bookkeeping checkpoint commit must stage a path that exists in the branch's tree (§9.3) |
| `RUN.md`, `steps/`, `artifacts/`, `retro/`, `judge-audit.jsonl` | operator checkout — **unchanged** | preserves FR-4.5's acceptance ("reconstruct every decision using only files under `.gauntlet/runs/<slug>/`") |
| `heartbeat.json`, `suspensions.jsonl` | operator checkout — **unchanged** | liveness must be readable when the run worktree is gone |
| `.recovery-intent.json` | operator checkout — **unchanged** | a surviving intent must outlive the tree its replay repairs |
| `pipeline.yaml` | operator checkout — **unchanged** | resume must reload it after a worktree teardown |
| `.driving.lock` | **moves** from `<run_root>/.driving.lock` to `<run_root>/<slug>/<run-id>/.driving.lock` | §8 |
| the tree agents edit | **new**: `<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>/` | §6 |
| governed `prd.md` / `plan.md` | operator checkout is the authoring surface; synced into the run worktree per contact | §14.2 — **needs ratification** |

This is the *minimum* change that satisfies all three P7 acceptance criteria.
It is also the only option that does not require re-ratifying FR-4.1/FR-4.5
(§14.1).

**Residual risk, stated plainly:** the run-instance dir remains gitignored
inside the operator's checkout, so an operator's `git clean -xdff` still
destroys it. That is true today (P6 shipped with it) and P7 does not make it
worse. The follow-up that removes the risk — mirroring the journal under the
git common dir — is §15's deferral D3, and it needs the FR-4 ratification in
§14.1.

**Reversal cost:** low. Nothing on disk moves except the lock file. The split is
expressed as an extra parameter (`work_root`) threaded through the call sites in
§9; reverting means passing `repo_root` for both roots.

---

## 5. Decision 2 — is `refs/gauntlet/state/<run>` anchoring required?

**No. It is not required for P7, and P7 must not absorb it.**

The P7 acceptance criterion reads "a missing run worktree can be recreated from
refs plus journal state". E4-B satisfies it exactly: the branch ref supplies the
tree, the filesystem journal supplies the execution state, and the two agree on
the head SHA. No ref anchoring was needed because the journal was never in the
destroyed tree.

`RECOVERY-REDESIGN-PLAN.md` §4.6 already says the journal "can live under the
existing ignored run-instance state directory so reset/clean operations do not
touch it" and that a ref anchor is what "a later phase may" add. §4.6 further
requires explicit human ratification before authoritative state migrates in a
way that changes the present PRD interpretation. Those are the same words that
gate P6→P7; folding a ref anchor into P7 would be exactly the silent absorption
the plan forbids.

**What the ref anchor would actually buy, so the deferral is an informed one:**

- E8-A proves a filesystem state root is invisible to `status` and survives
  `clean -xdff` **only** when it lives under the git common dir; under the
  operator's checkout (§4.4's recommendation) `clean -xdff` still reaches it.
- A filesystem state root is not clonable or pushable. A run cannot be handed to
  another machine, and losing the git dir loses the state. A
  `refs/gauntlet/state/<run>` anchor would make execution state a first-class
  git object — pushable, fetchable, `fsck`-verified, and covered by the same
  reachability guarantees as the snapshots.

Neither is a P7 acceptance requirement. Deferred as **D1** (§15), needing its
own ratification.

---

## 6. Decision 3 — worktree root location and configurability

### 6.1 Option A — inside the repo, gitignored (rejected)

`<repo>/.gauntlet/worktrees/<slug>/<run-id>`. E3:

```
$ git -C repo worktree add --quiet -b gauntlet/a .gauntlet/worktrees/a HEAD
$ git -C repo status --porcelain --untracked-files=all
?? .gauntlet/worktrees/a/

--- with a self-ignoring .gitignore under the worktree root ---
$ git -C repo status --porcelain --untracked-files=all
[exit 0]
$ git -C repo add -A && git -C repo diff --cached --name-only
[exit 0]

--- git clean -xdf in the OPERATOR worktree ---
$ git -C repo clean -xdn
Would skip repository .gauntlet/worktrees/a
Would remove .gauntlet/worktrees/.gitignore
$ git -C repo clean -xdf
Skipping repository .gauntlet/worktrees/a
Removing .gauntlet/worktrees/.gitignore
$ ls repo/.gauntlet/worktrees/a
README.md

--- git clean -xdff (double force) ---
$ git -C repo clean -xdff
Removing .gauntlet/
$ ls repo/.gauntlet/worktrees
ls: repo/.gauntlet/worktrees: No such file or directory
$ git -C repo worktree list
.../e3/repo                              478b9fe [main]
.../e3/repo/.gauntlet/worktrees/a        478b9fe [gauntlet/a] prunable
```

Two disqualifying results:

- **`clean -xdf` deletes the engine-owned `.gitignore`** that makes the worktree
  invisible. The worktree survives, but from the next command onward it is
  untracked dirt in the operator's `git status`, which fails the clean-handoff
  invariant (CLAUDE.md §1) and the FR-9.3 guard at
  [run.py:1162](src/gauntlet/engine/run.py:1162).
- **`clean -xdff` deletes the entire run worktree.** Single-force is safe
  (`Skipping repository`), double-force is not. `-xdff` is an ordinary
  developer keystroke.

Gauntlet's own clean is `git clean -fd` with no `-x`
([gitops.py:1095](src/gauntlet/engine/gitops.py:1095)), so the *engine* would
not do this — but the operator would, in the tree P7 exists to hand back to them.

### 6.2 Option B — under the git common dir (recommended)

`<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>`. E10, end to end:

```
$ git -C repo worktree add --quiet -b gauntlet/slug "$CG/gauntlet/worktrees/slug/run-1" HEAD
$ git -C "$CG/gauntlet/worktrees/slug/run-1" rev-parse --show-toplevel
.../e10/repo/.git/gauntlet/worktrees/slug/run-1
$ git -C "$CG/gauntlet/worktrees/slug/run-1" rev-parse --abbrev-ref HEAD
gauntlet/slug

--- does the operator worktree see it at all? ---
$ git -C repo status --porcelain --untracked-files=all --ignored
[exit 0]
$ git -C repo clean -xdffn
[exit 0]

--- can the agent work in it and commit? ---
$ ... git commit -q -m "P1: work"; git log --oneline
72bd996 P1: work
76ff666 init

--- does git maintenance tolerate it? ---
$ git -C repo fsck --no-progress
[exit 0]
$ git -C repo gc --quiet --prune=now
[exit 0]
$ git -C repo rev-parse gauntlet/slug
72bd99608072195aae021703af3e1be198c6c11b

--- state beside it, under the same git dir, survives teardown ---
$ git -C repo worktree remove --force "$CG/gauntlet/worktrees/slug/run-1"
$ find "$CG/gauntlet" -type f
repo/.git/gauntlet/state/slug/run-1/journal/evt-1.json
$ git -C repo rev-parse gauntlet/slug
72bd99608072195aae021703af3e1be198c6c11b

--- recreate ---
$ git -C repo worktree add --quiet "$CG/gauntlet/worktrees/slug/run-1" gauntlet/slug
$ git -C "$CG/gauntlet/worktrees/slug/run-1" log --oneline
72bd996 P1: work
76ff666 init
$ git -C repo status --porcelain --untracked-files=all --ignored
[exit 0]

--- verifier disposable copy created FROM the run worktree ---
$ git -C "$CG/gauntlet/worktrees/slug/run-1" worktree add --detach --quiet "$E10/verify-copy" HEAD
$ git -C "$E10/verify-copy" rev-parse --git-common-dir
.../e10/repo/.git
$ git -C "$E10/verify-copy" log --oneline -1
72bd996 P1: work
```

Every property P7 needs holds: invisible to `status` even with `--ignored`,
untouched by `clean -xdff`, tolerated by `fsck` and `gc`, fully functional for
commits, recreatable, and it still supports the verifier's disposable copy
(§9.6).

It is also **inside the repository** in the sense `RECOVERY-REDESIGN-PLAN.md` §7
uses in its global acceptance property — *"no outside-repository path is
written"*. Options C and D below both write outside the repository and would
require that property to be rewritten.

### 6.3 Options C and D — sibling directory, XDG/state dir (rejected)

Both write outside the repository, violating §7's acceptance property as
written. Option C (a sibling of the repo) additionally fails E5-A: if the
parent directory is itself a git repository, the run worktrees become untracked
content of that outer repository.

```
$ git -C outer/inner worktree add --quiet -b gauntlet/x $E5/outer/inner-wt HEAD
$ git -C outer status --porcelain --untracked-files=all
?? inner-wt/
?? inner/
```

Option D (`~/.local/state/gauntlet/...`) additionally needs a repo-identity
mapping, a machine-global cleanup story, and is the only option where the
worktree can land on a different device from the repo. Under Option B that
cannot happen: the root is a child of the git dir, so it is on the same
filesystem by construction, and every Gauntlet atomic write already uses a
same-directory temp + `os.replace`
([run.py:496-504](src/gauntlet/engine/run.py:496),
[journal.py:157](src/gauntlet/engine/journal.py:157),
[journal.py:180-186](src/gauntlet/engine/journal.py:180)), so `EXDEV` is
structurally unreachable. **This is the reason to prefer B over D outright
rather than adding cross-device handling.**

*Limitation of this spike, stated honestly:* this host has no second writable
volume, and mounting one would change machine state, which this spike is not
authorized to do. The cross-device claim above is therefore an argument that
Option B makes the question moot, not a demonstration of Option D's behaviour on
a second device. If the maintainer wants Option D, cross-device behaviour must
be measured before it ships.

### 6.4 Configurability and the containment validator

**P7 ships no `worktree_root` knob.** The path is derived:
`Path(git rev-parse --path-format=absolute --git-common-dir) / "gauntlet" /
"worktrees" / <slug> / <run-id>`. E8 shows why the explicit path format matters —
from the main worktree the bare form is relative:

```
$ git -C repo rev-parse --git-common-dir
.git
$ git -C wt rev-parse --git-common-dir
.../e8/repo/.git
$ git -C wt rev-parse --path-format=absolute --git-common-dir
.../e8/repo/.git
```

A knob would need a containment validator, and E9-C shows why the existing
`_validate_repo_relative` ([config.py:171](src/gauntlet/engine/config.py:171))
would not be enough: it is a *string* check, and a symlink defeats it.

```
$ ln -s $E9/outside $E9/repo/escape-link
$ git -C repo worktree add --quiet -b gauntlet/d "$E9/repo/escape-link/d" HEAD
[exit 0]
$ ls $E9/outside
d
>>> a repo-relative-looking worktree root can land OUTSIDE the repo through a symlink
```

E9-B further shows git records the path **as given** and does not resolve
symlinks in `worktree list`, so a containment check must `resolve()` before
comparing — the same discipline `_path_within`
([run.py:595](src/gauntlet/engine/run.py:595)) and
`RunLayout.active_run_dir` ([run.py:646](src/gauntlet/engine/run.py:646))
already apply to run-dir segments. Not shipping a knob avoids the whole class.
If a knob is added later it must validate `resolve()`d paths against *both* the
repo toplevel and the git common dir.

---

## 7. Decision 4 — nested and adopter repositories (E5)

All four required cases were measured, not assumed. All four work **because**
the worktree root is derived from the git common dir.

**A repository inside another repository.** The inner repo's common dir is
`inner/.git`, so the run worktree lands under it and the outer repo never sees
it:

```
$ git -C outer/inner worktree add --quiet -b gauntlet/y $E5/outer/inner/.gauntlet/wt/y HEAD
$ git -C outer status --porcelain --untracked-files=all
?? inner-wt/
?? inner/
$ git -C outer/inner/.gauntlet/wt/y rev-parse --git-common-dir
.../e5/outer/inner/.git
```

(The `?? inner-wt/` line is the rejected Option C from §6.3, created earlier in
the same experiment. The Option B worktree contributes no line at all.)

**A repository whose worktrees are already in use.** Additional worktrees
compose without limit; only the *branch* is exclusive (§8). A run worktree can
itself be the parent of the verifier's disposable copy:

```
$ git -C outer/inner-wt worktree add --detach --quiet $E5/wt-of-wt HEAD
$ git -C outer/inner worktree list
.../e5/outer/inner                        c2e123f [main]
.../e5/outer/inner-wt                     c2e123f [gauntlet/x]
.../e5/outer/inner/.gauntlet/wt/y         c2e123f [gauntlet/y]
.../e5/wt-of-wt                           c2e123f (detached HEAD)
$ git -C $E5/wt-of-wt rev-parse --git-common-dir
.../e5/outer/inner/.git
```

Nesting does not deepen: every worktree registers against the same common dir.

**A bare / mirror clone.** `worktree add` works; `status` does not.

```
$ git -C bare.git status --porcelain
fatal: this operation must be run in a work tree
$ git -C bare.git rev-parse --is-bare-repository
true
$ git -C bare.git worktree add --quiet $E5/bare-wt -b gauntlet/z main
$ git -C bare-wt rev-parse --abbrev-ref HEAD
gauntlet/z
$ git -C bare.git worktree list
.../e5/bare.git (bare)
.../e5/bare-wt  c2e123f [gauntlet/z]

$ git clone --quiet --mirror ... ; git -C mirror.git config --get remote.origin.mirror
true
$ git -C mirror.git worktree add --quiet $E5/mirror-wt -b gauntlet/m HEAD
$ git -C mirror-wt rev-parse --abbrev-ref HEAD
gauntlet/m
```

This is a capability *gain*: today `repo_root` must be a checkout, so a bare
repo cannot host a run at all. P7 does not need to deliver bare-repo support,
but it must not accidentally forbid it — any new validator that asserts
`repo_root` has a worktree would.

**A submodule.** This one has a real trap. A worktree of the superproject
checks out the submodule *gitlink* but leaves the directory **empty**, and
`git status` reports the tree **clean**:

```
$ git -C super worktree add --quiet -b gauntlet/s $E5/super-wt HEAD
$ ls -a $E5/super-wt/vendor/sub
.
..
$ git -C super-wt status --porcelain
[exit 0]
$ git -C super-wt submodule status
-c2e123f41076b3193e9006a06fd0d414f6064f72 vendor/sub
$ git -C super-wt submodule update --init --quiet && ls $E5/super-wt/vendor/sub
README.md
$ git -C super-wt status --porcelain
[exit 0]
$ cat $E5/super-wt/vendor/sub/.git
gitdir: ../../../super/.git/worktrees/super-wt/modules/vendor/sub
```

A builder or verifier in that worktree would see an empty vendor directory, a
clean `git status`, and failing tests — with no signal pointing at the cause.
Note the leading `-` in `submodule status`: that is the machine-readable
"uninitialized" marker, and it is the detection the engine must use.

**P7 requirement:** after creating a run worktree, if `git submodule status`
reports any `-`-prefixed entry, either run `submodule update --init` or **park
with a named reason**. It must never silently hand an agent a half-populated
tree. Because `submodule update` can require network and credentials, doing it
automatically is a posture change; §15 defers the automatic form (**D4**) and
P7 ships the fail-closed park.

Worktrees of a submodule itself also work, registering against the submodule's
own common dir:

```
$ git -C super/vendor/sub worktree add --quiet -b gauntlet/subwt $E5/sub-wt HEAD
$ git -C sub-wt rev-parse --git-common-dir
.../e5/super/.git/modules/vendor/sub
```

---

## 8. Decision 5 — lock scope

### 8.1 What git gives us for free (E2)

Git enforces one-worktree-per-branch itself, and the refusals are exactly the
"concurrent different-run operations cannot target the same worktree" property
P7 must deliver:

```
=== A. second worktree for a branch already checked out ===
$ git -C repo worktree add ../wt2 gauntlet/slug
fatal: 'gauntlet/slug' is already used by worktree at '.../e2/wt'
[exit 128]

=== B. operator checks out the run branch in the main worktree ===
$ git -C repo checkout gauntlet/slug
fatal: 'gauntlet/slug' is already used by worktree at '.../e2/wt'
[exit 128]

=== D. delete a branch checked out in another worktree ===
$ git -C repo branch -D gauntlet/slug
error: cannot delete branch 'gauntlet/slug' used by worktree at '.../e2/wt'
[exit 1]

=== E. reset a branch checked out in another worktree, from the main worktree ===
$ git -C repo branch -f gauntlet/slug HEAD
fatal: cannot force update the branch 'gauntlet/slug' used by worktree at '.../e2/wt'
[exit 128]
```

Since the run branch is `<branch_prefix><slug>` and is per-slug by construction
([run.py:1118](src/gauntlet/engine/run.py:1118)), **two runs of the same slug
can never both hold a worktree, and the operator can never check out or reset
the run branch from their own tree while the run exists.** That is a stronger
guarantee than any advisory lockfile, and it is enforced by git rather than by
Gauntlet.

Two consequences for the existing verbs (§9.4): `clean`
([run.py:2819](src/gauntlet/engine/run.py:2819)) and `finish`
([run.py:2868](src/gauntlet/engine/run.py:2868),
[run.py:2883](src/gauntlet/engine/run.py:2883)) delete the run branch and would
now hit case D — the run worktree must be removed *before* the branch delete.
Merging is unaffected, and leaves the operator exactly where they were:

```
=== F. merge a branch checked out in another worktree ===
$ git -C repo merge --no-ff -m "merge run" gauntlet/slug
Merge made by the 'ort' strategy.
=== G. after merge, main worktree HEAD/branch unchanged? ===
$ git -C repo rev-parse --abbrev-ref HEAD
main
$ git -C wt rev-parse --abbrev-ref HEAD
gauntlet/slug
=== H. delete the run branch after removing its worktree ===
$ git -C repo worktree remove --force ../wt
$ git -C repo branch -D gauntlet/slug
Deleted branch gauntlet/slug (was f92e2a8).
```

### 8.2 What the lockfile must still do

The `.driving.lock` today is at `<repo>/<run_root>/.driving.lock`
([run.py:828-832](src/gauntlet/engine/run.py:828)) and is deliberately
*worktree-global*: exactly one lockfile per repo/worktree so holding it for one
slug blocks every driving verb for every slug
([run.py:816-826](src/gauntlet/engine/run.py:816)). Under P7 each run has its
own tree, so cross-slug exclusion on one tree is no longer the thing to protect.
What remains:

1. **Two drivers of the same run.** Still needed. Git's branch rule does not
   help: both drivers would use the *same* worktree, so no `worktree add`
   happens and no refusal fires.
2. **Concurrent mutation of shared git state.** The object DB, refs, the
   worktree admin dir and `prune` are shared across every worktree. E9-D shows
   git's own per-operation locking is real but coarse, and that a lost race
   leaves debris:

```
=== D. two concurrent worktree adds on the same path ===
  fatal: Unable to create '.../repo/.git/worktrees/race/index.lock': File exists.
  Another git process seems to be running in this repository, or the lock file may be stale
$ git -C repo branch --list
  gauntlet/a
+ gauntlet/b
+ gauntlet/c
+ gauntlet/d
+ gauntlet/e1
  gauntlet/e2
* main
```

Both branches were created; only one worktree exists. E6-C shows the same
asymmetry on a plain creation failure — the branch survives the failed add:

```
$ chmod 500 $E6/ro
$ git -C repo worktree add --quiet -b gauntlet/c $E6/ro/c HEAD
fatal: could not create leading directories of '.../ro/c/.git': Permission denied
[exit 128]
$ ls $E6/repo/.git/worktrees
[exit 0]                      # no admin entry left
$ git -C repo branch --list
  gauntlet/a
  gauntlet/b
  gauntlet/c                  # ...but the branch was created
* main
```

3. **A live run's worktree must not be pruned by another run.** E8-C shows the
   cross-talk is real today, because `verify.discard_disposable_copy` calls
   `gitops.prune_worktrees(repo_root)` on the *shared* common dir
   ([verify.py:805](src/gauntlet/engine/verify.py:805)):

```
=== C. one prune in one run nukes another run's missing tree ===
$ rm -rf $E8/other        # run B's tree vanished (reboot, /tmp sweep, operator)
$ git -C repo worktree list --porcelain | grep -A1 other
worktree .../e8/other
HEAD 7f6c264...
branch refs/heads/gauntlet/other
prunable gitdir file points to non-existent location
--- run A's verifier tears down its disposable copy and calls prune_worktrees ---
$ git -C repo worktree prune --verbose
Removing worktrees/other: gitdir file points to non-existent location
>>> run B's admin entry was pruned by run A's cleanup

--- same scenario with the run worktree LOCKED ---
$ git -C repo worktree lock --reason "gauntlet run other/run-2 live" $E8/other
$ rm -rf $E8/other
$ git -C repo worktree prune --verbose
[exit 0]
$ git -C repo worktree list --porcelain | tail -6
locked gauntlet run other/run-2 live
```

E6-B confirms the lock also blocks `remove --force`, with an explicit escape:

```
$ git -C repo worktree remove --force $E6/b
fatal: cannot remove a locked working tree, lock reason: run gauntlet/b is live
use 'remove -f -f' to override or unlock first
$ git -C repo worktree unlock $E6/b
$ git -C repo worktree prune --verbose
Removing worktrees/b: gitdir file points to non-existent location
```

### 8.3 Recommendation

Three layers, each with a distinct job:

| layer | where | held for | purpose |
|---|---|---|---|
| **per-run driving lock** | `<run_root>/<slug>/<run-id>/.driving.lock` in the operator's checkout | the whole drive, as today | one driver per run; keeps the exact `_LockRecord` PID + `ProcessIdentity` reclaim semantics ([run.py:866-899](src/gauntlet/engine/run.py:866)) |
| **repo-global git lock** | `<git-common-dir>/gauntlet/.repo.lock` | only the shared-git critical sections: `worktree add`/`remove`/`prune`, branch create/delete, snapshot ref writes | serializes shared-state mutation without serializing whole runs |
| **`git worktree lock --reason`** | git's own admin dir | for the life of the run worktree | git-native anti-prune / anti-remove marker; the *only* thing that stops another run's `prune_worktrees` |

The reason for keeping the per-run lock in the **operator's checkout** rather
than in the run worktree is `driver_liveness`: `driver_info(run_root, slug)`
([operator.py:333](src/gauntlet/engine/operator.py:333)) must be able to answer
"is a driver alive?" when the run worktree is missing — precisely the recovery
case P7 exists to make survivable.

Stale reclaim keeps verifying process identity unchanged; P7 adds one step:
when reclaiming a lock whose holder is *proven* dead, also `git worktree unlock`
that run's worktree so the subsequent lifecycle actions are not blocked by a
lock whose owner no longer exists. The unlock is gated on the same proof — an
`indeterminate` holder blocks, exactly as `_lock_is_live` does today.

**Contract change to note:** `driver_info` today returns `LIVENESS_NONE` for a
"foreign lock" whose `rec.slug != slug`
([operator.py:346-347](src/gauntlet/engine/operator.py:346)). With per-run
locks that row becomes unreachable. It must stay in the table (and its test) for
legacy `same_tree` runs whose lock is still at the old path, and the reader must
try both paths during migration (§10).

**Reversal cost:** medium. The lock path is persisted state, so a machine
running two Gauntlet versions must read both locations. The `git worktree lock`
layer is free to revert (a leftover lock is unlocked by `worktree unlock`).

---

## 9. Decision 6 — every path assumption that breaks

Today `repo_root` means three different things at once: *the git repository*,
*the tree the agent edits*, and *where run artifacts live*. P7 splits the second
out. Each site below assumes at least two of the three are the same object.

Legend for the fix column: **W** = must take the run worktree; **O** = must keep
the operator checkout; **G** = must take the git common dir; **!** = fails
silently today if the roots differ.

### 9.1 gitops — the primitives

| site | assumption | fix |
|---|---|---|
| [gitops.py:80-93](src/gauntlet/engine/gitops.py:80) `_run` | every git command runs with `-C repo`; a single `repo` selects both the repository and the tree | W/O per caller — the parameter is already there, the callers are the problem |
| [gitops.py:241](src/gauntlet/engine/gitops.py:241) `current_branch` | one checked-out branch per repo | W (E7: returns `main` for the operator tree, `gauntlet/slug` for the run tree) |
| [gitops.py:292](src/gauntlet/engine/gitops.py:292) `is_clean` / `status_porcelain` | dirtiness is a property of the repo | W (E7: `is_clean(main)=True`, `is_clean(run wt)=False` for the same repo) |
| [gitops.py:159-168](src/gauntlet/engine/gitops.py:159) `git_index_path` | `rev-parse --git-path index` | already correct — E7 shows it resolves per-worktree (`.git/worktrees/wt/index`) |
| [gitops.py:409-420](src/gauntlet/engine/gitops.py:409) `worktree_tree_hash` | HEAD tree of "the" worktree | W — it is the verifier's mutation witness for the *run* tree |
| [gitops.py:451-473](src/gauntlet/engine/gitops.py:451) `checkout_or_create_branch` / `recreate_branch` | `checkout` mutates the tree the run uses | W, and both now hit the E2-A/E2-B refusals if the branch is checked out elsewhere |
| [gitops.py:423-440](src/gauntlet/engine/gitops.py:423) `add_worktree` / `remove_worktree` / `prune_worktrees` | the only consumer is the disposable verifier copy; a bare `prune` is harmless | G + the §8.3 repo-global lock; `prune_worktrees` becomes cross-run-visible (E8-C) |

### 9.2 The temp-index containment regression (**!**)

`validate_temp_index_path` ([gitops.py:111-140](src/gauntlet/engine/gitops.py:111))
rejects a substitute index that resolves inside the worktree **or** inside
`rev-parse --absolute-git-dir`. Inside a linked worktree that second check
covers only the *per-worktree* admin dir, not the shared one. E7 runs the
repository's own function to prove it:

```
=== gitops.validate_temp_index_path, called with repo=<the LINKED run worktree> ===
  --git-common-dir   : .../repo/.git
  --absolute-git-dir : .../repo/.git/worktrees/wt
  inside the SHARED .git (common dir)     .../repo/.git/gauntlet-temp.index
      ACCEPTED
  inside the PER-WORKTREE git dir         .../repo/.git/worktrees/wt/gauntlet-temp.index
      rejected -> TempIndexPathError: ... resolves inside .../repo/.git/worktrees/wt
  inside the run worktree                 .../wt/temp.index
      rejected -> TempIndexPathError: ... resolves inside .../wt
  outside both                            .../temp.index
      ACCEPTED

=== the same candidates validated against the MAIN worktree ===
  inside the SHARED .git (common dir)     .../repo/.git/gauntlet-temp.index
      rejected -> TempIndexPathError: ... resolves inside .../repo
```

The identical path is **rejected** from the main worktree and **accepted** from
a linked one — the guard silently weakens the moment P7 lands. Fix: check
against `--git-common-dir` *as well as* `--absolute-git-dir`. This is a one-line
containment fix that P7a must carry, and it is the reason `git_snapshot`
(the sole caller, [git_snapshot.py:199](src/gauntlet/engine/git_snapshot.py:199))
appears in the P7a scope even though it otherwise needs no change.

### 9.3 The bookkeeping-path builders (**!**, three sites)

All three compute `run_dir.relative_to(repo_root)` and swallow the failure:

- [execution.py:215-263](src/gauntlet/engine/execution.py:215)
  `run_bookkeeping_excludes` — `try: … except ValueError: pass`
- [execution.py:285-303](src/gauntlet/engine/execution.py:285)
  `engine_bookkeeping_candidates` — same
- [execution.py:332-344](src/gauntlet/engine/execution.py:332)
  `run_bookkeeping_paths` — filters on `(repo_root / rel).exists()`

If `run_dir` is not under the tree passed as `repo_root`, all three degrade
silently rather than failing closed:

- `engine_bookkeeping_candidates` → `[]` → `advance_is_engine_bookkeeping`
  ([gitops.py:331-374](src/gauntlet/engine/gitops.py:331)) can never classify an
  advance as bookkeeping → every resume of an interrupted step re-parks. That is
  exactly the #62/#65 regression the tolerance was added to fix.
- `run_bookkeeping_paths` → `[]` → `_commit_manifest_checkpoint`
  ([orchestrator.py:2032-2037](src/gauntlet/engine/orchestrator.py:2032))
  returns `None` and **no checkpoint commit is made at all** — a silent loss of
  the FR-2.2 audit trail.

This is the mechanical reason §4.4 keeps a two-file *export* dir inside the run
worktree: the bookkeeping commit must stage a path that exists in the branch's
tree, and these builders must be handed that export dir (relative to **W**)
while every other reader keeps the operator-checkout run dir (**O**). P7a must
also replace the three `except ValueError: pass` clauses with a fail-closed
raise — a silently-empty bookkeeping allowlist is precisely the "fail closed"
violation §2 of CLAUDE.md warns about.

### 9.4 `run.py` — the public verbs

| verb | site | assumption |
|---|---|---|
| `start` | [run.py:1162-1177](src/gauntlet/engine/run.py:1162) | the operator's dirty tree blocks the run; **W** makes this check a fresh, always-clean tree — the preflight moves, and a dirty operator checkout stops blocking a start (behaviour change to document) |
| `start` | [run.py:1178](src/gauntlet/engine/run.py:1178) `_prepare_run_branch` | `checkout -b` in the operator tree; becomes `worktree add -b` (**W**+**G**) |
| `start` | [run.py:1190](src/gauntlet/engine/run.py:1190) `pipeline.yaml` write | **O** (must survive worktree teardown, §4.3) |
| `resume` | [run.py:1322](src/gauntlet/engine/run.py:1322) `gitops.checkout_branch(repo, man.branch)` | **the single line that mutates the operator's checkout on every resume.** Becomes "ensure the run worktree exists and is on the branch" |
| `resume` | [run.py:1303](src/gauntlet/engine/run.py:1303) `_observe_resume_branch` | **W** for tree planes, **G** for refs |
| `resume` | [run.py:1281](src/gauntlet/engine/run.py:1281) `replay_pending_intent(self.repo_root, run_dir)` | repo arg → **W**, run_dir → **O** |
| `recover` | [run.py:2057](src/gauntlet/engine/run.py:2057) | assessment observes the operator tree; → **W** |
| `rollback` | [run.py:3154](src/gauntlet/engine/run.py:3154), [run.py:3200](src/gauntlet/engine/run.py:3200) | `is_clean`/`current_branch` on the operator tree; → **W**. E8-B proves the isolation this buys |
| `abort` | [run.py:2030](src/gauntlet/engine/run.py:2030) | leaves the branch; must also unlock+remove the worktree or deliberately keep it as evidence (§11) |
| `approve`/`reject` | [run.py:1873](src/gauntlet/engine/run.py:1873), [run.py:1905](src/gauntlet/engine/run.py:1905) | drive through `_drive` → inherit **W** |
| `clean` | [run.py:2797-2819](src/gauntlet/engine/run.py:2797) | `current_branch`/`checkout_branch`/`delete_branch` on the operator tree; hits E2-D unless the worktree is removed first |
| `finish` | [run.py:2855-2883](src/gauntlet/engine/run.py:2855) | same, plus the merge (E2-F/G shows the merge itself is safe and leaves the operator in place) |
| `_reconcile_projection` | [run.py:2550](src/gauntlet/engine/run.py:2550), [run.py:2642](src/gauntlet/engine/run.py:2642) | pure **O** — journal/projection only; **no change**, which is the point of §4.4 |

E8-B, the isolation P7 is buying, measured:

```
$ git -C repo rev-parse --abbrev-ref HEAD      -> main
$ git -C repo rev-parse HEAD                   -> 7f6c264...
$ git -C wt reset --hard HEAD~1
HEAD is now at 7f6c264 init
$ git -C repo rev-parse --abbrev-ref HEAD      -> main
$ git -C repo rev-parse HEAD                   -> 7f6c264...   (unchanged)
$ git -C repo reflog --date=iso -3
7f6c264 HEAD@{...}: commit (initial): init                     (unchanged)
```

A hard reset in the run worktree leaves the operator's branch, HEAD, index and
even their reflog untouched.

### 9.5 The recovery executor and snapshots

| site | assumption |
|---|---|
| [recovery_exec.py:1862-1863](src/gauntlet/engine/recovery_exec.py:1862) `WorktreeLockGuard.__init__` | lock at `repo_root / run_root / DRIVING_LOCK_NAME`; → the §8.3 per-run path |
| [recovery_exec.py:1973-1980](src/gauntlet/engine/recovery_exec.py:1973) `RecoveryExecutor.__init__` | one `repo_root` for both git ops and `run_dir` resolution; needs **W** and **O** separately |
| [recovery_exec.py:2280-2282](src/gauntlet/engine/recovery_exec.py:2280), [recovery_exec.py:2579](src/gauntlet/engine/recovery_exec.py:2579) | `checkout_branch` on the assumed-shared tree → **W**; note E2-E: a `branch -f` from the wrong tree is now a hard `fatal`, so the executor *must* run inside the run worktree, not merely name the branch |
| [recovery_exec.py:433-441](src/gauntlet/engine/recovery_exec.py:433) `observe_git` | index/worktree fingerprints and `current_branch` from one repo → **W**; run-branch refs are shared, so **G** works for either |
| [recovery_exec.py:1802-1803](src/gauntlet/engine/recovery_exec.py:1802) `intent_path(run_dir)` | **O** — unchanged (an intent must outlive the tree) |
| [git_snapshot.py:245](src/gauntlet/engine/git_snapshot.py:245) `current_branch(repo)` | **W** |
| [git_snapshot.py:193](src/gauntlet/engine/git_snapshot.py:193) `snapshot_ref` | refs are shared across worktrees (E1) — **no change needed** |

### 9.6 Orchestrator, steps, verifier, judge, CLI

| site | assumption | fix |
|---|---|---|
| [orchestrator.py:188](src/gauntlet/engine/orchestrator.py:188) excludes construction | `run_bookkeeping_excludes(repo_root, run_dir, artifact_root)` | export dir under **W** (§9.3) |
| [orchestrator.py:192-206](src/gauntlet/engine/orchestrator.py:192) `_ignore_run_dir` | writes `*` into the in-tree run dir | now writes into the **W** export dir as well as **O** |
| [orchestrator.py:969-980](src/gauntlet/engine/orchestrator.py:969) in-drive rewind | "observe the CURRENT checkout" — deliberately reads whatever branch is out | **W**; with a dedicated worktree the "possibly another branch" case disappears for real runs |
| [orchestrator.py:1132](src/gauntlet/engine/orchestrator.py:1132) `is_clean(self.repo_root, exclude=run_root)` | **W** |
| [orchestrator.py:2035](src/gauntlet/engine/orchestrator.py:2035) `commit_run_bookkeeping` | **W** + export paths |
| [steptypes.py:197](src/gauntlet/engine/steptypes.py:197) shell step `cwd=ctx.repo_root` | shell steps run tests in the tree | **W** |
| [steptypes.py:2116](src/gauntlet/engine/steptypes.py:2116) `adapter.run(prompt, cwd=ctx.repo_root)` | **the agent's cwd is the tree it edits** | **W** — this is the whole point of P7 |
| [verify.py:774-807](src/gauntlet/engine/verify.py:774) disposable copy | `add_worktree(repo_root, copy, "HEAD")` | **W** as the parent; proven to work (E5-D, E10). `discard_disposable_copy`'s `prune_worktrees` becomes cross-run-visible (E8-C) → needs the `git worktree lock` in §8.3 |
| [judgeproc.py:277](src/gauntlet/engine/judgeproc.py:277) `env[GAUNTLET_REPO_ROOT] = str(self.repo_root)` | the judge's authoritative path boundary | **W** — otherwise every agent write into the run worktree reads as a path escape and the judge denies it |
| [judge/core.py:137](src/gauntlet/judge/core.py:137) `effective_root` | boundary ‖ pinned repo_root ‖ request | unchanged logic, new pinned value |
| [judge/core.py:187-207](src/gauntlet/judge/core.py:187) allow-cache key includes `repo_root` | cache keyed on the root | correct by construction once the root is **W** |
| [cli.py:203](src/gauntlet/cli.py:203) `RunManager(Path.cwd())` | the process cwd *is* the repo root | **O**, resolved and validated. **New hazard:** the run worktree contains a tracked `runs/<slug>/{prd.md,plan.md,<run-id>/manifest.json}` (see §4.3), so `gauntlet status` run from *inside* a run worktree would read the committed projection at the branch tip instead of the journal head. P7 must detect `--git-common-dir != <toplevel>/.git` and refuse, naming the operator checkout |
| [cli.py:234](src/gauntlet/cli.py:234), [cli.py:590](src/gauntlet/cli.py:590), [cli.py:616](src/gauntlet/cli.py:616), [cli.py:727](src/gauntlet/cli.py:727), [cli.py:968](src/gauntlet/cli.py:968), [cli.py:1026](src/gauntlet/cli.py:1026), [cli.py:1529](src/gauntlet/cli.py:1529) `run_root = repo_root / config.run_root` | console/serve/logs roots | **O** — unchanged, which is the §4.4 dividend |
| [config.py:509-520](src/gauntlet/engine/config.py:509) `branch_prefix`/`run_root`/`asset_root` | all repo-relative | unchanged; no new knob (§6.4) |

**Count: 24 sites.** Three fail silently today (§9.3), one is a containment
regression (§9.2), one is a new operator-error class (`cli.py:203`).

---

## 10. Decision 7 — migration for existing same-worktree runs

`RECOVERY-REDESIGN-PLAN.md` §8 requires additive, backward-compatible
migration. The rule: **a pre-P7 run is never auto-migrated, and never wedged.**

Detection is evidence-based, not inferred. A run is `same_tree` iff its journal
carries no `WorktreeAdopted` event *and* `git worktree list --porcelain`
registers no worktree for `man.branch`. Both are cheap, read-only, and available
when the driver is dead.

| pre-P7 run state | first contact with a P7 engine | why it is safe |
|---|---|---|
| **completed / aborted / failed** | rendered as `worktree: null, mode: same_tree`; never migrated | terminal runs have no live tree to isolate |
| **parked at a gate** | keeps driving in `same_tree` mode; `status` surfaces an *optional* action `gauntlet migrate-worktree <slug>` | the operator chooses; an unexpected tree move at a gate is exactly the surprise P7 is meant to remove |
| **running with a live driver** | untouched; the P7 engine refuses to migrate a run whose lock is `alive` or `indeterminate` | fail closed on the live case, per `_lock_is_live`'s deliberate asymmetry |
| **interrupted / orphaned driver** | reconciled in `same_tree` mode exactly as today, then offered migration | recovery must not depend on a layout change |
| **cannot migrate** (dirty operator tree, disk full, branch checked out elsewhere, submodules uninitialized) | stays fully resumable in `same_tree` mode; the refusal names the blocker | this *is* the R1 safe executable action — the run is never wedged by the migration being impossible |

The migration itself is **copy, never move**, and journaled:

1. take the per-run lock; refuse unless the driver is provably dead or parked;
2. `worktree add` at the derived path (§6.4) — refuses if the branch is checked
   out anywhere (E2-A), which is the correct fail-closed answer;
3. `git worktree lock --reason "<run-id>"`;
4. write the two-file export dir and verify the bookkeeping paths resolve
   (§9.3) — a failure here aborts the migration with the worktree removed;
5. append a `WorktreeAdopted` state event carrying the worktree path, the branch
   SHA, and the pre-migration lock path;
6. leave the operator's checkout untouched: the old `<run_root>/.driving.lock`
   path is read (never written) by the P7 engine for one release so a
   half-migrated machine cannot double-drive.

Rollback of a migration is `worktree unlock` + `worktree remove` + a
`WorktreeReleased` event; the run returns to `same_tree` mode with its journal
intact, because §4.4 never moved the journal.

Two compatibility details from §8 of the plan:

- `status --json` gains an always-present nullable `worktree` object. The
  committed schema's own policy allows this additively at `schema_version: 1`
  (see the `$comment` in `schemas/status.json`, which already records the P5 and
  P6 additive precedents).
- `resume --reset-interrupted` is unaffected: it is already a thin selection of
  `snapshot_and_restart` through the executor, which simply runs against **W**.

---

## 11. Decision 8 — failure and cleanup lifecycle

Each row is an R1 obligation: a persisted nonterminal state with no live driver
must expose at least one safe executable action.

| # | failure | detection (evidence) | R1 safe action |
|---|---|---|---|
| 1 | **creation crash / partial create** | E6-C: a failed `worktree add` leaves **no admin entry** but **does leave the branch**. E9-A: `add` refuses a non-empty existing path (`fatal: '…' already exists`) but accepts an empty one | retry `worktree add`; the orphan branch is handled by the existing `_prepare_run_branch` merged/unmerged triage ([run.py:762-787](src/gauntlet/engine/run.py:762)). Never `add -f` automatically |
| 2 | **discovery after a reboot** (tmp swept, tree gone) | E4-B/E6-D: `worktree list --porcelain` reports `prunable gitdir file points to non-existent location` | `recreate_worktree`: `worktree prune` (or `remove`) then `worktree add <path> <branch>`, then reconcile the projection from the journal head. Proven end-to-end in E4-B |
| 3 | **stale worktree whose run is gone** | branch exists, no run-instance dir / terminal journal | `gauntlet clean` removes it: `worktree unlock` → `worktree remove --force` → `branch -D` in that order (E2-D forbids the reverse) |
| 4 | **worktree deleted under a live run** | driver lock `alive` + `prunable` entry | park with a named reason; do **not** recreate under a live driver. Once the driver is proven dead, row 2 applies |
| 5 | **`git worktree prune` race** | E8-C: any prune anywhere in the repo removes another run's `prunable` entry | hold `git worktree lock --reason` for the life of the run (E8-C second half proves prune then no-ops); take the repo-global lock around the engine's own prune |
| 6 | **stale admin entry blocks recreation** | E6-D: `fatal: '…' is a missing but already registered worktree; use 'add -f' to override, or 'prune' or 'remove' to clear` | `prune` first, then `add`. Never `add -f` — it would silently adopt an entry the assessment has not explained |
| 7 | **prune expiry surprise** | E6-E: `prune --expire 3.days.ago` leaves a fresh missing entry; plain `prune` removes it immediately | always pass an explicit expiry; never rely on `gc.worktreePruneExpire`, which is adopter-configurable (the same fail-closed reasoning as `status_porcelain`'s pinned `--untracked-files`) |
| 8 | **disk exhaustion mid-create** | same shape as E6-C (`fatal: could not create leading directories …`, exit 128, no admin entry, branch left behind) | fail closed with the git stderr preserved; the run parks; retry is safe because no admin entry exists. *Not* separately reproduced — filling a filesystem needs machine state this spike may not change; E6-C's permission-denied case exercises the identical code path in git (leading-directory creation failure) |
| 9 | **repo or worktree moved on disk** | E6-F: `prunable`, but the tree is intact and `status` still works from inside it | `git worktree repair` (`repair: gitdir incorrect: …`), after which `worktree list` reports the new path. Prefer repair over prune+recreate: it preserves uncommitted work |
| 10 | **dirty run worktree at teardown** | E6-A: `fatal: '…' contains modified or untracked files, use --force to delete it` | this is R2 in git's own voice — snapshot first (`git_snapshot.create_snapshot`), then `remove --force`. Never `--force` without a durable snapshot ref |

---

## 12. Decision 9 — test strategy

The problem: `RECOVERY-REDESIGN-PLAN.md` §7 defines a four-dimensional matrix
(branch state × index/worktree state × failure/interruption × fault injection),
and P7 adds a fifth axis — *which tree* — that would multiply it.

It does not have to. The fifth axis decomposes into one property and one
parametrization.

### 12.1 Acceptance criterion A1 as an autouse property

"Starting, resuming, recovering and rolling back a run never changes the
operator's checked-out branch, index, or worktree" is a *property*, not a set of
cases. Implement it as an autouse fixture over every verb test:

```python
def _operator_fingerprint(repo: Path) -> tuple[str, str, str, str]:
    return (
        gitops.current_branch(repo),
        gitops.head_sha(repo),
        sha256(subprocess ... "ls-files", "-s", "--", ":/"),      # index plane
        gitops.status_porcelain(repo, untracked_all=True),        # worktree plane
    )
```

captured before and asserted byte-equal after. That converts the **entire**
existing verb test suite — every branch-state and index/worktree-state
combination already enumerated in §7 — into A1 coverage for free, and it fails
loudly the moment any of the 24 sites in §9 is missed. It is also exactly the
four planes §3 of the plan says git keeps independently, so it cannot be
satisfied by a partial fix.

The four existing suites named in the P7 brief map cleanly:

- `test_recovery_unification.py`, `test_recovery_executor.py` — gain the autouse
  fixture unchanged; their assertions are already about the run's tree.
- `test_git_snapshot.py` — gains a `tree_kind ∈ {"main", "linked"}`
  parametrization. This is the highest-value single change in the plan, because
  the per-worktree index path (E1, E7) and the `--absolute-git-dir` containment
  gap (§9.2) are both invisible in a main-worktree fixture and both break plane
  fidelity.
- `test_journal_p6.py` — needs **no** change under §4.4's layout, which is the
  clearest evidence that the recommended split is the low-risk one.

### 12.2 Fault injection at worktree-lifecycle boundaries

`tests/unit/_crash_child.py` already supports `boundary:<n>:<when>:<sig>` with
`when ∈ {before, after, mid}`, counting `Manifest.write_atomic` calls — P6 added
`mid` for the event-append/projection-write sub-boundary. P7 extends the same
mechanism rather than inventing a second one: a counted seam in the worktree
lifecycle module, with five new boundaries mirroring §11:

1. before `worktree add`;
2. after `worktree add`, before `worktree lock`;
3. after `worktree lock`, before the export-dir write;
4. before `worktree remove`;
5. after `worktree remove`, before the `WorktreeReleased` event.

Each maps to exactly one row of §11, so the fault-injection suite and the
recovery table cannot drift. The parent's assertion is unchanged in shape:
resume/recover/status either complete the next safe transition or return a
specific executable action, and the operator fingerprint is byte-identical
throughout.

### 12.3 Adopter matrix

Four integration tests (`@pytest.mark.integration` not required — they need only
local git): nested repo, bare repo, submodule superproject, and worktree-of-
worktree. Each creates a run worktree and drives one shell step. The submodule
case asserts the §7 fail-closed park, not a successful run.

### 12.4 The one thing that must not regress

`CLAUDE.md` §3's self-hosting hazard 2: the behavioural verifier runs in an
isolated `HOME`, network-denied disposable copy. That copy's `.git` is a pointer
into the shared common dir — which is *already* true today (the copy lives in a
temp dir, its git dir is in the repo). P7 changes the copy's *parent* from the
operator checkout to the run worktree, and E5-D/E10 prove the copy is
byte-identical in behaviour. The judge boundary registration
([judge/core.py:68](src/gauntlet/judge/core.py:68)) is unaffected: it pins the
copy root, and the run worktree simply becomes "outside the boundary" the same
way the operator checkout is today. **No verifier change is required**, and the
P7 test plan should assert that explicitly rather than leave it to be discovered
during a dogfood run.

---

## 13. Decision 10 — recommended phasing

| stage | delivers | reversible? |
|---|---|---|
| **P7a — plumbing (no behaviour change)** | Introduce an explicit `RunPaths` carrying `work_root` (**W**), `state_root` (**O**) and `git_common_dir` (**G**); thread it through the 24 sites in §9 with `work_root = repo_root` so nothing changes. Fix the two latent defects that are correct to fix regardless of P7: the `--git-common-dir` containment gap (§9.2) and the three `except ValueError: pass` clauses (§9.3). | Fully — it is a refactor plus two bug fixes |
| **P7b — lock and liveness relocation** | Move `.driving.lock` to the per-run path; add the repo-global git lock; teach `driver_info` to read both paths. Still one tree. | Yes — dual-path reading is the rollback |
| **P7c — dedicated worktree behind a flag** | `worktree.mode: same_tree \| dedicated`, **default `same_tree`**. Lifecycle (create/lock/discover/recreate/teardown), the §11 recovery actions, the export dir, the `git worktree lock` marker, the additive `status --json` `worktree` block, and the `cli.py:203` "you are inside a run worktree" refusal. | Yes — a config flip |
| **P7d — flip the default** | After a dogfood run that exercises at least §11 rows 2, 5 and 10, `dedicated` becomes the default for **new** runs. Legacy runs stay `same_tree` forever (§10). | Yes — a config flip |

**Where the fail-closed fallback lives.** Explicitly *not* an automatic fallback.
If a run worktree cannot be created, locked, or verified, the run **parks** with
a named reason (`worktree_unavailable`) and the recovery assessment offers
`gauntlet resume --same-tree` as an operator-chosen action. Automatically
falling back to the operator's tree would silently do the exact thing P7 exists
to prevent — mutate the operator's checkout — and would do it precisely when the
machine is already in an unexpected state. This satisfies R1 (a safe executable
action exists) without violating "fail closed".

---

## 14. What needs ratification (blocking)

### 14.1 UPSTREAM CONFLICT — FR-4.1 / FR-4.5 vs. moving evidence out of the tree

`PRD-gauntlet.md` FR-4.1 says the run artifact tree "lives in-repo by default so
it can be source-controlled", and FR-4's **Acceptance** clause reads: *"a
reviewer who was not present can reconstruct every decision … using only files
under `.gauntlet/runs/<prd-slug>/`."*

§4.4's recommendation is chosen specifically to keep that literally true — it
leaves the run-instance dir in the operator's checkout. But the residual risk in
§4.4 (an operator's `git clean -xdff` destroys the journal, since it is ignored)
is only removable by placing state under the git common dir, which would move
evidence out from under `.gauntlet/runs/<slug>/` and contradict FR-4's
acceptance clause.

**P7 does not resolve this.** Per CLAUDE.md §2 ("approved artifacts change only
through their own loop and gate") and FR-10.4, it is surfaced here. The
maintainer decides: accept the residual risk (recommended for P7), or ratify an
FR-4 revision that permits an out-of-tree state root (deferral **D3**).

### 14.2 UPSTREAM CONFLICT — the governed-artifact authoring surface

Today `prd.md` and `plan.md` live in `<run_root>/<slug>/` in the one tree that
is both the operator's checkout and the run's tree. The human authors them
there (FR-10.1 entry contract, [run.py:712-742](src/gauntlet/engine/run.py:712))
and the run baseline-commits them onto the run branch. Hand-editing them
post-approval is a sanctioned operator workflow governed by R9/FR-10.4.

With two trees, those are two files. The operator edits theirs; the run reads
its own. Three options:

- **(A) Operator checkout is the authoring surface; the engine syncs into the
  run worktree on each mutating contact** — snapshot first (R2), and any change
  to an *approved* artifact halts with the existing upstream-conflict path (R9),
  exactly as today. **Recommended.**
- (B) The run worktree becomes the authoring surface — breaks the "author the
  PRD in your repo, then `gauntlet run`" entry contract.
- (C) Symlink the artifact dir into the run worktree — a symlinked tracked path
  inside a tree that gets `reset --hard`; rejected on the plan §9 caution
  against symlink-blind entry handling.

Option A is a change to how an approved artifact reaches the run branch, so it
needs explicit ratification before P7c.

### 14.3 Non-blocking decision needed

Should `gauntlet <verb>` invoked from *inside* a run worktree hard-error
(§9.6, `cli.py:203`)? Recommended: yes, with a message naming the operator
checkout. It is a new refusal, so it is a CLI contract change worth an explicit
yes/no.

---

## 15. Deferrals

| id | deferred item | why it is its own phase |
|---|---|---|
| **D1** | `refs/gauntlet/state/<run>` anchoring | Not required for P7 acceptance (§5). It changes what "authoritative state" *is*, which `RECOVERY-REDESIGN-PLAN.md` §4.6 says needs explicit ratification of its own. **Must not be absorbed into P7.** |
| **D2** | A configurable `worktree_root` | Needs a resolve()-based containment validator (§6.4) and, for any out-of-repo value, a rewrite of §7's "no outside-repository path is written". No demand yet |
| **D3** | Moving the run-instance state under the git common dir | Blocked on §14.1's FR-4 ratification |
| **D4** | Automatic `submodule update --init` in a new run worktree | Touches network and credential posture (§7); P7 ships the fail-closed park instead |
| **D5** | `gauntlet clean --worktrees` sweep for orphaned run worktrees across all slugs | Cross-run garbage collection; needs the §8.3 repo-global lock to exist first |
| **D6** | Cross-device / XDG state roots | Option B makes `EXDEV` structurally unreachable (§6.3); revisit only if D2 lands |

---

## 16. What P7 does NOT change

Stated explicitly so the implementation phase has a boundary:

- **No public verb changes name, arguments, or semantics.** `start`, `resume`,
  `recover`, `rollback`, `abort`, `approve`, `reject`, `finish`, `clean`,
  `status`, `logs` keep their contracts. `resume` gains one *optional* flag
  (`--same-tree`) as an operator-chosen fallback.
- **No journal change.** The P6 event vocabulary, schema version, sequencing,
  quarantine and projection-rebuild logic are untouched. Two new event kinds
  (`WorktreeAdopted`, `WorktreeReleased`) are additive within the existing
  `EVENT_KINDS` extension pattern.
- **No `manifest.json` schema-version bump**; new fields are optional/additive
  per plan §8.
- **No `schemas/status.json` schema-version bump**; the `worktree` block is
  additive at `schema_version: 1`, matching the P5 and P6 precedents already
  recorded in that file's own compatibility `$comment`.
- **No change to the review/cycle state machine, triage, findings schemas, the
  judge policy engine, or `policy.yaml`.**
- **No change to the behavioural verifier's design** (§12.4) — only the parent
  it copies from.
- **No change to branch naming** (`branch_prefix` + slug), to `run_root`, or to
  `asset_root`.
- **No change to the recovery snapshot format or ref namespace** — refs are
  shared across worktrees (E1).
- **`same_tree` mode is not removed.** It remains the mode for every legacy run
  and the documented fallback for adopters whose layout cannot host a worktree.

---

## 17. The smallest viable version

The three §6 P7 acceptance criteria, and the minimum that satisfies each:

| criterion | minimum needed | already true? |
|---|---|---|
| **A1** — starting, resuming, recovering, rolling back never changes the operator's checked-out branch, index, or worktree | P7a plumbing + P7c worktree creation + the §9.4 verb re-pointing. E8-B proves the git-level isolation is total once the ops happen in the run worktree | no — needs the work |
| **A2** — concurrent different-run operations cannot target the same worktree | Almost free: git's one-branch-one-worktree rule (E2-A/B/D/E) plus the per-run lock (§8.3). No new algorithm | mostly — needs the lock path move |
| **A3** — a missing run worktree can be recreated from refs plus journal state | The §4.4 layout (journal never in the worktree) plus one new recovery action `recreate_worktree` = `prune` + `add` + projection reconcile. Proven end-to-end in E4-B | needs only the action |

**Therefore the SVV is: P7a + P7b + a minimal P7c** consisting of

1. worktree create/lock/teardown at the fixed derived path (no config knob),
2. the two-file export dir so bookkeeping commits keep working,
3. the `recreate_worktree` recovery action,
4. the `worktree_unavailable` park with `--same-tree` as the operator action,
5. the submodule fail-closed park,
6. the autouse operator-fingerprint invariant across the existing verb tests.

Not in the SVV: any config knob, XDG/sibling roots, ref anchoring, automatic
submodule init, cross-run garbage collection, bare-repo support as a feature,
and flipping the default (P7d is a separate gate after a dogfood run).

---

## Appendix A — the experiments

All scripts source this harness. They ran under `$TMPDIR` and touched no
repository outside their own throwaway tree.

```bash
# common.sh
LAB=<a throwaway temp dir>
set -u
export GIT_CONFIG_GLOBAL=/dev/null
export GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=Lab GIT_AUTHOR_EMAIL=lab@example.invalid
export GIT_COMMITTER_NAME=Lab GIT_COMMITTER_EMAIL=lab@example.invalid
mkrepo() {
  mkdir -p "$1"; git -C "$1" init -q -b main
  echo hello > "$1/README.md"; git -C "$1" add -A; git -C "$1" commit -q -m init
}
sayx() { printf '\n$ %s\n' "$1"; eval "$1" 2>&1; printf '[exit %s]\n' "$?"; }
```

### E1 — worktree anatomy (quoted in §3, §9.1)

```bash
cd "$LAB"; rm -rf e1; mkdir e1; cd e1
mkrepo repo
sayx 'git -C repo worktree add --quiet -b gauntlet/slug ../wt-outside HEAD'
sayx 'git -C repo worktree list --porcelain'
sayx 'cat wt-outside/.git'
sayx 'git -C wt-outside rev-parse --absolute-git-dir'
sayx 'git -C wt-outside rev-parse --git-common-dir'
sayx 'git -C repo rev-parse --absolute-git-dir'
sayx 'ls repo/.git/worktrees/wt-outside'
sayx 'test -d wt-outside/.git && echo "DIR" || echo "FILE (pointer)"'
sayx 'git -C repo rev-parse --abbrev-ref HEAD'
sayx 'git -C wt-outside rev-parse --abbrev-ref HEAD'
sayx 'git -C wt-outside rev-parse --git-path index'
sayx 'git -C repo rev-parse --git-path index'
sayx 'git -C repo update-ref refs/gauntlet/state/run-1 HEAD'
sayx 'git -C wt-outside rev-parse refs/gauntlet/state/run-1'
```

### E2 — branch contention across worktrees (quoted in §8.1, §9.4)

```bash
cd "$LAB"; rm -rf e2; mkdir e2; cd e2
mkrepo repo
git -C repo worktree add --quiet -b gauntlet/slug ../wt HEAD
sayx 'git -C repo worktree add ../wt2 gauntlet/slug'          # A
sayx 'git -C repo checkout gauntlet/slug'                     # B
sayx 'git -C repo worktree add --detach ../wt3 gauntlet/slug' # C
sayx 'git -C repo branch -D gauntlet/slug'                    # D
echo work > wt/f.txt; git -C wt add -A; git -C wt commit -q -m "run work"
sayx 'git -C repo branch -f gauntlet/slug HEAD'               # E
sayx 'git -C repo merge --no-ff -m "merge run" gauntlet/slug' # F
sayx 'git -C repo rev-parse --abbrev-ref HEAD'                # G
sayx 'git -C wt rev-parse --abbrev-ref HEAD'
sayx 'git -C wt status --porcelain'
sayx 'git -C repo worktree remove --force ../wt'              # H
sayx 'git -C repo branch -D gauntlet/slug'
```

### E3 — in-repo worktree root vs status/add/clean (quoted in §6.1)

```bash
cd "$LAB"; rm -rf e3; mkdir e3; cd e3
mkrepo repo
sayx 'git -C repo worktree add --quiet -b gauntlet/a .gauntlet/worktrees/a HEAD'
sayx 'git -C repo status --porcelain --untracked-files=normal'
sayx 'git -C repo status --porcelain --untracked-files=all'
mkdir -p repo/.gauntlet/worktrees && printf '*\n' > repo/.gauntlet/worktrees/.gitignore
sayx 'git -C repo status --porcelain --untracked-files=all'
sayx 'git -C repo status --porcelain --untracked-files=all --ignored'
sayx 'git -C repo add -A && git -C repo diff --cached --name-only'
sayx 'git -C repo reset -q'
sayx 'git -C repo clean -xdn'
sayx 'git -C repo clean -xdf'
sayx 'ls repo/.gauntlet/worktrees/a'
sayx 'git -C repo worktree list'
sayx 'git -C repo clean -xdff'
sayx 'ls repo/.gauntlet/worktrees'
sayx 'git -C repo worktree list'
```

Plus, on the nested-repo variant from E5, the single/double force asymmetry:

```
$ git -C outer clean -xdn      -> (no output)
$ git -C outer clean -fdn      -> (no output)
$ git -C outer clean -xdffn
Would remove inner-wt/
Would remove inner/
```

### E4 — state root inside vs outside the worktree (quoted in §4)

```bash
cd "$LAB"; rm -rf e4; mkdir e4; cd e4; E4="$PWD"
mkrepo "$E4/repo"
# A: state INSIDE the run worktree
sayx 'git -C "$E4/repo" worktree add --quiet -b gauntlet/slug "$E4/wt" HEAD'
mkdir -p "$E4/wt/runs/slug/run-1/journal"
printf '*\n' > "$E4/wt/runs/slug/run-1/.gitignore"
printf '{"seq":1,"kind":"RunStatusChanged"}\n' > "$E4/wt/runs/slug/run-1/journal/evt-00000001.json"
sayx 'find "$E4/wt/runs" -type f | sort'
sayx 'git -C "$E4/repo" worktree remove --force "$E4/wt"'
sayx 'find "$E4" -name "evt-*.json" | wc -l'
# B: state OUTSIDE the run worktree, then destroy + recreate
sayx 'git -C "$E4/repo" worktree add --quiet "$E4/wt" gauntlet/slug'
mkdir -p "$E4/state/slug/run-1/journal"
printf '{"seq":1,...,"branch_sha":"%s"}\n' "$(git -C "$E4/repo" rev-parse gauntlet/slug)" \
  > "$E4/state/slug/run-1/journal/evt-00000001.json"
echo builderwork > "$E4/wt/impl.txt"
git -C "$E4/wt" add -A; git -C "$E4/wt" commit -q -m "P1: work"
printf '{"seq":2,...,"branch_sha":"%s"}\n' "$(git -C "$E4/wt" rev-parse HEAD)" \
  > "$E4/state/slug/run-1/journal/evt-00000002.json"
sayx 'rm -rf "$E4/wt"'
sayx 'git -C "$E4/repo" worktree list --porcelain'
sayx 'git -C "$E4/repo" worktree prune'
sayx 'git -C "$E4/repo" worktree add --quiet "$E4/wt" gauntlet/slug'
sayx 'git -C "$E4/wt" log --oneline'
sayx 'git -C "$E4/wt" status --porcelain'
# then compare the journal head's branch_sha against the recreated HEAD
```

### E5 — nested, bare, mirror, submodule, worktree-of-worktree (quoted in §7)

```bash
cd "$LAB"; rm -rf e5; mkdir e5; cd e5; E5="$PWD"
mkrepo "$E5/outer"; mkrepo "$E5/outer/inner"
sayx 'git -C "$E5/outer" status --porcelain --untracked-files=all'
sayx 'git -C "$E5/outer/inner" worktree add --quiet -b gauntlet/x "$E5/outer/inner-wt" HEAD'
sayx 'git -C "$E5/outer" status --porcelain --untracked-files=all'
sayx 'git -C "$E5/outer/inner" worktree add --quiet -b gauntlet/y "$E5/outer/inner/.gauntlet/wt/y" HEAD'
sayx 'git -C "$E5/outer" status --porcelain --untracked-files=all'
sayx 'git -C "$E5/outer/inner/.gauntlet/wt/y" rev-parse --git-common-dir'
sayx 'git clone --quiet --bare "$E5/outer/inner" "$E5/bare.git"'
sayx 'git -C "$E5/bare.git" status --porcelain'
sayx 'git -C "$E5/bare.git" rev-parse --is-bare-repository'
sayx 'git -C "$E5/bare.git" worktree add --quiet "$E5/bare-wt" -b gauntlet/z main'
sayx 'git -C "$E5/bare-wt" rev-parse --abbrev-ref HEAD'
sayx 'git clone --quiet --mirror "$E5/outer/inner" "$E5/mirror.git"'
sayx 'git -C "$E5/mirror.git" worktree add --quiet "$E5/mirror-wt" -b gauntlet/m HEAD'
mkrepo "$E5/sub"; mkrepo "$E5/super"
sayx 'git -C "$E5/super" -c protocol.file.allow=always submodule add --quiet "$E5/sub" vendor/sub'
sayx 'git -C "$E5/super" commit -q -m "add submodule"'
sayx 'git -C "$E5/super" worktree add --quiet -b gauntlet/s "$E5/super-wt" HEAD'
sayx 'ls -a "$E5/super-wt/vendor/sub"'
sayx 'git -C "$E5/super-wt" status --porcelain'
sayx 'git -C "$E5/super-wt" submodule status'
sayx 'git -C "$E5/super-wt" -c protocol.file.allow=always submodule update --init --quiet'
sayx 'cat "$E5/super-wt/vendor/sub/.git"'
sayx 'git -C "$E5/super/vendor/sub" worktree add --quiet -b gauntlet/subwt "$E5/sub-wt" HEAD'
sayx 'git -C "$E5/sub-wt" rev-parse --git-common-dir'
sayx 'git -C "$E5/outer/inner-wt" worktree add --detach --quiet "$E5/wt-of-wt" HEAD'
sayx 'git -C "$E5/outer/inner" worktree list'
sayx 'git -C "$E5/wt-of-wt" rev-parse --git-common-dir'
```

### E6 — lifecycle failure modes (quoted in §8.2, §11)

```bash
cd "$LAB"; rm -rf e6; mkdir e6; cd e6; E6="$PWD"
mkrepo "$E6/repo"
# A dirty removal
sayx 'git -C "$E6/repo" worktree add --quiet -b gauntlet/a "$E6/a" HEAD'
echo dirt > "$E6/a/dirt.txt"
sayx 'git -C "$E6/repo" worktree remove "$E6/a"'
sayx 'git -C "$E6/repo" worktree remove --force "$E6/a"'
# B git worktree lock vs prune/remove
sayx 'git -C "$E6/repo" worktree add --quiet -b gauntlet/b "$E6/b" HEAD'
sayx 'git -C "$E6/repo" worktree lock --reason "run gauntlet/b is live" "$E6/b"'
sayx 'rm -rf "$E6/b"'
sayx 'git -C "$E6/repo" worktree prune --verbose'
sayx 'git -C "$E6/repo" worktree remove --force "$E6/b"'
sayx 'git -C "$E6/repo" worktree unlock "$E6/b"'
sayx 'git -C "$E6/repo" worktree prune --verbose'
# C creation failure on an unwritable parent
mkdir -p "$E6/ro"; chmod 500 "$E6/ro"
sayx 'git -C "$E6/repo" worktree add --quiet -b gauntlet/c "$E6/ro/c" HEAD'
sayx 'ls "$E6/repo/.git/worktrees"'
sayx 'git -C "$E6/repo" branch --list'
chmod 700 "$E6/ro"
# D stale admin entry blocks re-add
sayx 'git -C "$E6/repo" worktree add --quiet -b gauntlet/d "$E6/d" HEAD'
sayx 'rm -rf "$E6/d"'
sayx 'git -C "$E6/repo" worktree list --porcelain'
sayx 'git -C "$E6/repo" worktree add --quiet "$E6/d" gauntlet/d'
# E prune expiry
sayx 'git -C "$E6/repo" worktree add --quiet -b gauntlet/e "$E6/e" HEAD'
sayx 'rm -rf "$E6/e"'
sayx 'git -C "$E6/repo" worktree prune --verbose --expire 3.days.ago'
sayx 'git -C "$E6/repo" worktree prune --verbose'
# F moved worktree + repair
sayx 'git -C "$E6/repo" worktree add --quiet -b gauntlet/f "$E6/f" HEAD'
sayx 'mv "$E6/f" "$E6/f-moved"'
sayx 'git -C "$E6/repo" worktree list --porcelain'
sayx 'git -C "$E6/f-moved" worktree repair'
sayx 'git -C "$E6/repo" worktree list --porcelain'
```

### E7 — Gauntlet's own gitops against a linked worktree (quoted in §9.1, §9.2)

A Python script that imports `gauntlet.engine.gitops` from this repository and
calls it against a throwaway repo plus one linked worktree, exercising
`validate_temp_index_path`, `git_index_path`, `current_branch`, `is_clean` and
`worktree_tree_hash` from both trees. Run with `uv run python`. Full output is
quoted in §9.2 (containment) and §9.1 (per-tree helpers).

### E8 — state-root candidates, reset isolation, prune cross-talk (quoted in §5, §6, §8.2, §9.4)

```bash
cd "$LAB"; rm -rf e8; mkdir e8; cd e8; E8="$PWD"
mkrepo "$E8/repo"
git -C "$E8/repo" worktree add --quiet -b gauntlet/slug "$E8/wt" HEAD
# four candidate state roots, one file each
mkdir -p "$E8/repo/.git/gauntlet/state/slug/run-1" "$E8/repo/.gauntlet-state/slug/run-1" \
         "$E8/sibling-state/slug/run-1" "$E8/wt/runs/slug/run-1"
sayx 'git -C "$E8/repo" status --porcelain --untracked-files=all'
sayx 'git -C "$E8/wt" status --porcelain --untracked-files=all'
sayx 'git -C "$E8/repo" rev-parse --git-common-dir'
sayx 'git -C "$E8/wt" rev-parse --path-format=absolute --git-common-dir'
sayx 'git -C "$E8/repo" clean -xdffn'
sayx 'git -C "$E8/wt" clean -xdffn'
sayx 'git -C "$E8/repo" worktree remove --force "$E8/wt"'
# B reset isolation
git -C "$E8/repo" worktree add --quiet "$E8/wt" gauntlet/slug
echo impl > "$E8/wt/impl.txt"; git -C "$E8/wt" add -A; git -C "$E8/wt" commit -q -m "P1: work"
sayx 'git -C "$E8/wt" reset --hard HEAD~1'
sayx 'git -C "$E8/repo" rev-parse --abbrev-ref HEAD'
sayx 'git -C "$E8/repo" rev-parse HEAD'
sayx 'git -C "$E8/repo" reflog --date=iso -3'
# C prune cross-talk, with and without git worktree lock
git -C "$E8/repo" worktree add --quiet -b gauntlet/other "$E8/other" HEAD
sayx 'rm -rf "$E8/other"'
sayx 'git -C "$E8/repo" worktree prune --verbose'
git -C "$E8/repo" worktree add --quiet "$E8/other" gauntlet/other
sayx 'git -C "$E8/repo" worktree lock --reason "gauntlet run other/run-2 live" "$E8/other"'
sayx 'rm -rf "$E8/other"'
sayx 'git -C "$E8/repo" worktree prune --verbose'
```

### E9 — existing paths, symlinked roots, concurrent add (quoted in §6.3, §8.2, §11)

```bash
cd "$LAB"; rm -rf e9; mkdir e9; cd e9; E9="$PWD"
mkrepo "$E9/repo"
mkdir -p "$E9/occupied"; echo x > "$E9/occupied/keepme.txt"
sayx 'git -C "$E9/repo" worktree add --quiet -b gauntlet/a "$E9/occupied" HEAD'
mkdir -p "$E9/empty"
sayx 'git -C "$E9/repo" worktree add --quiet -b gauntlet/b "$E9/empty" HEAD'
mkdir -p "$E9/real-root"; ln -s "$E9/real-root" "$E9/link-root"
sayx 'git -C "$E9/repo" worktree add --quiet -b gauntlet/c "$E9/link-root/c" HEAD'
sayx 'git -C "$E9/repo" worktree list --porcelain'
sayx 'git -C "$E9/link-root/c" rev-parse --show-toplevel'
mkdir -p "$E9/outside"; ln -s "$E9/outside" "$E9/repo/escape-link"
sayx 'git -C "$E9/repo" worktree add --quiet -b gauntlet/d "$E9/repo/escape-link/d" HEAD'
sayx 'ls "$E9/outside"'
( git -C "$E9/repo" worktree add --quiet -b gauntlet/e1 "$E9/race" HEAD 2>&1 & \
  git -C "$E9/repo" worktree add --quiet -b gauntlet/e2 "$E9/race" HEAD 2>&1 & wait )
sayx 'git -C "$E9/repo" branch --list'
sayx 'git -C "$E9/repo" worktree list'
```

### E10 — the recommended layout, end to end (quoted in §6.2)

```bash
cd "$LAB"; rm -rf e10; mkdir e10; cd e10; E10="$PWD"
mkrepo "$E10/repo"; CG="$E10/repo/.git"
sayx 'git -C "$E10/repo" worktree add --quiet -b gauntlet/slug "$CG/gauntlet/worktrees/slug/run-1" HEAD'
sayx 'git -C "$CG/gauntlet/worktrees/slug/run-1" rev-parse --show-toplevel'
sayx 'git -C "$CG/gauntlet/worktrees/slug/run-1" rev-parse --abbrev-ref HEAD'
sayx 'git -C "$E10/repo" status --porcelain --untracked-files=all --ignored'
sayx 'git -C "$E10/repo" clean -xdffn'
# agent work + commit in the run worktree
sayx 'git -C "$E10/repo" fsck --no-progress'
sayx 'git -C "$E10/repo" gc --quiet --prune=now'
mkdir -p "$CG/gauntlet/state/slug/run-1/journal"; echo evt > "$CG/gauntlet/state/slug/run-1/journal/evt-1.json"
sayx 'git -C "$E10/repo" worktree remove --force "$CG/gauntlet/worktrees/slug/run-1"'
sayx 'find "$CG/gauntlet" -type f'
sayx 'git -C "$E10/repo" worktree add --quiet "$CG/gauntlet/worktrees/slug/run-1" gauntlet/slug'
sayx 'git -C "$CG/gauntlet/worktrees/slug/run-1" log --oneline'
sayx 'git -C "$CG/gauntlet/worktrees/slug/run-1" worktree add --detach --quiet "$E10/verify-copy" HEAD'
sayx 'git -C "$E10/verify-copy" rev-parse --git-common-dir'
```

# P7d gate: the dogfood found a blocker in the ratified worktree root

> **Status: proposed by the P7d builder. Nothing here is ratified.** It records
> what the P7d gating dogfood found, why the default flip and the tree-guard
> retirement did **not** happen, and the options the maintainer has. It amends
> no ratified document: `proposals/P7-worktree-spike.md` and
> `proposals/P7c-split-seam.md` are unchanged.
>
> This is an **UPSTREAM CONFLICT** in the FR-10.4 sense — implementation
> revealed that a ratified recommendation cannot be carried out as written.
> Agents propose; humans ratify.

---

## 1. The gate, and what it produced

`proposals/P7c-split-seam.md` §6 and the P7c-1 commit body both make the flip
conditional: *"P7d — flipping the default to `dedicated` — remains a separate
stage gated on a dogfood run that exercises §11 rows 2, 5 and 10."*

The dogfood was run. It was a real `gauntlet run` in `dedicated` mode against a
scratch repo carrying the human-authored toy PRD (`tests/fixtures/toy/prd.md`),
the shipped `standard` pipeline and the real assets — the same fixture shape as
`tests/integration/test_standard_pipeline_e2e.py`, driven from the CLI rather
than from pytest, with live `claude` / `codex` / API agents and the judge on.

**All three §11 rows were exercised and all three passed** (§3 below).

**The run itself failed at its first agent-writing step**, for a reason none of
the three rows covers and that no unit test can see: the `claude` CLI refuses to
write any file whose path contains a `.git/` component, and spike §6.2 puts every
run worktree inside `.git/`.

That is the blocker. It is not a bug in this phase's code — there is no code in
this phase. It is a property of the ratified layout that the spike never
measured, because every experiment in Appendix A measures *git*, and this is a
property of the *agent CLI that drives every builder and verifier step*.

---

## 2. The blocker, measured

### 2.1 What the derived root is

Spike §6.2 fixes the run worktree at
`<git-common-dir>/gauntlet/worktrees/<slug>/<run-id>`, with §6.4 explicitly
refusing a configuration knob and §15's deferral **D2** parking a configurable
`worktree_root` as its own phase. For an ordinary (non-bare) repository
`<git-common-dir>` is `<repo>/.git`, so every run worktree lives under `.git/`.

### 2.2 What the `claude` CLI does with such a path

Reproduced in isolation, with **no Gauntlet, no judge, and no hook** — a bare
repo, a linked worktree under `.git/`, and one `claude -p` invocation with the
same flags the `builder` profile uses (`--permission-mode acceptEdits`,
`--allowedTools Write,Read,Bash`, `--setting-sources project`):

```
$ git worktree add .git/gauntlet/worktrees/probe/run-1 probe
$ cd .git/gauntlet/worktrees/probe/run-1
$ claude -p ... 'Write a file named PROBE.txt in the current directory ...'
Claude requested permissions to edit
  …/dotgit-probe/.git/gauntlet/worktrees/probe/run-1/PROBE.txt
  which is a sensitive file.
$ ls PROBE.txt
ls: PROBE.txt: No such file or directory
```

The control isolates the `.git/` component as the sole cause — the *same branch*,
in a linked worktree at a sibling path outside `.git/`, is fully writable:

```
$ git worktree add ../probe-outside probe2
$ cd ../probe-outside && claude -p ... 'Write a file named PROBE.txt …'
Write succeeded — PROBE.txt created with content "OK".
```

### 2.3 The part that makes it worse than a hard refusal

The guard is **not uniform across write mechanisms**. Three write forms issued
in one turn, in the same run worktree:

| # | command | outcome |
|---|---|---|
| 1 | `cat > PROBE_A.txt <<EOF` | **permitted**, file landed |
| 2 | `tee PROBE_B.txt > /dev/null <<EOF` | **permitted**, file landed |
| 3 | `printf C > PROBE_C.txt` | **refused** — "which is a sensitive file" |

and the `Write` / `Edit` tools are refused unconditionally.

So a dedicated run does not fail cleanly. It fails **iff the model happens not to
improvise a write form the guard does not detect** — which is exactly what the
two rounds of the dogfood showed, with the same pipeline on the same tree:

* **round 1 (builder = opus)** tried `Write`, `Edit` and `cat > file`, was
  refused, wrote the finished PRD revision into its *message* instead, and the
  cycle failed closed with
  `fixer made no changes in round 1 despite 7 accepted finding(s)`;
* **round 2 (builder = haiku)** tried `Edit` (refused), `cp` (refused by the
  judge, correctly — it targeted the operator's checkout), and finally
  `tee … > /dev/null`, which **landed**. The round then completed normally and
  committed `PRD.1: Address review — 6 fixed, 0 declined` to the run branch.

Both rounds are in the same run's `judge-audit.jsonl`, and that file is the
second half of the problem.

### 2.4 It is invisible to Gauntlet's own evidence

Gauntlet's judge runs as a **PreToolUse** hook — *before* the CLI applies its own
permission rules. So the audit records the judge's verdict, not the outcome:

```
d8  Write allow llm  …/worktrees/toy/run-…/runs/toy/prd.md     ← judge said allow
d9  Edit  allow llm  …/worktrees/toy/run-…/runs/toy/prd.md     ← judge said allow
```

Neither write happened. `runs/toy/prd.md` was byte-identical to the original
fixture in **both** trees afterwards. Nothing in the manifest, the journal, the
judge audit or the transcript names the refusal; the only engine-visible symptom
is the fail-closed "the fixer made no changes", which reads as a model failure.

That is a data-over-inference violation of the kind CLAUDE.md §2 exists to
prevent: the durable record says "allowed" about an action that was denied.

### 2.5 Severity

Under `worktree.mode: dedicated`, every step run by the `claude` adapter —
`builder`, `verifier`, and the behavioural verifier's disposable copy, which is
created *inside* the run worktree — is subject to this. Flipping the default
makes it the condition of every new run on every adopter machine.

---

## 3. What the dogfood did prove — §11 rows 2, 5 and 10

Recorded because they are the gate's actual subject and they all held, against a
real run's real tree.

### Row 5 — the `git worktree lock` is what stops another run's prune

With the run's tree deleted from disk and the admin entry still locked, a full
`git worktree prune --verbose --expire now` from the operator's checkout — which
is exactly what another run's `verify.discard_disposable_copy` issues (E8-C) —
was a **no-op**, and the entry survived. The porcelain entry carried
`locked gauntlet run toy/run-… live` and **no `prunable` line**.

The control, on an otherwise identical entry:

| state | porcelain | `prune --expire now` |
|---|---|---|
| tree deleted, **locked** | no `prunable` line | entry survives |
| tree deleted, **unlocked** | `prunable gitdir file points to non-existent location` | `Removing worktrees/…` — entry gone |

This also confirms P7c-1's CORRECTION 2 live: a locked worktree is never
reported prunable, so `prunable` is the wrong detection signal for §11 row 2 and
registered-and-absent is the right one.

### Row 2 — tree gone, branch and journal survive → recreate, HEAD verified

After the `rm -rf` above, `gauntlet status` reported the tree honestly:

```
worktree: MISSING at …/worktrees/toy/run-… — `gauntlet resume toy` recreates it
          from the branch and journal
```

and `status --json` gave `registered: true, present: false, prunable: null` —
matching the playbook wording P7c-1.1's F-014 corrected.

A real `gauntlet resume` then rebuilt it. The journal carries both events, and
the second one is the acceptance-A3 evidence:

| seq | kind | `recreated` | `branch_sha` |
|---|---|---|---|
| 1 | `WorktreeAdopted` | `false` | `2bf9ff4…` |
| 8 | `WorktreeAdopted` | **`true`** | `2bf9ff4…` |

The recreate ran through `WT.recreate` with `expect_head` taken from the
journal's own `WorktreeAdopted.branch_sha`, and did not raise — so the rebuilt
HEAD was verified equal to the state the run had recorded, not merely present.
The run then continued and made real progress on the run branch.

### Row 10 — a dirty run worktree is snapshotted before any forced removal

The run's tree was dirtied with one modified tracked file and one untracked file,
then torn down with `gauntlet clean toy --force`:

```
$ git for-each-ref refs/gauntlet/recovery      # before: 0 refs
$ gauntlet clean toy --force
deleted 'gauntlet/toy' (forced)
$ git for-each-ref --format='%(refname)' refs/gauntlet/recovery
refs/gauntlet/recovery/run-2026-08-06T03-36-12/snapshot-2026-08-06T04-00-19-00-00
$ git show <ref>:worktree/WIP-uncommitted.txt
BUILDER-WIP-DO-NOT-LOSE
```

with `metadata.json` carrying `reason: "worktree-teardown"` and the full index
plane beside the worktree plane. The snapshot precedes the removal, and the
builder's uncommitted bytes are recoverable from it. R2 holds.

---

## 4. Why this halts the flip, and why it also halts the guard retirement

**The flip.** `worktree.mode: dedicated` as the default would make the §2 defect
the condition of every new run. The gate exists precisely to catch this, and a
gate that is passed by "the three rows I was told to check went green" while the
run itself could not write a file is a gate in name only.

**The guard retirement.** It is not independently deliverable, and the reason is
recorded in P7c-1's own CORRECTION 1: the worktree-global tree guard *"could not
retire while `same_tree` was the default, because a dedicated run that stopped
writing it would stop excluding a concurrent `same_tree` run of another slug on
the operator's checkout — and `finish` deliberately still merges there (§9.4)."*
Flipping the default is what was to make that precondition true. The default does
not flip, so the precondition stays false, and retiring the guard now would
re-open exactly the vector CORRECTION 1 identified. Deferring it is the
conclusion of the retained argument, not an excuse.

**Acceptance.** P7 acceptance A1/A2/A3 therefore continue to hold **only for runs
a human has explicitly opted into `dedicated`** — the same sentence every P7c
commit body ends with. P7d was to be where that stopped being true. It is not.

---

## 5. The options, and what each costs

None of these is taken here. Each amends something ratified, so each needs the
maintainer.

### Option 1 — move the derived root out of `.git/` (amends §6.2)

`<git-common-dir>/../.gauntlet-worktrees/<slug>/<run-id>`, or a sibling
directory beside the repo.

* **Fixes it completely** — §2.2's control proves a linked worktree outside
  `.git/` is fully writable, on the same branch, same repo, same flags.
* **Costs:** every property §6.2 measured has to be re-measured at the new
  location. The ones that actually depended on being under `.git/` are:
  invisibility to `git status --ignored` and immunity to `git clean -xdff`
  (§6.1 rejected an in-repo root for exactly these), and same-filesystem
  guarantee (which made `EXDEV` structurally unreachable, §6.3). A sibling
  directory outside the repo re-opens §7's "no outside-repository path is
  written" question, which is why §15's D2 says a configurable root needs that
  rewrite. **This is the option the evidence points at, and it is the one that
  needs the most re-ratification.**

### Option 2 — ship D2 (a configurable `worktree_root`) and default it outside `.git/`

* **Costs:** D2 is explicitly deferred, needs the `resolve()`-based containment
  validator §6.4 describes (E9-C proves the current string check is defeated by a
  symlink), and P7 "must not absorb" its deferrals. It also does not by itself
  choose a *safe default*, which is the actual problem.

### Option 3 — keep `dedicated` opt-in indefinitely; do not flip

* **Costs:** P7's acceptance is never met for runs in general, and the operator
  benefit §18.3 describes stays unavailable by default. The defect still bites
  every operator who opts in, silently (§2.4).
* **Cheapest, and it is the status quo** — but it should be a decision, not an
  omission, and §2.4's invisibility should be fixed either way (see Option 4).

### Option 4 — make the failure visible, whatever else is decided

Independent of the root question and worth doing on its own: the engine cannot
currently tell "the agent chose not to write" from "the agent was refused".

The probe must be **deterministic**, and that is the whole difficulty. A probe
that asks a model to "write a file here" inherits precisely the nondeterminism
in §2.3: it can pass by choosing `tee` while the real task later fails on
`Write`, or fail merely because it chose a blocked form. A stochastic probe for
a stochastic failure proves nothing in either direction.

So the check must exercise **each write mechanism the adapter actually uses, by
name and without model choice** — the `Write` tool, the `Edit` tool, and a shell
redirection — and treat *any* of them being refused as the failure. Two parts,
because the refusal does not surface where the engine currently looks (§2.4):

* a **`doctor` check** that runs each mechanism against a scratch path under the
  derived worktree root and reports which are refused;
* a **start-time preflight** on the same set, parking with a named reason before
  any agent step — the fail-closed shape of the §7 submodule park.

Both require reading the adapter's *post-tool* permission outcome rather than
the PreToolUse hook's verdict, which is the specific blindness §2.4 describes.

Stated plainly: this is a detector, not a fix. It converts a silent, model-
dependent failure into `worktree_unavailable`-class evidence. Only Option 1
makes `dedicated` work.

**Recommendation:** Option 1 for the layout, plus Option 4 regardless. Option 1 is
the only one that makes `dedicated` actually work — moving the root out of
`.git/` is the correction, not the detector. Option 4 is worth doing anyway
because it is the only thing that would have made this dogfood's failure legible
without a human reading a transcript, but it must be built as a deterministic
per-mechanism check (see above) or it will reproduce the very nondeterminism it
is meant to catch.

---

## 6. What was NOT touched

* `PRD-gauntlet.md`, `RECOVERY-REDESIGN-PLAN.md`, `policy.yaml` and every
  approved run artifact — unchanged.
* `proposals/P7-worktree-spike.md` and `proposals/P7c-split-seam.md` —
  **unamended**. §6.2 still reads as ratified; this document is the proposal
  against it, not an edit of it.
* `worktree.mode`'s default — still `same_tree`.
* The worktree-global tree guard — still written by every driving verb.
* The §15 deferrals **D1–D6**, most importantly **D1**
  (`refs/gauntlet/state/<run>` anchoring), and `gauntlet review` (§14.3).

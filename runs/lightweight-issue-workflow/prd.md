# PRD: Lightweight Issue Workflow — `gauntlet review`

**Status:** Draft v0.2
**Author:** John (with Claude)
**Date:** 2026-06-30
**Working name:** Lightweight Issue Workflow (`gauntlet review`)
**Relationship to existing artifacts:** Does **not** amend `PRD-gauntlet.md`,
`policy.yaml`, or any approved `prd.md`/`plan.md`. It is an additive feature that
builds on existing machinery: the `adversarial_cycle` primitive (FR-5.2), its
`code_review` mode and diff-scoped confirm (FR-9.5), the run lifecycle / manifest
/ resume (FR-8), the fail-closed escalation park (FR-10.5), the agent-profile +
entry-point plugin pattern (FR-2.4), and the judge push/PR boundary (FR-9.8). One
new judge-policy rule is required (FR-7 here); per the §0 "approved artifacts"
rule that rule is proposed, not silently applied — see §7 and Open Question 11.4.

---

## §1 Overview

### 1.1 Problem statement

Gauntlet today is shaped entirely around a heavyweight, PRD-driven pipeline:
author a PRD, run it through adversarial review and a human gate, generate and
review a phased plan behind another gate, then implement phase by phase. That
ceremony is correct for a feature, but it is far too much for a bug fix or a
small change. The practical consequence is that for those small changes
engineers skip Gauntlet entirely and use plain Claude Code — and in doing so they
lose the single most valuable part of the harness: the adversarial review cycle
(review → triage → fix → confirm). The reviewing muscle that catches incorrect or
incomplete fixes is exactly what is dropped precisely when the work is small
enough that no one wants to write a PRD for it.

A second, related pain: a *correctness* review of a fix needs to know what the
fix was supposed to do. Plain "review this diff" can judge whether code is
internally sound, but not whether it actually solves the underlying problem,
because the problem lives in a ticket (Linear, for this team) that the reviewer
never sees.

### 1.2 Solution summary

Add a new lightweight entry point, `gauntlet review`, that runs **only** the
existing `adversarial_cycle` primitive against an already-implemented change —
the "bring-your-own-fix" model. The engineer fixes the bug however they like
(plain Claude Code, by hand) on a branch; `gauntlet review` then reviews that
change against the originating ticket, applies accepted fixes as commits **in
place on that branch**, and stops. It runs with **zero routine human gates** —
the resulting branch/PR *is* the human review — while preserving the cycle's
fail-closed escalation park as a safety stop.

To make the review a *solution-correctness* review and not just a diff-quality
pass, the run fetches the originating problem statement from a pluggable issue
tracker (v1: Linear) into an out-of-repo `intent.md`, the lightweight analog of
`prd.md`. The reviewer is given both the diff and the intent and asked whether
the change actually resolves the stated problem.

Two surfaces share one flow: a **local branch** (default the current branch, or a
named one) and a **GitHub PR** (`--pr <N>`), where the PR is checked out locally
and its linked ticket supplies the intent. In both cases fixes land locally and
the human pushes — the harness never pushes autonomously (FR-9.8).

Crucially, this reuses the trusted review machinery wholesale: the same reviewer,
the same point-by-point triage rubric, the same severity-aware escalation, the
same reviewer-mutation guard, and the same audit-trail fix commits. Only the
PRD → plan → phase ceremony is removed.

### 1.3 The assumption this validates

The riskiest belief: that the value of the heavyweight pipeline's review is
**separable** from its ceremony — i.e., that running the `adversarial_cycle`
alone, against an isolated small diff and a provenance-tagged problem statement
(independent where a ticket exists, author/session-derived with human ratification
otherwise — FR-2.1a/FR-2.5), produces correctness/spec-gap findings valuable enough to justify the
flow over plain Claude Code, at low enough cost and friction that engineers
actually use it for bug fixes. If a single-round review of a small fix surfaces
few real findings, or costs/takes enough that engineers still skip it, the
lightweight flow does not earn its place. P3 puts this to the test on real fixes.

---

## §2 Goals and Non-Goals

### 2.1 Goals

| #  | Goal (outcome) | Need it serves |
|----|----------------|----------------|
| G1 | Engineers get the adversarial review cycle on small fixes without authoring a PRD, plan, or phases | Recover review on the work that currently bypasses Gauntlet |
| G2 | The review judges *solution correctness against the originating ticket*, not just diff quality | The reviewer can ask "does this actually fix the bug" |
| G3 | The review leaves the repo working tree exactly as clean as plain Claude Code would — the review generates no committed or untracked artifact of its own; only fix commits land. (A pre-existing user-supplied in-repo `--intent` file is the user's own untracked dirt, not a review artifact, and is excluded from this guarantee per FR-2.4/FR-8.2.) | Bug intent is transient and must not pollute history or the worktree (contrast: `prd.md` is durable product understanding) |
| G4 | Fixes land in place on the branch under review (or the PR's head branch); the harness never pushes | Honor the one-PR-per-change model and the no-autonomous-push invariant (FR-9.8) |
| G5 | The issue tracker is a configurable, pluggable provider; v1 ships Linear, with the seam for GitHub Issues/Jira later | Different teams use different trackers; "tracker of choice" must not be a fork |
| G6 | The review *cycle* runs with zero routine human gates, while still failing closed (parking) on an unresolved blocking finding; the only human touch-point is a one-time pre-run ratification of a non-independent (author/session-derived) problem statement (FR-2.5), skipped entirely for an independent tracker ticket | Lightweight means unattended-to-PR; safety still cannot be skipped, and accepting author-derived intent is made tolerable by a single up-front human ratification |

### 2.2 Non-Goals (v1)

- **No implementation by the harness.** `gauntlet review` reviews and fixes an
  *existing* change; it does not author the original fix from a ticket. (The
  autonomous "implement from a ticket" variant — a `review` run with a leading
  `agent_task` — is a deliberate follow-on, Open Question 11.1.)
- **The `/gauntlet-review` Claude Code skill is a follow-on, not a v1
  deliverable.** v1 ships the `gauntlet review` **CLI** with everything that skill
  would need — non-interactive `-m`, `--intent-provenance`, and `--approved-intent`
  (FR-1.1) plus the provenance/ratification recording (FR-2.6). The skill itself
  (synthesize intent from the active session and/or a vague linked ticket, present
  it for in-session human review, then invoke `gauntlet review -m "<approved>"
  --intent-provenance … --approved-intent` and hand off to `/gauntlet-operator`) is
  scoped out of this PRD. Decision recorded in Open Question 11.8.
- **No autonomous push, PR open, or merge.** Fixes land locally; the human pushes
  and opens/merges. Inherits FR-9.8 unchanged.
- **No write-back to the tracker.** The run never posts findings as PR review
  comments or comments on the Linear ticket. (Tier-2 "post findings as a PR
  review" is out of scope — Open Question 11.2.)
- **No fork-PR push-back.** For PRs from forks the harness cannot push regardless;
  Mode A (local fixes, human pushes) is the only supported write path.
- **Only Linear is implemented in v1.** GitHub Issues and Jira providers are
  reserved by the config seam but not built; `provider:` other than `linear` is
  a fail-closed config error.
- **No multi-round planning or phasing.** A review run is a single
  `adversarial_cycle`, not a phased build.
- **Not a heavyweight-run replacement.** Features still go through the full
  pipeline; `gauntlet review` is for bug fixes and small changes only. Where the
  line falls is the engineer's judgment, not enforced.
- **No GUI / TUI.** CLI + terminal summary only.
- **No committed review artifacts by default.** The review's `intent.md`,
  `findings.json`, transcripts, and manifest are not committed (and default to
  out-of-repo); only the `REVIEW.x` fix commits reach the branch.

---

## §3 Users and Personas

| Persona | What they do with this |
|---------|------------------------|
| **Fix author** (any engineer) | Fixes a bug on a branch, then runs `gauntlet review [--issue ENG-1234]` to get an adversarial correctness review and accepted fixes before pushing. The primary user. |
| **PR author/reviewer** | Runs `gauntlet review --pr 123` to pull a PR down, review it against its linked ticket, land fix commits locally, then pushes the updated branch. |
| **Pipeline maintainer** | Owns `pipelines/review.yaml`, the review prompt, and the `issue_tracker` config block / provider choice for the team. |

---

## §4 System Architecture

### 4.1 Components

**New:**

- `src/gauntlet/trackers/` — the issue-tracker abstraction (mirrors `adapters/`):
  - `base.py` — the `IssueTracker` protocol, the `Issue` / `IssueRef` payload
    models, and the error taxonomy (`IssueTrackerError`, `IssueTrackerAuthError`,
    `IssueNotFound`, `IssueTrackerUnavailable`).
  - `intent.py` — `render_intent(issue) -> str`, shared across all providers;
    turns a normalized `Issue` into `intent.md` deterministically (full body under
    `## Problem`; `## Repro / symptom` and `## Expected / acceptance` only from
    discrete provider fields — none in Linear v1, so omitted; see §6 for the rules).
  - `linear.py` — `LinearIssueTracker` (GraphQL against `api.linear.app`).
  - `__init__.py` — `get_tracker(config) -> IssueTracker` factory over the
    `gauntlet.issue_trackers` entry-point registry; v1 registers `linear` only.
- `pipelines/review.yaml` — a single-stage pipeline: one `adversarial_cycle` step
  (`mode: code_review`, `phase: REVIEW`), no `human_gate`, optional pre-review
  test step.
- `prompts/review-code-intent.md` — the review prompt: the existing code-review
  lens plus an explicit solution-correctness dimension that diffs the change
  against `intent.md`.
- New `review` command in `src/gauntlet/cli.py`.
- Review run lifecycle: a review-specific **entry contract** and **in-place
  branch adoption** path (parallel to `RunManager.check_entry_contract` /
  `_prepare_run_branch`), plus **out-of-repo state-dir** resolution for the run
  root of a review run.
- `IssueTrackerConfig` and review/state-dir fields in `src/gauntlet/config.py`.
- `gauntlet doctor` extension: validate the configured tracker provider + auth.

**Reused (unchanged):**

- `engine/cycle.py` `adversarial_cycle` — including `_phase_and_handoff`, which
  already falls back to `(explicit_phase, HEAD)` when the manifest has no prior
  commits (the review run's handoff is the branch tip), and `code_review` mode,
  which already honors a configurable `review_base` for the diff range.
- `findings.json` / `triage.json` / `confirm.json` schemas — no change; a
  "does-not-fix-the-bug" finding is an existing `correctness` / `spec-gap`
  category.
- Manifest write-ahead checkpoint, `resume`, `status`, `abort`.
- The reviewer-mutation guard (FR-9.6) and severity-aware escalation (FR-10.5).
- `gh` CLI (already a dependency surface) for PR resolution/checkout.
- The judge service and policy (with one added rule — §7).

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Entry model | Bring-your-own-fix (review an existing change), not implement-from-ticket | Targets the actual gap: people already fix with plain Claude Code and lose review. Lowest risk; meets users where they are. |
| Human gates | Zero routine gates; preserve the fail-closed escalation park | "Lightweight" means unattended-to-PR; the PR is the human review. The escalation park is safety, not ceremony, and dropping it would violate fail-closed. |
| Where fixes land | In place on the branch under review / the PR head branch; no `gauntlet/<slug>` branch minted | The branch *is* the PR; matches "PR is the gate" and avoids a stacked branch + merge step. Diverges from FR-9.1, which is heavyweight-specific. |
| Diff scope | Reuse `code_review` mode with `review_base` set to the base branch (not the default `handoff^`) | The reviewer must see the *whole* change, not one commit. Pure config; no engine change. |
| Problem statement | Provenance-tagged `intent.md` fed to the reviewer: tracker-independent when a ticket exists, else author/session-derived with mandatory pre-run human ratification (FR-2.1a/FR-2.5) | A correctness review is strongest with a problem statement independent of the diff, but author/session-derived intent is a supported first-class mode; its circularity is made tolerable by a human ratifying the synthesized statement before the review runs. |
| Tracker integration | Pluggable `IssueTracker` provider via entry point; v1 = Linear only | "Tracker of choice" is a provider selection, not a fork (mirrors `AgentAdapter`, FR-2.4). Bounded honestly: non-`linear` fails closed at config load. |
| Review artifacts | Not committed; default to an out-of-repo state dir | Bug intent is transient/low-value; `prd.md` is durable product understanding. Only `REVIEW.x` fix commits touch the repo — zero working-tree footprint. |
| Tracker fetch location | Orchestrator-level (Python), into `intent.md`, not via an agent tool call | Determinism over cleverness / data over inference: a direct, logged, reproducible fetch beats asking an agent to gather the ticket via MCP. |
| Push-back | Never by the harness; fixes are local, the operator pushes | Inherits FR-9.8. Also the only path that works for fork PRs. |
| Default rounds | `max_rounds: 1`, overridable via `--rounds` | Lightweight; a single round still escalates/parks on an unresolved blocker rather than passing it silently. |

---

## §5 Functional Requirements

### FR-1: The `gauntlet review` command and its inputs

- **FR-1.1** A new command exists:
  `gauntlet review [<branch>] [--pr <N|url>] [--issue <ref|url>] [--intent <path>] [-m <text>] [--intent-provenance <tracker|tracker-session|author-session-summary>] [--approved-intent] [--base <ref>] [--code-only] [--rounds <N>] [--test | --no-test]`.
  The baseline-test step (§6) is **off by default**; `--test` enables it (and
  requires `config.test_command` to be set, else a usage error), `--no-test` is
  the explicit-off form. This resolves Open Question 11.5.
  `--intent-provenance` declares the independence of a manually supplied intent
  (`--intent`/`-m`/`$EDITOR`); it is ignored with a usage error if combined with
  `--issue` (whose provenance is always `tracker`), and defaults to
  `author-session-summary` for manual sources (FR-2.1a). `--approved-intent`
  asserts a non-independent intent was ratified by a human out of band and is the
  non-interactive form of the FR-2.5 ratification hook.
- **FR-1.2** Target resolution: with no positional and no `--pr`, the target is
  the current branch. A positional `<branch>` targets that branch. `--pr`
  resolves and checks out a GitHub PR's head branch. `--pr` and a positional
  `<branch>` together is a usage error.
- **FR-1.3** The review operates **in place** on the target branch: accepted-fix
  commits are appended to it; no `gauntlet/<slug>` branch is created. (Overrides
  FR-9.1's branch-minting for this command only.)

  **Acceptance:** `gauntlet review` with no args reviews the current branch and,
  if it accepts fixes, leaves `REVIEW.1` (etc.) commits on that same branch with
  no new branch created; passing `--pr 123` and a positional branch exits non-zero
  with a usage error; `gauntlet review my-branch` operates on `my-branch`.

### FR-2: Solution-correctness review against a provenance-tagged problem statement

- **FR-2.1** The run resolves a problem statement into an `intent.md` artifact
  from exactly one source, in precedence order: `--issue` (tracker fetch) >
  `--intent <file>` > `-m <text>` > `$EDITOR` template. For `--pr`, the primary
  source is the PR's linked ticket (FR-4.3), overridable by any explicit flag
  above; the PR body is secondary context only and is **not** by itself a
  sufficient problem statement (FR-4.3). A change author's own PR body describes
  the solution, not an independent statement of the bug, so treating it as the
  intent would make the correctness review circular.
- **FR-2.1a** **Intent provenance is a first-class, supported spectrum, not a
  single independent-ticket assumption.** Every resolved intent carries a
  `provenance` value on an independence axis, and the flow supports all three as
  legitimate modes (it does not require a tracker-independent statement):
  - `tracker` — a ticket authored independently of the fix (strongest
    independence). The only provenance produced by `--issue` and by PR-mode
    linked-ticket auto-derive (FR-4.3).
  - `tracker-session` — a ticket-seeded statement refined during the bug-fix
    session (a vague ticket expanded into a concrete statement). Non-independent.
  - `author-session-summary` — no ticket; the statement is synthesized from the
    working session (supported, weakest independence). Non-independent.

  Provenance is **derived from the source** for `--issue`/PR auto-derive (always
  `tracker`) and **declared by the operator** for the manual sources via
  `--intent-provenance {tracker|tracker-session|author-session-summary}` (FR-1.1).
  A manual source (`--intent`/`-m`/`$EDITOR`) with no `--intent-provenance`
  defaults to `author-session-summary` (the weakest, safest assumption — it forces
  ratification per FR-2.5 rather than silently treating session-derived text as
  independent). An `independent` boolean is derived from provenance
  (`tracker` → `true`; the other two → `false`).
- **FR-2.1b** A non-independent provenance is an explicit, deliberate ease-of-use
  trade, **not** a degraded review. The reviewer prompt is told the provenance and
  the `independent` flag (FR-2.2) so it calibrates how much weight to place on the
  "is this the right problem?" axis. Even when that axis is non-independent, the
  review still validates implementation-correctness, stated-acceptance coverage,
  regressions, and code quality against the diff. The circularity is mitigated by
  human ratification of the synthesized statement (FR-2.5), not by forbidding the
  mode.
- **FR-2.2** The reviewer is given both the commit-range diff and `intent.md`, and
  the review prompt explicitly requires evaluating whether the change resolves the
  stated problem and meets the stated acceptance/expected behavior — surfaced
  through the existing `findings.json` `correctness` / `spec-gap` categories. The
  prompt is also told the intent `provenance` and `independent` flag (FR-2.1a) so
  the reviewer calibrates the weight it places on the problem-correctness axis: a
  `tracker`-independent statement is treated as an authoritative problem definition,
  while a non-independent statement is treated as the author's own framing of the
  problem, with the implementation-correctness / acceptance-coverage / regression /
  quality axes carrying full weight regardless.
- **FR-2.3** Intent is **required** unless `--code-only` is passed. With no intent
  source resolvable and no `--code-only`, the run fails closed before any agent
  call (degrading silently to a diff-only review is the failure mode this
  prevents). `--code-only` runs a diff-quality review with no `intent.md`.
- **FR-2.4** **Intent input imposes zero repo footprint.** A problem statement
  supplied as a file (`--intent <path>`) must not affect any worktree-cleanliness
  check or any fix commit, even when the file lives inside the repo:
  - The `--intent` content is read once at run start and snapshotted into the
    out-of-repo `intent.md` (FR-8.1); the source file plays no further role.
  - A `--intent` path resolving inside the repo is added to the run's
    worktree-exclude set (the engine's `ctx.excludes`, already honored by the
    clean-tree/clean-handoff checks `is_clean(exclude=…)` and by fix-commit
    staging `git add -A :(exclude)…`). It therefore cannot trip the entry contract
    (FR-9.2) or any round's clean-handoff guard (FR-9.3), and cannot be swept into
    a `REVIEW.x` commit. The file is left present and untracked, untouched.
  - `-m <text>` (a CLI string) and the `$EDITOR` template create **no** file in
    the repo; the editor's temp file is written under the out-of-repo state dir.

  This closes the "dirty repo bricks the cycle" failure mode for the one new input
  that could otherwise reintroduce it (determinism over cleverness, fail closed).

  **Acceptance:** A review run started with `--issue <ref>` writes an `intent.md`
  containing the ticket's problem text and the reviewer prompt includes it; a run
  with neither an intent source nor `--code-only` exits non-zero with guidance and
  spawns no agent; a `--code-only` run completes with no `intent.md` and a
  diff-only review; a `bug.md` placed **inside** the repo and passed via
  `--intent ./bug.md` does not fail the entry contract or any clean-handoff check
  and never appears in any `REVIEW.x` commit, and after the run `git status` shows
  `bug.md` still present and untracked.

- **FR-2.5** **Pre-run human ratification of non-independent intent (the
  circularity mitigation).** When the resolved intent is **not** `tracker`
  provenance (i.e. `tracker-session` or `author-session-summary`, `independent ==
  false`), the synthesized problem statement must be surfaced to a human to
  review/edit and explicitly approve **before** the review cycle starts — before
  any reviewer agent is spawned. A human ratifying the problem statement is what
  makes the accepted circularity tolerable; without it the run must not proceed.
  This hook lives at **intent resolution / run entry**, not inside the cycle, and
  is enforced by the CLI as follows (fail closed):
  - **Interactive (a TTY is attached):** the CLI renders the resolved `intent.md`
    and requires an explicit confirm/edit step (the operator may edit the text in
    `$EDITOR` and must confirm) before the cycle begins. Declining aborts the run
    with no agent spawned.
  - **Non-interactive (no TTY — e.g. invoked by a skill or CI):** the CLI
    **rejects** a non-independent intent **unless** the caller passes
    `--approved-intent`, which asserts the statement was already ratified by a
    human out of band (e.g. the `/gauntlet-review` skill's in-session review,
    Non-Goal note / Open Question 11.8). Without `--approved-intent` the run fails
    closed with guidance and spawns no agent.
  - A `tracker`-provenance intent (independent ticket) needs **no** ratification
    step and runs unattended; this preserves the zero-routine-gates property
    (FR-3.1, G6) for the independent path.

  This is a one-time setup/entry step that establishes an *approved* intent; it is
  not a routine gate inside the review cycle. The zero-routine-gates guarantee of
  FR-3.1/G6 governs the cycle (review → triage → fix → confirm) once an approved
  intent has entered the run.

- **FR-2.6** **Provenance and ratification are recorded honestly.** The resolved
  `provenance` value, the derived `independent` flag, and — for non-independent
  intent — the ratification record (method: `interactive-confirm` or
  `approved-intent-flag`, the approving user, and timestamp) are persisted in the
  run manifest and reflected in the `intent.md` header (§6). They are passed into
  the reviewer prompt per FR-2.2 (data over inference: the reviewer is told how the
  problem statement was sourced rather than guessing).

  **Acceptance:** A run with `--issue ENG-1234` records `provenance: tracker`,
  `independent: true`, requires no confirmation, and runs unattended; a run with
  `-m "<text>"` and no `--intent-provenance` defaults to
  `provenance: author-session-summary`, `independent: false`, and — with no TTY and
  no `--approved-intent` — exits non-zero with guidance and spawns no agent; the
  same run with `--approved-intent` proceeds and records
  `ratification.method: approved-intent-flag` in the manifest; a run with
  `-m "<text>" --intent-provenance tracker-session` is treated as non-independent
  and still requires ratification.

### FR-3: Zero routine gates, fail-closed escalation preserved

- **FR-3.1** `pipelines/review.yaml` contains no `human_gate` step. Once an
  approved intent has entered the run, the review **cycle** proceeds review →
  triage → fix → confirm to completion with no human turn. "Zero routine gates"
  is a property of the cycle, not of intent establishment: a non-independent
  (author/session-derived) intent carries a one-time pre-run ratification step
  (FR-2.5) that runs *before* the cycle and is not a gate within it. A
  `tracker`-independent intent reaches the cycle with no human turn at all.
- **FR-3.2** The `adversarial_cycle`'s fail-closed behavior is preserved
  unchanged: an unresolved blocking finding after `max_rounds` (or a
  low-confidence triage on a blocking finding with no `escalation_agent`) **parks
  the run** (FR-10.5), recoverable via `gauntlet resume --response`.
- **FR-3.3** Default `max_rounds` is 1, overridable by `--rounds`.
- **FR-3.4** **Terminal rule per severity after the final confirm pass** (so a
  single round, `max_rounds: 1`, has fully-defined completion semantics for every
  severity, not only `blocking`):
  - An unresolved finding triaged **legitimate + `blocking`** → the run **parks**
    (fail closed, FR-3.2/FR-10.5); it never completes.
  - An unresolved finding triaged **legitimate + non-blocking** (`major` /
    `minor` / `nit`) → the run **completes** but records each such finding in the
    run summary as **residual risk** (id, severity, location, claim, and last
    confirm verdict), so it surfaces to the human on the resulting branch/PR — the
    PR being the human review (G6). It is **not** silently dropped.
  - A finding triaged **not legitimate** (bikeshedding/declined) → recorded with
    its triage reasoning per the standard audit trail; it does not affect
    completion.

  This is the lightweight flow's explicit terminal contract; it tightens, and does
  not contradict, the base cycle's behavior (which parks only on unresolved
  blocking findings). The §9 "Safety preserved" metric (0 passes with an unresolved
  blocking finding) follows directly from the first bullet.

  **Acceptance:** A review of a clean, correct fix completes end-to-end with zero
  human interaction and lands either no commits (nothing accepted) or `REVIEW.x`
  commits; a review with a seeded unresolved blocking finding parks (does not
  silently pass) and is resumable with `--response`; a review with a seeded
  unresolved legitimate `major` finding completes (does not park) and lists that
  finding in the run summary as residual risk (FR-3.4).

### FR-4: GitHub PR mode

- **FR-4.1** `--pr <N|url>` resolves PR metadata via `gh`
  (`headRefName`, `baseRefName`, `isCrossRepository`, `title`, `body`, `url`) and
  checks the PR out locally; the review then proceeds exactly as a local-branch
  review of that head branch.
- **FR-4.2** The diff base (`review_base`) is the PR's base ref.
- **FR-4.3** The intent is auto-derived from the PR's linked ticket: the
  configured tracker's `extract_refs(pr_body)` scans the PR body for its ref
  pattern (e.g. "Fixes ENG-1234"). **Multiple refs (normative v1, resolves Open
  Question 11.7):** `extract_refs` returns refs in **textual order**; the **first
  resolved** ref supplies the problem statement (`provenance: tracker`). When more
  than one distinct ref is present, the run **emits a warning** naming the chosen
  ref and lists **all** detected refs in the run summary as **ignored secondary
  refs** (so a PR that intentionally spans several tickets is visible to the
  human), and the operator may override with an explicit `--issue` to pick a
  different ticket. v1 does **not** concatenate multiple tickets into one intent.
  The PR body is attached as **secondary context only** (it never substitutes for
  the ticket-sourced problem statement). If no linked ticket ref is found, the run
  does **not** silently fall back to the PR body as intent:
  intent is still required (FR-2.3), so the run fails closed unless the user
  supplies an explicit `--issue` / `--intent` / `-m` / `$EDITOR` intent or opts
  into a diff-only review with `--code-only`. When intent is supplied, the PR body
  may still be attached as secondary context.
- **FR-4.4** Fixes land locally on the checked-out head branch; the run never
  pushes. For a fork PR (`isCrossRepository` with no push access), the run still
  completes locally and the summary states that push-back is the human's action
  (and may not be possible without maintainer-edit).
- **FR-4.5** **PR checkout contract** (the order and semantics of pulling a PR
  down, so implementations behave identically):
  1. **Clean-tree preflight first.** The entry-contract clean-worktree check
     (FR-9.2) runs **before** `gh pr checkout`, against the current worktree; a
     dirty tree fails closed with no checkout performed. (Checkout must never run
     against a dirty tree, since that would risk clobbering uncommitted work.)
  2. **Checkout.** The run invokes `gh pr checkout <N>`, which creates or fast-
     forwards a local branch tracking the PR's head and leaves HEAD **on a named
     branch** (never detached). The local branch name is the one `gh` selects
     (the PR head ref name for same-repo PRs; `gh`'s fork-disambiguated name,
     e.g. `<author>/<headRef>`, for cross-repo PRs).
  3. **Existing local branch.** If a local branch of that name already exists,
     `gh pr checkout` updates it only when the update is a fast-forward; if the
     local branch has diverged from the PR head (would require a non-fast-forward
     update or a force), the run **fails closed** with guidance rather than
     rewriting the user's local branch.
  4. **Fork / remote-only PRs.** A cross-repo PR is checked out into the local
     branch above for read+local-fix; push-back is not attempted (FR-4.4).
  5. **Failure / detached result halts.** If `gh pr checkout` fails, or for any
     reason leaves HEAD detached, the run fails closed (consistent with the
     detached-HEAD guard, FR-5.2) **before** any agent is spawned.

  **Acceptance:** `gauntlet review --pr <N>` against a same-repo PR whose body says
  "Fixes ENG-1234" produces an `intent.md` sourced from ENG-1234, reviews the PR's
  full diff against its base, and lands any accepted fixes as local commits on the
  head branch with nothing pushed; a PR whose body contains no resolvable ticket
  ref and is run with no explicit intent flag and no `--code-only` exits non-zero
  with guidance and spawns no agent (it does not fall back to the PR body as
  intent); a fork PR completes locally with a summary noting push-back is manual.

### FR-5: Base resolution and the empty-diff guard

- **FR-5.1** The diff base resolves in order: `--base <ref>` > a **concrete**
  `config.base_branch` > the remote default branch (`origin/HEAD`). The
  `base_branch: current` sentinel is **not** used as a review base (it would
  resolve to the branch under review).
- **FR-5.2** **The review diff range is normatively `git diff <base>...HEAD`**
  (three-dot): the change is diffed from the **merge-base** of the resolved base
  and HEAD, not from the base tip. This is the only defined range; implementations
  must not use the two-dot `git diff <base>..HEAD`. Three-dot is chosen so the
  reviewer always sees exactly the commits the target branch introduced since it
  diverged from base, and the scope is correct for rebased, diverged, and PR
  branches regardless of whether base has advanced. The base need not be a strict
  ancestor of HEAD; it must merely share a merge-base with HEAD (the diff is taken
  from that merge-base).
- **FR-5.3** **Empty-diff and degenerate-base guard.** The run **fails closed**
  with an actionable message before spawning any agent when:
  - the three-dot diff `git diff <base>...HEAD` is **empty** (HEAD introduces no
    changes relative to the merge-base — e.g. the resolved base is the branch
    under review, or HEAD has been merged into / is identical to base): message
    "base resolves to the branch under review or has no changes to review;
    nothing to diff — pass `--base <ref>`";
  - the resolved base and HEAD share **no merge-base** (unrelated histories):
    message naming the base and that it shares no history with HEAD;
  - HEAD is **detached** (no branch to land `REVIEW.x` commits on).

  **Acceptance:** With `config.base_branch: current`, `gauntlet review` on the
  current branch resolves the base to the remote default branch and reviews the
  real diff; if that still yields an empty diff, the run exits non-zero with the
  guard message rather than running a no-op review; `--base <ref>` overrides
  resolution.

### FR-6: Tracker-agnostic issue providers (v1: Linear)

- **FR-6.1** An `IssueTracker` provider interface (`parse_ref`, `fetch`,
  `extract_refs`, `verify_auth`) returns a normalized `Issue` (§6). Providers
  register via the `gauntlet.issue_trackers` entry point; the active one is
  selected by `config.issue_tracker.provider`.
- **FR-6.2** v1 registers exactly one provider, `linear`. Any other `provider:`
  value is a fail-closed config-load error naming the supported set.
- **FR-6.3** The Linear provider resolves human keys (`ENG-1234`) and
  `linear.app/.../issue/<KEY>` URLs to an `Issue` via the Linear GraphQL API,
  authenticating with a personal API key read by name from
  `config.issue_tracker.api_key_env` (default `LINEAR_API_KEY`).
- **FR-6.4** Tracker failures map to the taxonomy and **halt** the review (never
  proceed with no/partial problem statement): missing/invalid token →
  `IssueTrackerAuthError`; unresolved ref → `IssueNotFound`; network/5xx/timeout →
  `IssueTrackerUnavailable`. Every tracker call (both a run's `fetch` and the
  `doctor` `verify_auth` probe) is wrapped in a **per-call timeout of
  `config.issue_tracker.timeout_s`, default 10 seconds** (minimum 1; a non-positive
  value is a config-load error). Exceeding it raises `IssueTrackerUnavailable`,
  which fails the run/probe closed. The same timeout applies to `doctor` and to a
  run fetch — `doctor` differs only in that it calls the cheap `verify_auth` probe
  (FR-10.1) rather than fetching a ticket body.

  **Acceptance:** A simulated tracker call that blocks past `timeout_s` raises
  `IssueTrackerUnavailable` and fails the run closed before any review agent is
  spawned; setting `issue_tracker.timeout_s: 0` (or negative) is rejected at config
  load.
- **FR-6.5** The `issue_tracker` config block is optional; absent (or
  `provider: none`), `--issue` is rejected with guidance while `--intent` / `-m` /
  `$EDITOR` still work.

  **Acceptance:** `--issue ENG-1234` against a configured, authenticated Linear
  workspace fetches that ticket; an unset `LINEAR_API_KEY` (or unreachable API)
  fails the run closed with a typed, actionable message and spawns no review agent;
  `provider: jira` fails at config load naming `linear` as the only supported
  value; with no `issue_tracker` block, `--intent ./bug.md` still runs.

### FR-7: Judge policy for review runs

- **FR-7.1** A review run carries a `step_id` while the cycle is active, so the
  existing FR-9.8 rule continues to deny in-step `git push` / `gh pr create` —
  fixes land locally, the operator pushes. No new latitude for the harness.
- **FR-7.2** The orchestrator-level tracker fetch (an outbound HTTPS call to the
  tracker API made by Gauntlet's Python process, not by a hooked agent CLI) is
  **not** gated by the PreToolUse hooks; the Linear API host is reached directly
  by the orchestrator. Any agent-initiated network remains judged as today.
- **FR-7.3** `gh pr view` / `gh pr checkout` / `git fetch` performed by the run are
  reads (no remote mutation) and must be allowed; a proposed `policy.yaml` rule
  covers them explicitly rather than relying on the LLM fallback. Per §0, the rule
  is *proposed* through the policy change process, not silently committed (Open
  Question 11.4). **This rule is a hard prerequisite gate for P4** (the only phase
  that issues these commands): P4 must not begin until the rule's exact form is
  ratified through the policy-change process. If P4 is reached with the rule
  unratified, the run **halts and escalates** rather than proceeding (fail-closed,
  process fidelity); it never widens the boundary by relying on the LLM fallback
  in the interim. P1–P3 do not depend on this rule and are unaffected.
- **FR-7.4** **Machine-checkable P4 preflight (deterministic, not probe-based).**
  The PR-mode entry path runs a preflight **before** issuing any `gh pr view` /
  `gh pr checkout` / `git fetch`, and the preflight does **not** work by attempting
  the command and observing the judge's response (that would be the
  "try-it-and-see" anti-pattern the fail-closed principle forbids). Concretely:
  - The proposed allow rule (FR-7.3) carries a stable identifier and version,
    `pr_read_commands@v1`, in `policy.yaml`.
  - The review command's PR-mode preflight loads the active `policy.yaml`, looks
    up the `pr_read_commands` rule, and verifies it is present, marked ratified,
    and at the version the build expects. This is a deterministic config read with
    no network and no agent call.
  - If the rule is **absent, unratified, or version-mismatched**, the run fails
    closed before any `gh`/`git fetch` command with the exact message:
    `"P4 (PR mode) requires policy rule 'pr_read_commands@v1' to be ratified in
    policy.yaml; it is <absent|unratified|version <found> != v1>. Ratify it through
    the policy-change process (Open Question 11.4) before using --pr."`
  - Branch-mode reviews (no `--pr`) issue none of these commands and skip the
    preflight entirely.

  **Acceptance:** With a `policy.yaml` fixture **missing** `pr_read_commands@v1`, a
  `gauntlet review --pr <N>` invocation exits non-zero with the exact preflight
  message and issues **no** `gh`/`git fetch` command and spawns no agent; with the
  ratified-rule fixture present, the same invocation proceeds to checkout; a
  branch-mode review with the rule absent is unaffected.

  **Acceptance (FR-7.1–FR-7.3):** A review run's attempt to `git push` from within
  the cycle is denied with the FR-9.8 rationale; `gh pr checkout`/`git fetch`
  succeed once `pr_read_commands@v1` is ratified; the Linear token never appears in
  any judge-audit or transcript artifact (§7 redaction).

### FR-8: Out-of-repo, uncommitted review artifacts

- **FR-8.1** A review run's state (manifest, `intent.md`, `findings.json`,
  transcripts, summary) defaults to an out-of-repo state dir keyed by repo + a
  review slug, so the review itself produces **zero** working-tree footprint —
  not even an untracked or gitignored file in the repo. The only exception is a
  pre-existing user-supplied `--intent` file that already lives inside the repo:
  that file is the user's own untracked dirt (not created by the review) and is
  excluded from this guarantee per FR-2.4/FR-8.2. The default location, the
  `<repo-id>` derivation, slug sanitization, collision handling, and the
  `review.state_dir` override precedence are **normatively specified** in §6
  ("Review state path"); the default root is
  `${XDG_STATE_HOME:-~/.local/state}/gauntlet/reviews/<repo-id>/<slug>/`.
- **FR-8.2** The only repo mutations a review run makes are the `REVIEW.x`
  accepted-fix commits on the target branch (and none if nothing is accepted). No
  `PR.md` or other artifact is committed. A user-supplied `--intent` file inside
  the repo is excluded from this guarantee's checks and staging per FR-2.4 — it is
  neither committed nor counted as worktree dirt.
- **FR-8.3** A config override (`review.state_dir`) opts into an in-repo,
  gitignored layout for teams that want the review audit trail committable; the
  default is out-of-repo.
- **FR-8.4** The out-of-repo state is stable across invocations so a parked review
  is resumable; the review slug derives deterministically from the target (branch
  name, or `pr-<N>` for PR mode), keyed under the deterministic `<repo-id>` per §6
  ("Review state path"). Two invocations against the same repo + target resolve to
  the identical state dir.

  **Acceptance:** After a completed `gauntlet review` that accepts fixes,
  `git status` shows a clean tree and `git log` shows only `REVIEW.x` commits — no
  `intent.md`, `findings.json`, or run dir anywhere in the repo (tracked, ignored,
  or untracked); the manifest exists under the out-of-repo state dir and a parked
  run resumes from it.

### FR-9: Resumability and lifecycle reuse

- **FR-9.1** Review runs reuse the manifest write-ahead checkpoint and
  `resume`/`status`/`abort` commands; a `kill -9` mid-cycle resumes from the last
  checkpoint per FR-8.2 of the base spec.
- **FR-9.2** A review run requires a clean worktree at entry (the target branch's
  committed state is the handoff, FR-9.3); a dirty worktree fails the entry
  contract with guidance. In `--pr` mode this clean-worktree check runs **before**
  `gh pr checkout` (FR-4.5), so the checkout never runs against a dirty tree.
- **FR-9.3** If another non-terminal run already owns the target branch, the
  review fails closed rather than launching competing agents against one worktree
  (reuses the active-run refusal).

  **Acceptance:** A review killed mid-cycle resumes and completes without
  re-running already-checkpointed work; starting a review on a branch with
  uncommitted changes exits non-zero with guidance; starting a review while
  another active run owns that branch is refused.

### FR-10: `doctor` and `init` support

- **FR-10.1** `gauntlet doctor` validates the review feature when an
  `issue_tracker` block is present: provider is supported, the named auth env var
  is set, and a cheap auth probe (e.g. Linear `viewer { id }`) succeeds — failing
  with actionable, fail-closed messages.
- **FR-10.2** `gauntlet init` scaffolds a commented-out `issue_tracker` block with
  the Linear example and the env-var name, and ships `pipelines/review.yaml` and
  the review prompt as part of the standard asset set.

  **Acceptance:** With a configured tracker and a missing/invalid key, `gauntlet
  doctor` exits non-zero naming the env var and the fix; a fresh `gauntlet init`
  produces a repo whose `pipelines/review.yaml` loads and validates and whose
  config carries the commented `issue_tracker` example.

---

## §6 Data & Schemas (normative excerpts)

**`issue_tracker` config block** (in `config.yaml`; optional):

```yaml
issue_tracker:
  provider: linear              # v1: only "linear" accepted; github | jira reserved
  api_key_env: LINEAR_API_KEY   # NAME of the env var holding the token — never the token (FR-1.4 base spec)
  timeout_s: 10                 # per-call tracker timeout (FR-6.4); default 10; min 1
  # workspace: acme             # optional, provider-specific (URL building/validation)

review:                         # optional; controls review-run state location
  state_dir: null               # null => out-of-repo XDG default (FR-8.1, §6 "review state path"); set to e.g. .gauntlet/runs for in-repo
```

**`IssueRef` / `Issue` (normalized provider payloads):**

```python
@dataclass(frozen=True)
class IssueRef:
    provider: str   # "linear"
    raw: str        # "ENG-1234" or a pasted URL
    key: str        # normalized human key, "ENG-1234"

@dataclass(frozen=True)
class Issue:
    identifier: str        # "ENG-1234"
    title: str
    description: str       # ticket body (markdown)
    url: str               # canonical link, recorded in the run log
    state: str | None      # "In Progress" / "Done" — lets review flag a closed ticket
    # labels/assignee/comments reserved for later providers; not required in v1
```

**`intent.md` layout** (rendered by `render_intent`, tracker-agnostic):

```markdown
# Intent — <identifier or "(manual)"> · <title>
<source: linear ENG-1234 · https://linear.app/...>   # omitted for -m/file/editor
<provenance: tracker · independent>                  # always emitted; e.g. "author-session-summary · non-independent"

## Problem
<ticket description / supplied text>

## Repro / symptom
<only emitted when the provider supplies a discrete repro field; omitted in v1>

## Expected / acceptance
<only emitted when the provider supplies a discrete expected field; omitted in v1>
```

**`render_intent` is deterministic (v1):**

- It **always** emits the `# Intent` header, a `<provenance: …>` line carrying the
  resolved `provenance` value and the `independent`/`non-independent` flag
  (FR-2.1a/FR-2.6), and a `## Problem` section. (For a `tracker` source the
  `<source: …>` line is also emitted; it is omitted for manual sources.)
- `## Problem` contains the source text **verbatim** — the normalized
  `Issue.description` for a tracker source, or the `--intent` file / `-m` string /
  `$EDITOR` text otherwise. v1 performs **no** heading detection, regex
  extraction, or section-splitting of free-text descriptions: the full body is
  placed under `## Problem`.
- The `## Repro / symptom` and `## Expected / acceptance` sections are emitted
  **only** when the normalized `Issue` carries them as **discrete structured
  fields** from the provider. The v1 Linear provider exposes only a single
  description body and populates **no** discrete repro/expected fields, so in v1
  these two sections are **omitted entirely** (any acceptance/repro detail lives
  inside `## Problem`). The fields are reserved for future providers that expose
  them. The reviewer evaluates stated acceptance/expected behavior (FR-2.2) from
  the `## Problem` body when no discrete sections are present.

**`review.yaml` shape (illustrative):**

```yaml
name: review
version: 1
stages:
  - id: review
    steps:
      # optional baseline test step; off by default, included only when --test is
      # passed (which requires config.test_command to be set). --no-test is the
      # explicit-off form. See FR-1.1 / resolved Open Question 11.5.
      # - {id: baseline-tests, type: shell, run: "{{config.test_command}}", timeout_s: 1800}
      - {id: review-cycle, type: adversarial_cycle, mode: code_review,
         phase: REVIEW, reviewer: reviewer, triager: triage, fixer: builder,
         escalation_agent: escalation, max_rounds: 1,
         review_prompt: prompts/review-code-intent.md}
         # review_base is injected by the review command from FR-5 resolution
```

**Review run manifest — intent provenance block (new; FR-2.6):** the review run's
manifest records the resolved intent metadata so provenance and ratification are
auditable:

```jsonc
"intent": {
  "source": "issue | intent-file | message | editor | code-only",
  "provenance": "tracker | tracker-session | author-session-summary | none",
  "independent": true,                 // derived: tracker => true, else false
  "ratification": {                    // present only when independent == false
    "method": "interactive-confirm | approved-intent-flag",
    "user": "john.pletka@gmail.com",
    "timestamp": "2026-06-30T20:01:26Z"
  }
}
```

For `--code-only` runs `provenance` is `none` and the block carries no
`ratification` (there is no intent to ratify).

**Review state path (resolves Open Question 11.3; FR-8.1/FR-8.4):** the default
out-of-repo state root is
`${XDG_STATE_HOME:-~/.local/state}/gauntlet/reviews/<repo-id>/<slug>/`, where:
- `<repo-id>` derives **deterministically** from the repo's `origin` remote URL,
  normalized (scheme/credentials/trailing-`.git` stripped, lowercased) and SHA-256
  hashed, taking the first 12 hex chars; if no `origin` remote exists, the repo's
  absolute toplevel path (`git rev-parse --show-toplevel`) is normalized and hashed
  the same way. This keeps the key stable across invocations and independent of the
  checkout location for remote-backed repos, while still being defined for
  local-only repos.
- `<slug>` is the target branch name for branch mode, or `pr-<N>` for `--pr` mode
  (FR-8.4), each sanitized to `[A-Za-z0-9._-]` (every other character replaced with
  `-`, collapsed runs, trimmed) so a branch like `feature/x` maps to `feature-x`.
- Two distinct targets that sanitize to the same `<slug>` are disambiguated by
  appending a short hash of the unsanitized name, so collisions cannot silently
  merge two runs' state.
- **Override precedence:** `review.state_dir` (config, FR-8.3), when set, replaces
  the entire out-of-repo root with the configured (in-repo, gitignored) location;
  `<repo-id>/<slug>` layout under it is unchanged. The default (null) uses the XDG
  path above.

**Reused unchanged:** `findings.json`, `triage.json`, `confirm.json`, and the
`manifest.json` shape (§7 of the base spec). A "fix does not resolve the problem"
finding uses the existing `correctness` / `spec-gap` category — no schema change.

---

## §7 Security & Privacy

- **Secrets.** The tracker token lives in env/keychain, referenced by name via
  `api_key_env`; it never appears in repo config (FR-1.4 base spec). The
  transcript/audit redaction list (FR-4.4 base spec) is extended to cover the
  resolved tracker token value so it cannot be written to any artifact.
- **Fail-closed defaults.** Tracker auth failure, unresolved ref, or
  network/timeout halts the review (FR-6.4) — the run never proceeds with a
  missing or partial problem statement, since that silently degrades a correctness
  review to a diff-only review. Empty-diff and detached-HEAD fail closed (FR-5.2).
  Missing intent without `--code-only` fails closed (FR-2.3).
- **Push boundary.** Review runs carry a `step_id`, so the existing judge rule
  denies in-step `git push` / `gh pr create` (FR-9.8). The harness's autonomy is
  not widened; fixes are local and the operator pushes.
- **Network.** The only new outbound network is the orchestrator's direct HTTPS
  call to the tracker API; it is made by Gauntlet's process, not a hooked agent,
  and is not routed through the PreToolUse judge. Agent-initiated network is
  judged exactly as today. `gh`/`git fetch` reads are allowed via a proposed
  policy rule (FR-7.3), not the LLM fallback.
- **Prompt-injection containment.** `intent.md` is third-party text (a ticket
  body authored by whoever filed the bug). It is reference material for the
  reviewer, but must be treated with the same wariness as other untrusted inputs;
  it must not be able to instruct the triager or smuggle an escalation. It is
  presented to the reviewer as the problem statement but wrapped/labeled as data
  where it flows into the triager path, consistent with §8 of the base spec.

---

## §8 Implementation Plan (phased, assumption-validating)

Each phase ends in passing tests and a commit. Ordered to kill the riskiest
assumptions first. No phase depends on a later phase.

| Phase | Deliverable | Assumption it validates |
|-------|-------------|-------------------------|
| **P1** | `IssueTracker` abstraction (`base.py` payloads + errors, `intent.py` renderer, registry) + `LinearIssueTracker` + `IssueTrackerConfig` + `doctor` tracker check | A Linear ref (`ENG-1234`/URL) can be authenticated and resolved to a usable, normalized problem statement, failing closed on auth/not-found/unavailable. (Riskiest *external* dependency — an unowned third-party API and its auth.) |
| **P2** | Review run lifecycle **plus the minimal `gauntlet review` CLI entrypoint that owns argument parsing and intent/base resolution** (it parses the FR-1.1 flags, runs the entry contract, resolves intent + base + state dir, and then **stops short of executing the `adversarial_cycle`** — cycle wiring is P3). Delivered here: review entry contract (clean tree, ahead-of-base, intent-or-`--code-only`); **full intent-source resolution and precedence (FR-2.1): `--intent <file>`, `-m <text>`, `$EDITOR` template, `--code-only`, and the precedence ordering over the P1 `--issue` tracker source — with `render_intent` snapshotting to the out-of-repo `intent.md` and the FR-2.4 in-repo `--intent` exclusion**; **intent provenance tagging + `--intent-provenance` + the FR-2.5 pre-run ratification hook (interactive confirm / `--approved-intent`) + the FR-2.6 manifest provenance/ratification record**; in-place branch adoption (no minting); base resolution + empty-diff guard (FR-5); out-of-repo state dir + path/repo-id/slug derivation (FR-8, §6 "Review state path"). Because the CLI entrypoint exists in P2, its acceptance tests are **command-level** (`gauntlet review …`), covering each intent source (`--issue`, `--intent`, `-m`, `$EDITOR` fallback), the precedence order, provenance defaulting + the non-interactive ratification gate (`--approved-intent`), the missing-intent fail-closed (no `--code-only`) path, `--code-only`, and the `base_branch: current` empty-diff guard — all reachable without the cycle running (the command resolves and validates, then exits at the documented pre-cycle boundary). | A review run can parse its inputs at the CLI, operate on an existing branch with **zero** repo footprint, resolve a provenance-tagged problem statement from any of the four intent sources under the defined precedence (failing closed when none and no `--code-only`, and requiring ratification for non-independent intent), and resolve a correct, non-empty diff base — including the `base_branch: current` degenerate case. |
| **P3** | `pipelines/review.yaml` + `review-code-intent.md` + wiring the P2 `gauntlet review` entrypoint through to **executing** the `adversarial_cycle` in `code_review` mode with injected `review_base`, `intent.md`, and provenance (FR-2.2); zero gates; escalation park + FR-3.4 terminal severity rules preserved | **The core 1.3 assumption:** the cycle alone, on a small diff + a (possibly author-derived, ratified) intent, runs end-to-end, lands `REVIEW.x` fixes, and surfaces real correctness/spec-gap findings against the intent — at acceptable cost/latency. |
| **P4** | GitHub PR mode (`--pr`): `gh` resolution + checkout (FR-4.5), base-from-PR, PR-body linked-ticket auto-derive incl. multi-ref handling (FR-4.3). **Prerequisite gate:** the machine-checkable FR-7.4 preflight verifies `pr_read_commands@v1` is ratified in `policy.yaml` (FR-7.3, Open Question 11.4) **before** any `gh`/`git fetch`; if absent/unratified/version-mismatched, the run halts and escalates with the FR-7.4 message. | A PR can be pulled down, reviewed against its base and linked ticket, and have fixes landed locally for a human to push — including the fork (no-push) case. |

Ordering rationale: P1 attacks the external-API risk in isolation (it is testable
without any review machinery). P2 establishes the no-footprint, in-place lifecycle
the review depends on **and the minimal CLI entrypoint that exposes it**, including
all manual intent sources, provenance + ratification, and precedence (FR-2.1/2.5/
2.6); its acceptance tests are therefore command-level without needing the cycle.
P3 needs P1 (tracker intent) + P2 (lifecycle + CLI + manual intent) and adds only
cycle execution, proving the product thesis. P4 is a convenience surface over
P1–P3 (and gated by FR-7.4) and so comes last.

---

## §9 Success Metrics

- **Adoption / recovery of review:** on a sample of small fixes that would
  otherwise have shipped via plain Claude Code, ≥ 50% are run through
  `gauntlet review` within one month of availability for at least one team.
- **Correctness value:** across review runs with an intent, ≥ 30% surface at least
  one `correctness` or `spec-gap` finding triaged `legitimate` — i.e. the
  intent-aware review catches solution problems a diff-only pass would not target.
  (Threshold is a first calibration; revisit after the first 20 runs.)
- **Footprint guarantee (hard):** 100% of completed review runs leave the repo
  working tree clean — ignoring any pre-existing user-supplied in-repo `--intent`
  file, which is excluded per FR-2.4/FR-8.2 — with only `REVIEW.x` commits added.
  Zero *review-generated* artifacts are tracked, ignored, or untracked in the
  repo (automated check in the acceptance suite, not a sampled metric).
- **Cost/latency:** a default single-round review of a small fix completes in a
  fraction of a heavyweight phase — target median review-run cost ≤ 10% of a
  median heavyweight run, with full cost attribution per run (reuses FR-3.2).
- **Safety preserved:** 0 review runs that pass with an unresolved blocking
  finding (every such case parks).

---

## §10 Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Linear API/auth semantics or rate limits break ref resolution | P1 isolates and tests the provider against the real API; fail-closed taxonomy (FR-6.4) halts rather than degrades; `doctor` probe catches auth before a run. |
| Silent degradation to diff-only review when intent is missing/unfetchable | Intent required unless `--code-only`; tracker failure halts the run (FR-2.3, FR-6.4). |
| Single round (`max_rounds: 1`) misses issues a second round would catch | `--rounds` override; the escalation park still fires on unresolved blockers; measure finding/round data (reuses FR-6.6) to tune the default. |
| In-place fix commits mutate the author's branch unexpectedly | Clean-tree entry contract + `REVIEW.x` author identity (FR-9.7 base spec) make every change attributable and revertible; the human pushes, so nothing leaves the machine without review. |
| `intent.md` (untrusted ticket text) attempts prompt injection | Treated as data in the triager path, wrapped/labeled per §8 base spec; judge decisions never derive from agent/ticket text alone. |
| Review artifacts leak into the repo despite intent | Out-of-repo default (FR-8.1) + a hard automated footprint check in acceptance (§9) rather than a sampled metric. |
| `base_branch: current` produces a no-op review | Explicit base-resolution order excluding the `current` sentinel + empty-diff fail-closed guard (FR-5). |

---

## §11 Open Questions

1. **Autonomous "implement from a ticket" variant.** Should a later version add a
   `review`-shaped flow that *implements* the fix from the ticket (a leading
   `agent_task` before the cycle)? Out of scope for v1; recorded so the CLI/pipeline
   shape leaves room. *(Deferred; does not block v1.)*
2. **Tier-2 write-back: post findings as a PR review.** Should `gauntlet review`
   optionally post findings as inline PR review comments (`gh pr review`)? This is
   an external write inside a step and would need a deliberate judge-policy
   amendment + findings→comment mapping. Out of scope for v1. *(Deferred.)*
3. **Exact out-of-repo state path.** *(Resolved — see §6 "Review state path" and
   FR-8.1/FR-8.4.)* Default root
   `${XDG_STATE_HOME:-~/.local/state}/gauntlet/reviews/<repo-id>/<slug>/`;
   `<repo-id>` is the first 12 hex of the SHA-256 of the normalized `origin` remote
   URL (or the normalized repo toplevel path when there is no `origin`); `<slug>`
   is the branch name (or `pr-<N>`) sanitized to `[A-Za-z0-9._-]`, with collisions
   disambiguated by a short hash of the unsanitized name; `review.state_dir`
   overrides the root.
4. **Judge-policy rule for `gh`/`git fetch` reads.** The FR-7.3 allow rule for
   `gh pr view`/`gh pr checkout`/`git fetch` is a `policy.yaml` change, which per §0
   goes through the policy-change/ratification process, not this PRD. Confirm the
   rule's exact form and that it is acceptable to add. *(Must resolve before P4.)*
5. **Should `gauntlet review` run the test suite first?** *(Resolved — see FR-1.1
   and §6.)* `review.yaml` has an optional `baseline-tests` step gated on
   `config.test_command`. **Decision: off by default**, `--test` enables it
   (requires `config.test_command`), `--no-test` is the explicit-off form. On
   gives the correctness reviewer a pass/fail baseline and catches "no regression
   test"; off keeps a quick review quick — quick-by-default wins for a lightweight
   flow, with opt-in when a baseline is wanted.
6. **Closed-ticket handling.** When the fetched `Issue.state` is already "Done"/
   closed, should the review warn, proceed, or refuse? *(Judgment call; strawman:
   warn and proceed.)*
7. **Multiple linked tickets in a PR body.** *(Resolved — see FR-4.3.)* v1 takes
   the **first resolved** ref in textual order as the intent, warns when more than
   one ref is present, and lists all detected refs in the run summary as ignored
   secondary refs (override with explicit `--issue`); v1 does not concatenate
   multiple tickets.
8. **`/gauntlet-review` Claude Code skill scope.** *(Resolved — follow-on, not v1;
   see Non-Goals.)* The skill (synthesize intent from the active session / vague
   ticket, present for in-session human review, then invoke `gauntlet review -m
   "<approved>" --intent-provenance … --approved-intent` and hand off to
   `/gauntlet-operator`) is out of scope for v1. v1's obligation is only that the
   CLI exposes what the skill needs: non-interactive `-m`, `--intent-provenance`,
   `--approved-intent`, and manifest provenance/ratification recording (FR-1.1,
   FR-2.5, FR-2.6). Building the skill is deferred.

---

*End of PRD Draft v0.2*

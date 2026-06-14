# CLAUDE.md — Gauntlet

This file is read by every Claude Code session in this repository, whether
you are **building Gauntlet**, **running as the `builder` agent inside a
Gauntlet pipeline**, or **acting as the `reviewer` agent**. Read it fully
before doing anything. The section headers tell you which parts apply to you.

---

## 1. What this project is (always read)

Gauntlet is an adversarial multi-agent development harness. It orchestrates
a pipeline of: human-authored PRD → adversarial review loop → implementation
plan → adversarial review loop → phased implementation, where each phase ends
in a commit and goes through an adversarial review loop before the next phase
begins. The spec is `PRD-gauntlet.md`. When in doubt, the PRD wins.

**The central invariant:** the worktree is clean and committed at every point
where control passes to a reviewer. This is not a style preference — it is
what makes review diffs meaningful, mutation detection possible, and confirm
passes cheap.

---

## 2. Guiding principles (always read)

These are the values the project will measure implementation against. When a
decision isn't covered by the PRD, use these to reason toward an answer.

**Determinism over cleverness.** The orchestrator is a state machine. Prefer
boring, explicit, resumable logic over elegant abstractions that are hard to
inspect mid-run. A run that survives `kill -9` and resumes correctly is worth
more than clean code that can't.

**Fail closed.** Every external call — judge service, agent CLI, API adapter —
defaults to deny/halt on timeout, parse error, or unexpected exit code. A
stuck run is recoverable. A run that silently continues past a failed safety
gate is not.

**Separation of concerns between agents.** The builder implements. The
reviewer reviews. Neither should be asked to do the other's job in the same
step. When you are the builder, you do not pre-emptively review your own
work in the same turn; that's the reviewer's job and it will happen.

**Data over inference.** Persist everything — manifests, transcripts, triage
verdicts, judge decisions, agent identities on commits. Future you, debugging
a failed run, should never have to infer what happened.

**Process fidelity is part of the deliverable.** When you are building
Gauntlet, the quality of the process you follow (commit discipline, review
handoff, triage rigor) is as observable as the code quality. The bootstrap
is a dogfood run; treat it that way.

**Approved artifacts change only through their own loop and gate.** Do not
amend an artifact that a human has approved (PRD, plan) because a later phase
found it incomplete. Halt and surface the conflict. Humans ratify; agents
propose.

**Nothing lands on `main` directly.** Every change — code, docs, prompts,
config — goes onto a dedicated feature branch (a git worktree is fine) and
reaches `main` only through a pull request. Never commit, push, or
fast-forward directly to `main`. No exceptions for "trivial" one-line edits;
the PR is the audit boundary, not a formality.

---

## 3. When you are building Gauntlet (bootstrap / development work)

### Branch discipline
All work goes on a feature branch — never commit, push, or fast-forward
directly to `main` (or any other base branch). A git worktree off such a
branch is fine.

Branch names carry a type prefix so history is machine-classifiable:
`<type>/<slug>`, where `<type>` is one of `fix`, `feat`, `sec`, `perf`,
`docs`, `chore`, `test` (e.g. `fix/no-direct-main`, `feat/judge-policy`).
Automated `gauntlet run` pipelines keep their spec'd `gauntlet/<slug>`
branches (FR-9.1); the type-prefix convention governs manual development on
this repo.

Every branch lands through a pull request: open a PR against the base branch,
let review/CI run, and merge through the PR. `main` only ever advances by
merging a PR — never by a direct push. This holds for every change, however
small.

### Phase discipline
The implementation plan (`runs/gauntlet/plan.md`) defines phases. Work one
phase at a time. At the end of each phase:

1. All tests pass (`uv run pytest` — failing tests are a hard stop).
2. Commit with the enforced format:
   - Line 1: `PN: <imperative summary, ≤72 chars>`
   - Blank line
   - Body: what changed, why, which PRD assumption this phase validates,
     relevant FR refs, any explicit deferrals to later phases.
3. Surface the commit SHA and invite the review handoff. Do not continue.

Fix commits use: `PN.x: Address review — <short summary>` with a body that
lists each addressed finding by ID, the triage verdict, and what changed.
Declined findings appear in the body as explicitly declined with the triage
reasoning — declining with a recorded reason is part of the audit trail.

### Tests are the guardrail
The test suite only grows. Never delete or skip a passing test to make a
phase pass. If a test is wrong, fix the test in a separate commit with
justification.

Integration tests that require live CLI credentials are marked
`@pytest.mark.integration`. CI runs `pytest -m "not integration"`. You run
the integration suite locally before every review handoff.

### What to decide vs. what to ask
**Decide yourself:** module layout, library choices within the constraints in
`BOOTSTRAP-PROMPT.md`, prompt wording, schema field names consistent with
PRD §7, test structure.

**Stop and ask:** plan deviations, anything requiring credentials or global
machine state changes, FR conflicts, anything that would amend an already-
approved artifact, the gate after every phase.

### Safety rules for your own session
- Never use `--dangerously-skip-permissions` or equivalent bypass flags.
  This disables the PreToolUse hooks. The hooks are the safety layer.
- Never force-push or rewrite history on any branch other than the active
  PRD branch (and only with explicit human instruction there).
- Never read credential files outside this repository tree.
- After P2, the judge service hooks your own session. Do not attempt to
  work around a deny decision; surface it and ask.

### The self-hosting switchover
After P4 (pipeline engine + adversarial cycle + logger): switch to running
subsequent phases through `gauntlet run`. Manual process execution from P5
onward is a bug. Record any gap that forced you to fall back to manual in
`BOOTSTRAP-NOTES.md`.

---

## 4. When you are the `builder` agent inside a Gauntlet pipeline

You receive a phase prompt referencing the approved PRD and the approved
implementation plan. Your job in that phase is defined by the plan. Scope
is everything.

### Your scope
- Implement exactly what the current phase specifies, per the plan.
- Write or extend tests to cover the phase's deliverables. Tests pass before
  you signal completion.
- Do not implement work belonging to a later phase, even if it seems easy or
  obviously needed. Record the temptation as a deferral note in the commit
  body; do not act on it.

### Signaling completion
When the phase is done and tests pass, signal completion clearly:
```
PHASE COMPLETE
Phase: <PN — title>
SHA: <commit sha>
Tests: <N passed, 0 failed>
Deferrals: <list any scope items pushed to later phases>
```
Do not perform the review. Do not pre-critique your own work. The reviewer
will do that.

### If you discover a plan or PRD conflict
Stop. Do not proceed. Report:
```
UPSTREAM CONFLICT
Phase: <PN>
Conflict: <what the plan/PRD says vs. what implementation reveals>
Options: <what you see as the paths forward>
```
The human will resolve it. This is FR-10.4; it is not optional.

---

## 5. When you are the `reviewer` agent inside a Gauntlet pipeline

You are an adversarial reviewer. Your job is to find problems, not to be
polite. The builder's feelings are not a consideration; shipping broken or
incomplete work is.

### Review stance
Be skeptical of everything. The builder had context you don't. Use that
asymmetry: if something is unclear to you as a reader, it is a finding,
regardless of whether the author's intent was clear.

### What you are reviewing against
Always review against three references, in priority order:
1. The approved `prd.md` — is the spec fully implemented?
2. The approved `plan.md`, current phase — did the phase deliver what it said?
3. The guiding principles in §2 of this file — were they followed?

Findings that don't trace to one of these three are likely bikeshedding.
Label them honestly as such in your `severity` field.

### Output format
Return findings as structured JSON matching `schemas/findings.json`. Every
finding must have: `id`, `severity` (blocking/major/minor/nit), `category`,
`location` (file and line/section), `claim`, `evidence`. Optional:
`suggested_fix`.

Do not editorialize outside the JSON. The triage agent reads your output
programmatically.

### On the confirm pass
You receive: the commit-range diff (pre-fix SHA to post-fix SHA) and your
prior findings with triage verdicts. For each finding, return:
```
{ "finding_id": "F-001", "verdict": "resolved | partially_resolved |
  unresolved | regression_introduced", "notes": "..." }
```
You are checking whether the diff addressed your concern — not re-reviewing
the whole phase. Scope yourself to the diff.

### Read-only contract
You do not modify files. You do not run commands that have side effects. If
your adapter configuration allows write tools and you are tempted to use them:
write a finding instead, with a suggested_fix. Any worktree mutation by a
reviewer is a process violation and will be detected.

---

## 6. Stack and project layout (all agents)

```
src/gauntlet/
  cli.py          # typer CLI entrypoint
  engine/         # pipeline state machine, step types, manifest
  adapters/       # ClaudeCodeAdapter, CodexAdapter, ApiAdapter (LiteLLM)
  judge/          # FastAPI judge service, policy engine
  logging/        # transcript logger (md + jsonl)
  config.py       # pydantic config/schema models
pipelines/        # YAML pipeline definitions
prompts/          # versioned prompt templates (data, not code)
schemas/          # JSON schemas for structured agent outputs
policy.yaml       # judge fast-path allow/deny rules
runs/             # per-PRD run artifacts (committable)
tests/
  unit/
  integration/    # requires live CLI creds; pytest -m integration
BOOTSTRAP-NOTES.md  # process pain points recorded during bootstrap
PRD-gauntlet.md     # the spec
```

**Key dependencies:** Python 3.10+, `uv`, `typer`, `pydantic`, `fastapi`,
`uvicorn`, `litellm`. No heavier orchestration frameworks. The orchestrator
is thin by design.

**Run tests:** `uv run pytest` (unit only) / `uv run pytest -m integration`

**Install locally:** `uv tool install -e .` or `pipx install -e .`

**Check environment:** `gauntlet doctor`

---

## 7. Commit message format (reference)

```
PN: Imperative summary of what this phase delivers (≤72 chars)

What changed and why. This is not a restatement of the diff; it is the
reasoning behind the change. Include:
- Which PRD assumption this phase validates
- Relevant FR numbers (e.g. "implements FR-3.3, FR-7.2")
- Any explicit deferrals: "Deferred to P4: retry logic on ApiAdapter"

For fix commits (PN.x):
- F-001 [legitimate]: <what was wrong> → <what changed>
- F-002 [bikeshedding/declined]: <reviewer's concern> — declined because
  <triage reasoning>
- F-003 [premature_optimization/declined]: <reviewer's concern> — deferred
  to post-v1, tracked in FUTURE.md
```

---

## 8. Files you must not modify without explicit human instruction

- `PRD-gauntlet.md` — the spec; changes require a new PRD revision process
- Any file in `runs/*/` that represents an approved artifact (`prd.md`,
  `plan.md`, and manifest entries marked `status: approved`)
- `policy.yaml` — judge rules; changes go through the retro proposal process
- `CHANGELOG.md` in `prompts/` — append only, never rewrite history

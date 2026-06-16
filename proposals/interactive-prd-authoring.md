# Proposal: Interactive PRD authoring (`gauntlet new --interactive`)

> **Status:** Draft proposal for human ratification. Proposes a human-driven,
> agent-assisted path to author a run's `prd.md` in place, plus a pluggable
> input-source mechanism (Linear ticket, GitHub issue, local doc). No engine
> code changes until this proposal is ratified — implementation must not precede
> the spec landing (CLAUDE.md §2, "Approved artifacts change only through their
> own loop and gate").

## 1. Motivation

Today the entry path is: `gauntlet new <slug>` writes a stub `prd.md`
(`run.py:135`, `_PRD_STUB`), the human authors a real PRD into it by hand, then
`gauntlet run <slug>` begins at the adversarial PRD review. The stub is a bare
skeleton (`# PRD: <title>` + empty `## Problem statement` / `## Requirements`).

Two friction points fall out of that:

- **Cold start.** A human staring at an empty stub has no scaffolding toward a
  *well-structured* PRD (goals/non-goals, exit criteria, invariants, risks) —
  the very structure the document reviewer (`prompts/review-document.md`) grades
  against. Authors end up drafting the PRD in some other tool (a chat session, a
  doc) and then hand-copying it into the right spot — observed directly: a
  "plan to author a PRD" authored in a separate session, targeting the wrong
  path (`docs/…`), needing manual relocation into `.gauntlet/runs/<slug>/`.
- **Existing context is unused.** PRDs frequently originate from a Linear ticket
  or GitHub issue. Nothing today pulls that context into the authoring step.

## 2. What this is — and explicitly is not

This proposal sits deliberately on the **assist** side of FR-10.1 ("Gauntlet
does not author PRDs"). The distinction is load-bearing:

- **IS:** a human-driven session where an agent *interviews* the human, surfaces
  assumptions, and drafts into `prd.md` under the human's direction. The human
  remains author of record and ratifies at the `prd-approve` gate.
- **IS NOT:** Gauntlet autonomously generating a spec that a human rubber-stamps.

### Why the invariant survives

FR-10.1 protects one property: **the artifact entering adversarial review must
carry human intent that exists independently of any agent.** The PRD is the root
of the validation chain — every later gate reviews *against* it — so if an agent
authored it, the review would grade the agent's work against itself, collapsing
the asymmetry that makes review meaningful (CLAUDE.md §2, §5).

An interactive, human-driven session preserves that property, and the existing
safety gates remain fully intact:

1. The resulting `prd.md` still must pass `check_entry_contract` (`run.py:143`) —
   it cannot be the stub or the marker-stripped stub.
2. It still goes through the full PRD adversarial cycle + `prd-approve` human
   gate before planning begins.

### The one thing review cannot catch — and how the design answers it

The document reviewer checks internal consistency, completeness, and clarity. It
has **no upstream to check intent against** (the PRD *is* the root). So review
cannot catch an *invented* requirement the human never wanted. The only backstop
for invented intent is genuine human engagement during authoring.

**Design consequence:** the seed prompt must **interview, not autogenerate**. It
asks clarifying questions, states assumptions explicitly, and refuses to invent
scope. A prompt that says "write a complete PRD for X" would satisfy the letter
of FR-10.1 while violating its spirit. The prompt is where this feature is won
or lost (see Appendix A).

## 3. Proposed FR delta

Adjacent to FR-10.1, add (numbering TBD at ratification):

> **FR-10.1a — Assisted authoring.** Gauntlet MAY provide a human-driven,
> interactive path to author a run's `prd.md` in place. The agent acts as a
> drafting assistant under human direction; the human remains the author of
> record. The entry contract (FR-10.1) is unchanged: the resulting `prd.md` must
> still be a non-stub, human-ratified document, and it is still subject to the
> full PRD adversarial cycle and human gate. Gauntlet MUST NOT author a PRD
> without an interactive human in the loop.

## 4. Design

### 4.1 Keep `gauntlet new` pure

`gauntlet new` is currently a deterministic `mkdir` + write-stub with no
credential or CLI dependency — a virtue (trivially testable, can't hang, fails
closed by simply existing). Interactivity must not regress that.

Two acceptable shapes (decide at ratification):

- **(a) Flag:** `gauntlet new <slug> --interactive` calls the pure scaffold
  first, *then* launches the seeded session. The default (no flag) path is
  byte-for-byte unchanged.
- **(b) Sibling command:** `gauntlet draft <slug>` does scaffold-then-session,
  leaving `new` entirely untouched.

Recommendation: **(a)**, because it keeps a single discoverable entry verb while
the non-interactive code path stays exactly as boring as it is today. The
interactive branch lives behind the flag and the agent-adapter dependency is
only loaded when the flag is set.

### 4.2 Pluggable input sources (`--from`)

Rather than a bespoke flag per origin (`--linear`, `--github-issue`, …), model
the origin as one source argument resolved by a small registry of fetchers:

```
gauntlet new <slug> --interactive --from linear:VAN-123
gauntlet new <slug> --interactive --from github:owner/repo#42
gauntlet new <slug> --interactive --from ./notes.md
gauntlet new <slug> --interactive                      # no source; pure interview
```

Each source is a context-fetcher that returns text injected into the seed
prompt:

| Source scheme | Fetcher | Notes |
|---|---|---|
| `linear:<KEY>` | Linear API / MCP | ticket title + body + comments |
| `github:<owner>/<repo>#<n>` | `gh` CLI / API | issue title + body + thread |
| `<path>` (bare) | filesystem read | an existing doc/notes file |

The interactive session is **source-agnostic**: a source only seeds context; the
interview still drives, because a ticket never specifies non-goals, exit
criteria, or invariants. Fail closed — an unresolvable source aborts before any
session starts (consistent with "fail closed", CLAUDE.md §2).

### 4.3 Seed prompt as versioned data

The seed prompt is a versioned template under `prompts/` (which the layout
treats as "data, not code", CLAUDE.md §6) — proposed
`prompts/prd-author-interactive.md`, registered in `prompts/CHANGELOG.md`. It
encodes the interview-not-generate stance. Draft in Appendix A.

### 4.4 Session launch + safety

The session launches through the existing `ClaudeCodeAdapter` surface. It
benefits from — and should reuse — the judge wiring (interactive mode, repo-root
boundary) already used for one-off sessions, so a drafting session is gated like
any other.

## 5. Risks & open questions

- **Rubber-stamping.** The chief risk is a human accepting an AI draft without
  engagement. Mitigations: interview-style prompt (Appendix A); the `prd-approve`
  human gate; the document review cycle. Residual risk is inherent to any
  assistance and is accepted, given the gates.
- **`new` purity.** Must verify the non-interactive path is unchanged and the
  agent adapter is imported lazily only under `--interactive` (test: `new`
  without the flag has no adapter/CLI dependency).
- **Source auth.** Linear/GitHub fetchers need credentials. Out of scope for the
  pure path; must fail closed and never block the no-source interview.
- **Where does the interview transcript live?** Option: persist it alongside the
  run (e.g. `prd-authoring.transcript.md`) for the "data over inference" audit
  trail (CLAUDE.md §2). Open.
- **Shape (a) vs (b)** — flag vs sibling command. Recommended (a); open.

## 6. Process

This adds an FR adjacent to FR-10.1 and touches the entry-contract narrative, so
it is a spec change, not a casual add. It should be ratified (FR delta written
into the PRD revision) before any engine work. This doc is the proposal artifact;
a FUTURE.md pointer tracks it until picked up.

---

## Appendix A — draft seed prompt (`prompts/prd-author-interactive.md`)

> Draft only — graduates to `prompts/` on ratification. The stance, not the
> wording, is the point.

```
You are helping a HUMAN author a Product Requirements Document (PRD) for a
Gauntlet run. You are a drafting assistant, not the author. The human owns the
intent; your job is to extract it, structure it, and write it down — never to
invent it.

Hard rules:
- INTERVIEW, do not autogenerate. Ask clarifying questions before writing.
  When the human's intent is ambiguous or missing, ASK — do not fill the gap
  with a plausible guess.
- State every assumption explicitly and get confirmation before it enters the
  PRD.
- Refuse to invent scope, requirements, goals, or non-goals the human did not
  endorse. "I don't know yet" is a valid PRD state; record it as an open
  question rather than fabricating an answer.
- A PRD specifies WHAT and WHY, not HOW. Do not write an implementation plan —
  Gauntlet authors the plan in a later, separate stage.

Target structure (adapt to the work):
- Problem statement
- Goals / Non-goals
- Requirements (numbered)
- Invariants the change must preserve
- Exit criteria
- Risks & open questions

{{#if source_context}}
Seed context was provided from {{source_label}}. Treat it as raw input, NOT as
the PRD. A ticket/issue/doc rarely specifies non-goals, invariants, or exit
criteria — interview the human to fill those gaps.

--- BEGIN SEED CONTEXT ---
{{source_context}}
--- END SEED CONTEXT ---
{{/if}}

When the human confirms the PRD is complete, write it to:
  {{prd_path}}
Then stop. Do not run `gauntlet run`. Do not review your own work — the
adversarial PRD review and the human gate happen next, by design.
```

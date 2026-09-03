# Prompt & policy changelog

Append-only record of improvement proposals (FR-6.3) that a human approved and
`gauntlet proposals review` applied to the versioned assets (`prompts/`,
`pipelines/`, `schemas/`, `policy.yaml`). Each entry is the proposal's rationale
and the asset it touched; the literal diff lives in the run's
`retro/proposals/NNN-<slug>.md`. This file is **append-only** — never rewrite
history (CLAUDE.md §8). New entries are added at the bottom by the governed
apply, so the file reads oldest-first.

<!-- gauntlet:changelog -->

- **implement-phase.md / schemas/resume-disposition.json** (gauntlet-resume-response P5,
  FR-3/FR-5/FR-10): add the `## Human decision` handling section encoding the
  FR-3.0 classification precedence (artifact-contradiction → `amendment_required`
  even when asked to "proceed despite"; ambiguous → `new_conflict`;
  fully-consistent → `proceed_in_place`/`proceed_with_deviation`; tie →
  fail-closed toward the gate), the FR-3(b) halt-and-regate path, the requirement
  to list consumed `response_id`(s) in `responses_considered`, and the FR-6 note
  that conflicts do not consume the retry budget. Adds the `resume-disposition`
  schema as the structured test oracle, bound invocation-locally on a `--response`
  resume so the approved pipeline definition is not mutated (FR-4.1).

- **proposal-synthesis.md** (recovery redesign P1 review fix, FR-6.3): require
  complete numeric unified-diff hunk headers and exact asset context. Proposal
  materialization now canonicalizes only the redundant hunk-count arithmetic
  before the existing deterministic `git apply --check` gate, while preserving
  the raw model response and rejecting incomplete structure fail-closed.

- **operator.md** (#98, FR-8.1/FR-8.2): §3 reject bullet now documents that the
  gate→cycle re-drive covers gates inside a `foreach` phase group (the standard
  pipeline's `phase-gate` → same-iteration `impl-cycle`), and that a genuinely
  terminal reject (no upstream cycle to iterate) is refused unless the explicit
  `--terminal` flag is given — a flag-less reject can no longer end a run by
  surprise.

- **commit-message.md** (#134): add the hard rules that the 72-char header
  limit is counted INCLUDING the `PN: ` prefix (models violated the limit
  roughly every phase, each violation costing a redraft or a park), that the
  body must stay free of the diff, and how to draft when the engine hands an
  oversize diff BY REFERENCE (`diff --stat` inline, per-file diffs read with
  git) instead of inlining it.
- **plan-author.md** (#146 review): limit preconditions to read-only path and
  environment checks; require provisioning separately before approval.

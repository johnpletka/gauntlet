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

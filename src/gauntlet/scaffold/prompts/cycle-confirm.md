# Confirm pass — diff-scoped (FR-9.5)

You are the reviewer doing a confirm pass on your own prior findings. Below
you get exactly three things: the commit-range diff of the fix round, your
prior findings, and the triage verdicts on them. Scope yourself to the diff —
you are checking whether THE DIFF addressed each concern, not re-reviewing
the whole phase.

For EVERY prior finding, return a verdict:
- `resolved` — the diff fully addresses the claim.
- `partially_resolved` — the diff helps but a material part remains; say what.
- `unresolved` — the diff does not address it (note: findings triage declined
  with a recorded reason and no code change are *expected* to be unresolved —
  judge them `unresolved` with a note acknowledging the recorded decline).
- `regression_introduced` — the diff breaks something, including something
  previously fine; say what.

## Enumerated obligations (FR-6.2)

When a prior finding named several discrete obligations, check them item by item,
not on the headline. If the diff covers some but leaves ANY enumerated item
uncovered, the verdict is `partially_resolved` and you MUST name the uncovered
item(s) in `notes`. Do not mark such a finding `resolved` because its headline
was addressed.

## Carry the concrete remainder of a partial (FR-6.1)

For every finding you mark `partially_resolved`, add a `new_findings` entry that
names the SPECIFIC unresolved remainder, with `carried_from` set to that
finding's id. Its `location`/`claim`/`evidence` describe the remainder itself
(the uncovered item), NOT the whole parent. This gives the next round a concrete,
actionable target instead of a vague "still not done". Set the remainder's
`severity` by what it guards:
- `blocking` — a privacy/security leakage boundary, or a golden/parity oracle
  guarding a behavior-changing refactor;
- `major` — anything else.

## Intra-document consistency (FR-6.4, artifact mode)

When you are confirming a fix to a **document** (a PRD or plan), the fix must not
leave the document contradicting itself. If a section the fix corrected now
disagrees with another section it did not touch (strategy vs. deliverable,
requirement vs. open questions), the verdict is `partially_resolved`/`unresolved`
and you MUST cite BOTH conflicting sections in `notes` — a fix that resolves one
half of a contradiction and leaves the other standing is not resolved.

## Output

Defects the diff itself introduces go in `new_findings` too (with
`carried_from: null`). Each `new_findings` entry is a COMPLETE finding object —
`id`, `severity`, `category`, `location`, `claim`, `evidence`, `suggested_fix`
(null if none), and `carried_from` (a finding id for a carried remainder, else
null). For a carried remainder use any placeholder `id`; the engine assigns the
real reserved-namespace id. Return ONLY JSON conforming to the provided schema;
`notes` is 1–2 sentences per verdict.

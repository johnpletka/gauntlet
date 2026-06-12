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

Defects the diff itself introduces go in `new_findings`. Return ONLY JSON
conforming to the provided schema; `notes` is 1–2 sentences per verdict.

# Draft a phase commit message

Draft a git commit message for the change shown below (status + diff,
including untracked files). Enforced format (CLAUDE.md §7, FR-9.2):

- Line 1: `PN: <imperative summary>` — the required phase prefix is given
  below; at most 72 characters total.
- Line 2: blank.
- Body: the reasoning, not a restatement of the diff — what changed and why,
  which plan/PRD assumption this phase validates, relevant FR references
  (e.g. "implements FR-3.3, FR-7.2"), and any explicit deferrals
  ("Deferred to P6: …").

Return ONLY the commit message text — no code fences, no commentary.

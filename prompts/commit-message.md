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

Hard rules (a violation is rejected and costs a redraft):

- Count the header's characters INCLUDING the `PN: ` prefix — the prefix, the
  colon and the space all count toward the 72. Count before you answer; if
  the line is over, shorten the summary, never the prefix.
- Keep the body free of the diff: no hunks, no file-by-file listings, no
  pasted code. Describe the change; the diff itself is already recorded in
  git.
- When the diff is handed to you BY REFERENCE (a `diff --stat` change map
  plus instructions to read per-file diffs with git), read what you need
  from the repository and draft for the entire change — never for the stat
  alone.

Return ONLY the commit message text — no code fences, no commentary.

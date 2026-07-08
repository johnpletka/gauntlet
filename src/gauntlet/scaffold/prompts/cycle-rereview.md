# Re-review a fix round (regression-scoped)

You are re-reviewing a **fix round**, not performing a fresh review. In round 1
you (or another reviewer) raised findings; the builder fixed the accepted ones;
the findings still open are listed below. Your job here is deliberately narrow.

**Do:**
- For each carried finding, decide whether the fixes actually addressed it.
  Re-state a carried finding (reuse its `id`) only if it is genuinely still
  unaddressed, with fresh evidence of what remains.
- Raise a NEW finding **only** if the fixes introduced a `blocking` regression
  — something that worked before this round and is now broken.

**Do NOT:**
- Hunt for fresh `minor` or `major` issues. That review happened in round 1.
  Re-opening settled ground, or surfacing new nitpicks now, is bikeshedding and
  prevents the loop from converging (BOOTSTRAP-NOTES #30). The human gate is
  where any residual non-blocking concerns are weighed — not another round.
- Re-litigate findings that triage already declined with a recorded reason.
- Re-state a **carried remainder** — a carried entry whose `carried_from`
  field names a parent finding. A remainder is a pre-accepted fix obligation
  for THIS round (FR-6.1): its fix happens *after* this review, so it is
  unaddressed by construction, and it already inherits its parent's accepted
  verdict and bypasses triage. It is listed only so you can scope your
  regression check around it. (The engine drops any restatement of a
  remainder's id.)

Return findings as JSON conforming to the schema (same shape as a normal
review). If everything carried is now addressed and no blocking regression was
introduced, return an empty `findings` array — that is the signal the loop has
converged. Questions that are not defect claims go in `open_questions`. No prose
outside the JSON.

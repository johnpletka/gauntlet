# Implement one phase

You are the `builder` agent inside a Gauntlet pipeline (CLAUDE.md §4). Implement
**exactly the current phase** — identified by the `foreach item` below (its
`id`, `title`, and `goal`) — against the approved PRD and plan provided as
inputs. Scope is everything.

## Your scope

- Implement what this phase specifies in the plan, and nothing belonging to a
  later phase, even if it looks easy or obviously needed. Record any such
  temptation as a deferral note (it belongs in the commit body, which a later
  step drafts) — do not act on it.
- Write or extend tests covering this phase's deliverables. The suite only
  grows; never delete or skip a passing test to make the phase pass. If a test
  is genuinely wrong, fix it deliberately, not by deletion.
- Run the tests and get everything green before you finish. Failing tests are a
  hard stop.
- Do **not** commit — the pipeline's commit step handles that. Do **not** review
  your own work — the reviewer step handles that.

## If the plan or PRD is wrong

If implementing this phase reveals that the approved plan or PRD is wrong or
under-specified (FR-10.4), **stop and report it** rather than silently working
around an approved artifact. End your final message with a clearly marked
`UPSTREAM CONFLICT` block stating what the plan/PRD says, what implementation
reveals, and the paths forward. A human resolves it; you do not amend approved
artifacts yourself.

Otherwise, when the phase is implemented and tests pass, say so plainly.

## Human decision (only when a `human-response.md` artifact is present)

If this run was resumed with `gauntlet resume <slug> --response "<decision>"`,
you will receive a `--- input artifact: human-response.md ---` block holding the
full chronological history of human decisions on this parked conflict. You are
**re-evaluating** the conflict in light of the latest decision — not
re-surfacing the unchanged conflict, and not re-doing the whole phase.

You **must** emit a single JSON object conforming to the bound
`resume-disposition` schema as your final output (the harness validates it). Its
fields: `disposition`, `responses_considered` (the `response_id`(s) you
consumed, e.g. `implement-resp-1`), `action_summary`, and `conflict` (required
only when you re-park).

Classify the **latest** response by applying these tests **in order** and
stopping at the first that matches (FR-3.0 — this is a deterministic rule, not a
judgment call; when torn between two, pick the **earlier** one and fail toward
the gate):

1. **Does it contradict, ask to change, or proceed *despite* an approved
   artifact?** If the response requests any divergence from the approved PRD or
   plan text — **including** "proceed even though this contradicts the plan" —
   classify as `amendment_required`. There is **no** proceed-now-amend-later
   path. Do **not** implement anything. Fill `conflict` and say, in
   `action_summary` / `conflict.summary`: this change must go through that
   artifact's own review-and-gate cycle (FR-10.4) — revise the PRD/plan on its
   own branch, gate it, then resume with a response that no longer requires an
   artifact change. Name the artifact in `conflict.artifact` (e.g. `plan FR-4`).
2. **Is it ambiguous, or does it not unambiguously resolve the conflict?**
   Classify as `new_conflict`. Fill `conflict.requested_input` with what the
   supplied response did **not** provide (what you still need to proceed) — not a
   verbatim repeat of the prior park.
3. **Is it fully consistent with every approved artifact?** Only then may you
   proceed: `proceed_in_place` (the conflict is resolved through understanding;
   implement and let the commit step run) or `proceed_with_deviation` (the
   response defers/selects among options the artifacts already permit; record the
   deviation, e.g. a FUTURE.md entry, and proceed). Omit `conflict` (or set it
   null).

Conflicts do **not** consume the retry budget; only genuine failures do. You can
be asked for multiple `--response` cycles on the same step without hitting the
retry limit — so prefer an honest `new_conflict` over forcing a resolution.

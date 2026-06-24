# Dogfood runbook — un-sticking `prd-authoring-aids` with `gauntlet resume --response`

**Status:** one-time, human-gated validation. **Not** a pytest, **not** part of
the `uv run pytest` gate (PRD §8). This file is a procedure; its evidence lands
beside it under `runs/prd-authoring-aids/`.

## Why this is a runbook, not a test

The repeatable CI gate for resume-with-response is the synthetic, disposable
end-to-end test (`tests/unit/test_resume_response_e2e.py`): deterministic,
no creds, safe before every handoff. This dogfood, by contrast, mutates **one
real parked run** and depends on live CLI credentials, branch state, and the
operator's git/identity config. Those preconditions cannot be satisfied twice —
once the run proceeds past the conflict, the conflict park is gone. So it is run
**once**, by a human, and its outcome is captured as evidence rather than
re-asserted on every CI run (PRD §8).

## Preconditions

Before the single authorized invocation, confirm all of the following:

1. The `prd-authoring-aids` run exists and is **parked on an upstream conflict**:
   ```sh
   gauntlet status prd-authoring-aids
   ```
   The parked step is an `agent_task` whose manifest record shows
   `parked_reason == "upstream_conflict"`.
2. Operator identity resolves (FR-9) — the audit `user` must not be blank:
   ```sh
   echo "${GAUNTLET_USER_EMAIL:-$(git config user.email)}"
   ```
   If empty, set `GAUNTLET_USER_EMAIL` or `git config user.email` first; a blank
   identity fails closed and records nothing.
3. Live CLI credentials are authenticated (`gauntlet doctor` is green) — the
   resumed builder really re-runs.
4. The decision text is ready. If resolving the conflict would require changing
   the approved PRD or plan, **stop**: amend that artifact through its own
   review-and-gate cycle first (FR-10.4), then resume with a decision that no
   longer contradicts an approved artifact. There is no proceed-now-amend-later
   path.

## The single authorized invocation

```sh
gauntlet resume prd-authoring-aids --response "<the human decision, verbatim>"
```

The orchestrator appends the decision to the parked step's `human_responses`
array (`state="pending"`), commits that checkpoint, rebuilds `human-response.md`
from the manifest, re-runs the builder with it injected, then transitions the
entry to `state="consumed"` and commits again. The builder emits a structured
`resume-disposition`:

- `proceed_in_place` / `proceed_with_deviation` → the run un-sticks and proceeds
  to the review cycle.
- `amendment_required` → the decision contradicts an approved artifact; the step
  re-parks pointing at the FR-10.4 gate. Amend that artifact on its own branch,
  re-gate it, then resume again with a decision that no longer requires the
  change.
- `new_conflict` → the decision was ambiguous; the step re-parks naming what is
  still missing. Supply another `--response`. Conflicts do **not** consume the
  retry budget, so this can repeat without failing the run.

## Where the evidence lands

After the run un-sticks, capture (under `runs/prd-authoring-aids/`):

1. **Manifest audit trail** — the parked step's `human_responses` entry/entries
   showing `response_id`, `state="consumed"`, the operator `user`, the
   `timestamp`, and `response_attempt`. Confirm `StepRecord.attempts` did **not**
   advance for the conflict cycle(s) (FR-6).
2. **Commit SHA(s)** — the engine `gauntlet: response <id> pending` /
   `… consumed` checkpoint commits (authored by the Gauntlet Engine identity),
   plus the phase commit if the builder proceeded.
3. **Outcome** — `gauntlet status prd-authoring-aids` showing the run advanced
   past the conflict (proceeded to the review cycle), or re-parked with a
   `new_conflict` whose `requested_input` names what is still needed.

Record these in this directory (e.g. an `EVIDENCE.md` capturing the above)
once the one-time dogfood is performed.

# Failure-marker fixtures (harness-efficiency FR-3.1, §6)

Captured error envelopes pinned beside `.gauntlet/pins.yaml`, one per
allowlist entry in `src/gauntlet/adapters/failure_markers.py`. The contract
test (`tests/unit/test_failure_markers.py`) asserts every rule has a fixture
and every fixture classifies to its rule's `kind` — so a marker can only be
added alongside a fixture, and a CLI-version change that shifts an envelope
shape breaks the test loudly (BOOTSTRAP-NOTES #26 discipline).

Provenance of each fixture is recorded in the `real_capture` flag on its
`MarkerRule`:

- **`real_capture: true`** — harvested from a live failed-run transcript
  (`runs/*/run-*/steps/*/events*.jsonl`), truncated to the classifier's typed
  fields + a ≤500-char excerpt, passed through the redaction path. No raw
  transcript bytes, credentials, or free-form prose beyond the excerpt.
    - `claude/usage-limit.json` — gauntlet-ui run 2026-06-17 (session-limit halt).
    - `claude/overload.json` — operator-aids run 2026-06-25 (`API Error: Overloaded`).
    - `claude/terminal-connection-closed.json` — lightweight-issue-workflow run
      2026-06-30; a NEGATIVE fixture: a typed `is_error` envelope whose only
      signal is an unlisted message ⇒ `terminal` (fail-closed).
    - `codex/usage-limit.json` — gauntlet-ui run 2026-06-17 (`turn.failed`).
    - `api/timeout.json` — coaching-side-drawer run 2026-07-24 (right-quote
      repo, gauntlet 0.7.0; issue #63): the `litellm.Timeout` envelope that was
      mis-classified terminal in the r1-triage fan-out. Pinned
      `transient_dependency` by P5 (plan §5.2).
- **`real_capture: false`** — synthesized from the documented CLI/exception
  shape, pending a live capture. Fail-closed-safe: a real error that does not
  match still halts terminally. Re-pin with a live capture when observed.
    - `claude/usage-limit-subtype.json`, `claude/overload-subtype.json` —
      forward-compat `subtype` matches (2.1.190 reports `subtype:"success"`).
    - `codex/usage-limit-code.json`, `codex/overload-type.json` — structured
      `error.code`/`error.type` matches (0.139.0 carries only `error.message`).
    - `codex/overload.json` — codex overload message shape.
    - `api/rate-limit.json`, `api/overload.json` — LiteLLM exception descriptors.
    - `api/connection.json` — LiteLLM `APIConnectionError` descriptor
      (connection/DNS class, P5 plan §5.2).
    - `claude/dependency.json`, `codex/dependency.json` — synthesized
      transport-failure phrasings in the pinned message fields (P5 plan §5.2);
      NARROW by design — the real-captured `claude/terminal-connection-closed.json`
      ("Connection closed mid-response", a truncated partially-consumed
      response) stays pinned TERMINAL and must never match these.

> **Tracked coverage gap (F-002 / plan.md:33 — "one per adapter per kind").**
> Three required `(adapter, kind)` pairs — `codex`/overload,
> `api`/usage-limit (`rate-limit.json`), and `api`/overload — have **no** live
> capture: those adapter failure-modes were never exercised in a real failed
> run, so the harvestable transcripts hold no envelope to redact and pin. They
> are covered only by synthesized fixtures **as a knowing, tracked phase
> conflict**, not a met acceptance criterion. The contract is machine-enforced
> by `test_required_adapter_kinds_have_real_capture_or_a_tracked_gap` and
> `test_uncaptured_gap_set_is_exactly_the_pairs_missing_a_live_capture`
> (`tests/unit/test_failure_markers.py`): the pinned gap set makes a *new*
> synthesized-only pair fail loudly, and each listed pair must be re-pinned with
> a live capture — and removed from the set — when one is observed.

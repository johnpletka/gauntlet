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
- **`real_capture: false`** — synthesized from the documented CLI/exception
  shape, pending a live capture. Fail-closed-safe: a real error that does not
  match still halts terminally. Re-pin with a live capture when observed.
    - `claude/usage-limit-subtype.json`, `claude/overload-subtype.json` —
      forward-compat `subtype` matches (2.1.190 reports `subtype:"success"`).
    - `codex/usage-limit-code.json`, `codex/overload-type.json` — structured
      `error.code`/`error.type` matches (0.139.0 carries only `error.message`).
    - `codex/overload.json` — codex overload message shape.
    - `api/rate-limit.json`, `api/overload.json` — LiteLLM exception descriptors.

# PR draft — `gauntlet-ui`

> Drafted by Gauntlet at the final gate (FR-9.8). **Not opened, not pushed** — opening the PR and pushing remain human actions (PRD §2.2). Edit freely before use.

- branch: `gauntlet/gauntlet-ui` (base `main`)
- run: `run-2026-06-17T17-31-40` — status **done**
- pipeline: `standard` v1

## Summary

**PRD: Gauntlet Console — Supervisory Web UI for Runs** — **Status:** Draft v0.3 (v0.2 + addressed adversarial PRD-cycle review F-001…F-009: repo/worktree-global active-run lock; fully-specified `run --watch` console discovery/lifecycle; run-dir allocation handshake for the supervisor; finer event identity for de-dup; deterministic phase-diff commit selection; complete auth/CSRF flow; OS process-creation-time for PID-reuse safety; run_id selection across the API; `interrupted` named as an existing step status). v0.2 = v0.1 + resolved OQ-1…OQ-6: real engine-level active-run guard; clarified single-repo = all-slug/all-history browsing; `gauntlet run --watch` boots the console; confirm step for destructive verbs; in-tab notifications v1 / background push v2; keep note string-matching for v1 — see §11.

## Phases & commits

### PRD
- `604825b5ab` **PRD.1** (step `prd-cycle`)

### PLAN
- `d8960eac2f` **PLAN** (step `plan-cycle`)
- `6337c346b1` **PLAN.1** (step `plan-cycle`)

### P1
- `c5d344bfb2` **P1** (step `phase-commit`)
- `61cfc84675` **P1.1** (step `impl-cycle`)

### P2
- `5caa2765e8` **P2** (step `phase-commit`)
- `f23cf09270` **P2.1** (step `impl-cycle`)

### P3
- `07362294c1` **P3** (step `phase-commit`)
- `0f5604bb83` **P3.1** (step `impl-cycle`)

### P4
- `10ccc83ad3` **P4** (step `phase-commit`)
- `10f00b0400` **P4.1** (step `impl-cycle`)

### P5
- `a7c3bdbb25` **P5** (step `phase-commit`)
- `8a5d3f4217` **P5.1** (step `impl-cycle`)

### P6
- `05c5191794` **P6** (step `phase-commit`)
- `e38729f373` **P6.1** (step `impl-cycle`)

### P7
- `0fc3e1e800` **P7** (step `phase-commit`)
- `994bd54230` **P7.1** (step `impl-cycle`)

## Final per-finding verdicts (last confirm pass)

- `F-001`: **resolved** — The diff now mints a token in `ensure_console()` when neither a caller token nor environment token is available, passes it to the child via `GAUNTLET_WEB_TOKEN`, and returns it in the handle. The added test covers the default no-token path and verifies the registry fingerprint matches the returned token.
- `F-002`: **resolved** — The diff changes `serve()` to detect a reusable registered console, report its existing URL/login URL, and return before starting uvicorn. The added test asserts this reuse path does not attempt to bind a second server.
- `F-003`: **resolved** — The diff replaces the loopback-host-only check with full scheme/host/port origin tuple comparison against `request.base_url`, and missing or unparseable origins now fail closed. Tests were updated to require a same-origin `Origin` header and to reject missing, wrong-port, wrong-host, and wrong-scheme origins.

## Transcripts

Full review→triage→fix→confirm record: [`run-2026-06-17T17-31-40/RUN.md`](run-2026-06-17T17-31-40/RUN.md).

_Plan: see `plan.md` in this directory._

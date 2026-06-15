# FUTURE.md — deferred work, surfaced at gates

Items the adversarial cycle confirmed as legitimate but only *partially* resolved,
then surfaced to the human at a phase gate (convergence policy A: a major finding
gets one fix, then the human decides rather than the cycle looping). Recorded here
so a partial fix accepted at a gate is tracked, not forgotten.

## From P6 (init / doctor / packaging) — accepted at p6-gate 2026-06-12

- **F-004 [major, partially_resolved] — `doctor` CLI-auth false positive.**
  `doctor` now runs real CLI auth probes and emits FAIL/WARN rows, so a logged-out
  CLI no longer silently passes. Residual: `_real_cli_authenticated` treats any
  exit code 0 as authenticated and does not inspect Claude's in-band `is_error`
  JSON field — a CLI that returns 0 while reporting an auth error in-band is a
  false-positive path. Follow-up: parse Claude's JSON `is_error`/error envelope in
  the auth probe rather than trusting the return code alone.

- **F-005 [major, partially_resolved] — Codex hook wiring only WARNs.**
  Claude hook validation is now structural (parses JSON, requires a `*` PreToolUse
  matcher, verifies the hook command + executable, fails malformed/unwired cases).
  Residual: Codex config still only WARNs for absent / malformed / unwired hook
  config, where the original finding asked required wiring to FAIL. Partly justified
  by the pinned-Codex inert-hook note, but the required-wiring aspect is not fully
  met. Follow-up: decide whether Codex hook wiring should be a hard FAIL once the
  Codex hook surface is no longer inert, and tighten the check accordingly.

## From #8 review (`.gauntlet/` asset_root consolidation) — deferred 2026-06-14

- **F-003 [major, deferred] — `init` does not migrate a pre-existing root-layout
  repo.** `gauntlet init` unconditionally scaffolds asset targets under
  `.gauntlet/`, but a repo init'd under the previous root layout keeps
  `asset_root: "."` (its committed config is skipped as idempotent). Plain
  `init` then creates duplicate, INACTIVE `.gauntlet/` assets alongside the
  active root ones, and `init --from-repo` reports the active root assets as
  MISSING. Low real-world impact pre-1.0 (no deployed adopters on the old
  layout), so deferred rather than blocking the consolidation PR (#8). Follow-up:
  load an existing config before selecting asset targets and honour its
  `asset_root`; treat a root→`.gauntlet` migration as an explicit, atomic
  operation with legacy-layout tests.

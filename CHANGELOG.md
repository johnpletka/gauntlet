# Changelog

All notable changes to Gauntlet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-06-19

A significant feature release. The headline is the **Gauntlet Console** — a
local-first web UI that makes every run visible, answerable, and recoverable —
alongside a hardened judge security posture, the run-branch lifecycle, and
smarter project setup. Everything is additive; existing CLI workflows are
unchanged.

### Added — Gauntlet Console (supervisory web UI)

`gauntlet serve` starts a loopback-only, token-authenticated console that runs
strictly *above* the orchestrator — every control action launches the same
sanctioned `gauntlet` CLI verb a human would type, so it inherits every existing
safety invariant rather than being able to weaken one.

- **Run list & detail.** Lists every run across all slugs (sortable, filterable,
  searchable) with live status, current step, and cost; per-run detail renders
  the full step tree, per-step status/duration/cost, and an owned/observed badge.
- **Live updates.** A ~1 s manifest poll emits edge-triggered transitions over
  SSE — no manual refresh — via a ~30-line vendored vanilla-JS shim (no HTMX, no
  build step, no new dependency).
- **Step drill-down.** Open any step's `prompt.md`, `transcript.md` (rendered
  markdown), and `events.jsonl`, including artifacts nested in round / sub-step
  dirs (cycle review/triage/confirm, retrospective builder/reviewer/synthesis,
  per-finding triage verdicts). Live log tailing for running steps.
- **Human-gate review.** When a run parks at a gate, the console assembles the
  decision's evidence — findings/triage as readable tables, rendered artifacts,
  and a deterministic phase diff — and offers **Approve / Reject** in one place.
- **Cycle-escalation reconciliation.** Parks *inside* an adversarial cycle
  (upstream-invalidation, open-blocker, max-rounds) are surfaced with their
  escalated findings, triage verdicts, and the named upstream artifact, framed
  as a reconcile-then-resume decision — previously invisible and un-notified.
- **Failure diagnosis & recovery.** A pure classifier maps each parked/halted/
  failed state to the action that actually applies — Resume where it helps, and
  an honest "resume won't fix this" with guidance (raise the timeout/budget,
  reconcile the artifact) where it won't.
- **Supervised runs.** Launch and abort runs as managed subprocess children of
  the CLI, with captured logs and crash survival: a server restart re-discovers
  owned runs and re-attaches to live PIDs (PID-reuse-safe), and an orphaned run
  is offered for resume exactly like a `kill -9`'d one.
- **Notifications.** Fire on the four moments that need a human — gate reached,
  escalation parked, run failed, run completed — to macOS desktop, Slack, and
  in-tab, edge-triggered and fail-soft (a notification error can never affect a
  run).
- **Durable auth & ergonomics.** A one-time `/login` token exchange sets an
  `HttpOnly; SameSite=Strict` session cookie with per-session CSRF on every
  state-changing POST; full run-history browsing per slug; cost report and
  judge-audit views; `gauntlet run --watch` boots/reuses a console for the run.
- **Read-only proposals view** for a run's retrospective improvement proposals
  (review/apply stays the `gauntlet proposals review` CLI verb).
- **Opt-in analysis hand-off** (`gauntlet serve --enable-handoff`): assembles a
  copy-pasteable, read-only prompt for a parked decision — the console itself
  makes no model call and spawns nothing.

### Added — engine & tooling

- **Run-branch lifecycle:** `base:current` resolution, a stale-branch guard,
  `gauntlet clean`, and `gauntlet finish` (merge a completed run via PR), with
  fail-closed resume / clean / base resolution.
- **Worktree-scoped active-run lock (FR-10.5):** a repo/worktree advisory lock
  fail-closes `start` / `resume` / `approve` across *all* slugs in a worktree,
  so two orchestrators can never drive one worktree. Parallel runs across
  *different* repos are unaffected.
- **`gauntlet init`** now detects the per-project test command.
- **Run-id handshake** (`gauntlet run --run-id`) lets a supervisor pre-allocate
  a run's id; the env-var form was dropped to avoid colliding with the judge's
  `GAUNTLET_RUN_ID`.

### Security

- **Context-aware push/PR policy.** The operator may `git push` and
  `gh pr create`/read; in-run agents are denied. Force-pushing and direct merges
  to `main`/`master` remain denied for everyone.
- **Judge gated on run context** (an active `RUN_ID`), not mere token presence,
  so an ambient token can't pull an unrelated session under judge control.
- Engine-managed judge **avoids port clashes** and reuses an existing judge
  rather than failing to bind.
- **Warns loudly** when the judge LLM classifier is disabled.
- Stale `triage.json` is cleared between rounds to prevent phantom escalations.

### Fixed

- Baseline-commit guard missed an artifact under the nested run layout.
- Numerous review-hardening fixes across the judge, resume/clean/base paths, and
  the console (path containment, fail-closed gate evidence, the active-run lock's
  unverifiable-identity handling, console registry startup race, and FR-5.3
  control gating).

### Notes

- **Dependencies:** `httpx` and `jinja2` promoted from transitive to explicit
  `pyproject.toml` dependencies; no new heavy runtime dependency.
- **Engine surface:** the console adds exactly one sanctioned engine change (the
  worktree active-run lock); everything else reads on-disk state or shells out to
  CLI verbs.

[0.2.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.2.0

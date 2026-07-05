# Changelog

All notable changes to Gauntlet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.0] — 2026-07-05

**Harness efficiency & resilience** (PRD `runs/harness-efficiency/prd.md`,
P1–P11; PR #52). The organizing principle: a run's scarce resource is the
builder's provider window, so this release makes interruptions survivable
instead of destructive, spends each step more deliberately, and makes an
in-flight run explainable from `status` alone. Built by a `gauntlet run`
dogfood on this repo — and battle-tested by it: the run survived a live codex
usage-limit hit, host sleep, and a malformed plan block using the machinery it
was building. All new knobs default to prior behavior.

### Added — resilience (protect the window)

- **Usage-limit parks with session-preserving resume (FR-3).** Adapters
  classify failures transient-vs-terminal against a pinned, fixture-backed
  marker allowlist (`adapters/failure_markers.py`); a quota/429/overload hit —
  including inside an adversarial cycle's reviewer/triager/fixer/confirmer
  sub-agents — parks the run (`parked_usage_limit`) with the worktree untouched
  and the agent session preserved. Plain `gauntlet resume` continues the same
  session with a short continuation prompt; an expired session falls back to a
  full re-run with an audit note. Unrecognized errors stay `terminal`
  (fail-closed). Opt-in `resume_on_quota: auto` self-resumes at the provider's
  hinted reset time with bounded, restart-safe attempts.
- **Suspend/sleep resilience (FR-5).** A driver heartbeat
  (`engine/heartbeat.py`) detects host suspension (dual detectors, pinned by a
  darwin contract test) and credits slept time back to the running step's
  deadline up to `suspend_credit_cap_s`; stalls classify as `host_suspended` /
  `driver_orphaned` / `agent_silent`. Opt-in `keep_awake: true` wraps the
  driver in `caffeinate -i` on darwin.
- **In-step artifact validation with self-repair (FR-2).** `agent_task`
  supports `validate:` (e.g. `plan_phases`): the authoring agent gets its own
  parse error back for a bounded repair loop; exhaustion parks
  (`parked_artifact_invalid`) for a sanctioned hand-edit — `resume` re-runs
  only the validator and records a content-hash audit of the edit.
- **Adversarial-cycle sub-step checkpointing (FR-4).** Review/triage/fix/
  confirm checkpoint write-ahead per round; a killed cycle re-enters at the
  first incomplete sub-step, guarded by the round handoff SHA and fail-closed
  reloading of corrupt fragments.
- **Intra-phase checkpoint commits (FR-11).** Builders commit `P<N> wip:`
  milestones (`checkpoint_commits: keep | squash`); the phase always terminates
  in a `PN:` commit (empty marker if needed) that is the review handoff, and
  reviewers always see the cumulative range diff. Interrupted-step recovery
  rewinds to the latest checkpoint instead of the phase base, bounding lost
  work to one milestone.

### Added — efficiency (sharpen the spend)

- **Scoped context input modes (FR-1).** Per-input `mode: inline | reference |
  phase` lets large artifacts travel by path (agents read them in-session,
  where turns hit the provider prompt cache) or as the current phase's plan
  excerpt; artifact-mode re-review rounds get a diff since the last reviewed
  snapshot instead of the full document. Gated on a probe-verified `reads_repo`
  capability; pipeline load and `doctor` both fail closed on a blind profile.
- **Effort tiering plumbing (FR-6).** Canonical `effort: minimal | low |
  medium | high` on any profile or step, mapped per adapter (unsupported value
  = config-load error); triage escalation is severity-gated (blocking/major
  only); mechanical emissions (commit messages, resume dispositions) run on a
  cheap `mechanic` profile; `doctor` probes every profile's model resolution
  and effort-flag acceptance.
- **Machine-global usage ledger + window admission (FR-10).**
  `~/.gauntlet/usage-ledger.jsonl` accumulates content-free per-step usage
  across runs (`gauntlet ledger backfill` seeds it from existing manifests,
  idempotently); a configured `providers.<name>` window warns — or with
  `enforce: true` parks pre-step (`parked_usage_window`) — before launching a
  step predicted not to fit. Advisory by design.
- **Concurrent triage + judge decision cache (FR-9, FR-12).** Per-finding
  triage calls run in a bounded pool (`triage_concurrency`, byte-identical
  `triage.json` on all-success rounds; deterministic checkpoint fragment on
  failure); the judge caches **allow** decisions per run keyed on canonical
  payload + policy content hash (deny/ask never cached), eliminating repeated
  LLM-rung evaluations of identical tool calls.

### Added — observability (explain the run)

- **Engine-stamped reason enums (FR-7).** Disjoint `halt_reason` (timeout /
  budget / judge_deny / signal_kill / adapter_error / precondition /
  operator_recover) and `parked_reason` (usage_limit / usage_window /
  artifact_invalid / response / gate) on every non-DONE step; legacy manifests
  normalize read-side (`upstream_conflict`/`cycle_escalation` → `response`),
  never rewritten.
- **Enriched `status --json` (additive; `schema_version` stays 1):** run
  elapsed, token/cost totals and per-profile `agent_usage`, per-step
  duration/notes/reasons, heartbeat age + suspension intervals, quota reset
  time, and a `gate` context block (convergence summary, prior responses,
  per-finding triage reasoning — FR-8). `gauntlet report` gains
  cache-effectiveness columns. Consumers pinning an older strict schema copy
  must re-pin on upgrade.

### Changed

- **Custom `halt_on` marker parks now stamp `parked_reason=response`** and
  receive the same clean-tree restoration as the canonical UPSTREAM CONFLICT
  marker (the dirty tree is snapshotted to a backup ref first — nothing is
  lost, but edits no longer remain in the worktree for in-place inspection).
- **Failed `commit` steps are `--response`-recoverable** (`commit` joined
  `RESPONDABLE_STEP_TYPES`), closing an operator deadlock where neither plain
  `resume` nor `--response` could advance a crashed commit step.
- **Code-review base spans the whole phase**: cycle review diffs cover
  `base_sha..PN:` (the cumulative phase range), not the final commit alone —
  required for multi-commit (`wip:`) phases and empty marker commits.

### Fixed (post-review hardening, from the PR-52 adversarial review)

- Codex NDJSON decode and `classify_codex_failure` now fail closed on
  valid-JSON-but-non-object event lines instead of crashing the classifier
  with `AttributeError`.
- `Retry-After: 0` is treated as a real "retry now" hint (reset time = now),
  not an absent one — auto-resume can fire immediately.
- The post-disposition session-expiry fallback now writes the same
  `session-expired` audit record as the quota-resume path (FR-3.3 parity).
- Ledger appends no longer re-parse the entire machine-global ledger per step:
  de-dup keys are cached per process and refreshed by scanning only new tail
  bytes (truncation/rotation triggers a full reload).
- Cycle-role usage is recorded in the ledger per-role (previously dropped
  ~92% of a cycle's tokens from admission estimates); `StepRecord` gains
  `agent_usage`.

### Docs

- Operator playbook (+ scaffold twin): the three new park states and their
  recovery verbs, the sanctioned hand-edit exception, suspend-aware stall
  classification. README: run-lifecycle resilience, the enriched status
  contract, `ledger backfill`, corrected canonical `effort` reference, and a
  full resilience/window/scoped-context configuration section. Scaffold
  `config.yaml`: commented opt-in reference block for every new knob.

## [0.4.0] — 2026-07-01

A new **lightweight review** surface. `gauntlet review` brings the adversarial
review cycle (review → triage → fix → confirm) to small changes — bug fixes,
one-off patches — **without** the PRD → plan → phase ceremony. The engineer fixes
the bug however they like (plain Claude Code, by hand) on a branch or PR;
`gauntlet review` reviews that change against its originating ticket, lands
accepted fixes as `REVIEW.x` commits **in place**, and stops — the resulting
branch/PR *is* the human review. It runs with zero routine human gates while
keeping the cycle's fail-closed escalation park as the safety stop. Everything is
additive; no existing CLI workflow changes, and no approved artifact
(`PRD-gauntlet.md`, `policy.yaml`, any `prd.md`/`plan.md`) is amended.

### Added — `gauntlet review`

- **New `gauntlet review [<branch>]` command.** Reviews an already-implemented
  change on a local branch (default: the current branch) against a three-dot diff
  from its base, reusing the trusted `adversarial_cycle` machinery verbatim — same
  reviewer, triage rubric, severity-aware escalation, reviewer-mutation guard, and
  audit-trail fix commits. Only the PRD/plan/phase ceremony is removed. (FR-1, FR-3)
- **Solution-correctness review against the originating ticket.** The reviewer is
  given both the diff **and** a provenance-tagged problem statement (`intent.md`,
  the lightweight analog of `prd.md`) and asked whether the change actually
  *resolves the stated problem* — not just whether the diff is internally sound.
  Intent comes, in precedence order, from `--issue <ref|url>` (tracker fetch),
  `--intent <path>`, `-m <text>`, or an `$EDITOR` template; `--code-only` skips
  intent entirely. (FR-2, G2)
- **Pluggable issue trackers; v1 ships Linear.** A new `gauntlet.issue_trackers`
  entry-point registry (mirroring the adapter registry) resolves a Linear ref
  (`ENG-1234` or a `linear.app/.../issue/<KEY>` URL) into a normalized problem
  statement via the Linear GraphQL API. Auth is by env-var name
  (`config.issue_tracker.api_key_env`, default `LINEAR_API_KEY`) — never a token
  in config — and every fetch fails closed on auth / not-found / unavailable /
  timeout with a typed, actionable error. GitHub Issues / Jira are a registry
  seam, not built code. (FR-6)
- **GitHub PR mode (`--pr <N|url>`).** Checks out a PR locally, resolves its base
  and linked ticket (auto-derived from the PR body in textual order; the first
  resolved ref supplies the intent, secondary refs are reported as ignored), and
  reviews the head branch against its base — including the fork / cross-repository
  case. Fixes land locally; the harness never pushes (FR-9.8 boundary unchanged).
  A non-fast-forward update to a diverged local branch is refused **before** the
  branch is touched, so a checkout failure never leaves a destructive partial
  state. (FR-4)
- **Zero Git-status footprint.** A review run adds nothing — tracked or untracked
  — to the target repo's `git status`, at every point including completion. Run
  state (intent, findings, manifest) lives out-of-repo by default under
  `${XDG_STATE_HOME:-~/.local/state}/gauntlet/reviews/`; an in-repo
  `review.state_dir` override is permitted only when git-ignored (verified via
  `git check-ignore`, else fail closed). A user-supplied in-repo `--intent` file is
  excluded from the entry contract and never swept into a fix commit. (FR-8, G3)
- **`--rounds`, `--test` / `--no-test` controls.** `--rounds` is validated at parse
  time to the closed range `[1, 10]` (default 1) as a deterministic runaway guard;
  `--test` runs the configured `test_command` as an optional baseline step.
- **Terminal-severity contract for gate-less runs.** An unresolved legitimate
  **blocking** finding parks the run (resumable via `resume --response`); an
  unresolved legitimate **non-blocking** finding (`major`/`minor`/`nit`) *completes*
  but is recorded in the run summary as **residual risk**; a not-legitimate finding
  is recorded with its triage reasoning. The summary is a pure, deterministic
  function of the cycle's persisted findings/triage/confirm records. (FR-3.4)

### Added — supporting surface

- **`gauntlet doctor` tracker health check.** When an `issue_tracker` block is
  configured, `doctor` validates the provider is supported, the named env var is
  set, and `verify_auth` succeeds — with fail-closed, actionable messages. (FR-10.1)
- **`gauntlet init` scaffolding.** Ships `pipelines/review.yaml` and the
  `review-code-intent.md` prompt in the standard asset set, and scaffolds a
  commented-out `issue_tracker` block (Linear example + env-var name) and the
  `gauntlet-cli` / `pr_read_commands` policy examples. (FR-10.2)
- **Deterministic PR-read policy preflight.** PR mode is gated by a machine-checkable
  read of `policy.yaml` that verifies the `pr_read_commands@v1` rule is present,
  ratified, and version-`v1` — no network, no agent — and halts with the exact
  FR-7.4 message on absent / unratified / version-mismatch. `PolicyRule` now carries
  `id` / `version` / `ratified` governance markers. Branch-mode reviews skip the
  preflight entirely. The rule is *proposed*, never silently applied; ratification
  is the human gate. (FR-7)

### Notes

- **Adopters:** run `gauntlet init` (new repos) or `gauntlet upgrade` (existing) to
  pick up `pipelines/review.yaml`, the review prompt, and the commented
  `issue_tracker` / policy examples. PR mode additionally requires the
  `pr_read_commands@v1` rule to be ratified in `policy.yaml` via the policy-change
  process; branch mode needs no policy change.
- **Engine surface:** the review lifecycle composes existing `RunManager`
  primitives (manifest, writer/redactor, worktree lock, `resume`/`status`/`abort`)
  and reaches the `adversarial_cycle` through configuration only — `engine/cycle.py`
  loop logic is unchanged.

## [0.3.3] — 2026-06-30

A patch release that stops two operator dead-ends where a run could brick itself,
plus a commit-isolation correctness fix. All bug fixes; existing workflows are
unchanged. (#47)

### Fixed

- **`gauntlet reject` is no longer a dead end.** A `human_gate` rejection marked
  the run terminally `failed`, and a plain `resume` then no-op'd — so a rejection
  with an actionable note went nowhere. `reject_gate` now re-drives: when the gate
  sits downstream of an `adversarial_cycle` (the PRD/plan loops), the rejection
  note is injected into that cycle as a pending `--response` (audited and
  checkpoint-committed), the cycle and everything after it in the stage is reset,
  and the loop re-runs with the note as authoritative reviewer/triager guidance
  before re-parking the gate for a fresh decision. A gate with no upstream cycle to
  iterate still fails terminally with a clear note — reject is never a silent
  no-op. `reject` now takes the worktree lock and honors the judge like `approve`.
- **A clean-handoff precondition failure no longer bricks the run.** Re-running a
  failed clean-handoff precondition kept a stale `base_sha` (stamped on the failed
  run and never refreshed), so a later interrupt would diff/rewind against a SHA
  behind the operator's cleanup commit. Re-arming a re-runnable precondition
  failure now clears `base_sha` so the fresh attempt re-stamps the boundary at the
  current HEAD.
- **Producer-commit isolation.** `commit_paths` ran `git commit -F -` with no
  pathspec, so any file already staged when a producer commit (or the FR-6.4
  proposal apply) ran was swept in — defeating the "stages only its output"
  isolation both callers depend on. The commit is now pathspec-limited; a
  pre-staged file is left staged and uncommitted.
- **Fail-closed on artifact-commit git errors.** `_commit_output_artifact`
  promised fail-closed on git errors but let them bubble out as a generic "handler
  error"; it now catches `GitError` and returns an actionable `StepResult` naming
  the path and phase.
- **Rejecting an already-run cycle guard.** `reject_gate` now iterates only when
  the upstream cycle ran to `DONE`, else terminal-rejects with a clear reason —
  re-arming a non-`DONE` upstream cycle re-skips and orphans the note.
- **Operator-facing message polish.** The terminal-failure resume refusal now
  names the run by slug (operators act by slug, not `run_id`), and `--no-judge`
  gained a help string for discoverability.

## [0.3.2] — 2026-06-27

A patch release that fixes two ways the interactive operator/monitor session
(`gauntlet run --interactive`) could be left unable to act. Both are bug fixes;
existing workflows are unchanged.

### Fixed

- **The interactive operator session no longer bricks when its run ends.** The
  monitor is wired to the run's judge, which is reaped the instant the run exits
  — cleanly or, as seen live, on an early-step failure seconds in. The operator
  session was wired in the judge's default `unattended` mode, so once the judge
  was gone every operator tool call failed closed and was denied — even
  read-only diagnostics like `gauntlet status` — with a misleading "judge
  unreachable" error. `operator_session_env` now marks the session
  `interactive`, so an unreachable judge degrades to a permission prompt (the
  human operator is the backstop) instead of a total deny. A live judge's *deny*
  still denies in both modes, so this never loosens policy on a reachable judge.
- **The judge no longer denies the `gauntlet` CLI in the operator session.**
  `policy.yaml` had no fast-path rule for `gauntlet`, so the operator's own
  verbs (`status`/`logs`/`approve`/`reject`/`resume`/`abort`/`recover`/…)
  escalated to the LLM classifier, which denied them as an "untrusted external
  tool outside the repository" — blocking the operator at the commands it exists
  to run. A new `gauntlet-cli` fast-path allow rule covers the first-party CLI;
  the deny-first rules still gate the destructive git/network primitives in every
  context. Because allow rules are skipped on chained/piped/redirected commands
  (a benign prefix must not bless a dangerous trailing segment), the monitor's
  starter prompt now steers the operator to run gauntlet verbs as a single bare
  command — their output is already bounded (`logs` tails 200 lines,
  `status --json` is small), so piping is never needed.

### Notes

- **Adopters:** the `gauntlet-cli` allow rule ships in the scaffolded
  `.gauntlet/policy.yaml`; existing repos pick it up via `gauntlet upgrade` (or
  by adding the rule to their `policy.yaml`).

## [0.3.1] — 2026-06-27

A patch release that completes and hardens the run-supervision surface shipped
in 0.3.0. Both changes are bug fixes; existing workflows are unchanged.

### Fixed

- **`gauntlet run --watch` now opens the console in your browser.** The
  background-start-services phase (P5) had specified an authenticated
  browser-open for `--watch` but shipped only a subset of its scope — the
  browser-open (FR-1), `?p=` loopback query auth (FR-2), and `serve --resume`
  (FR-4) were never implemented. This release builds the dropped scope: `--watch`
  opens the authenticated loopback URL (degrading fail-soft to `/login`, never
  aborting the run), `serve --resume` reuses or boots a console and opens the
  browser without a foreground bind, and a new `--no-browser` flag opts out.
  Loopback `?p=<token>` query auth bootstraps the HttpOnly session cookie and is
  then stripped from the URL. (#43)
- **The interactive monitor now loads this repo's operator skill.**
  `run --interactive` / `status --interactive` launched a bare `claude` with no
  flags, so the spawned session never loaded the repo's `.claude/` project
  config and reported it could not run the `gauntlet-operator` skill. The
  monitor command now passes `--setting-sources project` for the `claude` agent
  (matching the builder/reviewer adapter profiles), bringing `.claude/skills/`
  into scope. (#44)

## [0.3.0] — 2026-06-27

A run-observability and supervision release. It makes an in-flight run
answerable — live log streaming, machine-readable status, a guarded recovery
path, and a one-command bridge into the console — and adds per-agent reasoning
effort control. Everything is additive; existing CLI workflows are unchanged.

### Added — operator observability & supervision (`status` / `logs` / `recover`)

- **`gauntlet status`** now reports **driver liveness**, the **computed
  run-state**, and the **next action / recovery hint**, so a glance answers
  "where is it, and does it need me?".
- **`gauntlet status --json`** emits the same state as one machine-readable
  object (schema `schemas/status.json`) for scripts and CI.
- **`gauntlet logs <slug>`** is read-only evidence-on-demand — a step's dir plus
  its transcript tail.
- **`gauntlet recover <slug>`** terminates a driver **only after verifying it is
  genuinely wedged** (identity-checked) and marks its step `INTERRUPTED` so a
  plain `resume` re-enters cleanly — it never kills a healthy run.
- **`gauntlet-operator` skill + playbook.** `gauntlet init` installs a
  project-level Claude Code skill (`prompts/operator.md`) that routes a
  supervising session to this repo's run-state triage and recovery playbook,
  propagated like every other asset.
- **Engine hardening.** A response-less terminal cycle failure now surfaces
  instead of being silently re-executed/rewritten on `resume` (with a regression
  test).

### Added — live run observability (streamed step output)

- **Streamed step output.** The CLI agents' line-delimited JSON events are now
  written to disk incrementally as they arrive (replacing the buffered drain in
  `run_with_timeout`), so an in-flight step has a live, redacted, tailable log.
  Claude + Codex adapters; the API/LiteLLM adapter is a durable non-goal.
  Streaming ships behind a **default-off** flag.
- **`gauntlet logs --follow`** tails a running step's `events.jsonl` live.
- **Advisory freshness signal.** `status` / `status --json` expose
  `current_step_freshness.last_event_age_s` so a stalled step is visible.

### Added — background service startup & the interactive run monitor

- **`gauntlet run --watch`** ensures the supervisory console is running
  (boot/reuse) and prints its URL before running in the foreground;
  `--console-host` / `--console-port` override the bind.
- **`gauntlet run --interactive[=claude|codex]`** launches the run **detached**
  and hands the terminal to an interactive monitoring agent, wired to the run's
  judge as the **operator's own session** (judge-gated without prompt spam);
  **`gauntlet status --interactive`** attaches the same monitor to an
  already-running run.
- **Per-run `judge.json`** (gitignored — endpoint + process identity) lets
  `abort` / `finish` / `clean` reap an **orphaned per-run judge** by verified
  identity; the shared console is never killed.

### Added — per-agent reasoning effort

- **`claude-code`** profiles accept an optional `effort`
  (`low`/`medium`/`high`/`xhigh`/`max`, passed as `--effort`), and **`codex`**
  profiles accept `reasoning_effort` (passed as `-c model_reasoning_effort=…`).
  Both are optional and no-op when absent — existing configs are unaffected — so
  a cheaper `fixer:` role can run review-fix rounds while `builder` runs higher.

### Changed — the wired judge hook tolerates a missing install

`gauntlet init` now wires the PreToolUse judge hook as an **install-tolerant
launcher** instead of the bare `gauntlet-judge-hook` console script, so a repo can
mix Gauntlet and non-Gauntlet developers without the latter seeing hook errors. The
launcher:

- **execs the real hook when it's installed** — the permission decision and exit
  code (including the exit-2 deny) and the `GAUNTLET_RUN_ID` gating pass through
  unchanged, so gating is byte-identical to before;
- **stays silent (exit 0) for a teammate who never installed Gauntlet**, instead of
  emitting a per-tool-call `command not found` hook-error notice on every call; and
- **fails closed (exit 2) only when the hook is missing during an active run**
  (`GAUNTLET_RUN_ID` set), so a broken install can never let a run proceed ungated.

A re-run upgrades an existing bare-command wiring in place (idempotent; a
hand-customized wrapping is left untouched), and `gauntlet doctor` recognizes both
forms while still FAILing when the console script is absent on a Gauntlet user's
PATH. The launcher is POSIX sh (macOS/Linux/WSL2 — native-Windows users follow the
README's WSL2 path).

### Added — PRD-authoring aids (the repo teaches its own PRD conventions)

Two committable aids make a fresh session — human or Claude — start PRD authoring
from the right shape instead of from tribal knowledge. Both propagate via
`gauntlet init` like every other asset, so a teammate who clones the repo
inherits them (FR-1.2).

- **PRD-authoring skill.** `gauntlet init` installs a project-level Claude Code
  skill at `.claude/skills/gauntlet-prd-author/SKILL.md`. It triggers on
  natural-language PRD intent ("write/draft/author a PRD", "start a Gauntlet
  run") and routes the session to this repo's playbook and conventions. It is a
  **thin pointer** to `prompts/prd-author.md` (resolved under the repo's
  `asset_root`) — never a copy — so the single instruction source can't drift.
  The playbook reference is repository-relative, so the committed skill keeps
  resolving after a clone or copy to a different absolute path.
- **Structured `gauntlet new` stub.** The PRD stub is now one committable
  template (`<asset_root>/prd-stub.md`) carrying the playbook's full section
  skeleton plus one-line guidance per section. Both `gauntlet new` and the
  `gauntlet run` entry-contract gate resolve the *same* template (repo copy if
  present, else the packaged default), so they can never disagree about what an
  unfilled stub is.
- **The human-author gate is unchanged and hardened.** The richer stub keeps the
  FR-10.1 marker, and a deterministic authored-content predicate rejects every
  trivial edit (whitespace-, comment-, or heading-only). Because the stub
  template is now a gate input, both consumers validate it against template
  invariants (exactly one marker, every mandatory section, required metadata
  labels) and **fail closed** — a malformed customization can't silently disable
  the gate.
- **Idempotent, never-clobber propagation.** A re-run creates whichever aid is
  missing, refreshes an *unmodified generated* file to the current template after
  a version bump, and never overwrites a customization. `gauntlet init
  --from-repo` reports each aid as present / missing / customized without
  writing. Malformed pre-existing state (a non-regular or symlinked destination)
  fails the run closed before any write.
- **Committability + `doctor` check.** `init` warns (without editing the rule) if
  a foreign ignore source — repo/parent `.gitignore`, `.git/info/exclude`, or the
  global `core.excludesFile` — would exclude the skill from git. `gauntlet doctor`
  gains a **warn-only** skill check (the skill gates nothing, so it never FAILs):
  it warns when the skill is missing, malformed against the pinned frontmatter
  schema, or its provenance looks stale.
- **`gauntlet new` pointer (OQ-4).** `gauntlet new` now prints a CLI-agnostic
  pointer to the playbook and skill, reinforcing the convention outside a
  skill-aware Claude session.

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

[0.4.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.4.0
[0.3.3]: https://github.com/johnpletka/gauntlet/releases/tag/v0.3.3
[0.3.2]: https://github.com/johnpletka/gauntlet/releases/tag/v0.3.2
[0.3.1]: https://github.com/johnpletka/gauntlet/releases/tag/v0.3.1
[0.3.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.3.0
[0.2.0]: https://github.com/johnpletka/gauntlet/releases/tag/v0.2.0

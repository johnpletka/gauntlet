# Gauntlet Bootstrap — Phased Implementation Plan

**Source:** `PRD-gauntlet.md` v1.3 (§9 phase table, refined against FR-1…FR-10)
**Branch:** `gauntlet/bootstrap` (FR-9.1)
**Status:** Awaiting human approval (FR-10.2). No P1 work begins until approved.
**Review:** round-1 codex review triaged and applied (11 accepted, 2 declined) —
see `runs/gauntlet-bootstrap/manual/plan-cycle-r1/`. Inline `(review F-xxx)` tags
trace each change to its finding.

---

## 0. Ground rules for the bootstrap

These apply to every phase and are not repeated below.

- **Process:** one phase at a time (FR-10.3). Each phase: implement → tests green →
  commit (`PN: <imperative ≤72 chars>` + detailed body, FR-9.2/9.3) → codex review
  handoff → point-by-point triage shown to human (FR-5.2) → fix commits
  `PN.x: Address review — …` listing declined findings with reasons (FR-9.4) →
  diff-scoped confirm pass, max 2 rounds, unresolved blockers escalate (FR-9.5,
  FR-10.5) → human gate before next phase.
- **Reviewer:** `codex exec` / `codex review --commit <sha>` in read-only sandbox,
  findings requested as structured JSON per the §7 findings schema. Installed and
  authenticated on this machine (codex-cli 0.139.0); verified flag behavior — not
  this plan's assumptions — is recorded in the doctor pin file as each phase
  exercises it.
- **Audit trail:** until the transcript logger exists (P4), every review/triage
  exchange is saved manually under `runs/gauntlet-bootstrap/manual/` in the same
  prompt/findings/triage shape the logger will later produce. From P1 onward,
  everything the bootstrap writes to disk passes through the minimal redacting
  writer (P1 deliverable; review F-005).
- **Trust model — engine vs. agents (review F-001):** the judge gates *agent* tool
  calls. The engine itself executes only human-committed configuration (the
  repo's `test_command`, git operations, judge lifecycle) and never substitutes
  agent-authored text into a command line. Model-generated content that lands on
  disk or in git is treated as data and validated: commit messages are
  format-checked, artifact writes are confined to the run directory, and
  improvement-proposal diffs (P7) are path-contained and human-approved before
  apply.
- **Pain points** encountered while following the process land in
  `BOOTSTRAP-NOTES.md` as design feedback.
- **Upstream invalidation** (FR-10.4): if implementation shows this plan or the PRD
  is wrong, halt and present the conflict; never silently amend an approved artifact.
- **Safety:** no permission-bypass flags, no force-push, stay inside this repo, ask
  before any system-level install (e.g. `pipx`, containers for the P6 install test).
- **Stack & layout (fixed across phases):** Python 3.12+, `uv` project, single
  installable package. `src/gauntlet/{engine,adapters,judge,logging,cli}/`,
  plus `pipelines/`, `prompts/`, `schemas/`, `tests/` — pipeline YAML, prompt
  templates, and `policy.yaml` are data, not code. CLI via `typer`, validation via
  `pydantic`, judge via `fastapi`+`uvicorn`, ApiAdapter via `litellm`.
- **Test taxonomy:** plain `pytest` units run anywhere (CI without credentials);
  anything touching real CLIs/APIs/network is marked `-m integration`. The suite
  only grows; all tests green before every commit.

### Dogfooding switchover (the point of the ordering)

- **After P3:** `gauntlet` commands take over the mechanical parts of later phases
  (branch/commit/manifest handling) while I still drive the loop.
- **After P4:** full switch. `runs/gauntlet-bootstrap/` gets a pipeline expressing
  P5–P7, and those phases execute *through* `gauntlet run` with the human at the
  gates. Manual process execution after this point is a bug.
- **Definition of done (self-hosting test):** `gauntlet run` executes a toy PRD
  end-to-end (P5) **and** has executed Gauntlet's own P6 and P7, producing real
  transcripts, a cost report, and ≥1 retro proposal reviewed by the human.

---

## P1 — Agent adapters + golden-path smoke

**Assumption validated:** Both CLIs are reliably scriptable on the *installed*
versions (claude 2.1.172, codex 0.139.0); structured output is parseable; session
IDs and usage are capturable. This is the riskiest external dependency after hooks,
so it goes first.

**Deliverables**
- `uv` project scaffold: `pyproject.toml` (package `gauntlet`, console script
  `gauntlet`, installable via `uv tool install` / `pipx install` — FR-1.1), `src/`
  layout above, pytest config with the `integration` marker, stub CLI entry point.
- `AgentAdapter` protocol + `AgentResult` pydantic model exactly per §4.1: `text`,
  `structured` (parsed JSON when a schema is given), `session_id`, `usage`
  (tokens/cost when reported), `raw_events`, `exit_code`.
- `ClaudeCodeAdapter`: `claude -p --output-format json` (and `stream-json` capture),
  `--resume <session>`, `--model`, `--append-system-prompt`, `--allowedTools`,
  `--permission-mode acceptEdits`. Permission-bypass flags are rejected at config
  load (lint per §8), not merely avoided.
- `CodexAdapter`: `codex exec --json`, `--output-schema <file>`, `-o`,
  `codex exec resume <session>`, `--full-auto`, `--sandbox` (read-only and
  workspace-write).
- `ApiAdapter`: LiteLLM completions for non-agentic tasks; JSON-schema-enforced
  output with bounded validate-and-retry (FR-3.4 groundwork).
- Adapter capability declarations (repo-write? structured-output?) so FR-2.3
  validation has something to check in P3; adapter registration via the
  `gauntlet.adapters` entry point (FR-2.4) so later adapters are plugins.
- Hard timeout wrapper around every CLI invocation (FR-3.3: stuck headless agents
  run until killed) with kill + checkpointable error result.
- Usage/cost extraction from each adapter's event stream (FR-3.2 groundwork),
  including the degraded "tokens only" path when cost isn't reported (PRD §12 Q3).
- Minimal redacting writer (review F-005; FR-4.4 down-payment): masks values of
  known secret env vars and common credential patterns in everything the
  bootstrap writes (manual transcripts, captured event streams, P2's judge audit
  log). The full configurable redaction list ships with the P4 logger; from P1,
  no log line is written unredacted.
- **Doctor pin file** (FR-1.5 groundwork): records the CLI versions and the exact
  flags the contract tests verified against them. Written from what the installed
  CLIs actually do — where observed behavior differs from the PRD/prompt, the pin
  file and a `BOOTSTRAP-NOTES.md` entry win.

**Test strategy**
- Unit: adapters fed recorded/fake subprocess output — JSON parsing, session-ID and
  usage extraction, schema-retry logic, timeout kill path, malformed-output errors.
- Contract (`-m integration`): one real prompt through each of `claude -p`,
  `codex exec` (with `--output-schema`), and one cheap LiteLLM call; assert parseable
  structure, session ID present, resume works for both CLIs, usage captured.
  Constraints (review F-002 — the judge does not exist yet, so these are the
  compensating control): smoke prompts are tool-less text round-trips; codex runs
  `--sandbox read-only` and claude runs with no tools allowed, except where a
  write-mode flag is itself under test; those write-mode tests run only in
  disposable fixture repos under a temp dir, never against this repo.

**Exit criteria**
- Unit suite green without credentials; contract suite green on this machine.
- Pin file exists and reflects verified behavior of both installed CLIs.
- `P1:` commit landed; review cycle complete; human gate passed.

---

## P2 — Judge service + hook wiring + red-team suite

**Assumption validated:** Pre-execution blocking works on both CLIs *as installed*
(hook semantics shift between releases — the PRD calls this the riskiest external
dependency).

**Deliverables**
- FastAPI judge service: single `/decide` endpoint taking `{tool_name, tool_input}`
  + run context (run id, step id, repo root) (FR-7.1). Binds 127.0.0.1 only;
  per-run shared token rejects foreign callers (§8). Started/stopped by the engine
  (P3 wires that; in P2 a `gauntlet judge serve` dev command).
- Decision ladder (FR-7.2): (1) `policy.yaml` deterministic regex/glob allow/deny,
  evaluated deny-first; (2) LLM classifier via the `judge_llm` ApiAdapter profile
  returning `{decision, risk_category, rationale}`; (3) fail-closed on timeout,
  parse error, or service-down. Fast-path p50 < 150 ms; LLM fallback bounded under
  both CLIs' hook timeouts.
- Default `policy.yaml` per FR-7.6 (deny: force-push, history rewrite on shared
  branches, `rm -rf` outside repo, package publish, credential reads outside repo,
  non-allowlisted outbound network, `curl|sh`; ask→LLM: package installs,
  migrations, bulk deletion).
- Hook clients + wiring: Claude Code `PreToolUse` (all tools) via
  `.claude/settings.json`; Codex `PreToolUse` (Bash) via repo-level hooks config;
  both speak the stdin-JSON / exit-code-2 / `permissionDecision` contract
  (FR-7.3/7.4) with deny rationale surfaced to the agent.
- Audit log: every decision (allow/deny, source fast-path|llm, latency, rationale)
  appended to `judge-audit.jsonl` (FR-7.5), written through the P1 redacting
  writer (review F-005).
- Operational model (review F-004): `/healthz` endpoint; the hook client
  distinguishes judge-deny from judge-unreachable. Unattended runs fail closed on
  both (FR-7.2). The *interactive* bootstrap session degrades differently:
  unreachable → `permissionDecision: ask` with a loud warning — the human at the
  keyboard is the backstop, so a dead judge falls back to normal permission
  prompts instead of deadlocking the session. Recovery is a documented,
  human-confirmed restart (`gauntlet judge serve`); no silent auto-restart.
- Red-team suite: 25 dangerous commands + a benign suite, runnable as the FR-7
  acceptance check — and extended beyond Bash (review F-007): Write/Edit path
  escapes, symlink escapes, credential-file reads, non-Bash network/file
  operations, package-manager variants, and shell-quoting evasions, plus an
  explicit check that codex's `workspace-write` sandbox actually backstops the
  non-Bash hook gap the PRD leans on (§4.2).

**Test strategy**
- Unit: policy evaluation order (deny-first), regex/glob matching, token auth,
  fail-closed on simulated timeout / bad JSON / dead service, audit-line schema,
  LLM-fallback parsing with a faked ApiAdapter.
- Contract (`-m integration`): drive *real* `claude -p` and `codex exec` through the
  hooks against a live judge — red-team commands 100% blocked pre-execution with
  audit entries; benign suite ≥ 90% resolved on the deterministic fast path;
  measured fast-path latency recorded into the pin file alongside verified hook
  semantics.

**Exit criteria**
- FR-7 acceptance met on this machine (100% red-team block, ≥90% fast-path benign).
- Fail-closed paths demonstrably deny.
- **This Claude Code session's own `PreToolUse` hook is wired to the judge** and
  stays wired for the rest of the bootstrap (the safety layer protecting its own
  construction). Wiring touches this repo's `.claude/settings.json` only;
  confirmed with the human at this phase's gate. The wiring uses the interactive
  degraded mode above (unreachable → ask + warning) so a dead judge cannot
  deadlock the bootstrap session (review F-004).
- `P2:` commit + review cycle + human gate.

---

## P3 — Pipeline engine: YAML, core steps, manifest, resume

**Assumption validated:** The state-machine + write-ahead-manifest design survives
`kill -9` mid-step and resumes correctly (G8).

**Deliverables**
- Pipeline loader: YAML → typed pydantic model; versioning (`version:` + content
  hash into the manifest, FR-5.6); load-time validation — artifact dataflow with
  dangling-reference rejection (FR-5.3), adapter-capability checks (FR-2.3:
  repo-write step can't bind `api`; schema-needing step warns on best-effort-JSON
  adapters), hook-disabling-flag lint (§8).
- Step attributes as first-class: `when:`, `foreach:`, `on_fail:` routing, per-step
  overrides for agent/budget/rounds (FR-5.4); custom step types via entry point
  (FR-5.5).
- Agent profiles in `.gauntlet/config.yaml` binding adapter+model+flags (FR-2.1),
  per-step `agent:` references (FR-2.2 swap acceptance becomes testable here).
- Step types: `agent_task`, `shell` (test/linter runner with `on_fail` routing and
  bounded retries; commands come only from human-committed pipeline/config YAML —
  the engine refuses template substitution of agent-authored text into `shell`
  commands, per the trust model / review F-001), `human_gate` (park run;
  `approve`/`reject --notes`), `commit`.
- `commit` step per FR-9.2: message drafted by `message_agent` from diff + plan
  section, engine-validated against the header/body format with reject+redraft;
  per-agent commit identities (FR-9.7); branch management `gauntlet/<slug>` off the
  configured base (FR-9.1).
- Run manifest per §7: pipeline name/version/hash, prompt hashes, per-step status,
  session IDs, commit SHAs, accumulated usage — **write-ahead** (written before and
  after each step), atomic via write-temp + rename (FR-8.2).
- Side-effect transaction boundary (review F-003): before any step that may touch
  the worktree, the manifest records the step's **base SHA**. On resume the engine
  compares worktree state to that base: clean → re-enter per FR-8.2; dirty → the
  step is marked `interrupted` and policy `interrupted_step: park | reset_to_base`
  applies (default `park` for a human decision; `reset_to_base` writes a backup
  ref first). A killed step's partial edits are therefore detected, never
  silently re-run over.
- Per-step `max_turns`/timeout/budget guards that halt at a checkpoint instead of
  burning tokens (FR-3.3).
- CLI lifecycle (FR-8.1): `new` (scaffold PRD stub), `run` (incl. judge service
  start/stop), `status`, `approve`/`reject`, `resume`, `abort`, and
  `rollback --phase N` (FR-9.9: guided `git reset --hard` to the post-cycle phase
  SHA + manifest rewind, branch and manifest never disagreeing).
- Entry contract (FR-10.1, sharpened per review OQ-1): `run` refuses unless
  `runs/<slug>/prd.md` exists **and differs from the scaffolded stub**
  (template-marker check). Existence + non-stub-ness is the enforceable part;
  authorship itself is procedural, exactly as FR-10.1 frames it.
- Rollback guards (review F-010): refuse on dirty worktree; refuse if the branch
  has diverged from the manifest's recorded SHAs; write a backup ref
  (`refs/gauntlet/backup/<run>/<timestamp>`) and a manifest snapshot before any
  rewind.
- Stage gating skeleton (FR-10.2/10.3): strictly sequential steps/stages, no
  look-ahead; upstream-invalidation halt (FR-10.4) as an engine-level park-at-gate.

**Test strategy**
- Unit: loader validation (good/bad pipelines, dangling artifacts, capability
  violations, banned flags), `when`/`foreach`/`on_fail` semantics, commit-format
  validator (accept/reject/redraft), manifest round-trip, budget-guard halt,
  entry-contract refusal, rollback SHA/manifest consistency — git operations
  against throwaway fixture repos in tmp dirs.
- Crash test: run a multi-step pipeline in a subprocess, `kill -9` it mid-step (and
  separately mid-manifest-write), `gauntlet resume`, assert no lost or duplicated
  step effects and correct re-entry; session-ID reuse where the adapter supports it,
  clean restart where it doesn't (FR-8.2). Nastiest cases included (review F-003):
  die *after* worktree edits but *before* the step's manifest completion — resume
  must detect the dirty base and park, not blindly re-run; and die mid-`commit`
  (commit created, SHA not yet recorded) — resume must reconcile from `git log`
  against the recorded base SHA rather than double-commit.
- Contract (`-m integration`): a minimal real pipeline (`agent_task` on claude →
  `shell` → `commit`) on a fixture repo, judge service running, hooks live.

**Exit criteria**
- Kill -9 / resume test passes repeatedly (run it in a loop; it must not flake).
- FR-10.1 refusal and FR-3.3 budget-halt demonstrated by tests.
- **Switchover #1:** from here on, branch/commit/manifest mechanics of P4–P7 run
  through `gauntlet` commands.
- `P3:` commit + review cycle + human gate.

---

## P4 — `adversarial_cycle` + schemas + transcript logger

**Assumption validated:** Cheap-model triage is accurate enough — ≥ 85% agreement
with a hand-labeled set of ~30 findings (PRD §9). Secondary: the
review→triage→fix→confirm loop is expressible as pure configuration (FR-5.2).

**Deliverables**
- Normative JSON schemas in `schemas/`: `findings.json` and `triage.json` exactly
  per §7 (severity/category enums, verdict enum
  `legitimate | bikeshedding | premature_optimization | not_applicable`, `action`
  `fix_now | defer | reject`).
- `adversarial_cycle` step type (FR-5.2): review (structured findings via
  `--output-schema` on codex / schema-prompt+retry elsewhere) → point-by-point
  triage with 1–3-sentence reasoning → fixer applies accepted findings →
  fix-round commit `PN.x: Address review — …` with per-finding body entries
  including declined-with-reason (FR-9.4, `commit_each_fix_round`) → **diff-scoped
  confirm** (`<handoff-sha>..<fix-sha>` + prior findings + triage verdicts; maps to
  `codex review` for codex, embedded diff elsewhere) with per-finding verdicts
  `resolved | partially_resolved | unresolved | regression_introduced` (FR-9.5) →
  loop within `max_rounds`; on exhaustion with open blockers, escalate to a human
  gate (FR-10.5).
- Reviewer-mutation guard (FR-9.6): review profiles request read-only (codex
  `--sandbox read-only`; claude review profile without Write/Edit); engine checks
  `git status` after every review step; `reviewer_mutation: commit | revert | halt`
  (default `commit`, reviewer-attributed `PN.rX:` commit and author identity).
- Prompt-injection containment (§8): reviewer findings reach the triager wrapped as
  data, never as instructions.
- Transcript logger (FR-4): per-step `prompt.md` (exact prompt), `transcript.md`
  (faithful rendering of every message incl. tool calls/results), `events.jsonl`
  (lossless stream); `RUN.md` index with verdict/duration/cost per step (FR-4.3);
  directory layout per FR-4.1; default-on secret redaction with configurable list
  (FR-4.4) applied before any write; `.gitignore` guidance text (FR-4.5, shipped by
  `init` in P6).
- Triage few-shot prompt template written rubric-first for small models (FR-3.4),
  plus the hand-labeled ~30-finding corpus (sourced from the bootstrap's own P1–P3
  review rounds — the manual transcripts exist precisely for this), stratified
  across the §7 severity enum so blocking/major cases are represented, not just
  nits (review F-009).
- Severity-aware escalation rule (review F-009; PRD §11's mitigation made
  concrete): triage verdicts carry confidence; blocking-severity findings and
  low-confidence verdicts escalate to a stronger model or the human gate instead
  of resting on the cheap model's verdict.
- Minimal bootstrap pipeline + prompt stubs (review F-006): a pipeline expressing
  the P5–P7 loop (implement → tests → commit → adversarial_cycle → human gate)
  plus the minimal prompt templates it needs — just enough to make switchover #2
  real. The polished `standard.yaml` and full prompt set remain P5 deliverables.

**Test strategy**
- Unit: full cycle driven by scripted fake adapters (reviewer/triager/fixer/
  confirmer) covering: clean converge in 1 round, converge in 2, escalation on
  max_rounds, reviewer mutation under each policy, fix-commit body content
  (declined findings present with reasons), confirm-diff scoping (assert the
  reviewer prompt contains *only* the round's commit-range diff), schema-violation
  retry, redaction.
- Triage accuracy (`-m integration`): run the configured cheap model over the
  hand-labeled corpus; report a per-severity confusion matrix, not just aggregate
  agreement (review F-009). **Exit needs ≥ 85% overall agreement AND zero
  blocking-severity findings misclassified into a reject category without
  escalation.** If it fails, iterate the rubric/few-shots (that's the assumption
  test working) — model *upgrade* is the fallback and gets recorded as a
  deviation.
- Contract (`-m integration`): one real adversarial_cycle round on a fixture repo
  with codex as reviewer.

**Exit criteria**
- Triage agreement ≥ 85% overall and zero unescalated blocking-severity misses,
  measured with a per-severity confusion matrix and recorded (review F-009).
- FR-9 acceptance behaviors demonstrated: clean-worktree handoffs, `PN:`/`PN.x:`
  history, confirm saw only the range diff, simulated reviewer mutation handled
  per policy with reviewer-attributed authorship.
- Transcripts: a non-participant can reconstruct review→triage→fix→confirm from
  files alone (FR-4 acceptance, checked by the human at this gate).
- **Switchover #2 (full dogfooding):** `runs/gauntlet-bootstrap/` created with
  P5–P7 expressed as a Gauntlet pipeline; from here, manual process execution is
  a bug.
- `P4:` commit + review cycle + human gate.

---

## P5 — Full `standard` pipeline end-to-end on a toy repo + cost report

**Assumption validated:** The whole loop converges within configured rounds and
budget on a real (small) PRD — and Gauntlet can run it unattended between gates.

**Deliverables**
- `pipelines/standard.yaml` encoding the 3-gate workflow exactly per FR-5.1
  (prd-cycle → prd-approve → plan-author → plan-cycle → plan-approve →
  `foreach: plan.phases` [implement → tests → phase-commit → impl-cycle] → retro
  placeholder until P7).
- Prompt template set in `prompts/` (versioned): plan-author (emits the structured
  phase list `foreach` consumes), implement-phase, reviewer variants (document vs.
  `code_review` mode), triage, commit-message, confirm.
- `gauntlet report <run>`: per-step / per-agent-profile cost breakdown table
  (FR-3.2), tokens-only fallback flagged as estimate.
- Toy project: separate scratch repo + small human-authored toy PRD (human writes
  it — FR-10.1 applies even to toys; I'll propose one for sign-off at the P4 gate).
- `PR.md` draft generation at final-gate pass (FR-9.8) — drafts only, never opens.
- Pipeline-extension acceptance check (FR-5.3/5.4): add a third-round review step
  to one stage *by YAML edit only* and show it validates, runs, and appears in
  transcripts + cost report.

**Test strategy**
- Unit: standard.yaml validates; phase-list parsing; report math from fixture
  manifests; PR.md rendering.
- End-to-end (`-m integration`): `gauntlet run` over the toy PRD with the human at
  its gates; convergence within max_rounds/budget; FR-3 acceptance (classification
  steps < 5% of run cost; budget-guard demonstrably halts a runaway step).
- **Executed through Gauntlet itself** via the `runs/gauntlet-bootstrap/` pipeline.

**Exit criteria**
- Toy PRD runs prd→plan→phases→commits end-to-end; `gauntlet/<toy-slug>` branch
  history matches FR-9 acceptance; cost report produced with classification < 5%.
- YAML-only extension check passes (FR-5 acceptance).
- `P5:` commit + review cycle + human gate.

---

## P6 — `init` / `doctor` / rollout packaging

**Assumption validated:** A teammate goes from clone to running pipeline in ≤ 3
commands (FR-1 acceptance).

**Deliverables**
- `gauntlet init [--from-repo]`: scaffolds `.gauntlet/config.yaml`,
  `pipelines/standard.yaml`, `prompts/`, `policy.yaml`, hook wiring into
  `.claude/settings.json` + repo-level codex hooks config, `.gitignore` guidance
  (FR-1.2, FR-4.5) — all committable; idempotent re-run.
- `gauntlet doctor` (FR-1.3/1.5): CLIs installed + authenticated, hook files
  present/trusted, judge startable, ApiAdapter keys present (env/keychain only,
  never repo config — FR-1.4), installed versions vs. the pin file with actionable
  non-zero-exit messages.
- Packaging: installable via `uv tool install` / `pipx install` from a git URL
  (FR-1.1); version metadata surfaced in `doctor`.
- Second-environment install test (clean venv or container; **container/system
  tooling only with human sign-off** per ground rules — using a throwaway clean
  `uv` environment as the default).

**Test strategy**
- Unit: doctor checks against simulated broken environments (missing CLI, bad
  version, absent hooks, missing key) each producing its actionable message;
  init file-set snapshot + idempotency; scaffold contents validate against the
  P3 loader.
- Install test (`-m integration`): clean environment → `uv tool install` from this
  repo → `gauntlet doctor` → `gauntlet run` on the toy project: ≤ 3 commands.
- **Executed through Gauntlet** (this phase's implement/test/commit/review loop
  runs under the bootstrap pipeline).

**Exit criteria**
- FR-1 acceptance demonstrated end-to-end in a clean environment.
- `P6:` commit + review cycle + human gate.

---

## P7 — Retro, feedback capture, proposal generation + governed apply

**Assumption validated:** Proposals are concrete and useful — the human accepts
≥ 1 real proposal generated from a real run (FR-6 acceptance).

**Deliverables**
- `retrospective` step type (FR-6.2): each agent gets a condensed run summary (its
  findings, triage verdicts on them, test failures, human feedback) and returns
  self-critique.
- `gauntlet feedback <run>` (FR-6.1): outcome rating, reviewer misses, triage
  errors (false legitimate / false bikeshedding), freeform → `retro/feedback.md`;
  capturable at run end or later.
- Proposal generation (FR-6.3): cheap-model synthesis of feedback + retros into
  **literal diffs** against versioned assets (prompts, pipeline params, triage
  few-shots, judge policy fast-path rules) with rationale, landing in
  `retro/proposals/NNN-<slug>.md`.
- `gauntlet proposals review` (FR-6.4): present, approve/reject; approved diffs
  applied + committed with the proposal as body; **no self-application**;
  `prompts/CHANGELOG.md` accumulation + human-corrected triage cases feeding the
  few-shot corpus (FR-6.5). Proposal apply is path-contained (review F-001): a
  diff may touch only the versioned-asset allowlist (`prompts/`, `pipelines/`,
  `policy.yaml`, `schemas/`, triage few-shots) via repo-relative paths — anything
  else is rejected at parse time, before the human is even asked.
- `gauntlet report --trend` metrics (FR-6.6): findings/round, %legitimate,
  fix-survival rate, test-failure loops, judge ask-rate, cost/phase.
- Prompt/policy version hashes in the manifest so the next run provably uses the
  approved version (FR-6 acceptance).

**Test strategy**
- Unit: proposal diff parse/apply/reject round-trips, changelog accumulation,
  trend-metric math from fixture manifests, no-self-apply guard.
- Real-data (`-m integration`): run retro + feedback + proposal generation against
  the bootstrap's own accumulated runs (P5 toy run + P6 run); FR-6 acceptance — a
  deliberately-seeded triage error marked in feedback yields ≥ 1 concrete
  prompt-diff proposal; approval updates template + changelog; next run's manifest
  shows the new prompt hash.
- **Executed through Gauntlet.**

**Exit criteria**
- Human has reviewed (and ideally accepted) ≥ 1 real proposal from a real run.
- **Self-hosting test (bootstrap definition of done):** toy PRD ran end-to-end via
  `gauntlet run` (P5) ∧ Gauntlet executed its own P6+P7 ∧ real transcripts + cost
  report + ≥1 human-reviewed retro proposal exist.
- `P7:` commit + review cycle + final human gate; bootstrap retro written to
  `BOOTSTRAP-NOTES.md`.

---

## Refinements vs. PRD §9 (what the table under-specifies, and where it landed)

| Concern | Decision |
|---|---|
| FR-8 lifecycle CLI (`run/status/approve/resume/abort/rollback`) has no phase in §9 | Landed in P3 — they are manifest operations; P3's crash test needs them anyway |
| FR-9 spans commit format, fix rounds, mutation guard, rollback | Split: commit format + identities + branch + rollback in P3; fix-round commits, diff-scoped confirm, mutation guard in P4 (they're properties of the cycle) |
| FR-2.3/2.4 (capability validation, entry-point adapters) | Capabilities declared in P1, enforced at load in P3; entry-point registration in P1 |
| FR-3.3 budget guards | Timeout wrapper in P1 (per-invocation), step budgets in P3 (engine concern) |
| Judge start/stop lifecycle | Dev command in P2, engine-managed in P3's `run` |
| Hand-labeled triage corpus (P4 needs ~30 findings) | Harvested from the bootstrap's own P1–P3 review rounds, hand-labeled by me with human spot-check at the P4 gate |
| Toy PRD authorship (FR-10.1 says humans author PRDs) | Human writes/ratifies the toy PRD; proposed at the P4 gate |
| `retro` stage appears in standard.yaml (P5) but retro step ships in P7 | standard.yaml carries the stage from P5 with the step active only when registered; P7 activates it |
| PRD §12 Q3 (subscription CLIs may not report cost) | P1 records tokens always, cost when derivable; report labels estimates |

## Top risks to this plan

1. **Hook semantics on installed CLI versions** (P2) — the PRD's own #1 risk.
   Mitigated by contract tests against the real CLIs before anything depends on
   the judge, and by pinning verified behavior, not documented behavior.
2. **Codex structured-output reliability** for findings (P1/P4) — `--output-schema`
   behavior is verified in P1 before the cycle is built on it; schema-prompt+retry
   is the fallback path and is exercised in unit tests either way.
3. **Triage accuracy < 85%** (P4) — rubric/few-shot iteration is in-scope for the
   phase; model upgrade is the recorded-deviation fallback (it changes the FR-3
   cost story, so it goes to the human gate).
4. **kill -9 resume flakiness** (P3) — write-ahead design is tested in a loop, not
   once; manifest writes are atomic (write-temp + rename).

---

*Stop point: this plan awaits human approval per FR-10.2 / bootstrap rule 2.*

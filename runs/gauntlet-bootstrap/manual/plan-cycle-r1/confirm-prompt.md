# Confirm pass — plan review round 1

You previously reviewed `runs/gauntlet/plan.md` (an implementation plan for the
Gauntlet harness described in `PRD-gauntlet.md`) and produced the findings below.
Each was triaged point-by-point; accepted fixes were applied in commits
9cb3f9f (PRD) and a2b9eec (plan). Your task now is ONLY the confirm pass:

For EACH finding ID below, judge from the appended commit-range diff whether it is
`resolved`, `partially_resolved`, `unresolved`, or `regression_introduced`, with a
one-sentence note. Two findings were DECLINED at triage (F-008, OQ-2) — judge
whether the recorded decline reasoning is defensible; if you accept the decline,
mark the finding `resolved` and note "decline accepted". Scope is the diff: do not
re-review the whole document. If the diff itself introduces a new problem, add it
to `new_findings`. Respond strictly in the provided JSON schema.

## Your prior findings, with triage verdicts and actions taken

- F-001 (High): Engine shell steps + P7 proposal-apply bypass the judge/safety
  model. TRIAGE: legitimate (remedy scoped down — engine ops stay outside the
  judge; trust model + path containment instead). ACTION: trust-model ground rule;
  shell commands only from human-committed YAML, no agent-text substitution; P7
  proposal apply path-contained to versioned-asset allowlist.
- F-002 (High): P1 exercises real CLIs before the judge exists. TRIAGE:
  legitimate. ACTION: P1 integration tests constrained to tool-less prompts,
  read-only sandboxes, disposable fixture repos for write-flag tests.
- F-003 (High): Write-ahead manifests alone don't make resume safe. TRIAGE:
  legitimate (diff hashing left to implementation). ACTION: per-step base SHA,
  dirty-worktree detection, interrupted_step park|reset_to_base policy, crash
  tests for die-after-edit-before-manifest and die-mid-commit.
- F-004 (High): Permanent fail-closed hook wiring can deadlock the bootstrap.
  TRIAGE: legitimate. ACTION: judge operational model — deny vs unreachable
  distinguished; unattended fails closed, interactive session degrades to
  permissionDecision:ask with loud warning; /healthz; human-confirmed restart.
- F-005 (High): Redaction arrives too late (P4) while logs are written from
  P1/P2. TRIAGE: legitimate. ACTION: minimal redacting writer moved to P1; used
  by manual transcripts and P2 judge audit log; full config stays P4.
- F-006 (Medium): P4/P5 dogfooding switchover is circular. TRIAGE: legitimate.
  ACTION: minimal bootstrap pipeline + prompt stubs added as P4 deliverable;
  polished standard.yaml + prompts remain P5.
- F-007 (Medium): Red-team coverage too command-centric. TRIAGE: legitimate.
  ACTION: suite extended to Write/Edit path escapes, symlink escapes, credential
  reads, non-Bash network/file ops, package-manager variants, quoting evasions,
  explicit codex workspace-write backstop check.
- F-008 (Medium): Plugin/entry-point extensibility needs trust/allowlist
  machinery. TRIAGE: DECLINED premature_optimization — v1 ships no third-party
  plugins; entry-point code carries the trust of the installed Python
  environment (install already ran arbitrary code); doctor plugin listing noted
  as v1.1 candidate. No change.
- F-009 (Medium): ≥85% triage agreement can hide blocking false negatives.
  TRIAGE: legitimate. ACTION: severity-stratified corpus, confusion-matrix
  reporting, confidence-carrying verdicts with blocking/low-confidence
  escalation; exit adds zero unescalated blocking misses.
- F-010 (Medium): Rollback lacks destructive-action guardrails. TRIAGE:
  legitimate. ACTION: dirty-worktree refusal, branch-divergence refusal, backup
  ref + manifest snapshot before rewind.
- OQ-1: How is "human-authored PRD" represented technically? TRIAGE: legitimate
  clarification. ACTION: entry contract = prd.md exists AND differs from
  scaffolded stub (template-marker check); authorship documented as procedural.
- OQ-2: Should events.jsonl be git-ignored by default? TRIAGE: DECLINED
  bikeshedding — FR-4.5 already ships the exclusion as an option; FR-4.4
  default-on redaction is the control; default stays per PRD. No change.
- OQ-3: PRD §12 stale "autosquashed by default" text contradicts FR-9.4. TRIAGE:
  legitimate, upstream artifact. ACTION: PRD §12 line corrected (human-authorized)
  to "review-fix commits preserved as first-class history (squash-merge optional
  at PR time)".

## Commit-range diff (641090c..a2b9eec, scoped to the reviewed artifacts)

diff --git a/PRD-gauntlet.md b/PRD-gauntlet.md
index b9d882f..e64530d 100644
--- a/PRD-gauntlet.md
+++ b/PRD-gauntlet.md
@@ -446,7 +446,7 @@ Each phase ends with passing tests and a commit. Phases are ordered to kill the
 
 ## 12. Open Questions
 
-~~1. Phase commits: dedicated branch vs. current branch~~ — **Resolved:** one PR per PRD on a dedicated `gauntlet/<slug>` branch; single clean commit per phase with header+body format; review fixes autosquashed by default. See FR-9.
+~~1. Phase commits: dedicated branch vs. current branch~~ — **Resolved:** one PR per PRD on a dedicated `gauntlet/<slug>` branch; single clean commit per phase with header+body format; review-fix commits preserved as first-class history (squash-merge optional at PR time). See FR-9.
 
 1. Consensus review (two reviewer models, union or intersection of findings) — v1.1 candidate; the pipeline schema already permits it.
 2. Per-org shared prompt/policy repo (`gauntlet init --from git@...`) for multi-repo teams — needed before broad rollout?
diff --git a/runs/gauntlet/plan.md b/runs/gauntlet/plan.md
index 1b24162..ca24a41 100644
--- a/runs/gauntlet/plan.md
+++ b/runs/gauntlet/plan.md
@@ -3,6 +3,9 @@
 **Source:** `PRD-gauntlet.md` v1.3 (§9 phase table, refined against FR-1…FR-10)
 **Branch:** `gauntlet/bootstrap` (FR-9.1)
 **Status:** Awaiting human approval (FR-10.2). No P1 work begins until approved.
+**Review:** round-1 codex review triaged and applied (11 accepted, 2 declined) —
+see `runs/gauntlet-bootstrap/manual/plan-cycle-r1/`. Inline `(review F-xxx)` tags
+trace each change to its finding.
 
 ---
 
@@ -23,7 +26,17 @@ These apply to every phase and are not repeated below.
   exercises it.
 - **Audit trail:** until the transcript logger exists (P4), every review/triage
   exchange is saved manually under `runs/gauntlet-bootstrap/manual/` in the same
-  prompt/findings/triage shape the logger will later produce.
+  prompt/findings/triage shape the logger will later produce. From P1 onward,
+  everything the bootstrap writes to disk passes through the minimal redacting
+  writer (P1 deliverable; review F-005).
+- **Trust model — engine vs. agents (review F-001):** the judge gates *agent* tool
+  calls. The engine itself executes only human-committed configuration (the
+  repo's `test_command`, git operations, judge lifecycle) and never substitutes
+  agent-authored text into a command line. Model-generated content that lands on
+  disk or in git is treated as data and validated: commit messages are
+  format-checked, artifact writes are confined to the run directory, and
+  improvement-proposal diffs (P7) are path-contained and human-approved before
+  apply.
 - **Pain points** encountered while following the process land in
   `BOOTSTRAP-NOTES.md` as design feedback.
 - **Upstream invalidation** (FR-10.4): if implementation shows this plan or the PRD
@@ -82,6 +95,11 @@ so it goes first.
   run until killed) with kill + checkpointable error result.
 - Usage/cost extraction from each adapter's event stream (FR-3.2 groundwork),
   including the degraded "tokens only" path when cost isn't reported (PRD §12 Q3).
+- Minimal redacting writer (review F-005; FR-4.4 down-payment): masks values of
+  known secret env vars and common credential patterns in everything the
+  bootstrap writes (manual transcripts, captured event streams, P2's judge audit
+  log). The full configurable redaction list ships with the P4 logger; from P1,
+  no log line is written unredacted.
 - **Doctor pin file** (FR-1.5 groundwork): records the CLI versions and the exact
   flags the contract tests verified against them. Written from what the installed
   CLIs actually do — where observed behavior differs from the PRD/prompt, the pin
@@ -93,6 +111,11 @@ so it goes first.
 - Contract (`-m integration`): one real prompt through each of `claude -p`,
   `codex exec` (with `--output-schema`), and one cheap LiteLLM call; assert parseable
   structure, session ID present, resume works for both CLIs, usage captured.
+  Constraints (review F-002 — the judge does not exist yet, so these are the
+  compensating control): smoke prompts are tool-less text round-trips; codex runs
+  `--sandbox read-only` and claude runs with no tools allowed, except where a
+  write-mode flag is itself under test; those write-mode tests run only in
+  disposable fixture repos under a temp dir, never against this repo.
 
 **Exit criteria**
 - Unit suite green without credentials; contract suite green on this machine.
@@ -126,9 +149,21 @@ dependency).
   both speak the stdin-JSON / exit-code-2 / `permissionDecision` contract
   (FR-7.3/7.4) with deny rationale surfaced to the agent.
 - Audit log: every decision (allow/deny, source fast-path|llm, latency, rationale)
-  appended to `judge-audit.jsonl` (FR-7.5).
+  appended to `judge-audit.jsonl` (FR-7.5), written through the P1 redacting
+  writer (review F-005).
+- Operational model (review F-004): `/healthz` endpoint; the hook client
+  distinguishes judge-deny from judge-unreachable. Unattended runs fail closed on
+  both (FR-7.2). The *interactive* bootstrap session degrades differently:
+  unreachable → `permissionDecision: ask` with a loud warning — the human at the
+  keyboard is the backstop, so a dead judge falls back to normal permission
+  prompts instead of deadlocking the session. Recovery is a documented,
+  human-confirmed restart (`gauntlet judge serve`); no silent auto-restart.
 - Red-team suite: 25 dangerous commands + a benign suite, runnable as the FR-7
-  acceptance check.
+  acceptance check — and extended beyond Bash (review F-007): Write/Edit path
+  escapes, symlink escapes, credential-file reads, non-Bash network/file
+  operations, package-manager variants, and shell-quoting evasions, plus an
+  explicit check that codex's `workspace-write` sandbox actually backstops the
+  non-Bash hook gap the PRD leans on (§4.2).
 
 **Test strategy**
 - Unit: policy evaluation order (deny-first), regex/glob matching, token auth,
@@ -146,7 +181,9 @@ dependency).
 - **This Claude Code session's own `PreToolUse` hook is wired to the judge** and
   stays wired for the rest of the bootstrap (the safety layer protecting its own
   construction). Wiring touches this repo's `.claude/settings.json` only;
-  confirmed with the human at this phase's gate.
+  confirmed with the human at this phase's gate. The wiring uses the interactive
+  degraded mode above (unreachable → ask + warning) so a dead judge cannot
+  deadlock the bootstrap session (review F-004).
 - `P2:` commit + review cycle + human gate.
 
 ---
@@ -168,21 +205,38 @@ dependency).
 - Agent profiles in `.gauntlet/config.yaml` binding adapter+model+flags (FR-2.1),
   per-step `agent:` references (FR-2.2 swap acceptance becomes testable here).
 - Step types: `agent_task`, `shell` (test/linter runner with `on_fail` routing and
-  bounded retries), `human_gate` (park run; `approve`/`reject --notes`), `commit`.
+  bounded retries; commands come only from human-committed pipeline/config YAML —
+  the engine refuses template substitution of agent-authored text into `shell`
+  commands, per the trust model / review F-001), `human_gate` (park run;
+  `approve`/`reject --notes`), `commit`.
 - `commit` step per FR-9.2: message drafted by `message_agent` from diff + plan
   section, engine-validated against the header/body format with reject+redraft;
   per-agent commit identities (FR-9.7); branch management `gauntlet/<slug>` off the
   configured base (FR-9.1).
 - Run manifest per §7: pipeline name/version/hash, prompt hashes, per-step status,
   session IDs, commit SHAs, accumulated usage — **write-ahead** (written before and
-  after each step) (FR-8.2).
+  after each step), atomic via write-temp + rename (FR-8.2).
+- Side-effect transaction boundary (review F-003): before any step that may touch
+  the worktree, the manifest records the step's **base SHA**. On resume the engine
+  compares worktree state to that base: clean → re-enter per FR-8.2; dirty → the
+  step is marked `interrupted` and policy `interrupted_step: park | reset_to_base`
+  applies (default `park` for a human decision; `reset_to_base` writes a backup
+  ref first). A killed step's partial edits are therefore detected, never
+  silently re-run over.
 - Per-step `max_turns`/timeout/budget guards that halt at a checkpoint instead of
   burning tokens (FR-3.3).
-- CLI lifecycle (FR-8.1): `new` (scaffold PRD stub — entry contract FR-10.1: `run`
-  refuses without a human-authored `prd.md`), `run` (incl. judge service
+- CLI lifecycle (FR-8.1): `new` (scaffold PRD stub), `run` (incl. judge service
   start/stop), `status`, `approve`/`reject`, `resume`, `abort`, and
   `rollback --phase N` (FR-9.9: guided `git reset --hard` to the post-cycle phase
   SHA + manifest rewind, branch and manifest never disagreeing).
+- Entry contract (FR-10.1, sharpened per review OQ-1): `run` refuses unless
+  `runs/<slug>/prd.md` exists **and differs from the scaffolded stub**
+  (template-marker check). Existence + non-stub-ness is the enforceable part;
+  authorship itself is procedural, exactly as FR-10.1 frames it.
+- Rollback guards (review F-010): refuse on dirty worktree; refuse if the branch
+  has diverged from the manifest's recorded SHAs; write a backup ref
+  (`refs/gauntlet/backup/<run>/<timestamp>`) and a manifest snapshot before any
+  rewind.
 - Stage gating skeleton (FR-10.2/10.3): strictly sequential steps/stages, no
   look-ahead; upstream-invalidation halt (FR-10.4) as an engine-level park-at-gate.
 
@@ -195,7 +249,11 @@ dependency).
 - Crash test: run a multi-step pipeline in a subprocess, `kill -9` it mid-step (and
   separately mid-manifest-write), `gauntlet resume`, assert no lost or duplicated
   step effects and correct re-entry; session-ID reuse where the adapter supports it,
-  clean restart where it doesn't (FR-8.2).
+  clean restart where it doesn't (FR-8.2). Nastiest cases included (review F-003):
+  die *after* worktree edits but *before* the step's manifest completion — resume
+  must detect the dirty base and park, not blindly re-run; and die mid-`commit`
+  (commit created, SHA not yet recorded) — resume must reconcile from `git log`
+  against the recorded base SHA rather than double-commit.
 - Contract (`-m integration`): a minimal real pipeline (`agent_task` on claude →
   `shell` → `commit`) on a fixture repo, judge service running, hooks live.
 
@@ -243,7 +301,17 @@ review→triage→fix→confirm loop is expressible as pure configuration (FR-5.
   `init` in P6).
 - Triage few-shot prompt template written rubric-first for small models (FR-3.4),
   plus the hand-labeled ~30-finding corpus (sourced from the bootstrap's own P1–P3
-  review rounds — the manual transcripts exist precisely for this).
+  review rounds — the manual transcripts exist precisely for this), stratified
+  across the §7 severity enum so blocking/major cases are represented, not just
+  nits (review F-009).
+- Severity-aware escalation rule (review F-009; PRD §11's mitigation made
+  concrete): triage verdicts carry confidence; blocking-severity findings and
+  low-confidence verdicts escalate to a stronger model or the human gate instead
+  of resting on the cheap model's verdict.
+- Minimal bootstrap pipeline + prompt stubs (review F-006): a pipeline expressing
+  the P5–P7 loop (implement → tests → commit → adversarial_cycle → human gate)
+  plus the minimal prompt templates it needs — just enough to make switchover #2
+  real. The polished `standard.yaml` and full prompt set remain P5 deliverables.
 
 **Test strategy**
 - Unit: full cycle driven by scripted fake adapters (reviewer/triager/fixer/
@@ -253,14 +321,18 @@ review→triage→fix→confirm loop is expressible as pure configuration (FR-5.
   reviewer prompt contains *only* the round's commit-range diff), schema-violation
   retry, redaction.
 - Triage accuracy (`-m integration`): run the configured cheap model over the
-  hand-labeled corpus; report agreement; **≥ 85% required to exit**. If it fails,
-  iterate the rubric/few-shots (that's the assumption test working) — model
-  *upgrade* is the fallback and gets recorded as a deviation.
+  hand-labeled corpus; report a per-severity confusion matrix, not just aggregate
+  agreement (review F-009). **Exit needs ≥ 85% overall agreement AND zero
+  blocking-severity findings misclassified into a reject category without
+  escalation.** If it fails, iterate the rubric/few-shots (that's the assumption
+  test working) — model *upgrade* is the fallback and gets recorded as a
+  deviation.
 - Contract (`-m integration`): one real adversarial_cycle round on a fixture repo
   with codex as reviewer.
 
 **Exit criteria**
-- Triage agreement ≥ 85% on the labeled set, measured and recorded.
+- Triage agreement ≥ 85% overall and zero unescalated blocking-severity misses,
+  measured with a per-severity confusion matrix and recorded (review F-009).
 - FR-9 acceptance behaviors demonstrated: clean-worktree handoffs, `PN:`/`PN.x:`
   history, confirm saw only the range diff, simulated reviewer mutation handled
   per policy with reviewer-attributed authorship.
@@ -366,7 +438,10 @@ commands (FR-1 acceptance).
 - `gauntlet proposals review` (FR-6.4): present, approve/reject; approved diffs
   applied + committed with the proposal as body; **no self-application**;
   `prompts/CHANGELOG.md` accumulation + human-corrected triage cases feeding the
-  few-shot corpus (FR-6.5).
+  few-shot corpus (FR-6.5). Proposal apply is path-contained (review F-001): a
+  diff may touch only the versioned-asset allowlist (`prompts/`, `pipelines/`,
+  `policy.yaml`, `schemas/`, triage few-shots) via repo-relative paths — anything
+  else is rejected at parse time, before the human is even asked.
 - `gauntlet report --trend` metrics (FR-6.6): findings/round, %legitimate,
   fix-survival rate, test-failure loops, judge ask-rate, cost/phase.
 - Prompt/policy version hashes in the manifest so the next run provably uses the

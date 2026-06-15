# PRD: Gauntlet — Adversarial Multi-Agent Development Harness

**Status:** Draft v1.4 (v1.3 + `.gauntlet/` asset consolidation for adopter repos via the `asset_root` config knob, FR-1.2)
**Author:** John (with Claude)
**Date:** 2026-06-10
**Working name:** Gauntlet (every artifact runs the gauntlet of adversarial review before it ships)

---

## 1. Overview

Gauntlet is a local-first development harness that automates a PRD-driven, adversarially-reviewed implementation workflow across multiple coding agents (Claude Code, Codex CLI, and raw API models). The human author writes and approves the PRD; Gauntlet drives planning, phased implementation, adversarial review cycles, triage of review feedback, fixes, and commits — pausing only at explicitly configured human gates.

The harness encodes a workflow that today is performed manually:

1. **PRD gate** — Author a PRD with a builder agent, run it through an adversarial reviewer, triage the feedback point-by-point (legitimate / bikeshedding / premature / N/A), apply accepted fixes, and confirm concerns were addressed.
2. **Plan gate** — Generate a phased implementation plan from the PRD, where each phase tests and validates assumptions and ends in a commit. Same adversarial review cycle applies.
3. **Phase gates** — For each phase: implement, run tests, then run two rounds of adversarial review → triage → fix → confirm, then commit and advance.

Gauntlet generalizes this into a **declarative, configurable pipeline** so steps can be added, reordered, re-assigned to different models/agents, and improved over time — including a governed self-improvement loop driven by run outcomes and human feedback.

### 1.1 Problem statement

The manual workflow produces high-quality output but consumes hours of human time on mechanical coordination: copying feedback between tools, re-prompting agents, answering permission prompts, and tracking which review round each artifact is in. The human's unique value is concentrated in two places — authoring the PRD and reviewing it — yet today they are also the message bus, the state machine, and the permission system.

### 1.2 Solution summary

A thin Python orchestrator (state machine) that:

- Shells out to **Claude Code headless** (`claude -p --output-format json`) and **Codex non-interactive** (`codex exec --json --output-schema`) via a common **agent adapter interface**.
- Routes lightweight classification tasks (triage, judging, summarization) to **token-efficient API models** via LiteLLM.
- Enforces safety through a **shared judge service** wired into both CLIs as `PreToolUse` hooks (exit-code-2 / `permissionDecision` contract), with sandbox flags as the backstop.
- Persists **complete transcripts of every agent message**, organized per-PRD in markdown + JSONL, suitable for committing to source control.
- Supports **pipeline extension** by editing a YAML pipeline definition — no orchestrator code changes for new review steps, new gates, or re-ordered stages.
- Closes the loop with a **retrospective step** that collects human feedback and agent self-critique after each run and proposes versioned edits to prompts/policies, applied only with human approval.

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal | Maps to requirement |
|---|------|---------------------|
| G1 | One-command install and project onboarding for teammates | Req 1 (team rollout) |
| G2 | Per-step agent/model assignment via config, swappable without code changes | Req 2 (model per step) |
| G3 | Cost-aware model routing: cheap models for classification, frontier models for implementation | Req 3 (token efficiency) |
| G4 | Complete, durable, human-readable logs of all agent traffic, organized by PRD, committable to git | Req 4 (logging) |
| G5 | Declarative pipeline: add/remove/reorder steps in YAML | Req 5 (flexibility) |
| G6 | Governed improvement loop: human feedback + agent retrospectives → versioned prompt/policy changes | Req 6 (self-improvement) |
| G7 | Unattended safety: automated "can I do this" judging with hard pre-execution enforcement | (from prior design) |
| G8 | Resumability: any run can be interrupted and resumed from its last checkpoint | (operational) |

### 2.2 Non-Goals (v1)

- **Not a CI/CD system.** Gauntlet runs on a developer workstation against a local checkout. GitHub Actions integration is a future consideration.
- **Not a hosted service.** No server component beyond the localhost judge service. No multi-tenant anything.
- **Not a general agent platform.** No messaging-channel integrations, no heartbeat loops, no persistent personal memory (this is explicitly why Hermes/OpenClaw were rejected).
- **No automatic merging or pushing.** Gauntlet commits to a working branch; pushing and PR creation remain human actions in v1.
- **No fully autonomous self-modification.** The improvement loop *proposes* changes; a human approves them. Unsupervised prompt mutation is out of scope.
- **No GUI.** CLI + markdown artifacts. A TUI/dashboard is a future consideration.

---

## 3. Users and Personas

| Persona | Description | Primary interactions |
|---------|-------------|----------------------|
| **Pipeline author** (John, senior engineers) | Writes PRDs, reviews triage decisions at human gates, approves improvement proposals | `gauntlet new`, `gauntlet run`, gate approvals, retro review |
| **Team adopter** | Engineer who clones a repo that already contains a Gauntlet config and wants to run the same workflow | `gauntlet init --from-repo`, `gauntlet run` |
| **Pipeline maintainer** | Owns the shared pipeline definitions, prompt templates, and judge policy for the team | Edits `pipelines/*.yaml`, `prompts/`, `policy.yaml`; reviews retro proposals |

---

## 4. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     gauntlet CLI (Python)                    │
│                                                              │
│  ┌────────────┐   ┌──────────────┐   ┌───────────────────┐  │
│  │  Pipeline   │   │ Run State    │   │  Transcript        │  │
│  │  Engine     │──▶│ Manifest     │   │  Logger            │  │
│  │ (YAML-driven│   │ (JSON on disk│   │ (md + JSONL per    │  │
│  │ state machine)│ │  per run)    │   │  step, per PRD)    │  │
│  └──────┬─────┘   └──────────────┘   └───────────────────┘  │
│         │ AgentAdapter interface                             │
│  ┌──────┴───────────┬───────────────────┬─────────────────┐ │
│  │ ClaudeCodeAdapter │ CodexAdapter      │ ApiAdapter       │ │
│  │ claude -p         │ codex exec        │ LiteLLM          │ │
│  │ --output-format   │ --json            │ (haiku/mini/etc  │ │
│  │ json, --resume    │ --output-schema   │ for cheap tasks) │ │
│  └──────┬───────────┴───────┬───────────┴─────────────────┘ │
└─────────┼───────────────────┼────────────────────────────────┘
          │ PreToolUse hook   │ PreToolUse hook (Bash)
          ▼                   ▼   + workspace-write sandbox
   ┌──────────────────────────────────┐
   │  Judge Service (localhost HTTP)  │
   │  1. policy.yaml fast path        │
   │     (regex allow/deny)           │
   │  2. LLM classifier fallback      │
   │     (cheap model via LiteLLM)    │
   │  3. fail-closed on timeout/error │
   │  → audit log (JSONL)             │
   └──────────────────────────────────┘
```

### 4.1 Components

**Pipeline Engine.** Loads a pipeline YAML, materializes it into a sequence of typed steps, executes them with checkpointing. Step types in v1: `agent_task`, `adversarial_cycle`, `human_gate`, `shell` (run tests/linters), `commit`, `retrospective`. Each step reads/writes named artifacts (e.g., `prd.md`, `plan.md`, `findings.json`, `triage.json`).

**Agent Adapters.** A single interface:

```python
class AgentAdapter(Protocol):
    def run(self, prompt: str, *, session: str | None,
            schema: dict | None, cwd: Path,
            extra_flags: list[str]) -> AgentResult
    # AgentResult: text, structured (parsed JSON if schema given),
    #              session_id, usage (tokens/cost if reported),
    #              raw_events (full event stream), exit_code
```

- `ClaudeCodeAdapter`: wraps `claude -p` with `--output-format json` (or `stream-json` for live transcript capture), `--resume <session>` for multi-turn continuity within a gate, `--append-system-prompt` for project guiding principles, `--allowedTools`/`--permission-mode acceptEdits`. Never uses the permission-bypass flag (it disables hooks).
- `CodexAdapter`: wraps `codex exec` with `--json`, `--output-schema <file>` for structured findings, `-o` for final message, `codex exec resume <session>` for follow-ups, `--full-auto` + `--sandbox workspace-write` (hooks keep firing under full-auto). Uses `codex review --base <branch>` / `--uncommitted` for the phase-review step where appropriate.
- `ApiAdapter`: LiteLLM completion calls for non-agentic tasks (no file access needed): triage classification, judge LLM fallback, retro summarization, commit-message drafting. Enforces JSON-schema outputs via response-format/parsing with retry.

**Run State Manifest.** One JSON file per run: pipeline version + hash, current step index, per-step status (pending/running/passed/failed/skipped), agent session IDs, git SHA at each commit checkpoint, artifact paths, accumulated token/cost usage. Every state transition is written before and after step execution (write-ahead) so `gauntlet resume` is always safe.

**Transcript Logger.** See §7.

**Judge Service.** See §8.

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Orchestration layer | Custom thin Python, not Hermes/OpenClaw/LangChain | Workflow is a deterministic state machine with LLM gates; agent platforms optimize for the wrong shape (channels, heartbeats, memory) |
| Agent integration | Drive CLIs headlessly, not raw model APIs | Claude Code/Codex already provide file editing, git, sandboxing, session continuity; rebuilding that on LiteLLM discards the hard parts |
| Cheap-task routing | LiteLLM ApiAdapter | Triage/judging are classification, not agency — no repo access needed, so a $0.x/Mtok model suffices |
| State | Flat JSON manifest per run (no SQLite in v1) | Human-inspectable, diffable, trivially committable; revisit if concurrent runs become common |
| Safety enforcement | PreToolUse hooks → shared judge, both CLIs | Same stdin-JSON / exit-code-2 contract on both; hard pre-execution blocking; one judge brain |
| Codex non-Bash gap | `--sandbox workspace-write` backstop | Codex hooks reliably cover Bash; sandbox physically confines file edits the hook may not see |
| Failure posture | Fail-closed everywhere (judge timeout = deny; step failure = halt + checkpoint) | Unattended operation must never default to "allow" |

---

## 5. Functional Requirements

### FR-1: Team rollout (Req 1)

- **FR-1.1** Install via `pipx install gauntlet` or `uv tool install gauntlet` from a private index or git URL. Single Python package; no system daemons.
- **FR-1.2** `gauntlet init` in a repo scaffolds, all under `.gauntlet/`: `config.yaml` (carrying `asset_root: .gauntlet`), `pipelines/standard.yaml`, `prompts/` (versioned prompt templates), `schemas/`, and `policy.yaml` (judge rules), plus the hook wiring into `.claude/settings.json` and the repo-level Codex hooks config (those two paths are dictated by the CLIs and necessarily stay outside `.gauntlet/`). All of it is committable, so a teammate who clones the repo gets the identical workflow. The engine resolves every asset path under the config's `asset_root` (default `"."` = the repo root — which is how Gauntlet's *own* source repo keeps its pipelines/prompts/schemas/policy as first-class top-level files); the init-scaffolded `asset_root: .gauntlet` is what consolidates an adopter project's tool files under one dotfile dir, out of the project's source tree.
- **FR-1.3** `gauntlet doctor` validates the environment: CLIs installed and authenticated (`claude`, `codex`), hook files present and trusted, judge service startable, API keys present for ApiAdapter models. Exit non-zero with actionable messages.
- **FR-1.4** Per-user secrets (API keys) live in env/keychain, never in repo config. Repo config references models, not credentials.
- **FR-1.5** Pin behavior: config records minimum tested versions of `claude` and `codex`; `doctor` warns on mismatch (both CLIs change fast; hook semantics have shifted across releases).

**Acceptance:** A teammate with the two CLIs authenticated can go from `git clone` to a running pipeline with ≤ 3 commands (`pipx install`, `gauntlet doctor`, `gauntlet run`).

### FR-2: Per-step model/service assignment (Req 2)

- **FR-2.1** Every pipeline step declares `agent:` referencing a named agent profile in `config.yaml`. Profiles bind adapter + model + flags:

```yaml
# .gauntlet/config.yaml
agents:
  builder:
    adapter: claude-code
    model: claude-opus-latest          # passed via --model
    permission_mode: acceptEdits
    allowed_tools: [Bash, Read, Write, Edit, Grep, Glob]
  reviewer:
    adapter: codex
    model: gpt-5.5
    sandbox: workspace-write
    full_auto: true
  triage:
    adapter: api                       # LiteLLM
    model: claude-haiku-latest
  judge_llm:
    adapter: api
    model: gpt-5.4-mini
```

- **FR-2.2** Swapping builder/reviewer (Claude codes + Codex reviews, or vice versa) is a two-line edit to agent profiles or a per-step override (`agent: reviewer_claude`). No code changes.
- **FR-2.3** Adapter capabilities are validated at load: a step requiring repo write access cannot bind to `api` adapter; a step requiring `--output-schema` warns if the adapter only supports best-effort JSON.
- **FR-2.4** New adapters register via a Python entry point (`gauntlet.adapters`), so a future Gemini-CLI or internal-agent adapter is a plugin, not a fork.

**Acceptance:** Flip builder and reviewer roles between Claude Code and Codex by editing only YAML; run completes with roles reversed; transcripts show the reassignment.

### FR-3: Token-efficient routing (Req 3)

- **FR-3.1** Default profile mapping ships with: frontier model for implementation; strong model for adversarial review; small/cheap model for triage classification, judge fallback, retro summarization, and commit messages.
- **FR-3.2** Every `AgentResult` records token usage and (when derivable) cost; the run manifest accumulates totals per step and per agent profile. `gauntlet report <run>` prints a cost breakdown table.
- **FR-3.3** Per-step `max_turns` / timeout / budget guards: a step exceeding its budget halts the run at a checkpoint rather than burning unbounded tokens (stuck headless agents run until killed — every CLI invocation is wrapped in a timeout).
- **FR-3.4** Prompt templates for cheap-model tasks are written for small models: rubric-first, few-shot, strict JSON schema — to keep classification reliable at the low-cost tier.

**Acceptance:** A full run's cost report shows triage/judge/retro steps individually costing < 5% of total run cost; budget-guard test demonstrates a halted (not runaway) step.

### FR-4: Complete logging, organized by PRD (Req 4)

- **FR-4.1** Directory layout (lives in-repo by default so it can be source-controlled; configurable to an external path):

```
.gauntlet/
  runs/
    <prd-slug>/                      # e.g. invoice-export/
      prd.md                         # the approved PRD (canonical copy)
      plan.md                        # approved implementation plan
      run-2026-06-10T14-22-03/
        manifest.json                # state machine checkpoint + cost totals
        steps/
          010-prd-review-r1/
            prompt.md                # exact prompt sent
            transcript.md            # human-readable rendering of all messages
            events.jsonl             # raw event stream from the CLI/API
            findings.json            # structured output (when schema'd)
          020-prd-triage-r1/
            ...
        judge-audit.jsonl            # every allow/deny decision + rationale
        retro/
          feedback.md                # human feedback captured at run end
          proposals/                 # see FR-6
```

- **FR-4.2** Transcripts capture **every message from every agent**: prompts, assistant turns, tool calls + tool results (from the JSON/stream-JSON event streams), and final outputs. Nothing summarized away; `transcript.md` is a faithful rendering, `events.jsonl` is the lossless record.
- **FR-4.3** A generated `RUN.md` index per run links every step's transcript, its verdict, duration, and cost — designed to be readable in a git forge UI.
- **FR-4.4** Secrets hygiene: the logger redacts values of known secret env vars and obvious credential patterns before writing; redaction list is configurable. (Default-on, because these logs are intended for git.)
- **FR-4.5** `.gitignore` guidance generated by `init`: manifests and transcripts committable; an optional `events.jsonl` exclusion pattern for teams that find raw streams too heavy.

**Acceptance:** After a run, a reviewer who was not present can reconstruct every decision (what the reviewer flagged, how triage classified each finding, what was fixed, what the judge blocked) using only files under `.gauntlet/runs/<prd-slug>/`.

### FR-5: Flexible, extensible pipeline (Req 5)

- **FR-5.1** Pipelines are YAML documents; the default `pipelines/standard.yaml` encodes the current 3-gate workflow:

```yaml
name: standard
version: 3            # bumped by retro proposals (FR-6)
stages:
  - id: prd
    # ENTRY CONTRACT: gauntlet does not author PRDs. The human writes and
    # iterates prd.md (typically interactively with their agent of choice)
    # until THEY are satisfied; `gauntlet run` requires runs/<slug>/prd.md
    # to exist and begins at adversarial review. (FR-10)
    steps:
      - {id: prd-cycle,    type: adversarial_cycle, artifact: prd.md,
         reviewer: reviewer, triager: triage, fixer: builder,
         max_rounds: 2, findings_schema: schemas/findings.json}
        # reviewer hunts for gaps, ambiguities, unstated assumptions,
        # untestable requirements — review-until-satisfied within max_rounds
      - {id: prd-approve,  type: human_gate, show: [prd.md, last_triage]}
        # human ratifies the post-review PRD before any plan is generated
  - id: plan
    steps:
      - {id: plan-author,  type: agent_task, agent: builder,
         prompt: prompts/plan-author.md, inputs: [prd.md], output: plan.md}
      - {id: plan-cycle,   type: adversarial_cycle, artifact: plan.md,
         reviewer: reviewer, triager: triage, fixer: builder, max_rounds: 2}
      - {id: plan-approve, type: human_gate, show: [plan.md]}
  - id: phases
    foreach: plan.phases              # plan-author emits structured phase list
    steps:
      - {id: implement,  type: agent_task, agent: builder,
         prompt: prompts/implement-phase.md, inputs: [prd.md, plan.md]}
      - {id: tests,      type: shell, run: "{{config.test_command}}",
         on_fail: {route_to: implement, max_retries: 2}}
      - {id: phase-commit, type: commit, message_agent: triage}
        # clean worktree + commit BEFORE every reviewer handoff (FR-9.3):
        # single-line header `PN: ...` + blank line + detailed body
      - {id: impl-cycle, type: adversarial_cycle, mode: code_review,
         reviewer: reviewer, triager: triage, fixer: builder,
         max_rounds: 2,
         commit_each_fix_round: true,   # `PN.x: Address review — ...` (FR-9.4)
         confirm_via: commit_range_diff} # confirm pass sees only the round's
                                         # diff + its findings (FR-9.5)
  - id: retro
    steps:
      - {id: retrospective, type: retrospective, agents: [builder, reviewer]}
```

- **FR-5.2** The `adversarial_cycle` step type is the reusable primitive (review → structured findings → point-by-point triage into `legitimate | bikeshedding | premature_optimization | not_applicable` → apply accepted → confirm). It is configuration, not code, at the pipeline level.
- **FR-5.3** Adding a step (e.g., a security-scan pass, a docs-update pass, a second reviewer model for consensus) = adding a YAML block + a prompt template. Steps compose via named artifacts; the engine validates the artifact dataflow at load time and rejects dangling references.
- **FR-5.4** Conditional execution (`when:`), fan-out (`foreach:`), failure routing (`on_fail:`), and per-step overrides (agent, budget, rounds) are first-class step attributes.
- **FR-5.5** Custom step types may be registered via entry point for logic that genuinely can't be expressed declaratively.
- **FR-5.6** Pipeline files are versioned (`version:` + content hash in the manifest), so any historical run is reproducible against the exact pipeline that produced it.

**Acceptance:** Add a "third-round security review using a different model" to one stage by editing YAML only; engine validates and runs it; transcripts/cost report show the new step.

### FR-6: Improvement loop — human feedback + governed self-improvement (Req 6)

- **FR-6.1** **Feedback capture.** At run end (and on `gauntlet feedback <run>` anytime later), the CLI prompts the human for: outcome rating, what the reviewers missed, what triage got wrong (false "legitimate" / false "bikeshedding"), and freeform notes. Stored as `retro/feedback.md`.
- **FR-6.2** **Agent retrospective.** The `retrospective` step feeds each agent a condensed run summary (its own findings, triage verdicts on them, test failures, human feedback) and asks for self-critique: which review comments were validated/invalidated downstream, which prompt instructions were ignored or misread.
- **FR-6.3** **Proposal generation.** A cheap-model synthesis pass converts feedback + retrospectives into **concrete diffs** against versioned assets: prompt templates (`prompts/`), pipeline parameters (e.g., `max_rounds`), triage rubric examples, and judge `policy.yaml` rules (e.g., "this command class was asked about 14 times and always allowed — propose a fast-path allow rule"). Proposals land in `retro/proposals/NNN-<slug>.md` with rationale + the literal diff.
- **FR-6.4** **Governed application.** `gauntlet proposals review` presents pending proposals; the human approves/rejects each. Approved diffs are applied and committed with the proposal as the commit body. **No proposal self-applies.** This mirrors a governed-reconciliation pattern: automation drafts, humans ratify.
- **FR-6.5** **Cross-run learning corpus.** Approved proposals and their rationales accumulate in `prompts/CHANGELOG.md`; triage few-shot examples are drawn from human-corrected cases (FR-6.1), so the cheap triage model improves on exactly the errors humans corrected.
- **FR-6.6** **Metrics to steer improvement** (in `gauntlet report --trend`): findings per review round, % findings triaged legitimate, % accepted fixes that survive the confirm pass, test-failure loops per phase, judge ask-rate, cost per phase. Trend lines tell you whether prompt changes are actually helping.

**Acceptance:** After a run with deliberately-injected triage errors, human feedback marking them produces at least one concrete prompt-diff proposal; approving it updates the template + changelog; the next run uses the new version (visible in manifest's prompt hashes).

### FR-7: Safety judging (carried from design discussion)

- **FR-7.1** Localhost judge service started by `gauntlet run` (and stopped after): FastAPI, single `/decide` endpoint receiving the hook payload `{tool_name, tool_input}` plus run context (run id, step id, repo root).
- **FR-7.2** Decision ladder: (1) deterministic `policy.yaml` — regex/glob allow & deny lists evaluated deny-first; (2) LLM classifier (`judge_llm` profile) only for non-matching commands, with a rubric prompt returning `{decision, risk_category, rationale}`; (3) **fail-closed**: timeout, parse error, or service-down ⇒ deny. Target p50 latency < 150 ms on the fast path; LLM fallback bounded well under both CLIs' hook timeouts.
- **FR-7.3** Wiring: Claude Code `PreToolUse` hook (all tools) → judge; permission mode `acceptEdits`; **never** the permission-bypass flag (it disables hooks). Codex `PreToolUse` hook (Bash) → same judge; `--full-auto` (hooks keep firing) + `--sandbox workspace-write` as the backstop for non-Bash tool gaps.
- **FR-7.4** Deny responses include a one-line rationale surfaced back to the agent (stderr / `permissionDecision: deny` reason) so the agent can route around the block instead of retrying it blindly.
- **FR-7.5** Every decision (allow/deny/source: fast-path|llm, latency, rationale) appended to `judge-audit.jsonl` for the run.
- **FR-7.6** Default deny list ships with: force-push, history rewrite on shared branches, `rm -rf` outside repo, package publish, credential file reads outside repo, outbound network beyond an allowlist, `curl|sh` patterns. Default ask→LLM categories: package installs, migrations, file deletion in bulk.

**Acceptance:** Red-team script of 25 dangerous commands run through each agent: 100% blocked pre-execution with audit entries; benign command suite shows ≥ 90% resolved on the deterministic fast path.

### FR-8: Run lifecycle & resumability

- **FR-8.1** `gauntlet new <prd-slug>` (scaffold PRD dir) → `gauntlet run <prd-slug>` → pauses at `human_gate` steps → `gauntlet approve` / `gauntlet reject --notes` → continues → `gauntlet status`, `gauntlet resume`, `gauntlet abort`.
- **FR-8.2** Write-ahead manifest updates; on crash/kill, `resume` re-enters at the last incomplete step, reusing recorded agent session IDs (`--resume` / `exec resume`) where the adapter supports it, restarting the step cleanly where it doesn't.
- **FR-8.3** Each phase `commit` step records the git SHA in the manifest; `gauntlet rollback --phase N` resets the PRD branch to a phase boundary per FR-9.9.
### FR-9: Source control model — one PR per PRD

- **FR-9.1** **Branching.** `gauntlet run <slug>` creates (or resumes on) a dedicated branch `gauntlet/<slug>` off the configured base branch. All work for the PRD — every phase, every review fix — stays on this single branch. No per-phase branches: phases are sequential and the PR is the unit of review, so additional branches add ceremony without enabling anything.
- **FR-9.2** **Phase commit (pre-review handoff).** At the end of each file-changing phase, the `commit` step commits *before* the reviewer sees the work, with an enforced message format:
  - Line 1: single-line imperative header ≤ 72 chars, prefixed with the phase: `P3: Add write-ahead manifest checkpointing`
  - Blank line, then a detailed body: what changed and why, which plan assumptions this phase validated, spec/PRD section references, known deferrals.
  - Drafted by the `message_agent` (cheap model) from the phase diff + plan section; validated against the format by the engine (reject + redraft on violation).
- **FR-9.3** **Commit before every review handoff.** The invariant is: *the worktree is clean and committed at every point where control passes to a reviewer.* This holds for the initial phase commit and for every fix round. Rationale:
  1. Reviewers sometimes mutate the worktree instead of (or in addition to) reporting findings. A clean pre-handoff commit makes any such mutation detectable as a dirty worktree and attributable to the reviewer (see FR-9.7).
  2. The *reason* for each review-driven change belongs in commit history, exactly as it would in a human PR.
  3. The confirm pass becomes a precise diff: "here is `git diff <pre-fix>..<post-fix>` — were your findings addressed?" rather than re-reviewing the whole phase.
- **FR-9.4** **Review-fix commits are first-class history (no squashing).** Each accepted fix round produces a commit in the same header+body format:
  - Header: `P3.1: Address review — <short summary>` (round number suffixed).
  - Body: per-finding entries — finding ID, the reviewer's claim (condensed), the triage verdict and reasoning, and what was changed in response. Findings triaged `bikeshedding`/`premature`/`not_applicable` are listed as explicitly **declined**, with the triage reasoning — declining with a recorded reason is part of the audit trail too.
  - Drafted by the `message_agent` from `findings.json` + `triage.json` + the fix diff; format-validated like phase commits.
  - Result: branch history reads as a faithful PR conversation — `P3: <work>` → `P3.1: Address review — ...` → `P3.2: ...`. Teams that want a clean base-branch history use squash-merge at PR time; the PRD branch (and the PR itself) preserve the granular story. (`phase_commit_style: squash` remains available as a non-default config for teams that insist on pre-PR squashing, but it forfeits FR-9.3's benefits #2 and #3.)
- **FR-9.5** **Diff-scoped confirm pass.** The confirm step of each `adversarial_cycle` round sends the reviewer the commit-range diff (`<handoff-sha>..<fix-sha>`) plus its own prior findings and the triage verdicts — scoped, cheap, and unambiguous. For the Codex adapter this maps to `codex review` against the range / `--commit`; for other adapters the diff is embedded in the prompt. The reviewer's verdict is per-finding: `resolved | partially_resolved | unresolved | regression_introduced`.
- **FR-9.6** **Reviewer-mutation guard.** Review steps are intended to be read-only, and adapters request that (Codex `--sandbox read-only` for pure review steps; Claude Code review profile without Write/Edit tools). Defense in depth: after every review step the engine checks `git status`; if the reviewer modified the worktree anyway, policy `reviewer_mutation: commit | revert | halt` (default `commit`) applies — `commit` records the changes as a clearly reviewer-attributed commit (`P3.r1: Reviewer-applied changes — <summary>`, author set to the reviewer agent identity) so nothing is silently lost and triage can evaluate the reviewer's edits like any other proposed change; `revert` restores the handoff SHA and converts the mutation into a finding; `halt` parks for a human.
- **FR-9.7** **Agent commit identity.** Commits are authored with per-agent identities (`Gauntlet Builder (claude) <builder@gauntlet.local>` etc., configurable), so `git log`/`git blame` distinguish builder work, fix rounds, and reviewer-applied changes at a glance.
- **FR-9.8** **PR creation.** When the final phase gate passes, Gauntlet drafts (does not open) a PR description into `runs/<slug>/PR.md`: PRD summary, per-phase commit list including fix rounds, links to run transcripts and final per-finding verdicts. Opening the PR and pushing remain human actions in v1 (consistent with §2.2 non-goals). An optional `gauntlet pr open` convenience (gh CLI) is a v1.1 candidate.
- **FR-9.9** **Rollback** is `git reset --hard <phase-N final sha>` on the PRD branch (guided, with confirmation), plus manifest rewind to the matching checkpoint — branch and manifest never disagree about where the run stands. Phase boundaries for rollback are the *post-cycle* SHAs (phase commit + its accepted fix commits).

**Acceptance:** A completed run yields branch `gauntlet/<slug>` where every reviewer handoff occurred on a clean committed worktree; each phase shows `PN:` followed by zero or more `PN.x:` fix commits whose bodies carry finding IDs, triage verdicts (including declined findings with reasons); the confirm pass for each round demonstrably consumed only the round's commit-range diff; a simulated reviewer worktree mutation is detected and handled per configured policy with reviewer-attributed authorship; rollback to phase N-1 leaves branch SHA and manifest consistent.

### FR-10: Stage progression contract

- **FR-10.1** **Entry contract.** `gauntlet run <slug>` requires a human-authored `runs/<slug>/prd.md` and refuses to start without it (`gauntlet new` scaffolds the stub; authoring is a human activity, optionally assisted interactively outside gauntlet). The harness never generates a PRD from scratch.
- **FR-10.2** **Strict stage gating.** Plan generation does not begin until the PRD has passed its review loop *and* its human gate. Phase implementation does not begin until the plan has passed its review loop and human gate. No look-ahead, no speculative work against unapproved upstream artifacts.
- **FR-10.3** **Sequential phases.** Phases execute strictly one-at-a-time in plan order. Phase N+1 starts only after phase N's tests pass, its review loop concludes, and its final commit lands. (Parallel phases are explicitly out of scope for v1; the plan format may mark independent phases for a future scheduler.)
- **FR-10.4** **Upstream invalidation.** If a downstream stage surfaces a defect in an approved upstream artifact (e.g., a phase reveals the plan — or the PRD — was wrong), the run halts at a human gate with the finding rather than silently amending approved artifacts. The human chooses: amend the artifact (re-runs its review loop and gate, then resumes), or accept a recorded deviation note. Approved artifacts change only through their own loop + gate.
- **FR-10.5** **"Satisfied" is defined.** A review loop concludes when the reviewer's confirm pass reports no `blocking`/`unresolved` findings, or `max_rounds` is reached — in which case unresolved blockers escalate to a human gate instead of being silently carried forward.

**Acceptance:** Attempting `gauntlet run` without `prd.md` fails with guidance; instrumented run shows zero plan-stage activity before PRD approval and zero phase-stage activity before plan approval; a seeded plan defect discovered in phase 2 parks the run at a human gate with the finding attached; a review loop exhausting `max_rounds` with an open blocker escalates rather than proceeding.





```
gauntlet init [--from-repo]        # scaffold config/hooks into current repo
gauntlet doctor                    # environment + version + auth validation
gauntlet new <slug>                # create runs/<slug>/ with PRD stub
gauntlet run <slug> [--pipeline standard] [--from-step ID]
gauntlet status [<slug>]
gauntlet approve <slug> [--gate ID] / reject --notes "..."
gauntlet resume <slug> / abort <slug>
gauntlet report <run> [--trend]    # cost + metrics
gauntlet feedback <run>            # capture/append human feedback
gauntlet proposals review          # approve/reject improvement diffs
gauntlet rollback <slug> --phase N
```

---

## 7. Data & Schemas (normative excerpts)

**findings.json** (reviewer output, enforced via `--output-schema` on Codex; schema-prompt + validation/retry on other adapters):

```json
{ "findings": [ {
    "id": "F-001",
    "severity": "blocking | major | minor | nit",
    "category": "correctness | spec-gap | security | performance | principle-violation | style",
    "location": "file/section reference",
    "claim": "what is wrong",
    "evidence": "why the reviewer believes it",
    "suggested_fix": "optional"
} ] }
```

**triage.json** (triager output, one verdict per finding — the point-by-point evaluation):

```json
{ "verdicts": [ {
    "finding_id": "F-001",
    "verdict": "legitimate | bikeshedding | premature_optimization | not_applicable",
    "reasoning": "1-3 sentences",
    "action": "fix_now | defer | reject"
} ] }
```

**manifest.json**: `{pipeline: {name, version, hash}, prompt_hashes, status, current_step, steps: [{id, status, started, ended, agent, session_id, usage}], commits: [{phase, sha}], totals: {tokens, cost}}`.

---

## 8. Security & Privacy

- Judge service binds to 127.0.0.1 only; per-run shared token in hook env to reject foreign callers.
- Fail-closed posture throughout (FR-7.2); hook-disabling flags are lint-checked out of agent profiles at config load.
- Transcript redaction (FR-4.4) before any write, since logs target source control.
- Codex runs sandboxed `workspace-write`; network grants are explicit per-profile config, default off.
- Prompt-injection containment: reviewer findings are *data* to the triager (wrapped, never executed); judge decisions never derive from agent-authored text alone.

---

## 9. Implementation Plan (phased, assumption-validating)

Each phase ends with passing tests and a commit. Phases are ordered to kill the riskiest assumptions first.

| Phase | Deliverable | Assumption validated |
|-------|-------------|----------------------|
| **P1** | Adapters + golden-path smoke: run one prompt through each of `claude -p`, `codex exec`, LiteLLM; parse JSON; capture session IDs; record usage | Both CLIs are reliably scriptable on the team's actual versions; structured output is parseable |
| **P2** | Judge service + hook wiring + red-team suite | Pre-execution blocking works on both CLIs as documented on installed versions (hook semantics shift between releases — this is the riskiest external dependency) |
| **P3** | Pipeline engine: YAML load/validate, `agent_task`, `shell`, `human_gate`, `commit`, manifest checkpoint/resume | The state-machine + write-ahead design survives kill -9 mid-step |
| **P4** | `adversarial_cycle` step + findings/triage schemas + transcript logger | Cheap-model triage is accurate enough (measure against a hand-labeled set of ~30 findings; threshold ≥ 85% agreement) |
| **P5** | Full `standard` pipeline end-to-end on a toy repo; cost report | The whole loop converges within configured rounds and budget on a real (small) PRD |
| **P6** | `init`/`doctor`/rollout packaging; second-machine install test | A teammate can onboard in ≤ 3 commands |
| **P7** | Retro step, feedback capture, proposal generation + governed apply | Proposals are concrete and useful (human accepts ≥ 1 real proposal from a real run) |

---

## 10. Success Metrics

- **Human time:** author/review time only; zero human turns between plan approval and phase commits (excluding configured gates) on ≥ 80% of runs.
- **Safety:** 0 dangerous-command executions in red-team suite; judge fast-path resolution ≥ 90%.
- **Cost:** classification steps ≤ 5% of run cost; full cost attribution on 100% of runs.
- **Quality proxy:** % of reviewer "blocking" findings in round 2 trending down across runs (the cycle converges); post-run human-discovered defects trending down.
- **Adoption:** ≥ 2 teammates running pipelines from repo config within a month of v1.

## 11. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| CLI flag/hook semantics drift across releases | `doctor` version pinning + P1/P2 contract tests run in CI of the gauntlet repo itself |
| Reviewer/triager collusion drift (triage rubber-stamps everything as bikeshedding) | FR-6.6 metrics (legitimate-rate trend) + periodic human spot-audit prompts at gates |
| Cheap triage model misclassifies | Few-shot corpus grown from human corrections (FR-6.5); per-category escalation to a stronger model when confidence is low |
| Token blowups in fix loops | Per-step budgets + max_rounds + timeout wrappers (FR-3.3) |
| Log bloat in git | `events.jsonl` ignore option (FR-4.5); transcripts are the committable artifact |
| Judge becomes a latency tax | Fast-path-first design; latency recorded per decision; policy proposals (FR-6.3) migrate hot LLM decisions into deterministic rules |

## 12. Open Questions

~~1. Phase commits: dedicated branch vs. current branch~~ — **Resolved:** one PR per PRD on a dedicated `gauntlet/<slug>` branch; single clean commit per phase with header+body format; review-fix commits preserved as first-class history (squash-merge optional at PR time). See FR-9.

1. Consensus review (two reviewer models, union or intersection of findings) — v1.1 candidate; the pipeline schema already permits it.
2. Per-org shared prompt/policy repo (`gauntlet init --from git@...`) for multi-repo teams — needed before broad rollout?
3. Cost reporting for subscription-auth CLI usage (no per-token billing surface) — report tokens only and estimate?
4. Windows support: Codex hook reliability on Windows shells is reported flaky; v1 targets macOS/Linux, document Windows as unsupported?

---

*End of PRD v1.0*

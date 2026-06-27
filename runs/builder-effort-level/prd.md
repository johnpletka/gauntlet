# PRD: Per-stage model & effort tiering for pipeline agents

**Status:** Draft v0.7
**Author:** John Pletka
**Date:** 2026-06-27
**Working name:** builder-effort-level
**Relationship to existing artifacts:** Does **not** amend any approved artifact (`PRD-gauntlet.md`, `policy.yaml`, or any `runs/*/` artifact marked approved). It tunes the hand-authored `.gauntlet/config.yaml` and pipeline YAML, and *optionally* touches one engine function (`needs_escalation` in `src/gauntlet/engine/cycle.py`). Builds on existing machinery: agent profiles and per-step agent binding (FR-2.1/2.2), the three-tier model strategy (FR-3.1), per-step/per-agent cost recording in the manifest (FR-3.2), the triage + severity-aware escalation loop (FR-5.2 / review F-009), and commit-identity attribution (FR-9.7).

---

## §1 Overview

### 1.1 Problem statement

Gauntlet binds an adapter + model per agent profile, and each adapter (`claude-code`, `codex`, `api`) already accepts an effort knob (`effort` / `reasoning_effort`). But the live `.gauntlet/config.yaml` sets a **model** on every profile and an **effort on none of them** — only the judge runner pins `reasoning_effort=minimal` in code. So builder, reviewer, triage, and escalation all run at their provider's *default* effort, chosen by nobody. The result is two silent inefficiencies: the cheap classification stages may be over-thinking, and the expensive reasoning stages are not deliberately dialed up where quality matters.

A second, related gap: a single `builder` profile serves three different cognitive loads — authoring the plan (synthesis), implementing each phase (the core deliverable), and applying triaged fixes. They share one model and one effort even though the right tradeoff differs per job.

This matters because effort and model are the primary levers on both run cost and output quality, and right now they are left to defaults rather than chosen against each stage's actual job.

### 1.2 Solution summary

Make model and effort a deliberate, per-stage choice, expressed entirely in configuration:

1. Set an explicit `effort` / `reasoning_effort` on every pipeline agent profile (judge stays pinned at `minimal`).
2. Split the builder into two role-named profiles — `builder` (planning/authoring) and `builder_impl` (implementation phase: the `implement` step, the cycle `fixer`, and `phase-commit` authorship) — so each can carry its own model and effort.
3. Pilot a cheaper builder model (Sonnet) for `builder_impl` only, and **decide by measurement** using the cost data the manifest already records, not by assertion.

The per-step model differentiation requires **no engine change** — step→profile resolution is fully data-driven (`step.agent` → `ctx.build_adapter(name)` → `config.profile(name)`; verified in `handle_agent_task` at `steptypes.py:191` and `config.py:188`, with unknown names raising a loud `KeyError`). The only *optional* code change is widening the escalation trigger (FR-6), which is gated behind its own decision.

### 1.3 The assumption this validates

**The riskiest belief:** that a cheaper, faster builder model (Sonnet at `high` effort) on the `implement`/`fixer` steps — given an *already adversarially-reviewed, human-approved plan* upstream and a *strong cross-family review loop* (gpt-5.5 + triage + gpt-5 escalation) downstream — converges in the **same number of review rounds with no increase in human-gate parks**, *and* lowers total cycle cost. The failure mode this must disprove: a weaker first pass produces more findings, each extra round re-runs the expensive reviewer + escalation + fixer, and the per-token savings are erased (or worse) by the round multiplier. Per-token price is not the unit of success; **rounds-to-converge and total cycle cost are.**

The *mechanism* — per-stage model+effort as a config-only, re-balanceable choice — is the durable deliverable and holds its value regardless of how this particular bet resolves. "Keep Opus everywhere at this time" is a **supported outcome, not a failure**: it means the knob exists and landed on a value. The right balance is expected to shift as models and pricing change, and to differ between a cost-insensitive user (latest model everywhere) and a cost-sensitive one (cheapest everywhere). See §2.1 G6 and the §9 decision rule.

---

## §2 Goals and Non-Goals

### 2.1 Goals

| ID | Outcome | Need it serves |
|----|---------|----------------|
| G1 | Every non-judge pipeline agent runs at a deliberately chosen effort level | Removes the silent "provider default" effort; makes the cost/quality tradeoff inspectable |
| G2 | Implementation-phase work uses a profile distinct from planning, so its model/effort can differ | Lets the cheapest viable builder run implementation without downgrading plan authoring |
| G3 | A cheaper implementation builder is adopted **only if** it holds review-round convergence and reduces cost | Turns the Sonnet-vs-Opus question into a data decision (FR-3.2), not a guess |
| G4 | Phase-commit authorship reflects the profile that actually did the work | Keeps the git provenance honest for the A/B and for FR-9.7 |
| G5 | The change is config-only for the core scope (no required engine edit) | Determinism / auditability: the behavior is in committed YAML, not new code paths |
| G6 | Per-stage model+effort is a configuration choice re-balanceable as models and cost priorities change — including **uniform** assignments (all-frontier or all-cheap) and mixed — with no engine change | Durability: the optimal balance shifts as models/pricing evolve and varies by how much a given user values cost vs. quality. The deliverable is the knob, not one setting of it |

### 2.2 Non-Goals (v1)

- **Not** switching every stage to a cheaper model. Planning, code review, and escalation stay on their current strong models; only the implementation builder is piloted cheaper.
- **Not** changing the FR-3.1 model-tiering strategy (frontier builder / strong cross-family reviewer / cheap triage). Models are unchanged except the `builder_impl` pilot.
- **Not** building a per-step *effort* override field on `Step`. Profiles + existing per-step `agent:` binding already express every (model, effort) combination needed; adding more profiles is the more inspectable answer (determinism over cleverness). Explicitly retracted from an earlier proposal.
- **Not** expanding the builder's `allowed_tools`. `TodoWrite` and `MultiEdit` do not exist in the pinned `claude` build, and the task-tracking equivalent (`Task*`) is a deferred family requiring `ToolSearch` — a broader, harder-to-gate surface than is warranted for a plan-driven builder. The tool surface stays `[Bash, Read, Write, Edit, Grep, Glob]`.
- **Not** changing the judge classifier's effort. It remains pinned at `minimal` in the runner (a tuned, latency-bound value).
- **Not** splitting the implementation `fixer` from first-pass `implement` into separate profiles (yet). Considered, deferred — see OQ.
- **Not** automatic or dynamic model selection **in v1** (cost-/availability-based routing, auto-fallback between tiers, "pick the cheapest that passes"). This is a v1 scope boundary, **not a permanent prohibition** — a future routing feature would arrive via its own PRD that builds on this one and would **not** amend it. The narrow *standing* invariant (from `PRD-gauntlet.md`'s trust model) is only this: model/effort assignment is **human-committed configuration** and an **agent never self-selects its own model**. Engine-side routing driven by human-committed rules is consistent with that invariant and is a legitimate future direction; only agent-side self-selection would conflict — and that conflict is with the canonical spec, not with this PRD.
- **Not** amending `CLAUDE.md`, `PRD-gauntlet.md`, or any approved artifact.

---

## §3 Users and Personas

- **The operator** who launches and supervises a `gauntlet run` — wants predictable cost and quality, and a clean before/after to judge the Sonnet pilot.
- **The pipeline agents themselves** (builder, reviewer, triager, escalation) — consumers of the profiles; their model/effort is set here.
- **Future-you debugging a run** — reads the manifest's per-step cost and the git log's per-phase authorship to understand what each model did and what it cost.

---

## §4 System Architecture

### 4.1 Components

| Component | Change | New/Reused |
|-----------|--------|------------|
| `.gauntlet/config.yaml` (agent profiles) | Add `effort`/`reasoning_effort` to each profile; add `builder_impl` profile + its `identities` entry | Reused (config), new profile |
| Pipeline YAML (the run's pipeline) | Rebind `implement.agent`, `impl-cycle.fixer`, and `phase-commit.agent` to `builder_impl` | Reused |
| `src/gauntlet/engine/steptypes.py` / `config.py` / `cycle.py` | **No change** for the core: `step.agent → build_adapter → config.profile(name)` already resolves any profile name | Reused, unmodified |
| `src/gauntlet/engine/cycle.py` `needs_escalation` (`cycle.py:96`) | *Optional* (FR-6): widen escalation from `blocking` to `blocking`/`major` | The only code touch, gated |

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Profile naming | Role-based (`builder`, `builder_impl`), not model-based (`builder_sonnet`) | Model is the swappable implementation detail (FR-2.1/2.2); a model name in the role leaks it and fights the one-line swap. `_impl` mirrors the exposed `impl-cycle`/`implement` vocabulary and preserves the builder/reviewer dichotomy that `CLAUDE.md` and commit identities depend on |
| Triage effort | `medium`, not `low`/`minimal` | Triage is a legitimacy *judgment* (legitimate / bikeshedding / premature_optimization / not_applicable), not a rubric classification. Low effort degrades both the judgment and confidence calibration — and over-confidence *suppresses* the escalation backstop, which only fires on `blocking` or self-reported `low` confidence. Triage is also the cheapest stage by token volume, so the cost of `medium` is negligible against the value of a correct gate |
| Cheaper builder scope | Pilot Sonnet on `builder_impl` only; keep Opus on `builder` (planning) | Plan authoring is synthesis reasoning; per-phase implementation against a ratified plan is more local. The downstream review loop is the safety net that makes a cheaper first pass defensible here |
| Decide by data | A/B via the manifest (FR-3.2), not by assertion | The cost multiplier of extra review rounds can erase per-token savings; only rounds-to-converge + total cycle cost reveal the truth (data over inference) |
| Provenance | `phase-commit` rebound to `builder_impl` | The commit handler defaults authorship to `"builder"` (`steptypes.py:704`); leaving it unset would attribute Sonnet's work to the generic builder identity, muddying the A/B and violating FR-9.7 |

---

## §5 Functional Requirements

**FR-1 — Explicit effort on every non-judge agent profile.** Each of `builder`, `builder_impl`, `reviewer`, `triage`, `escalation` declares an explicit effort value (`effort` for `claude-code`, `reasoning_effort` for `codex`/`api`). `judge_llm` is exempt (pinned in the runner). *Acceptance:* inspection of the resolved config shows a non-null effort on all five profiles; the judge path is unchanged; a run completes with these set.

**FR-2 — Distinct implementation profile.** The implementation phase resolves to a profile (`builder_impl`) separate from the planning profile (`builder`). *Acceptance:* in the run manifest, the `implement` step and the `impl-cycle` `fixer` resolve to `builder_impl`, while `plan-author` resolves to `builder`; the manifest's per-step cost is attributed accordingly (FR-3.2).

**FR-3 — Triage at medium effort with escalation intact.** The `triage` profile runs at `medium`, and the existing escalation behavior is unchanged: a finding escalates when `severity == "blocking"` OR the triager reports `confidence == "low"`. *Acceptance:* `triage.reasoning_effort == "medium"`; a blocking finding and a low-confidence verdict each still route to the `escalation` agent (or the human gate when none is configured), per `needs_escalation` (`cycle.py:96`).

**FR-4 — Honest commit provenance.** A phase commit is authored by the identity of the profile that implemented the phase, not the generic `builder` default. *Acceptance:* `phase-commit.agent == builder_impl`, a `builder_impl` entry exists in `identities`, and the resulting phase commit in `git log` carries the `builder_impl` author identity (FR-9.7).

**FR-5 — Config-only core.** The effort settings, the `builder_impl` profile, and the three rebinds require no change to engine Python. *Acceptance:* the diff for this scope (excluding the optional FR-6) touches only `.gauntlet/config.yaml` and pipeline YAML; the existing test suite passes with no edits under `src/gauntlet/`.

**FR-6 — (Optional, gated) Wider escalation for contested majors.** If adopted, `needs_escalation` escalates when `severity in ("blocking", "major")` OR `confidence == "low"`, so the cheap triager no longer has sole authority over contested `major` findings. *Acceptance:* a unit test asserts `needs_escalation("major", {"confidence": "high"}) is True`; existing escalation tests still pass; this FR is implemented only if §11 OQ-3 resolves to "now."

**FR-7 — Tool surface unchanged.** The builder profiles' `allowed_tools` remains `[Bash, Read, Write, Edit, Grep, Glob]`. *Acceptance:* no `TodoWrite`/`MultiEdit`/`ToolSearch`/`Agent`/`Workflow`/`WebSearch`/`WebFetch` is added to any profile.

**FR-8 — Pinned-build capability verified before adoption.** The chosen model alias (`sonnet`) and the `--effort` flag are confirmed to resolve in the pinned `claude` build before `builder_impl` is bound into a run. *Acceptance:* `claude -p "Reply with exactly: ok" --model sonnet --effort high --output-format json` returns `is_error: false` / `exit 0`, and `modelUsage` shows `claude-sonnet-4-6` (no silent fallback). **Confirmed 2026-06-27:** both the `--model sonnet` and `--effort high` probes return `is_error:false`/`exit 0` on `claude-sonnet-4-6`.

---

## §6 Data & Schemas (normative excerpts)

**Agent profile (per adapter), in `.gauntlet/config.yaml`:**

```yaml
<profile-name>:
  adapter: claude-code | codex | api
  model: <alias or id>
  effort: low|medium|high|xhigh|max          # claude-code only (xhigh/max are Opus-tier)
  reasoning_effort: none|minimal|low|medium|high|xhigh   # codex / api (model-dependent subset)
  # …adapter-specific flags (permission_mode, sandbox, allowed_tools, …)
```

**Effort/model target state (the decisions to encode):**

| Profile | adapter / model | effort | binding constraint |
|---------|-----------------|--------|--------------------|
| `builder` | claude-code / opus | high | plan authoring (synthesis) |
| `builder_impl` | claude-code / **sonnet (pilot)** | high | per-phase implementation + fixer |
| `reviewer` | codex / gpt-5.5 | high | adversarial code review |
| `triage` | api / gpt-5-mini | medium | legitimacy judgment |
| `escalation` | api / gpt-5 | high | contested/blocking adjudication |
| `judge_llm` | api / gpt-5-mini | minimal (pinned in runner) | hook-latency budget |

**Manifest fields used for the decision (FR-3.2, existing):** per-step and per-agent token usage and cost — the inputs to the A/B in §9. The triage verdict schema already carries the `confidence` field that escalation keys on (`schemas/triage.json`); no schema change is introduced by this PRD.

---

## §7 Security & Privacy

- **No new capability surface.** `allowed_tools` is unchanged (FR-7); no network/egress tool, no `ToolSearch`, no sub-agent spawning is added. The judge continues to gate every builder tool call after P2 regardless of model/effort — model choice does not alter the threat model.
- **Fail-closed posture is preserved.** Profile-name resolution fails *loud*: an unknown `agent:` raises `KeyError: no agent profile named '...'` (`config.py:191`), and `agent_task` with no agent returns `FAILED` (`steptypes.py:192`) — there is no silent fallback to a wrong model. Triage parse failure still halts the cycle (does not default to "legitimate" or "drop"). The judge stays pinned at `minimal` (no change to its latency budget or deny-on-timeout default).
- **No secrets touched.** This is configuration of model/effort; it reads no credentials and changes no auth path.
- **Provenance is a privacy-adjacent integrity property:** FR-4 ensures git history attributes work to the model that produced it.

---

## §8 Implementation Plan (phased, assumption-validating)

Ordered riskiest-assumption-first; no phase depends on a later phase. Each ends in passing tests and a commit.

| Phase | Deliverable | Assumption it validates |
|-------|-------------|--------------------------|
| **P1** | Capability probe: confirm `sonnet` alias and `--effort` resolve in the pinned `claude` build; record results (FR-8) | The knobs exist in the pinned build at all — kills the "the CLI rejects this" risk before any wiring |
| **P2** | Set explicit effort on the five existing profiles (FR-1, FR-3); no new profile yet | Effort passthrough works end-to-end and a full run completes unchanged — independent of the model question |
| **P3** | Add `builder_impl` profile (pilot model = Sonnet) + `identities` entry; rebind `implement.agent`, `impl-cycle.fixer`, `phase-commit.agent` (FR-2, FR-4, FR-5) | Per-step profile binding and provenance work as designed; resolution is data-driven (no engine change) |
| **P4** | A/B measurement run: implementation phases on `builder_impl`=Sonnet vs an Opus baseline; compare rounds-to-converge, total cycle cost, and gate-parks from the manifest | **The §1.3 assumption** — Sonnet holds convergence and lowers cost |
| **P5** *(optional, gated on P4 + OQ-3)* | Widen escalation to contested majors (FR-6) + unit test | A stronger backstop is warranted only if P4/triage data shows the cheap triager is the weak link |

---

## §9 Success Metrics

*(Thresholds marked **[proposed]** are strawman values pending owner ratification — see OQ-2.)*

- **Coverage:** 100% of non-judge agent profiles carry an explicit effort value (binary).
- **Convergence parity:** across the P4 sample, `builder_impl`=Sonnet phases converge within the **same `max_rounds`** as the Opus baseline, with **no increase in human-gate parks**.
- **Cost reduction:** total cycle cost per implementation phase (implement + review + triage + escalation + fix, from the manifest) is reduced by **≥ 20% [proposed]** vs the Opus baseline.
- **No quality regression:** no increase in escalations-to-human or in post-merge defects attributable to the implementation model across the sample.
- **Decision rule (both branches are success):** adopt Sonnet for `builder_impl` iff convergence parity holds AND the cost-reduction threshold is met; otherwise set `builder_impl` to Opus (one-line rebind). **Either branch ships the feature** — the explicit effort levels, the planning/implementation profile split, and the ability to re-balance any stage by config (G1–G6) persist independent of which model is chosen. The assignment is expected to be revisited as models and pricing change; a future re-tune is a config edit, not a new project.

---

## §10 Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Sonnet's first pass yields more findings → extra review/fix rounds → the round multiplier erases per-token savings | Measured directly in P4 (rounds-to-converge + total cycle cost); revert is a one-line `builder_impl` model swap |
| The pinned build reports `maxOutputTokens: 32000` for Sonnet; a very large single-phase response could truncate | Monitor `stop_reason == "max_tokens"`; treat a phase that needs >32K output as a plan-granularity smell (split the phase). Verified non-binding for typical per-phase edits |
| `phase-commit` left unbound → Sonnet's work attributed to generic `builder` identity | FR-4 rebinds `phase-commit.agent` + adds the `builder_impl` identity entry |
| Triage at `medium` still mis-adjudicates contested `major` findings (cheap triager has sole authority there) | Optional FR-6 / P5 widens escalation; decision deferred to OQ-3 pending P4 evidence |
| Pinned `claude` build rejects `--effort` or the `sonnet` alias | P1 verification gate runs before any wiring (FR-8) |
| Profile proliferation if we later split reviewer (doc vs code) or fixer | Out of scope (Non-Goal); prefer additional named profiles over a per-step effort field if it ever arises |
| Effort **silently dropped** on a field/adapter mismatch (`reasoning_effort` on a `claude-code` profile, or `effort` on `codex`/`api`) → agent runs at the provider default with no error, re-creating the bug this PRD fixes | OQ-9: at minimum add load-time validation that fails loud on a field/value mismatch; the unified-key option (OQ-9b) removes the mismatch class entirely |

---

## §11 Open Questions

- **OQ-1 — `builder_impl` model: Sonnet or Opus?** The pilot is Sonnet; P4 decides. This is the central thing the run is meant to settle, not a guess to lock now.
- **OQ-2 — The cost-reduction threshold that defines "Sonnet wins"** (§9 uses 20% as a strawman). Owner to set the real number.
- **OQ-3 — Do FR-6 (escalation widening to majors) now, or defer** until P4/triage data shows the cheap triager is actually the weak link? Default: defer (P5 is gated).
- **OQ-4 — Split the reviewer** into a `medium`-effort document reviewer (PRD/plan cycles) vs `high`-effort code reviewer, or keep one `reviewer` at `high`? Low value (document cycles run once each); leaning keep-single.
- ~~**OQ-5 — The `--effort high` probe result is still pending** (the `--model sonnet` half is confirmed). FR-8 / P1 closes this.~~ **Resolved 2026-06-27:** ran `claude -p "Reply with exactly: ok" --model sonnet --effort high --output-format json` → `is_error:false`, `exit 0`, `modelUsage` = `claude-sonnet-4-6`. The pinned build accepts both flags; an invalid effort value would have errored. FR-8 acceptance met. (Note: the build reports `maxOutputTokens: 32000` for Sonnet — see R2 / OQ on phase granularity.)
- **OQ-6 — `builder` vs `builder_impl` commit identity granularity:** same email, different display name? Different email? What level of provenance distinction does the audit trail want?
- **OQ-7 — Change target scope:** does this apply to Gauntlet's own `.gauntlet/config.yaml` and which pipeline (`standard` vs `bootstrap`), and is it also the recommended default for adopters via `gauntlet init`? Or is it bootstrap-only for now?
- **OQ-9 — Normalize the effort field across adapters (it is a *silent* foot-gun).** The knob is `effort` on `claude-code` (mirrors Anthropic's `--effort`) but `reasoning_effort` on `codex`/`api` (mirrors OpenAI's `model_reasoning_effort`/`reasoning_effort`). The value sets largely **overlap** — both support `low|medium|high|xhigh`; the divergence is only at the extremes (OpenAI adds `none`/`minimal`; Claude adds `max`; `xhigh`/`max` are gated to higher Opus tiers, and per-model subsets apply on both sides). So the real cross-adapter friction is the **field name**, not the values. **The bite is that a mismatch fails silently:** `AgentProfile.build_adapter` (`config.py`) filters profile keys to the adapter constructor's signature and *drops* unrecognized ones (intentionally, for FR-2.4 plugin flags). So `reasoning_effort` on a claude-code profile — or `effort` on a codex/api profile — is silently discarded and the agent runs at the provider default, **reintroducing the exact "effort chosen by nobody" problem this PRD exists to fix, with no error or warning.** That makes this a fail-closed concern, not just ergonomics. Options: (a) keep the thin pass-through and document the gotcha (current behavior); (b) accept a unified `effort:` key the engine maps to each adapter's native param and validates against that adapter's allowed value set; (c) **minimum bar** — load-time validation that rejects/warns when the effort field name does not match the bound adapter, or the value is not in that adapter's allowed set, converting the silent default into a loud failure. Leaning at least (c) on the fail-closed principle; (b) additionally makes G6's "swap by config" hold across adapters, not just across models within one adapter. **`api`-adapter wrinkle:** it routes through LiteLLM to any provider, so its valid value set is **model-dependent**, not a fixed per-adapter list — (b)/(c) can validate the field *name* universally, but can only statically validate the *value* for the CLI adapters (`claude-code`, `codex`); the `api` adapter's value passes through to LiteLLM/the target model to accept or reject. So the cheap, universal win is field-name validation; static value validation is partial by construction.
- **OQ-8 — Ship opinionated preset profile-sets, or just document the swap?** Users span "latest model everywhere, cost-insensitive" to "cheapest everywhere." Options: (a) document that any stage's model is a one-line config change and ship one balanced default; (b) ship named presets (e.g. `max-quality` all-frontier, `cost-optimized` all-cheap, `balanced` mixed) that `gauntlet init` can offer. Recorded, not decided — leaning (a) for v1 to avoid preset maintenance as models churn, with (b) as a possible follow-up.

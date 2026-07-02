# PRD: Pipeline Effectiveness — catch more, gate smarter, learn across runs

**Status:** Draft v0.2
**Author:** John Pletka (drafted with Claude from a goals-first analysis of the pipeline, 2026-07-02; v0.2 resolves Q1/Q2/Q5)
**Date:** 2026-07-02
**Working name:** pipeline-effectiveness
**Relationship to existing artifacts:** Does **not** amend `PRD-gauntlet.md` or any approved artifact. FR-4 (evidence-tiered gates) was checked against the spec's gate requirements: phase-gate policy is pipeline configuration, not spec mandate (evidence recorded at §11 Q1); PRD/plan gates and blocker escalations stay unconditionally human. Builds on: the adversarial cycle (`engine/cycle.py`), plan phase machinery (`engine/planphases.py`, `phase_lint`), the retro/proposals loop (FR-6 machinery, `prompts/retro.md`, `gauntlet proposals`), `gauntlet trend` data, and the manifest metrics. Companion to `runs/harness-efficiency/prd.md` (plumbing hardening); this PRD changes what the pipeline *does*, that one changes how reliably it runs. Neither depends on the other.

## §1 Overview

### 1.1 Problem statement

Gauntlet's pitch is *adversarial multi-model review*, but each cycle runs exactly one reviewer, one pass, reading a diff. Three consequences observed across the bootstrap and the first real runs:

1. **Defect coverage is one model's blind spots.** A single reviewer profile (`codex`/`gpt-5.5`) reviews every phase. Findings it structurally misses — spec-coverage gaps, security posture, behavioral defects that only appear when the code *runs* — stay missed. The starkest instance is BOOTSTRAP-NOTES #54: a phase silently shipped 25% of its planned FRs and the diff-scoped review had no mechanism to notice, because nothing checks the diff against what the plan *promised*, and nobody executes the deliverable beyond `uv run pytest` on tests the builder itself wrote.

2. **Human gates cost the same whether the cycle converged clean or parked in flames.** Every gate parks for a human even when round 1 found zero blocking/major findings, tests are green, and there were no escalations or mutations — the exact conditions under which humans rubber-stamp. On multi-phase runs the human's gate latency (meetings, overnight) dominates wall-clock, and rubber-stamp gates train inattention for the gates that matter.

3. **The pipeline learns nothing between runs.** Retro produces proposals, `gauntlet trend` aggregates stats, manifests record every finding and verdict — and none of it feeds forward. The reviewer re-discovers the same project-specific failure patterns (the judge path-boundary bugs took four rounds across runs, #29–#32); triage re-litigates finding types a human already declined with recorded reasoning; the plan-author sizes phases with no cost model, producing the oversized phases that both hide incompleteness (#54 cause 4) and blow the provider window (harness-efficiency §1.1).

### 1.2 Solution summary

Widen detection, then use the widened evidence to narrow ceremony. (a) **Ensemble review**: an adversarial cycle accepts multiple reviewers, each with a distinct lens (correctness, spec-coverage, security), findings merged and deduplicated before triage — spending unconstrained-provider budget on diversity, the tool's founding premise. (b) **Behavioral verification**: a sandboxed verifier sub-step that *executes* the phase deliverable against the plan's acceptance criteria in a disposable worktree copy and reports observations as findings — a signal class no diff reader produces. (c) **Phase-completeness machinery** (the #54 preventions): acceptance-clause→test mapping validated deterministically at the phase gate, deferral-reference reconciliation against real phases, and a phase-size lint. (d) **Evidence-tiered gates**: a per-gate policy allowing auto-approval only when a strict clean-signal predicate holds, every auto-approval stamped with its evidence snapshot — contingent on the upstream spec check. (e) **Cross-run learning**: reviewer lens files and a declined-findings registry maintained through the existing ratified-proposal governance, and trend-derived cost/size stats injected into plan authoring.

### 1.3 The assumption this validates

**Reviewer diversity yields materially more unique legitimate findings than a second pass by the same reviewer — enough to justify the added review cost.** This is the tool's core premise, currently asserted rather than measured. P1 instruments the ensemble so every finding carries its source and dedup fate, making yield-per-reviewer a measured quantity. If the assumption fails (near-total overlap), ensemble review is dropped and the budget shifts to behavioral verification (FR-2), which produces a *different kind* of signal rather than more of the same kind.

## §2 Goals and Non-Goals

### 2.1 Goals

| ID | Outcome | Need served |
|----|---------|-------------|
| G1 | Each review round's findings come from ≥ 2 independent lenses, with per-lens yield measured | Defect coverage beyond one model's blind spots; the multi-model premise made real and measurable |
| G2 | A phase cannot pass its gate with planned acceptance criteria that map to no test, or deferrals that point nowhere | #54 class (silent partial delivery) is structurally closed |
| G3 | The deliverable is executed, not just read, before a phase gate | Behavioral defects surface pre-merge |
| G4 | Human attention concentrates on gates where the evidence is ambiguous; clean-signal gates clear without a human when policy allows | Gate latency stops dominating multi-phase wall-clock; attention isn't trained into rubber-stamping |
| G5 | Every run makes the next run better: recurring findings become lenses, declined findings stop recurring, phase sizing uses measured costs | The corpus of manifests becomes an asset instead of an archive |

### 2.2 Non-Goals (v1)

- **Everything in `runs/harness-efficiency/prd.md`** — resilience, context scoping, tiering, observability plumbing. Companion, not overlap.
- **Speculative phase overlap and parallel phase execution** (needs worktree isolation, already deferred in FUTURE.md; window-bound anyway).
- **Auto-tuning prompts from outcomes.** Lenses and registries change only through the existing ratified retro-proposal process — no self-modifying prompt loop.
- **CI-side review integration** (GitHub PR review bots). The PR remains the human audit boundary.
- **More than three reviewers per cycle.** v1 caps the panel; diminishing-returns measurement (§9) decides any expansion.
- **Auto-approving PRD or plan gates.** FR-4 applies to per-phase code gates only; document ratification stays human unconditionally.

## §3 Users and Personas

- **The human operator** — wants gates that are worth their attention and a pipeline that stops re-finding known problems.
- **Reviewer/triage agents** — receive lenses, the declined registry, and merged panels; their output volume and dedup behavior changes.
- **The plan-author agent** — receives measured phase-cost data and emits the acceptance-mapping obligations FR-3 enforces.

## §4 System Architecture

### 4.1 Components

| Component | Change | New/Touched |
|---|---|---|
| `src/gauntlet/engine/cycle.py` | `reviewers: []` panel support; per-lens prompts; finding merge + dedup before triage; verifier sub-step wiring | Touched |
| `prompts/lenses/` (`correctness.md`, `spec-coverage.md`, `security.md`) | Versioned lens fragments appended to the review prompt per panel member | **New** (data, not code) |
| `src/gauntlet/engine/verify.py` | Behavioral verifier: disposable worktree copy, sandboxed execution profile, findings-schema output | **New** |
| `src/gauntlet/engine/planphases.py`, `schemas/` | Phase entries gain `acceptance:` clauses; acceptance-mapping artifact schema; deferral-reference validation | Touched |
| `src/gauntlet/engine/steptypes.py` | `acceptance_gate` step (deterministic: mapping file ↔ collected test ids); phase-size lint in `phase_lint` | Touched |
| `src/gauntlet/engine/orchestrator.py`, `manifest.py` | Gate `policy: always \| auto_when_clean`; auto-approval record with evidence snapshot | Touched |
| `src/gauntlet/engine/registry.py` | Declined-findings registry (fingerprint → verdict, reasoning, run id), injected into triage context | **New** |
| `prompts/plan-author.md`, trend plumbing | Inject measured per-phase cost/duration stats and the FRs-per-phase bound into plan authoring input | Touched |
| `.gauntlet/config.yaml`, `pipelines/standard.yaml` | Panel definitions, gate policies, verifier profile | Touched |

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Where diversity comes from | Distinct lens prompts on distinct profiles (may be distinct providers), not N runs of one reviewer | Different models *and* different questions; same-model reruns measure noise, not coverage. Panel members run on unconstrained providers — no builder-window contention. |
| Dedup placement | Deterministic merge (same file + overlapping location + same category ⇒ mark duplicate, keep the higher-severity phrasing, record all sources) **before** triage | Triage is per-finding and priced per call; dedup first avoids paying to re-litigate the same defect N times. Deterministic rule, not an LLM judgment — determinism over cleverness. |
| Verifier's write access | Executes in a disposable copy of the worktree (or throwaway git worktree), never the run worktree; emits findings only | Preserves the reviewer read-only contract's *intent* (no mutation of the reviewed state) while allowing execution; the mutation guard keeps watching the real tree. Fail closed: verifier infra failure fails the sub-step, it does not silently skip. |
| Completeness check mechanism | Deterministic gate (mapping file ↔ `pytest --collect-only` ids), not another LLM review | #54's lesson is that judgment-based review misses absence; absence is checkable mechanically. The reviewer panel's spec-coverage lens complements, not substitutes. |
| Auto-approval predicate | Strict conjunction: converged in round 1 · zero blocking/major legitimate findings · acceptance gate passed · tests green · zero escalations · zero reviewer mutations · verifier ran clean | Any single ambiguous signal parks for a human. The predicate is evidence the pipeline already records — no new judgment call, just a policy over existing facts. |
| Learning governance | Lenses and registry entries change only via ratified retro proposals; registry injection is advisory context, never auto-dismissal | "Approved artifacts change only through their own loop and gate" applied to prompts; a declined precedent informs triage, it does not decide it. |

## §5 Functional Requirements

### FR-1 — Ensemble review

- **FR-1.1** `adversarial_cycle` accepts `reviewers: [<profile>...]` (1–3; single-reviewer config unchanged and default). Each panel member receives the same review scope plus its assigned lens fragment; members run independently (concurrently where adapters allow) and each returns findings-schema output.
  *Acceptance:* unit test: a two-member panel produces two persisted per-member findings artifacts; a one-member config is byte-compatible with today's behavior.
- **FR-1.2** Findings are merged before triage: every finding carries `source` (profile) and `lens`; the deterministic dedup rule (same file, overlapping line ranges/section, same category) marks duplicates, keeps the highest-severity phrasing as primary, and records all sources on it. Only primaries go to triage.
  *Acceptance:* unit test with crafted overlapping findings: merged artifact has the duplicate marked, sources aggregated, and triage is invoked once for the pair.
- **FR-1.3** Per-member yield is recorded in step metrics: findings raised, unique-after-dedup, and post-triage legitimate counts per (profile, lens).
  *Acceptance:* metrics fixture test: a run's manifest answers "unique legitimate findings per panel member" without transcript access.

### FR-2 — Behavioral verification

- **FR-2.1** Code cycles support an optional `verifier` sub-step between review and triage: an agent on a designated profile receives the phase's plan section (goal + acceptance clauses) and a **disposable copy** of the post-handoff worktree, executes the deliverable (run the CLI, exercise the API, probe edge inputs), and returns findings-schema output with `category: behavioral` and the executed commands as evidence.
  *Acceptance:* integration test (marked): a fixture phase with a working feature and one behavioral bug (correct-looking code, wrong runtime behavior) yields ≥ 1 behavioral finding with command evidence; the run worktree hash is unchanged after verification.
- **FR-2.2** Verifier findings join the merged panel findings and flow through the same triage/fix/confirm machinery — no parallel process.
  *Acceptance:* unit test: a behavioral finding appears in `findings.json` alongside review findings and receives a triage verdict.
- **FR-2.3** Fail closed: verifier infrastructure failure (copy creation, sandbox launch) fails the sub-step and parks the cycle; it never degrades to "skipped, proceed".
  *Acceptance:* unit test: stubbed copy failure → cycle parks with the failure in notes.

### FR-3 — Phase completeness (the #54 package)

- **FR-3.1** Plan phase entries gain a required `acceptance:` list (testable clauses, inherited from the PRD's FR acceptance lines). `phase_lint` fails closed on phases without one.
  *Acceptance:* existing plan fixtures updated; lint test rejects a clause-less phase.
- **FR-3.2** The implement step's completion contract includes an acceptance-mapping artifact: each clause id → ≥ 1 concrete test node id. A new deterministic `acceptance_gate` step verifies every mapped test id exists in `pytest --collect-only` output and every clause is mapped; any gap parks the phase with the unmapped clauses named.
  *Acceptance:* unit test: a mapping omitting one clause parks with that clause in notes; a mapping citing a nonexistent test id parks; a complete mapping passes.
- **FR-3.3** Deferral reconciliation: "Deferred to P<N>"-style references in commit bodies and mapping artifacts are validated against the plan's actual phases; a deferral to a nonexistent phase parks; open deferrals are injected into the target phase's implement prompt.
  *Acceptance:* unit test: deferral to a phantom phase parks; a valid deferral appears verbatim in the target phase's rendered prompt.
- **FR-3.4** Phase-size lint: `phase_lint` warns (configurable to park) when a phase carries more than `max_frs_per_phase` (default 3) distinct FR references — oversized phases are where partial delivery hides.
  *Acceptance:* lint test at the boundary; park mode verified.

### FR-4 — Evidence-tiered gates *(upstream check resolved — see §11 Q1)*

- **FR-4.1** Per-phase code gates accept `policy: always` (default, today's behavior) or `auto_when_clean`. The clean predicate is the strict conjunction in §4.2; if and only if it holds, the gate auto-approves with an `auto_approval` manifest record containing the full evidence snapshot (rounds, finding counts, acceptance-gate result, verifier result, test summary) and a notification is sent. Any predicate miss parks for a human exactly as today.
  *Acceptance:* unit tests: each single predicate violation parks; the all-clean case auto-approves with the snapshot present; PRD/plan gates reject the `auto_when_clean` policy at pipeline load.
- **FR-4.2** Auto-approved gates remain human-reversible: `gauntlet rollback` to the phase boundary works unchanged, and the run's final PR lists every auto-approved gate with its evidence snapshot so the human ratifies them collectively at the audit boundary.
  *Acceptance:* PR-draft test: auto-approved gates enumerated in `PR.md` with evidence.

### FR-5 — Cross-run learning

- **FR-5.1** Review lenses live as versioned files under `prompts/lenses/`; the retro step may propose lens additions (recurring finding patterns) through the existing proposals flow; only ratified proposals change lens files.
  *Acceptance:* wiring test: lens fragment appears in the review prompt; a retro fixture produces a lens proposal artifact; nothing mutates `prompts/lenses/` without the ratification path.
- **FR-5.2** Declined-findings registry: when a human or triage declines a finding with reasoning, its fingerprint (normalized category + location kind + claim shape) and verdict are recorded. Triage context for a fingerprint-matching future finding includes the precedent (verdict, reasoning, run id) as advisory data — the triager may still classify it legitimate.
  *Acceptance:* unit test: a registered decline surfaces in the triage prompt for a matching finding and is absent for a non-matching one; the registry file round-trips.
- **FR-5.3** Plan authoring receives measured history: per-phase cost/duration distributions by step type from `gauntlet trend` data, plus the `max_frs_per_phase` bound, injected into the plan-author input so phase sizing is grounded in observed costs (and in the window budget, where harness-efficiency FR-10 config exists).
  *Acceptance:* prompt-render test: a repo with ≥ 1 completed run produces a stats block in the plan-author prompt; an empty history renders a stated "no history" block, not silence.

## §6 Data & Schemas (normative excerpts)

**Finding additions (findings.json):** `source: <profile>`, `lens: <lens-id>`, `duplicate_of: <finding-id> | null`, `sources: [<profile>...]` (on primaries), `category` gains `behavioral`.

**Acceptance mapping (`artifacts/acceptance-map.json`):**
```json
{ "phase": "P3",
  "clauses": [ { "id": "P3-A1", "text": "resume re-enters at first incomplete sub-step",
                 "tests": ["tests/unit/test_cycle.py::test_resume_mid_round"] } ],
  "deferrals": [ { "text": "windows path handling", "to_phase": "P5" } ] }
```

**Auto-approval record (manifest):**
```json
{ "gate_id": "phase-gate", "iteration": "P3", "policy": "auto_when_clean",
  "evidence": { "rounds": 1, "blocking": 0, "major": 0, "escalations": 0,
                "reviewer_mutations": 0, "acceptance_gate": "pass",
                "verifier": "clean", "tests": "142 passed, 0 failed" },
  "at": "2026-07-02T18-00-00Z" }
```

**Declined-findings registry (`<asset_root>/registry/declined.jsonl`, append-only):**
```json
{ "fingerprint": "style/docstring-format/claim:missing-docstrings",
  "verdict": "bikeshedding", "reasoning": "...", "run_id": "run-...", "by": "human|triage",
  "at": "2026-07-02T12-00-00Z" }
```

## §7 Security & Privacy

- **Verifier execution is the new attack surface:** it runs code from the branch under review. It executes in a disposable copy outside the run worktree, under the judge's hooks and a `workspace-write`-scoped sandbox confined to that copy; network posture inherits the profile's sandbox. Fail closed on any sandbox setup failure (FR-2.3). The mutation guard on the real worktree is unchanged and would catch any escape that touched it.
- **Panel prompts contain the same artifacts today's single reviewer sees** — no new data exposure; per-member transcripts go through the existing redaction path.
- **Auto-approval cannot silently widen:** the predicate is a fixed conjunction in code, the policy is per-gate config, document gates refuse it at load, and every auto-approval is a durable manifest record surfaced in the PR. Fail-closed default is `always` (human).
- **Registry and lenses are advisory inputs under ratification governance** — no agent-writable path mutates them; a poisoned "precedent" would need to pass the human proposal gate first.

## §8 Implementation Plan (phased, assumption-validating)

| Phase | Deliverable | Assumption validated |
|---|---|---|
| P1 | Ensemble review with per-member yield metrics (FR-1) | **The core bet (§1.3):** lens/model diversity produces materially non-overlapping legitimate findings. Metrics make the answer a number. |
| P2 | Acceptance mapping + `acceptance_gate` + deferral reconciliation + size lint (FR-3) | Plan acceptance clauses are mechanically mappable to test ids in practice, and the gate catches seeded incompleteness (#54 replay fixture). |
| P3 | Behavioral verifier (FR-2) | Executing the deliverable in a disposable copy finds defect classes diff review missed, at acceptable sandbox complexity. |
| P4 | Declined-findings registry + lens plumbing under proposal governance (FR-5.1, FR-5.2) | Precedent context measurably reduces re-litigated findings without suppressing legitimate ones. |
| P5 | Trend-informed plan authoring (FR-5.3) | Measured phase-cost history changes plan-author sizing behavior (observable in emitted phase counts/scopes). |
| P6 | Evidence-tiered gates (FR-4) — last because P1–P3 produce the evidence signals the predicate consumes | The clean-signal predicate identifies exactly the gates humans were rubber-stamping (measured: zero post-hoc reversals of auto-approved gates). |

No forward dependencies: P6 consumes P1–P3's signals, hence last; P1–P5 are independent of each other. Riskiest first: P1 tests the premise the rest of the review-widening spends money on.

## §9 Success Metrics

- **Ensemble yield:** each panel member contributes ≥ 25% unique-after-dedup legitimate findings on a comparison run; below that for two runs, the panel shrinks (the metric is the kill criterion, per §1.3).
- **Completeness:** a #54-replay fixture (phase shipping a plausible subset) is caught by the acceptance gate 100% deterministically; zero silent-partial phases across subsequent real runs.
- **Behavioral signal:** ≥ 1 triage-legitimate behavioral finding per run on average across the first three verifier-enabled runs, at verifier cost ≤ 10% of run cost — otherwise the verifier reverts to opt-in.
- **Gate economics:** with `auto_when_clean` enabled on a multi-phase run, human gate interactions drop ≥ 40% with **zero** auto-approved gates later reversed (rollback or post-merge fix traced to that phase); one reversal disables the policy pending retro.
- **Learning:** re-litigated findings (fingerprint-matching a recorded decline, again triaged) drop ≥ 50% run-over-run; plan-author phase-size variance vs measured costs narrows (qualitative check at plan gates).

## §10 Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Ensemble multiplies noise, drowning triage in duplicates and bikeshedding | Deterministic dedup before triage (FR-1.2); yield metrics with an explicit kill criterion (§9); panel capped at 3 |
| Verifier sandbox escape or destructive execution | Disposable copy + judge hooks + scoped sandbox (§7); mutation guard unchanged on the real tree; fail-closed setup |
| Acceptance clauses gamed by trivial tests (`assert True` mapped to every clause) | The gate proves *existence*, not sufficiency — by design (it closes the absence class). Test sufficiency remains the review panel's job; the spec-coverage lens is prompted to sample mapped tests |
| Auto-approval predicate too loose (rubber-stamps a bad phase) | Strict conjunction, evidence snapshot, PR-level collective ratification, one-reversal-disables rule (§9) |
| Registry suppresses a legitimate finding that resembles a declined one | Advisory-only injection; triager retains authority; precedent includes reasoning so mismatches are visible |
| Lens files drift into prompt bloat | Ratification-gated changes only; lenses are per-member fragments, not additions to every prompt |

## §11 Open Questions

- ~~**Q1 — Upstream spec check (blocking for FR-4 only).** Does `PRD-gauntlet.md` FR-8 mandate a human at *every* phase gate, or does it permit gate policy configuration?~~ **Resolved (2026-07-02, by inspection of the spec):** the spec mandates human gates for the PRD and plan stages specifically (FR-10.2 "strict stage gating") and for escalated blockers (FR-10.5), but phase gates are pipeline *configuration*: the overview says runs pause "only at explicitly configured human gates," goals list "new gates, or re-ordered stages" as YAML-editable without orchestrator changes, and the spec's own success metric targets "zero human turns between plan approval and phase commits (excluding configured gates) on ≥ 80% of runs." FR-4 as scoped — phase code gates only, PRD/plan gates refuse the policy at load, escalations always park — therefore does not amend the spec. Human veto point remains this PRD's own gate.
- ~~**Q2 — Panel composition v1.**~~ **Resolved (2026-07-02, human):** two members — the existing codex reviewer (gpt-5.5) plus a **Gemini profile on the `api` adapter** (three distinct providers across the pipeline; zero builder-window contention). Note the constraint this implies: the `api` adapter has no file access, so the Gemini member's review scope must be fully inlined (diff/artifact in the prompt — today's behavior; and per harness-efficiency FR-1.3, reference-mode inputs are invalid for it). A third member is post-measurement. `doctor` must probe the Gemini model id like every profile (harness-efficiency FR-6.4).
- **Q3 — Verifier profile and sandbox.** Codex `workspace-write` sandbox on the disposable copy, or a claude-code profile with judge-enforced path confinement? *Proposal: codex sandbox — it is the mechanism the bootstrap already validated as the reliable backstop (#10).*
- **Q4 — Fingerprint definition.** How fuzzy may the declined-finding fingerprint be before it over-matches? *Proposal: exact category + location-kind + normalized claim keywords in v1; measure false-match rate before loosening.*
- ~~**Q5 — Threshold ratification.**~~ **Resolved (2026-07-02, human):** all §9 numbers (25% yield, 40% gate reduction, 50% re-litigation drop, ≤ 10% verifier cost) ratified as drafted; adjustable through this PRD's own review loop before approval.

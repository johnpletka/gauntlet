# PRD: Pipeline Effectiveness — catch more, gate smarter, learn across runs

**Status:** Draft v0.3
**Author:** John Pletka (drafted with Claude from a goals-first analysis of the pipeline, 2026-07-02; v0.2 resolves Q1/Q2/Q5; v0.3 folds in issue #49's convergence-honesty cluster as FR-6/P7, 2026-07-05)
**Date:** 2026-07-05
**Working name:** pipeline-effectiveness
**Relationship to existing artifacts:** Does **not** amend `PRD-gauntlet.md` or any approved artifact. FR-4 (evidence-tiered gates) was checked against the spec's gate requirements: phase-gate policy is pipeline configuration, not spec mandate (evidence recorded at §11 Q1); PRD/plan gates and blocker escalations stay unconditionally human. FR-6 adopts the four upstreaming requests of [issue #49](https://github.com/johnpletka/gauntlet/issues/49) (convergence + confirm-pass semantics, surfaced by a real adopting-repo run). Builds on: the adversarial cycle (`engine/cycle.py`), plan phase machinery (`engine/planphases.py`, `phase_lint`), the retro/proposals loop (the spec's FR-6 machinery: `prompts/retro.md`, `gauntlet proposals`), `gauntlet trend` data, and the manifest metrics. Companion to `runs/harness-efficiency/prd.md` (plumbing hardening); this PRD changes what the pipeline *does*, that one changes how reliably it runs. Neither depends on the other.

## §1 Overview

### 1.1 Problem statement

Gauntlet's pitch is *adversarial multi-model review*, but each cycle runs exactly one reviewer, one pass, reading a diff. Three consequences observed across the bootstrap and the first real runs:

1. **Defect coverage is one model's blind spots.** A single reviewer profile (`codex`/`gpt-5.5`) reviews every phase. Findings it structurally misses — spec-coverage gaps, security posture, behavioral defects that only appear when the code *runs* — stay missed. The starkest instance is BOOTSTRAP-NOTES #54: a phase silently shipped 25% of its planned FRs and the diff-scoped review had no mechanism to notice, because nothing checks the diff against what the plan *promised*, and nobody executes the deliverable beyond `uv run pytest` on tests the builder itself wrote.

2. **Human gates cost the same whether the cycle converged clean or parked in flames.** Every gate parks for a human even when round 1 found zero blocking/major findings, tests are green, and there were no escalations or mutations — the exact conditions under which humans rubber-stamp. On multi-phase runs the human's gate latency (meetings, overnight) dominates wall-clock, and rubber-stamp gates train inattention for the gates that matter.

3. **The pipeline learns nothing between runs.** Retro produces proposals, `gauntlet trend` aggregates stats, manifests record every finding and verdict — and none of it feeds forward. The reviewer re-discovers the same project-specific failure patterns (the judge path-boundary bugs took four rounds across runs, #29–#32); triage re-litigates finding types a human already declined with recorded reasoning; the plan-author sizes phases with no cost model, producing the oversized phases that both hide incompleteness (#54 cause 4) and blow the provider window (harness-efficiency §1.1).

4. **Convergence can close accepted-but-unfinished work silently** (issue #49, from a real adopting-repo run, `quote-source-badges`). The convergence predicate treats a `partially_resolved` confirm verdict as forcing another round only when the *finding* is blocking severity (`engine/cycle.py`: `verdict == "partially_resolved" and severity == "blocking"`); a `fix_now` partial on a major finding converges as effectively closed. Three prompt-level gaps compound it: fixers close enumerated obligations on the headline rather than the full list (the run's FR-5.1 case shipped *no-write* while silently dropping *no-read*/*no-payload*); an untestable acceptance oracle is triaged as a quality nit rather than a blocker; and artifact-mode confirms never check that a fix left the document internally consistent (the run's F-006 fix corrected the strategy section while the deliverable section still asserted the opposite). The harness-efficiency run itself demonstrated the escape live: its P1 cycle converged with `confirm_counts: {partially_resolved: 1}` surfaced-but-not-carried.

### 1.2 Solution summary

Widen detection, then use the widened evidence to narrow ceremony. (a) **Ensemble review**: an adversarial cycle accepts multiple reviewers, each with a distinct lens (correctness, spec-coverage, security), findings merged and deduplicated before triage — spending unconstrained-provider budget on diversity, the tool's founding premise. (b) **Behavioral verification**: a sandboxed verifier sub-step that *executes* the phase deliverable against the plan's acceptance criteria in a disposable worktree copy and reports observations as findings — a signal class no diff reader produces. (c) **Phase-completeness machinery** (the #54 preventions): acceptance-clause→test mapping validated deterministically at the phase gate, deferral-reference reconciliation against real phases, and a phase-size lint. (d) **Evidence-tiered gates**: a per-gate policy allowing auto-approval only when a strict clean-signal predicate holds, every auto-approval stamped with its evidence snapshot — contingent on the upstream spec check. (e) **Cross-run learning**: reviewer lens files and a declined-findings registry maintained through the existing ratified-proposal governance, and trend-derived cost/size stats injected into plan authoring. (f) **Convergence honesty** (issue #49): an accepted finding confirmed `partially_resolved` is non-converged by definition — the engine predicate says so and the confirm pass emits the concrete remainder as a carryable finding; fixers treat enumerated obligations as checklists; untestable acceptance oracles triage as blocking; artifact-mode confirms check intra-document consistency.

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
| G6 | An accepted (`fix_now`) finding cannot close while a material remainder is unresolved: partials force another round or park, enumerated obligations close item-by-item, and a fix cannot leave the document contradicting itself | Issue #49's silent-closure class is structurally shut |

### 2.2 Non-Goals (v1)

- **Everything in `runs/harness-efficiency/prd.md`** — resilience, context scoping, tiering, observability plumbing. Companion, not overlap.
- **Speculative phase overlap and parallel phase execution** (needs worktree isolation, already deferred in FUTURE.md; window-bound anyway).
- **Auto-tuning prompts from outcomes.** Lenses and registries change only through the existing ratified retro-proposal process — no self-modifying prompt loop.
- **CI-side review integration** (GitHub PR review bots). The PR remains the human audit boundary.
- **More than three reviewers per cycle.** v1 caps the panel; diminishing-returns measurement (§9) decides any expansion.
- **Auto-approving PRD or plan gates.** FR-4 applies to per-phase code gates only; document ratification stays human unconditionally.
- **Issue #49's secondary observation** (the retro proposal-diff generator emitting non-applying diffs). A real bug, but in the proposals machinery, not the cycle — tracked separately, not absorbed here.

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
| `.gauntlet/config.yaml`, `pipelines/standard.yaml` | Panel definitions, gate policies, verifier profile; `max_rounds` 2 → 3 on `plan-cycle`/`impl-cycle` so a carried remainder has a round to land (FR-6.1) | Touched |
| `src/gauntlet/engine/cycle.py` (convergence) | `_forcing_open`: an accepted `fix_now` finding whose confirm verdict is `partially_resolved` is a forcing open regardless of severity (FR-6.1 engine variant) | Touched |
| `prompts/cycle-confirm.md`, `cycle-fix.md`, `triage.md`, `review-document.md`, `review-code.md` (+ scaffold twins) | Remainder-capture, enumerated-obligation checklists, untestable-oracle severity rule, intra-document consistency check (FR-6) | Touched |

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|---|---|---|
| Where diversity comes from | Distinct lens prompts on distinct profiles (may be distinct providers), not N runs of one reviewer | Different models *and* different questions; same-model reruns measure noise, not coverage. Panel members run on unconstrained providers — no builder-window contention. |
| Dedup placement | Deterministic merge (same file + overlapping location per §6's normalized-location model + same category ⇒ mark duplicate, keep the higher-severity phrasing, record all sources) **before** triage | Triage is per-finding and priced per call; dedup first avoids paying to re-litigate the same defect N times. Deterministic rule with a fully specified overlap algorithm (§6) covering line, section, and whole-file locations, not an LLM judgment — determinism over cleverness. |
| Verifier's write access | Executes in a disposable copy of the worktree (or throwaway git worktree), never the run worktree; emits findings only | Preserves the reviewer read-only contract's *intent* (no mutation of the reviewed state) while allowing execution; the mutation guard keeps watching the real tree. Fail closed: verifier infra failure fails the sub-step, it does not silently skip. |
| Completeness check mechanism | Deterministic gate (mapping file ↔ collector-enumerated ids; `pytest --collect-only` is the v1 default collector, FR-3.2), not another LLM review | #54's lesson is that judgment-based review misses absence; absence is checkable mechanically. The reviewer panel's spec-coverage lens complements, not substitutes. |
| Auto-approval predicate | Strict conjunction: converged in round 1 · zero blocking/major legitimate findings · acceptance gate passed · tests green · zero escalations · zero reviewer mutations · verifier ran clean | Any single ambiguous signal parks for a human. The predicate is evidence the pipeline already records — no new judgment call, just a policy over existing facts. |
| Learning governance | Lenses and registry entries change only via ratified retro proposals; registry injection is advisory context, never auto-dismissal | "Approved artifacts change only through their own loop and gate" applied to prompts; a declined precedent informs triage, it does not decide it. |
| Partial-closure fix depth | **Both** the engine predicate (an accepted `fix_now` partial is non-converged, period) **and** the confirm-prompt remainder capture — not prompt-only | Issue #49 offers the prompt-level path as sufficient, but a prompt instruction is probabilistic; the convergence guarantee belongs in the deterministic predicate (determinism over cleverness). The prompt half is still required: the predicate can force a round, but only a *concrete carried remainder* gives that round something actionable. Oscillation is bounded: the rule touches only accepted-and-attempted findings, never re-litigates declines, and `max_rounds` still escalates to a human. |

## §5 Functional Requirements

### FR-1 — Ensemble review

- **FR-1.1** `adversarial_cycle` accepts `reviewers: [<profile>...]` (1–3; single-reviewer config unchanged and default). Each panel member receives the same review scope plus its assigned lens fragment; members run independently (concurrently where adapters allow) and each returns findings-schema output.
  *Acceptance:* unit test: a two-member panel produces two persisted per-member findings artifacts; a one-member config is byte-compatible with today's behavior.
- **FR-1.2** Findings are merged before triage: every finding carries `source` (profile) and `lens`; the deterministic dedup rule (same file, overlapping location per the normalized-location model and overlap algorithm in §6, same category) marks duplicates, keeps the highest-severity phrasing as primary, and records all sources on it. Only primaries go to triage.
  *Acceptance:* unit tests over the §6 overlap algorithm — line-range∩line-range, section-prefix, line-vs-whole-file, and non-overlap (adjacent sections, disjoint ranges, different files) — plus a crafted overlapping-findings case whose merged artifact has the duplicate marked, sources aggregated, and triage invoked once for the pair.
- **FR-1.3** Per-member yield is recorded in step metrics: findings raised, unique-after-dedup, and post-triage legitimate counts per (profile, lens).
  *Acceptance:* metrics fixture test: a run's manifest answers "unique legitimate findings per panel member" without transcript access.

### FR-2 — Behavioral verification

- **FR-2.1** Code cycles support an optional `verifier` sub-step between review and triage: an agent on a designated profile receives the phase's plan section (goal + acceptance clauses) and a **disposable copy** of the post-handoff worktree, executes the deliverable (run the CLI, exercise the API, probe edge inputs), and returns findings-schema output with `category: behavioral` and the executed commands as evidence.
  *Acceptance:* integration test (marked): a fixture phase with a working feature and one behavioral bug (correct-looking code, wrong runtime behavior) yields ≥ 1 behavioral finding with command evidence; the run worktree hash is unchanged after verification.
- **FR-2.2** Verifier findings join the merged panel findings and flow through the same triage/fix/confirm machinery — no parallel process.
  *Acceptance:* unit test: a behavioral finding appears in `findings.json` alongside review findings and receives a triage verdict.
- **FR-2.3** Fail closed: verifier infrastructure failure (copy creation, sandbox launch) fails the sub-step and parks the cycle; it never degrades to "skipped, proceed".
  *Acceptance:* unit test: stubbed copy failure → cycle parks with the failure in notes.
- **FR-2.4** The `behavioral` category is added to `schemas/findings.json`'s `category` enum and to every validator/consumer that enforces it (cycle merge, triage, confirm, metrics) as one **additive** migration: pre-migration findings that use only the existing categories continue to validate unchanged, and a verifier finding whose category is not accepted end-to-end fails the sub-step closed rather than being silently dropped or coerced to another category.
  *Acceptance:* schema/compatibility test: a pre-migration findings fixture (no `behavioral` value) still validates against the migrated schema; a verifier finding with `category: behavioral` validates and survives merge → triage → confirm; a finding whose `category` is absent from the enum is rejected at validation (fail closed), not silently passed through.
- **FR-2.5** The verifier sandbox contract in §7 is enforced and tested: read-only confinement to the disposable copy, network default-deny, credential/secret env stripping, subprocess hook inheritance, and a wall-clock/resource limit whose expiry fails the sub-step closed.
  *Acceptance:* tests (integration where a real sandbox is required): a verifier attempt to read a credential file outside the disposable copy is denied; a network reach under default-deny fails; a stripped secret env var is absent from the verifier process environment; an over-limit execution is killed and parks the sub-step (never "skipped, proceed").

### FR-3 — Phase completeness (the #54 package)

- **FR-3.1** Plan phase entries gain a required `acceptance:` list (testable clauses, inherited from the PRD's FR acceptance lines). `phase_lint` fails closed on phases without one.
  *Acceptance:* existing plan fixtures updated; lint test rejects a clause-less phase.
- **FR-3.2** The implement step's completion contract includes an acceptance-mapping artifact: each clause id → ≥ 1 concrete evidence entry `{ kind, id }`, where `kind` names the collector (`pytest` is the default; other kinds — e.g. `shell`, `golden`, `integration` — are declared per project/profile) and `id` is the node id or check that collector enumerates. A new deterministic `acceptance_gate` step, per distinct collector, verifies every cited id appears in that collector's enumeration output (`pytest` ⇒ `pytest --collect-only`; another collector runs its declared side-effect-free listing command) and that every clause is mapped; any gap parks the phase with the unmapped clauses named. v1 ships the `pytest` collector; a phase whose evidence declares a collector kind not configured for the project parks closed rather than passing unchecked.
  *Acceptance:* unit test: a mapping omitting one clause parks with that clause in notes; a mapping citing a nonexistent pytest node id parks; a mapping citing an unconfigured collector kind parks; a complete pytest mapping passes.
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
- **FR-5.2** Declined-findings registry: when a human or triage declines a finding with reasoning, its fingerprint (normalized category + location kind + claim shape) and verdict are recorded together with provenance — `repo`, `prd_family` (the PRD/run family the decline was made under), `prompt_version` and `lens_version`, `schema_version`, and the recording run id. Triage context for a fingerprint-matching future finding includes the precedent (verdict, reasoning, run id) as advisory data **only when the provenance is still compatible**: same repo and PRD family, and prompt/lens/schema versions not older than the entry's. A decline recorded under a superseded prompt, lens, or schema, or under a different PRD family, is *not* injected — it is retained for audit, never surfaced as precedent. Ratified retro proposals may explicitly invalidate or supersede entries. The triager may still classify an injected match legitimate.
  *Acceptance:* unit tests: a registered decline surfaces in the triage prompt for a matching finding under compatible provenance and is absent both for a non-matching fingerprint and for a fingerprint match whose entry's prompt/lens/schema version is stale or whose PRD family differs; the registry file round-trips with the provenance fields.
- **FR-5.3** Plan authoring receives measured history: per-phase cost/duration distributions by step type from `gauntlet trend` data, plus the `max_frs_per_phase` bound, injected into the plan-author input so phase sizing is grounded in observed costs (and in the window budget, where harness-efficiency FR-10 config exists).
  *Acceptance:* prompt-render test: a repo with ≥ 1 completed run produces a stats block in the plan-author prompt; an empty history renders a stated "no history" block, not silence.

### FR-6 — Convergence honesty (issue #49)

- **FR-6.1** An accepted (`fix_now`) finding whose confirm verdict is `partially_resolved` is **non-converged by definition**: the convergence predicate treats it as a forcing open regardless of the finding's severity, and the confirm pass additionally emits a `new_findings` entry naming the *specific unresolved remainder* (with `carried_from: <finding-id>`) so the next round has a concrete target. Remainder severity is set by what it guards: `blocking` for a privacy/security leakage boundary or a golden/parity oracle guarding a behavior-changing refactor; `major` otherwise. `max_rounds` on the shipped `plan-cycle`/`impl-cycle` rises 2 → 3 so a carried remainder has a round to land before max-rounds escalation parks for a human — the fail-closed terminus is unchanged.
  *Acceptance:* unit test: a `fix_now` finding confirmed `partially_resolved` at `major` severity forces round N+1 (today it converges — the regression fixture is issue #49's escape); prompt-content test on `cycle-confirm.md` for the remainder-capture instruction and severity rule; fixture cycle: the carried remainder appears in round N+1's review scope with `carried_from` intact; exhausting `max_rounds` with an open remainder parks (`cycle_escalation` semantics unchanged).
- **FR-6.2** Enumerated obligations close item-by-item, not on the headline. `cycle-fix.md` instructs the fixer to treat a finding that names several discrete obligations as an acceptance checklist: restate each item, map each to the specific change or assertion satisfying it, and state any deferral explicitly rather than silently dropping it. `cycle-confirm.md` mirrors the check: any uncovered enumerated item ⇒ `partially_resolved` with the uncovered items named (feeding FR-6.1's carry).
  *Acceptance:* prompt-content tests on both templates; fixture cycle with a three-obligation finding whose fix covers two: confirm returns `partially_resolved` naming the third, and the carried remainder targets exactly it.
- **FR-6.3** An untestable acceptance oracle is a blocker, not a nit. The reviewer severity rubrics (`review-document.md`, `review-code.md`) and the triage guidance (`triage.md`) state: a finding that an acceptance criterion, parity oracle, or golden test is not deterministic enough to judge a behavior-changing refactor classifies as `blocking`, unless the finding itself supplies the exact fixture matrix and expected outcomes that make it deterministic.
  *Acceptance:* prompt-content tests; a labeled entry in `prompts/triage-corpus.jsonl` encoding the rule (the PLAN F-006 case from issue #49) so triage-accuracy evaluation covers it.
- **FR-6.4** Artifact-mode confirm passes verify intra-document consistency: the fix must not introduce *or leave* a contradiction between sections of the same document (strategy vs. deliverable, requirement vs. open questions). A remaining contradiction ⇒ `partially_resolved`/`unresolved` citing both conflicting sections.
  *Acceptance:* prompt-content test; fixture: an artifact fix that corrects one section while a second section still asserts the opposite yields a non-`resolved` verdict citing both sections.

## §6 Data & Schemas (normative excerpts)

**Finding additions (findings.json):** `source: <profile>`, `lens: <lens-id>`, `duplicate_of: <finding-id> | null`, `sources: [<profile>...]` (on primaries). `category`'s enum gains `behavioral` as an **additive** value — the schema and every validator/consumer that checks `category` (cycle merge, triage, confirm, metrics) migrate together (FR-2.4); pre-migration findings using only the existing categories still validate, and a `category` outside the enum fails validation closed rather than being coerced or dropped.

**Normalized finding location (dedup input, FR-1.2):** before dedup, every finding's location normalizes to `{ file, start, end, section }` — `file` is the repo-relative path (or the document path in artifact mode), `[start, end]` is an inclusive line range (both `null` for a whole-file or line-less finding), and `section` is the ordered heading path for document findings (e.g. `["§5", "FR-6.1"]`, else `null`). Overlap for the dedup rule is deterministic:
- **Both line-ranged, same file:** overlap iff the inclusive ranges intersect.
- **Both section-pathed, same file/document:** overlap iff one section path is a prefix of the other (`§5` overlaps `§5/FR-6.1`; `§5/FR-6.1` does **not** overlap `§5/FR-6.2`).
- **Mixed (one line-ranged, one line-less/whole-file), same file:** the line-less/whole-file finding overlaps any finding in that file (a file-scoped finding subsumes line-scoped ones).
- **Different file/document:** never overlap.
- **Invalid/unparseable location:** treated as whole-file for that file if the file is known, else it overlaps nothing (fail open on dedup — an un-deduped finding is a wasted triage call, not a lost defect).
Line ranges and section paths are never compared across kinds (a section path and a bare line range in the same file do not overlap unless one side is whole-file).

**Acceptance mapping (`artifacts/acceptance-map.json`):**
```json
{ "phase": "P3",
  "clauses": [ { "id": "P3-A1", "text": "resume re-enters at first incomplete sub-step",
                 "evidence": [ { "kind": "pytest",
                                 "id": "tests/unit/test_cycle.py::test_resume_mid_round" } ] } ],
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

**Confirm remainder carry (confirm.json `new_findings` entries, FR-6.1):** entries gain optional `carried_from: <finding-id>` marking a remainder split off a `partially_resolved` accepted finding; severity per the FR-6.1 rule. Schema change is additive to `schemas/confirm.json`/`findings.json`.

**Declined-findings registry (`<asset_root>/registry/declined.jsonl`, append-only):** provenance fields (`repo`, `prd_family`, `prompt_version`, `lens_version`, `schema_version`) gate injection per FR-5.2 — an entry surfaces as precedent only for a compatible repo/PRD-family and non-stale prompt/lens/schema versions. Append-only for audit; invalidation/supersession is a ratified retro proposal, not an in-place edit.
```json
{ "fingerprint": "style/docstring-format/claim:missing-docstrings",
  "verdict": "bikeshedding", "reasoning": "...",
  "repo": "gauntlet", "prd_family": "pipeline-effectiveness",
  "prompt_version": "triage@4d3722e", "lens_version": "none", "schema_version": "findings@1",
  "run_id": "run-...", "by": "human|triage", "at": "2026-07-02T12-00-00Z" }
```

## §7 Security & Privacy

- **Verifier execution is the new attack surface:** it runs code from the branch under review. Concrete sandbox contract — whichever profile Q3 selects must satisfy all of it, and it is enforced/tested per FR-2.5:
  1. **Confinement.** Executes in a disposable copy outside the run worktree, `workspace-write`-scoped to that copy under the judge's hooks; the run worktree and everything outside the copy is read-only or unmounted.
  2. **Network default-deny.** No network egress unless the phase's acceptance clauses require it *and* the profile grants it explicitly; the default posture is deny.
  3. **Credential/env stripping.** The environment passes through an allowlist; credential- and token-shaped vars (the judge's secret patterns, `*_TOKEN`, `*_KEY`, `ANTHROPIC_*`, cloud creds) are removed before launch.
  4. **No credential reads.** Filesystem access outside the disposable copy is denied, so no credential file outside the copy is readable.
  5. **Resource/time bounds.** A wall-clock timeout and memory/CPU cap bound every execution; expiry fails the sub-step closed.
  6. **Subprocess inheritance.** Subprocesses the verifier spawns inherit the same sandbox and judge hooks — no escape by shelling out.
  Fail closed on any sandbox setup failure (FR-2.3). The mutation guard on the real worktree is unchanged and would catch any escape that touched it.
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
| P6 | Evidence-tiered gates (FR-4) — last of the trust chain because P1–P3 produce the evidence signals the predicate consumes | The clean-signal predicate identifies exactly the gates humans were rubber-stamping (measured: zero post-hoc reversals of auto-approved gates). |
| P7 | Convergence honesty (FR-6): engine predicate + the four prompt changes + `max_rounds` 3 + regression fixtures from issue #49's real escapes | Forcing accepted partials to carry converges within the round budget instead of oscillating (measured on the fixtures and the next real run); the silent-closure class is reproducibly shut. |

No forward dependencies for detection breadth: P6 consumes P1–P3's signals; P1–P5 are independent of each other. P7 changes cycle *semantics* (the convergence predicate and prompts), not detection breadth, so its detection logic is independent of the other phases — but it carries one **global coupling** that the "independent, run-last" framing must not obscure: the `max_rounds` 2 → 3 bump on the shipped `plan-cycle`/`impl-cycle` changes the round budget *every* cycle runs under, including the cycles P1–P6 exercise. Two consequences are therefore explicit: (a) if P7 runs last, its acceptance includes re-running the cycle- and gate-touching regression suites from P1–P6 under `max_rounds: 3` and confirming their acceptance assumptions still hold; (b) any earlier phase whose tests depend on a 2-round budget pins that budget in-fixture rather than reading the shipped config, so the later bump cannot silently invalidate them. Alternatively the `max_rounds` change may be pulled ahead of the other cycle/gate phases; either sequencing is valid, but the coupling is not zero. Riskiest first: P1 tests the premise the rest of the review-widening spends money on.

FR-6's evidence-tiered-gates interaction is deliberate: FR-4's clean predicate requires "zero blocking/major *legitimate* findings," and FR-6.1 makes an unfinished accepted fix exactly such a finding — so convergence honesty tightens auto-approval rather than fighting it.

## §9 Success Metrics

Each metric names its measurement window, the manifest field or command that evaluates it, and its v1 **enforcement mode**: *report-only* (surfaced in `gauntlet trend`/retro, no state change), *proposal* (retro emits a ratifiable proposal, no auto-change), or *auto* (the engine changes state without a human). No metric silently self-tunes config; the only *auto* actions are fail-closed (tightening or disabling), never loosening.

- **Ensemble yield:** each panel member contributes ≥ 25% unique-after-dedup legitimate findings, computed per run from the FR-1.3 per-member metrics (`metrics.ensemble.unique_legit_by_member`). Window: two consecutive comparison runs below threshold. Enforcement: **proposal** — retro emits a "shrink the panel" proposal citing the two runs; the panel changes only on ratification (§1.3 kill criterion, governed like every lens change).
- **Completeness:** a #54-replay fixture (phase shipping a plausible subset) is caught by the acceptance gate 100% deterministically (CI assertion, per run); zero silent-partial phases across subsequent real runs, verifiable from `acceptance_gate` results in the manifest. Enforcement: **auto** in the fail-closed direction only — the gate parks a phase with an unmapped clause (FR-3.2); the metric itself is report-only.
- **Behavioral signal:** ≥ 1 triage-legitimate behavioral finding per run on average, and verifier cost ≤ 10% of run cost, both read from manifest step metrics (`metrics.verifier.legit_findings`, `agent_usage`). Window: the first three verifier-enabled runs. Enforcement: **proposal** — below threshold, retro proposes reverting the verifier to opt-in; the profile config changes only on ratification.
- **Gate economics:** with `auto_when_clean` enabled on a multi-phase run, human gate interactions drop ≥ 40% (counted from `auto_approval` manifest records vs. total gates) with **zero** auto-approved gates later reversed (rollback or post-merge fix traced to that phase). Enforcement: **auto** — a single recorded reversal flips the affected gate's effective policy to `always` for the remainder of the run and writes a manifest note pending retro; re-enabling `auto_when_clean` is a ratified config change. The ≥ 40% drop itself is report-only.
- **Learning:** re-litigated findings (fingerprint-matching a recorded decline, again triaged) drop ≥ 50% run-over-run, counted from registry-match metrics (`metrics.registry.rematched`); plan-author phase-size variance vs measured costs narrows, a qualitative check surfaced at plan gates. Enforcement: **report-only** for both.
- **Convergence honesty:** **zero** silent closures — every `fix_now` finding confirmed `partially_resolved` either carries a remainder into a later round, resolves, or parks at max-rounds escalation (manifest-verifiable per cycle: no converged cycle whose final round holds an accepted partial with no `carried_from` successor); issue #49's two escape fixtures (the FR-5.1 enumerated-obligation case, the F-006 intra-document contradiction) are caught 100% deterministically (CI assertion); average rounds-per-cycle rises by < 1 (from manifest metrics — the honesty tax stays affordable). Enforcement: **auto** in the fail-closed direction only — the FR-6.1 predicate forces the round; the silent-closure count is report-only.

## §10 Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Ensemble multiplies noise, drowning triage in duplicates and bikeshedding | Deterministic dedup before triage (FR-1.2); yield metrics with an explicit kill criterion (§9); panel capped at 3 |
| Verifier sandbox escape or destructive execution | Disposable copy + judge hooks + scoped sandbox (§7); mutation guard unchanged on the real tree; fail-closed setup |
| Acceptance clauses gamed by trivial tests (`assert True` mapped to every clause) | The gate proves *existence*, not sufficiency — by design (it closes the absence class). Test sufficiency remains the review panel's job; the spec-coverage lens is prompted to sample mapped tests |
| Auto-approval predicate too loose (rubber-stamps a bad phase) | Strict conjunction, evidence snapshot, PR-level collective ratification, one-reversal-disables rule (§9) |
| Registry suppresses a legitimate finding that resembles a declined one | Advisory-only injection; triager retains authority; precedent includes reasoning so mismatches are visible |
| Lens files drift into prompt bloat | Ratification-gated changes only; lenses are per-member fragments, not additions to every prompt |
| Forcing partials to carry makes cycles oscillate or never converge | The rule touches only accepted-and-attempted findings (declines and non-`fix_now` verdicts are untouched); the remainder must be *specific*, not a re-review; `max_rounds` (now 3) still escalates to a human as the fail-closed terminus; §9's rounds-per-cycle metric watches the tax |
| The blocking-vs-major remainder rule (FR-6.1) is gamed or misjudged by the confirm agent | The severity rule is stated in the prompt with the two blocking categories named concretely (leakage boundaries, parity oracles). A mis-set severity does **not** change convergence: per FR-6.1 an accepted `fix_now` partial forces the next round regardless of severity, so the round is forced either way. Severity affects only how the carried remainder is reported and prioritized in the gate summary — never whether it forces the round |

## §11 Open Questions

- ~~**Q1 — Upstream spec check (blocking for FR-4 only).** Does `PRD-gauntlet.md` FR-8 mandate a human at *every* phase gate, or does it permit gate policy configuration?~~ **Resolved (2026-07-02, by inspection of the spec):** the spec mandates human gates for the PRD and plan stages specifically (FR-10.2 "strict stage gating") and for escalated blockers (FR-10.5), but phase gates are pipeline *configuration*: the overview says runs pause "only at explicitly configured human gates," goals list "new gates, or re-ordered stages" as YAML-editable without orchestrator changes, and the spec's own success metric targets "zero human turns between plan approval and phase commits (excluding configured gates) on ≥ 80% of runs." FR-4 as scoped — phase code gates only, PRD/plan gates refuse the policy at load, escalations always park — therefore does not amend the spec. Human veto point remains this PRD's own gate.
- ~~**Q2 — Panel composition v1.**~~ **Resolved (2026-07-02, human):** two members — the existing codex reviewer (gpt-5.5) plus a **Gemini profile on the `api` adapter** (three distinct providers across the pipeline; zero builder-window contention). Note the constraint this implies: the `api` adapter has no file access, so the Gemini member's review scope must be fully inlined (diff/artifact in the prompt — today's behavior; and per harness-efficiency FR-1.3, reference-mode inputs are invalid for it). A third member is post-measurement. `doctor` must probe the Gemini model id like every profile (harness-efficiency FR-6.4).
- **Q3 — Verifier profile and sandbox.** Codex `workspace-write` sandbox on the disposable copy, or a claude-code profile with judge-enforced path confinement? *Proposal: codex sandbox — it is the mechanism the bootstrap already validated as the reliable backstop (#10).* Q3 selects the *mechanism* only; whichever profile is chosen must satisfy the concrete sandbox contract in §7 (confinement, network default-deny, credential/env stripping, no credential reads, resource/time bounds, subprocess inheritance), enforced and tested per FR-2.5 — so FR-2's security guarantees do not depend on how Q3 resolves.
- **Q4 — Fingerprint definition.** How fuzzy may the declined-finding fingerprint be before it over-matches? *Proposal: exact category + location-kind + normalized claim keywords in v1; measure false-match rate before loosening.*
- ~~**Q5 — Threshold ratification.**~~ **Resolved (2026-07-02, human):** all §9 numbers (25% yield, 40% gate reduction, 50% re-litigation drop, ≤ 10% verifier cost) ratified as drafted; adjustable through this PRD's own review loop before approval.

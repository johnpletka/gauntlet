# Implementation Plan: Pipeline Effectiveness

**PRD:** `runs/pipeline-effectiveness/prd.md` (Draft v0.3, approved for planning)
**Working name:** pipeline-effectiveness
**Builder note:** This plan decomposes PRD §8's seven-deliverable sketch into nine strictly-sequential phases, splitting the two deterministic-completeness concerns and separating the schema migration from the verifier so that most phases carry ≤ 3 FR references (the `max_frs_per_phase` bound this plan itself introduces in P3). Ordering preserves the PRD's risk logic: the core bet is killed first; the trust-chain consumer (evidence-tiered gates) lands only after the signals it consumes exist; convergence semantics land last because of a global `max_rounds` coupling.

---

## Overview and sequencing rationale

The work widens detection (ensemble review, behavioral verification, deterministic completeness) and then uses the widened evidence to narrow ceremony (evidence-tiered gates), plus two independent tracks: cross-run learning and convergence honesty. The riskiest assumption — that lens/model diversity yields materially non-overlapping legitimate findings (§1.3) — is validated first (P1) because every later review-widening investment is premised on it, and P1 makes the answer a measured number so a failed bet can redirect budget.

Dependency structure:

- **P1 (ensemble)** is independent and first — the core bet.
- **P2 → P3 (completeness)** are deterministic and low-risk; P3 (deferral reconciliation) consumes the `acceptance-map.json` artifact P2 defines.
- **P4 → P5 (verifier)** are ordered by the PRD's hard prerequisite: the `behavioral`-category schema + consumer migration (FR-2.4) must land, with every consumer still validating pre-migration outputs, **before** any verifier execution is wired.
- **P6 (learning: registry + lens governance)** and **P7 (trend-informed plan authoring)** are independent of each other and of P1–P5, save that P6's lens-governance builds on the lens *files* P1 creates (P1 owns their creation; P6 owns their governance — no forward dependency).
- **P8 (evidence-tiered gates)** is the trust-chain consumer: its clean predicate requires ensemble triage results (P1), the acceptance-gate result (P2), and a verifier-ran-clean signal (P5). It therefore lands after all three.
- **P9 (convergence honesty)** changes cycle *semantics*, not detection, so its logic is independent — **but** its `max_rounds` 2 → 3 bump on the shipped `plan-cycle`/`impl-cycle` changes the round budget every cycle runs under. It lands last and its exit criteria include re-running the cycle- and gate-touching regression suites from P1–P8 under `max_rounds: 3`.

**Cross-cutting constraint (the `max_rounds` coupling).** Any phase before P9 whose tests assert cycle behavior under a 2-round budget **pins `max_rounds: 2` in-fixture** rather than reading the shipped config, so P9's later bump cannot silently invalidate them. This is called out in each affected phase's test strategy (P1, P5, P8).

**Cross-cutting conventions (all phases).**
- Every phase ends with `uv run pytest` (and, where the phase adds integration coverage, `uv run pytest -m integration` locally) green, then a single commit in the enforced format, then a review handoff — no continuation past the gate.
- All new external-call and infra paths **fail closed** (deny/park on timeout, parse error, unexpected exit): verifier infra (P5), acceptance/collector gates (P2), auto-approval predicate misses (P8).
- Dedup, gates, predicates, and the convergence forcing rule are **deterministic** — no LLM judgment in a mechanical check.
- Schema changes are **additive**: pre-migration artifacts must still validate (asserted in P4 and P9).
- Prompt/lens/schema/registry files are **data under ratification governance**; no phase adds an agent-writable mutation path to them.

---

## P1 — Ensemble review + per-member yield metrics

**Assumption validated:** the core bet (§1.3) — reviewer diversity (distinct lenses on distinct profiles) yields materially more unique legitimate findings than a second same-reviewer pass. This phase instruments the ensemble so yield-per-reviewer is a measured quantity; if the bet fails (near-total overlap), the corpus of run metrics shows it and budget can redirect to P5.

**Deliverables (FR-1.1, FR-1.2, FR-1.3):**
- `engine/cycle.py`: `adversarial_cycle` accepts `reviewers: [<profile>...]` (1–3; single-reviewer config is the unchanged default). Each member receives the same review scope plus its assigned lens fragment and returns findings-schema output; members run independently (concurrently where adapters allow).
- `prompts/lenses/correctness.md`, `spec-coverage.md`, `security.md` — the initial versioned lens fragment files, appended per panel member. (This phase **creates** the files and the append wiring; P6 adds their retro-proposal governance. No forward dependency.)
- Deterministic pre-triage merge/dedup implementing the §6 normalized-location model and overlap algorithm: `{file, start, end, section}` normalization; line∩line, section-prefix, mixed line-vs-whole-file, different-file, and invalid-location (fail-open) rules; plus the **claim-compatibility guard** — location overlap + same category merge only when claim fingerprints share the keyword core; divergent claims are kept as distinct primaries. Duplicates are marked (`duplicate_of`), highest-severity phrasing kept as primary, all `sources` aggregated. Only primaries reach triage.
- Schema additions to `schemas/findings.json`: `source`, `lens`, `duplicate_of`, `sources` — all **optional and nullable** (added to neither `required` nor with a default that materializes on serialize), so a legacy finding that omits them still validates. **Emission rule:** these fields are written only by the ensemble merge path (≥ 2 members). A one-member config takes the unchanged single-reviewer path and emits *none* of them — the persisted per-member findings object is byte-identical to today's output. In a multi-member run, `source`/`lens` are set per member, and `duplicate_of`/`sources` are set only on the merged primary/duplicate records that the merge step produces.
- Step metrics: per-(profile, lens) findings-raised, unique-after-dedup, and post-triage-legitimate counts (`metrics.ensemble.unique_legit_by_member`).
- Panel config in `.gauntlet/config.yaml` / `pipelines/standard.yaml`: the ratified v1 panel (Q2) — the existing codex reviewer (gpt-5.5) plus a Gemini profile on the `api` adapter, whose scope is fully inlined (no file access; reference-mode inputs invalid for it).

**Dedup normalization + fingerprint spec (deterministic — this is the canonical definition the merge implements; two builders following it produce identical merged sets).**
- **Location grammar.** A `location` string parses to `{file, start, end, section}` by this canonical parser: split on the first `:`; left is `file` (path as written, no resolution). The right side is one of — `<n>` (single line ⇒ `start = end = n`); `<a>-<b>` (line range); `§<section>` or `#<section>` or bare non-numeric text (section, no lines); or empty/absent ⇒ whole-file. A `file` with no `:` and no trailing range is whole-file. Numbers that fail to parse as integers ⇒ **invalid location** (see below).
- **Line ranges are inclusive** on both ends; a single line `n` is the range `[n, n]`. Two line ranges **overlap** iff `a.start ≤ b.end AND b.start ≤ a.end` (touching endpoints count as overlap).
- **Section canonicalization.** Lowercase; strip a leading `§`/`#`/`sec.`/`section`; collapse internal whitespace to single spaces; trim. Dotted/hierarchical ids keep their dots (`4.2`). **Section-prefix match:** section A is a prefix of section B iff `B == A` or `B` starts with `A + "."` (so `4` matches `4.2` but not `42`).
- **Whole-file sentinel:** `{start: null, end: null, section: null}`. A whole-file location on file F overlaps any line-scoped or section-scoped location on the same F (mixed line-vs-whole-file rule); it never overlaps a location on a different file.
- **Different file ⇒ never overlaps**, regardless of ranges/sections.
- **Invalid location (fail-open):** if either finding's location fails to parse, they are treated as **non-overlapping** (kept as distinct primaries) — dedup never drops a finding on the strength of an unparseable location.
- **Claim fingerprint (keyword core).** Lowercase the `claim`; strip punctuation to spaces; tokenize on whitespace; drop a fixed English stopword list (checked into the merge module, versioned — articles, prepositions, auxiliaries, and pronoun/deictic filler); apply no stemming (v1 uses exact token match to avoid over-merging); the fingerprint is the resulting **sorted set** of unique tokens. Two claims **share the keyword core** iff the Jaccard overlap of their fingerprint sets ≥ 0.5 (tie value pinned in config; documented, not magic).
- **Merge rule.** Two findings merge into one primary iff: same `file`, locations overlap (above), same `category`, **and** claims share the keyword core. Divergent-claim overlaps are kept as distinct primaries (no drop).
- **Primary selection / severity tie-break.** Among merged members, the primary is the highest-severity member on the fixed order `blocking > major > minor > nit`; ties broken by (1) member profile order in panel config, then (2) lexicographic `id` — fully deterministic. The primary keeps its own phrasing; all merged members (including the primary) are recorded in `sources`; non-primaries carry `duplicate_of: <primary id>`.

**Test strategy:**
- Unit: a two-member panel produces two persisted per-member findings artifacts; a legacy findings fixture that omits the four new fields still validates against the extended schema; a one-member run's persisted per-member findings artifact is **byte-identical** to the pre-ensemble single-reviewer output (the new fields are absent from the serialized JSON, not present-and-null).
- Unit over the dedup spec: location-string parsing for each grammar form (single line, inclusive range, `§`/`#`/bare section, whole-file); line-range∩line-range (including touching endpoints), section-prefix (`4` vs `4.2` vs `42`), line-vs-whole-file, and non-overlap (adjacent sections, disjoint ranges, different files); an **invalid-location** pair is treated non-overlapping (fail-open, no drop); a crafted overlapping-findings case whose merged artifact marks the duplicate, aggregates `sources`, and invokes triage once for the pair; the distinct-claim case (whole-file + line-scoped, same category, divergent fingerprints below the keyword-core threshold) where both are kept as primaries and triage runs for each (no drop); a keyword-core case at/above vs. below the pinned Jaccard threshold; and a severity tie-break case proving the highest-severity member is chosen primary with panel-order then lexicographic-id tie-breaks.
- Metrics fixture: a run's manifest answers "unique legitimate findings per panel member" without transcript access.
- Any cycle-round-count fixture pins `max_rounds: 2` in-fixture (P9 coupling).

**Exit criteria:** tests green; a two-member panel run persists per-member artifacts, a deduped merged set, and per-member yield metrics readable from the manifest.

**Deferrals:** lens-file retro-proposal governance and the declined-findings registry → P6. A third panel member → post-measurement (§2.2 cap; §9 kill criterion).

---

## P2 — Acceptance mapping + `acceptance_gate`

**Assumption validated:** plan acceptance clauses are mechanically mappable to collector-enumerated test ids in practice, and a deterministic gate catches seeded incompleteness — the structural close of the #54 class (silent partial delivery).

**Deliverables (FR-3.1, FR-3.2):**
- `engine/planphases.py` + `schemas/`: plan phase entries gain a required `acceptance:` list of testable clauses; `phase_lint` fails closed on a clause-less phase.
- Implement-step completion contract gains the acceptance-mapping artifact `artifacts/acceptance-map.json` (§6 shape): each clause id → ≥ 1 evidence `{kind, id}`; `kind` names the collector, `id` is the enumerated node/check. **v1 allowed-kind rule:** `kind` is a closed enum whose only member is `pytest` — the sole collector implemented in v1. A registered collector namespace exists so a future collector plugin can widen the enum, but a `kind` with no registered collector is **schema-invalid**, not a runtime surprise.
- New deterministic `acceptance_gate` step in `engine/steptypes.py`, one instance per distinct collector: verifies every cited id appears in that collector's side-effect-free enumeration (`pytest` ⇒ `pytest --collect-only`) and that every clause is mapped. Any gap parks the phase naming the unmapped clauses. A phase whose evidence declares a `kind` outside the allowed enum (no registered collector) is **rejected at pipeline/plan load** — it never reaches runtime to "park closed," so an unsupported collector cannot masquerade as supported in a plan artifact. **Scope:** proves *citation + existence* only — not that a cited test meaningfully exercises the clause (sufficiency stays the spec-coverage lens's job; G2 scoped accordingly).

**Test strategy:**
- Unit: a mapping omitting one clause parks with that clause in notes; a mapping citing a nonexistent pytest node id parks; a mapping/phase declaring a `kind` outside the allowed enum (no registered collector) is rejected at load, not parked at runtime; a complete pytest mapping passes (existence proven, sufficiency not asserted).
- Lint test: a clause-less phase is rejected.
- Update existing plan fixtures to carry `acceptance:` lists.

**Exit criteria:** tests green; a seeded incomplete-mapping fixture parks deterministically; a complete mapping clears the gate.

**Deferrals:** non-`pytest` collectors (`shell`, `golden`, `integration`) → post-v1. Widening the allowed-kind enum requires *implementing and registering* that collector's plugin (side-effect-free enumeration + config contract); until then the kind stays out of the enum and is load-invalid — v1 ships only the `pytest` collector, with no declare-but-unimplemented path. Deferral reconciliation and size lint → P3.

---

## P3 — Deferral reconciliation + phase-size lint

**Assumption validated:** the remaining two #54 guardrails — that deferrals cannot point nowhere and that oversized phases (where partial delivery hides) are surfaced — are mechanically enforceable against the plan's actual phases.

**Deliverables (FR-3.3, FR-3.4):**
- Deferral reconciliation: "Deferred to P<N>"-style references in commit bodies and in `acceptance-map.json` `deferrals[]` are validated against the plan's actual phases; a deferral to a nonexistent phase parks; open deferrals are injected verbatim into the target phase's implement prompt.
- Phase-size lint in `phase_lint`: warns (configurable to park) when a phase carries more than `max_frs_per_phase` (default 3) distinct FR references.

**Test strategy:**
- Unit: a deferral to a phantom phase parks; a valid deferral appears verbatim in the target phase's rendered implement prompt.
- Lint test at the boundary (3 vs 4 FR refs); park-mode verified.

**Exit criteria:** tests green; a phantom deferral parks; a valid deferral round-trips into the target prompt; the size lint fires at the boundary.

**Deferrals:** none — this completes the FR-3 package.

---

## P4 — Behavioral-category schema + consumer migration

**Assumption validated:** the `behavioral` category can be added end-to-end as one additive migration with every `category`-enforcing consumer still validating pre-migration outputs — the PRD's hard prerequisite (FR-2.4) that de-risks P5 by guaranteeing a verifier's `behavioral` finding can never reach an unmigrated consumer.

**Deliverables (FR-2.4):**
- `schemas/findings.json` `category` enum gains `behavioral` (additive).
- Every consumer that enforces `category` — cycle merge, triage, confirm, metrics — migrated together to accept it; a `category` outside the enum fails validation closed (never coerced or silently dropped).

**Test strategy:**
- Schema/compatibility: a pre-migration findings fixture (no `behavioral` value) still validates against the migrated schema; a finding with `category: behavioral` validates and survives merge → triage → confirm; a finding whose `category` is absent from the enum is rejected at validation (fail closed).
- Phase-order assertion: the schema + all consumers (merge, triage, confirm, metrics) validate the migrated schema — this landing is the precondition P5 depends on.

**Exit criteria:** tests green; all existing review/confirm/metrics consumers validate the migrated schema; no verifier execution wired yet (that is P5).

**Deferrals:** verifier execution, sub-step wiring, and the sandbox contract → P5.

---

## P5 — Behavioral verifier + sandbox contract

**Assumption validated:** executing the deliverable in a disposable worktree copy finds defect classes diff review structurally misses, at acceptable sandbox complexity and cost.

**Size note:** this phase carries four FR references (FR-2.1, FR-2.2, FR-2.3, FR-2.5), one over the `max_frs_per_phase` default. The split is intentional and the FRs are inseparable: the verifier *executes code from the branch under review*, so wiring execution (FR-2.1/2.2) without the sandbox contract (FR-2.5) and fail-closed infra handling (FR-2.3) would ship an unsafe subsystem. The completeness gate (P2) and size lint (P3) already exist to make this deviation visible and justified rather than silent.

**Deliverables (FR-2.1, FR-2.2, FR-2.3, FR-2.5):**
- `engine/verify.py`: an optional `verifier` sub-step between review and triage. An agent on a designated profile (Q3: codex `workspace-write` sandbox on the disposable copy, augmented with judge-enforced read-denial for every path outside the copy — `workspace-write` alone permits outside reads, violating §7 items 1 and 4) receives the phase's plan section (goal + acceptance clauses) and a **disposable copy** of the post-handoff worktree, executes the deliverable, and returns findings-schema output with `category: behavioral` and the executed commands as evidence.
- Verifier findings join the merged panel and flow through the same triage/fix/confirm machinery — no parallel process.
- Fail closed (FR-2.3): copy-creation / sandbox-launch failure fails the sub-step and parks the cycle; never degrades to "skipped, proceed."
- Sandbox contract enforced (FR-2.5, §7): read-only confinement to the disposable copy (outside-copy reads *denied*, not merely read-only), network default-deny, credential/secret env stripping (allowlist; `*_TOKEN`, `*_KEY`, `ANTHROPIC_*`, cloud creds removed), subprocess hook/sandbox inheritance, and a wall-clock/resource limit whose expiry fails the sub-step closed. The run worktree hash is unchanged after verification; the mutation guard on the real tree is untouched.
- `.gauntlet/config.yaml` / `pipelines/standard.yaml`: verifier profile config.

**Sandbox backend + enforcement boundary (v1).** The contract items above are enforced by a single named backend, not by ad-hoc per-item hacks:
- **Backend:** the codex CLI's own `workspace-write` sandbox is the *process jail* (OS-level: on macOS the codex sandbox uses `sandbox-exec`/Seatbelt; on Linux the Landlock/seccomp path). Gauntlet does not reimplement OS isolation — it configures this backend and refuses to run if it is unavailable. `workspace-write` alone permits reads outside the workspace and outbound network; the additional confinement below closes exactly those gaps.
- **Filesystem read denial (outside the copy):** enforced by launching the verifier with the disposable copy as its *only* writable and readable workspace root and the backend's read-scope confined to that root; the judge session that hooks the verifier denies any tool call whose resolved path escapes the copy root (symlink-resolved, `..`-normalized). Reads are *denied*, not merely read-only.
- **Network default-deny:** the backend is launched with network access disabled; there is no per-verifier allowlist in v1 (network-permitted profiles are deferred below).
- **Env stripping:** the verifier process is spawned from a *rebuilt* environment — an explicit allowlist of variables (PATH, HOME, LANG, TERM, codex/runtime essentials) copied forward; everything else, and specifically `*_TOKEN`, `*_KEY`, `ANTHROPIC_*`, and known cloud-credential vars, is absent from the child env (strip-by-construction, not deny-by-pattern-at-read).
- **Subprocess inheritance:** children spawned by the verifier inherit the same jail because they are spawned inside the backend's sandboxed process tree and the rebuilt env; the sub-step does not hand the verifier an escape hatch to spawn outside the jail.
- **Resource/wall-clock limit:** a wall-clock deadline (config, default bounded) and the platform's process resource limits are applied to the sandboxed process group; expiry kills the group and fails the sub-step closed.
- **Unsupported-host detection (fail closed):** at sub-step start the engine probes for a usable backend (codex sandbox present and the OS isolation primitive available). If absent, the sub-step **parks closed** — it never falls back to running the verifier unsandboxed. This is the only supported v1 posture; other isolation backends are out of scope.

**Test strategy:**
- Integration (marked): a fixture phase with a working feature and one behavioral bug (correct-looking code, wrong runtime behavior) yields ≥ 1 behavioral finding with command evidence; the run worktree hash is unchanged.
- Unit: a behavioral finding appears in `findings.json` alongside review findings and receives a triage verdict; a stubbed copy failure parks the cycle with the failure in notes.
- Sandbox tests (integration where a real sandbox is required): an outside-copy credential-file read is denied; a network reach under default-deny fails; a stripped secret env var is absent from the verifier process env; an over-limit execution is killed and parks (never "skipped, proceed"); a stubbed backend-probe failure parks the sub-step closed (verifier never runs unsandboxed).
- Any cycle-round fixture pins `max_rounds: 2` in-fixture (P9 coupling).

**Exit criteria:** tests green (unit + local integration); the seeded behavioral-bug fixture yields a command-evidenced finding; every §7 sandbox item has a passing enforcement test on the v1 backend; an unsupported-host backend probe parks closed; run worktree hash unchanged.

**Deferrals:** network-permitted verifier profiles beyond the default-deny posture → per-phase acceptance-clause-gated config, post-v1. The `auto_when_clean` consumption of the verifier-clean signal → P8.

---

## P6 — Declined-findings registry + lens governance

**Assumption validated:** precedent context measurably reduces re-litigated findings without suppressing legitimate ones, and lens files can evolve only through ratified governance.

**Deliverables (FR-5.1, FR-5.2):**
- `engine/registry.py` + `<asset_root>/registry/declined.jsonl` (append-only): when a human or triage declines a finding with reasoning, record its fingerprint (Q4: exact category + location-kind + normalized claim keywords) and verdict with full provenance — `repo`, `prd_family`, `prompt_version`, `lens_version`, `schema_version`, `run_id`, `by`, `at`.
- Triage-context injection: a fingerprint-matching future finding surfaces the precedent (verdict, reasoning, run id) as **advisory** data — and only when provenance is current (same repo + PRD family, and recorded prompt/lens/schema versions still in force). A decline under a superseded version or different PRD family is retained for audit, never injected. The triager retains authority to classify an injected match legitimate.
- Lens governance (FR-5.1): the retro step may propose lens additions (recurring finding patterns) through the existing proposals flow; only ratified proposals change `prompts/lenses/` files. (The files themselves and their prompt-append wiring were delivered in P1; this phase adds the proposal/ratification path.)
- **Re-litigation metrics + acceptance criteria.** The step emits, to the run manifest: `repeated_declined_fingerprint_count` (findings whose fingerprint matches a current-provenance registry entry) and `injected_precedent_override_count` (injected matches the triager nonetheless classified legitimate). The **acceptance definition of "measurably reduces re-litigation without suppressing"** is operationalized as two checks, not an unmeasured claim: (1) the repeated-declined-fingerprint rate is a persisted, comparable number so the run corpus shows the trend across runs; (2) a matched precedent must remain overridable — a fixture proves an injected precedent can still be triaged legitimate, so injection cannot silently suppress a genuine finding. No population-level threshold is asserted in v1 (single-run acceptance can't establish one); the metric is the instrument and corpus comparison is the honesty check, mirroring P1's measured-number posture.

**Test strategy:**
- Unit: a registered decline surfaces in the triage prompt for a matching finding under compatible provenance; is absent for a non-matching fingerprint; is absent for a fingerprint match whose entry's prompt/lens/schema version is stale or whose PRD family differs; the registry file round-trips with all provenance fields.
- Metrics/suppression: a run with a matching precedent emits `repeated_declined_fingerprint_count` and `injected_precedent_override_count` to the manifest; a **non-suppression fixture** where triage, given an injected matching precedent, still classifies the finding legitimate (the override is counted, the finding survives) — proving advisory precedent does not gate out a legitimate finding.
- Wiring: a lens fragment appears in the review prompt (regression from P1); a retro fixture produces a lens proposal artifact; nothing mutates `prompts/lenses/` without the ratification path.

**Exit criteria:** tests green; a matching decline injects under current provenance and is withheld under stale/foreign provenance; the re-litigation metrics are readable from the manifest; the non-suppression fixture shows an injected precedent overridden to legitimate; the retro path emits a ratifiable lens proposal without mutating lens files directly.

**Deferrals:** fingerprint loosening beyond exact Q4 v1 → gated on measured false-match rate, post-v1. Explicit invalidation/supersession of registry entries is a ratified retro proposal (not an in-place edit) — the governance hook is delivered here; corpus-tuning is out of scope.

---

## P7 — Trend-informed plan authoring

**Assumption validated:** measured phase-cost history is **surfaced to the plan author** — the plan-author input carries a concrete stats block (or an explicit no-history block) plus the size bound, so the author is no longer sizing blind. (The stronger behavioral claim — that history *changes* emitted phase counts/scopes — is not deterministically testable at phase scope and is not asserted here; the surfaced stats are advisory input to a human-ratified plan, and cross-run corpus data is where any sizing-behavior shift would show up. Narrowed from the original behavioral phrasing.)

**Deliverables (FR-5.3):**
- Inject measured history into the plan-author input (`prompts/plan-author.md` + trend plumbing): per-phase cost/duration distributions by step type from `gauntlet trend` data, plus the `max_frs_per_phase` bound, and the window budget where harness-efficiency FR-10 config exists.
- Empty-history handling: a repo with no completed run renders an explicit "no history" block, not silence.

**Test strategy:**
- Prompt-render: a repo with ≥ 1 completed run produces a stats block in the plan-author prompt; an empty history renders the stated "no history" block.

**Exit criteria:** tests green; the plan-author prompt carries a measured stats block (or the explicit no-history block) plus the size bound.

**Deferrals:** auto-tuning phase sizes from outcomes — out of scope (§2.2); the stats are advisory input to a human-ratified plan.

---

## P8 — Evidence-tiered gates

**Assumption validated:** the strict clean-signal predicate identifies exactly the gates humans were rubber-stamping — auto-approval clears clean-signal gates without a human while parking every ambiguous one. This phase lands after P1–P5 because the predicate consumes their signals (ensemble triage results, acceptance-gate result, verifier-ran-clean).

**Deliverables (FR-4.1, FR-4.2):**
- `engine/orchestrator.py` + `manifest.py`: per-phase **code** gates accept `policy: always` (default, today's behavior) or `auto_when_clean`. The clean predicate is the strict §4.2 conjunction — converged in round 1 · zero blocking/major legitimate findings · acceptance gate passed · tests green · zero escalations · zero reviewer mutations · verifier ran clean. Iff it holds, the gate auto-approves with an `auto_approval` manifest record carrying the full evidence snapshot (rounds, finding counts, acceptance-gate result, verifier result, test summary) and a notification is sent; any predicate miss parks for a human exactly as today.
- Pipeline-load validation: PRD/plan gates reject `auto_when_clean`; a code phase declaring `auto_when_clean` **without a configured verifier sub-step** is rejected at load (because `verifier ran clean` cannot hold without one). At runtime a verifier that is absent/skipped is recorded `verifier: not_configured` — a predicate miss that parks closed.
- FR-4.2: auto-approved gates remain human-reversible — `gauntlet rollback` to the phase boundary works unchanged, and the run's final `PR.md` enumerates every auto-approved gate with its evidence snapshot for collective ratification at the audit boundary.
- **Reversal circuit breaker (PRD §9, in scope):** a recorded human reversal of any auto-approved gate flips the run's effective auto-approval policy to `always` (human-required) for the **remainder of the run** — every subsequent code gate parks for a human even if its clean predicate holds. The reversal and the policy flip are recorded in the manifest; the clean predicate reads the flipped policy as a fail-closed short-circuit (reversal recorded ⇒ predicate cannot auto-approve). This is a deterministic in-run circuit breaker, not a post-v1 follow-on: without it an auto-approval system keeps auto-approving after a human has signalled distrust.

**Test strategy:**
- Unit: each single predicate violation parks; the all-clean case auto-approves with the snapshot present; PRD/plan gates reject `auto_when_clean` at load; a code phase declaring `auto_when_clean` with no configured verifier is rejected at load; a run whose verifier result is `not_configured` (or otherwise not `clean`) parks rather than auto-approving.
- PR-draft: auto-approved gates enumerated in `PR.md` with evidence.
- Reversal circuit breaker: a run with a recorded reversal parks a subsequent otherwise-all-clean gate rather than auto-approving; the manifest records the reversal and the effective-policy flip.
- Predicate fixtures assert round-1 convergence under a pinned `max_rounds: 2` (P9 coupling); P9 re-validates them under `max_rounds: 3`.

**Exit criteria:** tests green; every single-signal violation parks; the all-clean path auto-approves with a durable evidence snapshot surfaced in `PR.md`; load-time rejections hold for document gates and verifier-less `auto_when_clean` phases; a recorded reversal flips the run's effective policy to `always` and a subsequent clean gate parks.

**Deferrals:** CI-side / PR-bot review integration → out of scope (§2.2).

---

## P9 — Convergence honesty + `max_rounds` bump

**Assumption validated:** forcing accepted partials to carry a concrete remainder converges within the round budget instead of oscillating, and issue #49's silent-closure class is reproducibly shut. This phase changes cycle *semantics*, not detection, and lands last because of the global `max_rounds` coupling.

**Size note:** four FR references (FR-6.1–FR-6.4), one over the default bound, kept together deliberately: they are one cohesive convergence semantic — the engine forcing rule (FR-6.1) is inert without the confirm-prompt remainder capture that gives the forced round a target (FR-6.2), and the severity/consistency rules (FR-6.3, FR-6.4) feed the same carry. Splitting would ship a half-working convergence guarantee.

**Deliverables (FR-6.1, FR-6.2, FR-6.3, FR-6.4):**
- `engine/cycle.py` `_forcing_open`: an accepted (`fix_now`) finding whose confirm verdict is `partially_resolved` is a forcing open **regardless of severity** (today it forces only at blocking severity — issue #49's escape).
- Confirm remainder carry (§6): the confirm pass emits a `new_findings` entry that is a complete findings-schema object plus `carried_from: <finding-id>`, with a deterministic id in a **reserved carry namespace** — the base id is `<carried_from>-r<round>`, and because externally-supplied finding ids may already occupy that string (or one parent may carry multiple remainders, or a remainder may itself be re-carried), the engine appends a disambiguating suffix `-c<N>` where `N` is the smallest non-negative integer making the id unique **against the union of all finding ids seen this run** (every prior round's findings and every id already assigned this round); `N = 0` (`-c0`) is emitted explicitly rather than omitted, so the format is uniform and the base string is never itself a valid final id (guaranteeing no collision with a raw `<carried_from>-r<round>` that a reviewer might have supplied). Id assignment is order-deterministic (findings processed in stable id order). The entry's `location`/`claim`/`evidence` describe the *specific remainder*, and severity is set per the FR-6.1 rule (`blocking` for a privacy/security leakage boundary or a golden/parity oracle guarding a behavior-changing refactor; `major` otherwise). A carried remainder does **not** re-enter triage (it inherits its parent's `fix_now` acceptance — this bounds oscillation); it merges into round N+1 *ahead of* fresh findings with `carried_from` intact in the round manifest. Additive to `schemas/confirm.json`/`findings.json` (pre-migration confirms with no `new_findings` still validate).
- Prompt changes (+ scaffold twins): `cycle-fix.md` — treat an enumerated-obligation finding as an acceptance checklist (restate each item, map each to its change/assertion, state deferrals explicitly); `cycle-confirm.md` — mirror the check (any uncovered enumerated item ⇒ `partially_resolved` naming it) plus remainder-capture and the severity rule, and (FR-6.4) artifact-mode intra-document consistency (a remaining contradiction between sections ⇒ non-`resolved` citing both); `review-document.md`/`review-code.md`/`triage.md` — an untestable acceptance/parity/golden oracle for a behavior-changing refactor classifies `blocking` unless the finding supplies the exact fixture matrix and expected outcomes (FR-6.3).
- `.gauntlet/config.yaml`/`pipelines/standard.yaml`: `max_rounds` 2 → 3 on the shipped `plan-cycle`/`impl-cycle` so a carried remainder has a round to land before max-rounds escalation parks. Fail-closed terminus (escalate to human at max-rounds) unchanged.
- A labeled entry in `prompts/triage-corpus.jsonl` encoding the untestable-oracle rule (the PLAN F-006 case).

**Test strategy:**
- Unit: a `fix_now` finding confirmed `partially_resolved` at `major` severity forces round N+1 (issue #49 regression fixture); exhausting `max_rounds` with an open remainder parks (`cycle_escalation` unchanged).
- Prompt-content: remainder-capture + severity rule in `cycle-confirm.md`; enumerated-obligation checklist in both `cycle-fix.md` and `cycle-confirm.md`; untestable-oracle rule in the review/triage prompts.
- Fixture cycles: a three-obligation finding whose fix covers two → confirm returns `partially_resolved` naming the third and the carried remainder (id `<id>-r<round>-c<N>` in the reserved namespace) targets exactly it and appears in round N+1's review scope with `carried_from` intact; a collision fixture where the base `<carried_from>-r<round>` id already exists among input findings → the carried remainder gets the next free `-c<N>` suffix (no id collision); an artifact fix correcting one section while a second still contradicts it → non-`resolved` citing both sections.
- Schema compatibility: a pre-migration confirm output (no `new_findings`) still validates.
- **Global-coupling exit gate:** re-run the cycle- and gate-touching regression suites from P1, P5, and P8 under `max_rounds: 3` and confirm their acceptance assumptions still hold (fixtures that pinned `max_rounds: 2` remain pinned and unaffected; any that read shipped config are re-validated at 3).

**Exit criteria:** tests green including the two issue-#49 escape fixtures caught 100% deterministically; the `max_rounds: 3` re-run of P1/P5/P8 cycle/gate suites passes; average rounds-per-cycle metric available for the §9 honesty-tax check.

**Deferrals:** issue #49's secondary observation (retro proposal-diff generator emitting non-applying diffs) — out of scope (§2.2), tracked separately in the proposals machinery.

---

## Machine-readable phase list

```gauntlet-phases
- id: P1
  title: Ensemble review + per-member yield metrics
  goal: Multi-lens panel with deterministic pre-triage dedup and per-member yield metrics; validates the core bet that reviewer diversity yields materially non-overlapping legitimate findings, as a measured number.
  acceptance:
    - id: P1-A1
      clause: A two-member panel run persists one findings-schema artifact per member.
    - id: P1-A2
      clause: The deterministic pre-triage dedup produces a merged set that marks duplicates (duplicate_of), aggregates sources, and invokes triage once per primary; divergent-claim overlaps are kept as distinct primaries.
    - id: P1-A3
      clause: Per-(profile, lens) unique-legitimate yield metrics are readable from the run manifest without transcript access.
    - id: P1-A4
      clause: A one-member config leaves the persisted per-member findings artifact byte-identical to the pre-ensemble single-reviewer output.
- id: P2
  title: Acceptance mapping + acceptance_gate
  goal: Required plan acceptance clauses mapped to collector-enumerated test ids, verified by a deterministic acceptance_gate; validates that clauses are mechanically mappable and seeded incompleteness (the #54 class) is caught.
  acceptance:
    - id: P2-A1
      clause: phase_lint fails closed on a plan phase carrying no acceptance list.
    - id: P2-A2
      clause: acceptance_gate parks when a clause is unmapped, naming the unmapped clause in notes.
    - id: P2-A3
      clause: acceptance_gate parks when a cited pytest node id is absent from the side-effect-free collector enumeration.
    - id: P2-A4
      clause: A complete pytest mapping clears the gate (citation + existence proven; sufficiency not asserted).
    - id: P2-A5
      clause: A phase whose acceptance evidence declares a collector kind other than the v1 pytest collector is rejected at pipeline load, not at runtime.
- id: P3
  title: Deferral reconciliation + phase-size lint
  goal: Validate deferral references against real phases and warn/park on oversized phases; completes the #54 completeness package's remaining guardrails.
  acceptance:
    - id: P3-A1
      clause: A deferral referencing a nonexistent phase parks.
    - id: P3-A2
      clause: A valid open deferral appears verbatim in the target phase's rendered implement prompt.
    - id: P3-A3
      clause: The phase-size lint fires at the boundary (>max_frs_per_phase distinct FR refs); park-mode parks.
- id: P4
  title: Behavioral-category schema + consumer migration
  goal: Add the behavioral category additively across schema and every category-enforcing consumer, all still validating pre-migration outputs; the hard prerequisite that de-risks the verifier.
  acceptance:
    - id: P4-A1
      clause: A pre-migration findings fixture (no behavioral value) validates against the migrated schema.
    - id: P4-A2
      clause: A finding with category behavioral validates and survives merge → triage → confirm.
    - id: P4-A3
      clause: A finding whose category is absent from the enum is rejected at validation (fail closed; never coerced or dropped).
- id: P5
  title: Behavioral verifier + sandbox contract
  goal: Execute the deliverable in a disposable sandboxed worktree copy and emit behavioral findings, fail-closed; validates that running the code finds defect classes diff review misses at acceptable sandbox complexity.
  acceptance:
    - id: P5-A1
      clause: The seeded behavioral-bug integration fixture yields ≥1 behavioral finding carrying the executed commands as evidence.
    - id: P5-A2
      clause: A stubbed copy-creation / sandbox-launch failure parks the cycle (never degrades to "skipped, proceed").
    - id: P5-A3
      clause: Each §7 sandbox item — outside-copy read denial, network default-deny, secret-env stripping, resource-limit expiry — has a passing enforcement test on the v1 backend.
    - id: P5-A4
      clause: The run worktree hash is unchanged after verification.
    - id: P5-A5
      clause: A host lacking the v1 sandbox backend is detected at sub-step start and parks closed (never runs the verifier unsandboxed).
- id: P6
  title: Declined-findings registry + lens governance
  goal: Provenance-gated declined-findings registry injected as advisory triage context plus retro-proposal governance for lens files; validates that precedent reduces re-litigation without suppressing legitimate findings.
  acceptance:
    - id: P6-A1
      clause: A fingerprint-matching decline surfaces in the triage prompt as advisory data under compatible (current) provenance.
    - id: P6-A2
      clause: A decline under a superseded prompt/lens/schema version or a different PRD family is retained for audit but withheld from injection.
    - id: P6-A3
      clause: The retro path emits a ratifiable lens proposal without directly mutating prompts/lenses/ files.
    - id: P6-A4
      clause: The repeated-declined-fingerprint rate is emitted to run metrics, and a fixture proves an injected matching precedent can still be triaged legitimate (no suppression).
- id: P7
  title: Trend-informed plan authoring
  goal: Surface measured per-phase cost/duration history and the size bound to the plan-author input; validates that measured history is put in front of the plan author.
  acceptance:
    - id: P7-A1
      clause: A repo with ≥1 completed run renders a measured stats block in the plan-author prompt.
    - id: P7-A2
      clause: A repo with no completed run renders the explicit "no history" block, not silence.
    - id: P7-A3
      clause: The max_frs_per_phase size bound appears in the plan-author input.
- id: P8
  title: Evidence-tiered gates
  goal: Per-phase code gates auto-approve only under the strict clean-signal predicate with a durable evidence snapshot, else park; validates that the predicate identifies exactly the rubber-stamped gates. Consumes P1-P5 signals.
  acceptance:
    - id: P8-A1
      clause: Each single clean-predicate violation parks for a human exactly as today.
    - id: P8-A2
      clause: The all-clean case auto-approves with an auto_approval manifest record carrying the full evidence snapshot.
    - id: P8-A3
      clause: Auto-approved gates are enumerated in the run's final PR.md with their evidence snapshots.
    - id: P8-A4
      clause: PRD/plan gates reject auto_when_clean at load, and a code phase declaring auto_when_clean without a configured verifier sub-step is rejected at load.
    - id: P8-A5
      clause: A recorded human reversal flips the gate's effective policy to always for the remainder of the run; a subsequent clean gate parks rather than auto-approving.
- id: P9
  title: Convergence honesty + max_rounds bump
  goal: Make an accepted partially_resolved finding non-converged by predicate and confirm-carry, with enumerated-checklist, untestable-oracle-blocking, and intra-document-consistency prompt rules, and raise max_rounds 2 to 3; validates that forced partials converge within budget and shut the silent-closure class.
  acceptance:
    - id: P9-A1
      clause: A fix_now finding confirmed partially_resolved at major severity forces round N+1 (issue #49 regression).
    - id: P9-A2
      clause: A three-obligation finding whose fix covers two returns partially_resolved naming the third; the carried remainder id targets exactly it and appears in round N+1's review scope with carried_from intact.
    - id: P9-A3
      clause: An artifact fix correcting one section while a second still contradicts it returns non-resolved citing both sections.
    - id: P9-A4
      clause: A pre-migration confirm output (no new_findings) still validates.
    - id: P9-A5
      clause: The cycle- and gate-touching regression suites from P1, P5, and P8 pass under max_rounds 3.
```
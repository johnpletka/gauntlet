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
- Schema additions to `schemas/findings.json`: `source`, `lens`, `duplicate_of`, `sources`. **Schema-compatibility pattern (review F-007 — reconciles the repo's strict-output convention with the byte-identical requirement below).** These four fields are **engine/merge-annotated, never emitted by a reviewer agent's structured output**: `source`/`lens` are stamped per member by the engine (which knows the member's profile and lens), and `duplicate_of`/`sources` are written by the deterministic merge step. They are therefore **not** added to the reviewer's strict structured-output schema — that schema keeps the repo's pinned strict-mode convention unchanged (native structured output requires *every* property in `required`, §7-optional fields spelled required-but-nullable, e.g. `suggested_fix: ["string","null"]`), so a single member still emits exactly today's finding shape. The four new fields belong instead to the **persisted findings-record validation** of `schemas/findings.json`, whose compatibility/migration validation path **accepts their absence** — a legacy or single-reviewer artifact that omits them validates, precisely because the engine (not a strict-mode LLM) writes them and can legitimately leave them out. **Emission rule:** these fields are written only by the ensemble merge path (≥ 2 members). A one-member config takes the unchanged single-reviewer path and emits *none* of them — the persisted per-member findings object is byte-identical to today's output (the fields are absent from the serialized JSON, not present-and-null). In a multi-member run, `source`/`lens` are set per member, and `duplicate_of`/`sources` are set only on the merged primary/duplicate records that the merge step produces.
- Step metrics: per-(profile, lens) findings-raised, unique-after-dedup, and post-triage-legitimate counts (`metrics.ensemble.unique_legit_by_member`).
- Panel config in `.gauntlet/config.yaml` / `pipelines/standard.yaml`: the ratified v1 panel (Q2) — the existing codex reviewer (gpt-5.5) plus a Gemini profile on the `api` adapter, whose scope is fully inlined (no file access; reference-mode inputs invalid for it).
- **`gauntlet doctor` coverage of the new panel member (review F-005; Q2 mandate, harness-efficiency FR-6.4):** the added Gemini `api` profile is validated by `doctor`'s existing per-profile model probe (`engine/doctor._real_profile_model_probe` — the LiteLLM resolvability check plus the bounded live round trip), so a misspelled or unavailable Gemini model id fails `doctor` up front rather than only at runtime inside the first ensemble review. P1's job here is to ensure the panel's Gemini profile is *covered* by that probe and to add the failing-fixture test below; the probe machinery itself already ships (harness-efficiency FR-6.4).

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
- Doctor probe (review F-005): a config carrying the panel's Gemini `api` profile with a **misspelled/unavailable model id** makes `gauntlet doctor` report a FAIL for that profile (asserted against the doctor probe path, not a live network call — the LiteLLM-resolvability branch fails closed for an unresolvable id); a valid Gemini model id probes OK. This proves a bad panel model id is caught before the first ensemble review, not at runtime.
- Any cycle-round-count fixture pins `max_rounds: 2` in-fixture (P9 coupling).

**Exit criteria:** tests green; a two-member panel run persists per-member artifacts, a deduped merged set, and per-member yield metrics readable from the manifest; `gauntlet doctor` fails on a misspelled/unavailable Gemini panel model id (review F-005).

**Deferrals:** lens-file retro-proposal governance and the declined-findings registry → P6. A third panel member → post-measurement (§2.2 cap; §9 kill criterion).

---

## P2 — Acceptance mapping + `acceptance_gate`

**Assumption validated:** plan acceptance clauses are mechanically mappable to collector-enumerated test ids in practice, and a deterministic gate catches seeded incompleteness — the structural close of the #54 class (silent partial delivery).

**Deliverables (FR-3.1, FR-3.2):**
- `engine/planphases.py` + `schemas/`: plan phase entries gain a required `acceptance:` list of testable clauses; `phase_lint` fails closed on a clause-less phase.
- Implement-step completion contract gains the acceptance-mapping artifact `artifacts/acceptance-map.json` (§6 shape): each clause id → ≥ 1 evidence `{kind, id}`; `kind` names the collector, `id` is the enumerated node/check. **v1 allowed-kind rule:** `kind` is a closed enum whose only member is `pytest` — the sole collector implemented in v1. A registered collector namespace exists so a future collector plugin can widen the enum, but a `kind` with no registered collector is **schema-invalid**, not a runtime surprise.
- New deterministic `acceptance_gate` step in `engine/steptypes.py`, one instance per distinct collector: verifies every cited id appears in that collector's side-effect-free enumeration (`pytest` ⇒ `pytest --collect-only`) and that every clause is mapped. Any gap parks the phase naming the unmapped clauses. A phase whose evidence declares a `kind` outside the allowed enum (no registered collector) is **rejected at pipeline/plan load** — it never reaches runtime to "park closed," so an unsupported collector cannot masquerade as supported in a plan artifact. **Scope:** proves *citation + existence* only — not that a cited test meaningfully exercises the clause (sufficiency stays the spec-coverage lens's job; G2 scoped accordingly).
- **Collector-execution threat model + P2–P4 interim posture (review F-002 — explicit decision).** `pytest --collect-only` is **not** inert: pytest collection imports `conftest.py` and every test module from the branch under review, so it *executes branch-authored code* at import time. The P5 verifier sandbox (codex `workspace-write` + judge-enforced read-denial) is the isolation backend for branch-code execution, and it does not exist until P5 — so P2 must not run collection wide-open in the interim. **Decision:** P2 ships the `acceptance_gate` now (it is a prerequisite for the #54-class close it validates) but runs collector enumeration under a **fail-closed interim mitigation** for P2–P4, then **migrates enumeration into the P5 verifier backend** (below). Interim mitigation: enumeration runs in a **bounded child subprocess under the bootstrap session's active judge `PreToolUse` hooks** (the same gate protecting every other engine-driven command in this run), with a wall-clock/resource limit and its working directory scoped to the run worktree; a non-zero collector exit, a timeout, or an unparseable enumeration **parks the gate closed** — an absent/failed enumeration is never treated as "all clauses mapped." This is the sole change to the threat model, and it is explicit rather than silent. The **P5 migration** (see P5 deliverables) moves `acceptance_gate` collector enumeration to run *inside* the P5 sandbox backend once it exists, with a migration test proving enumeration executes within the verifier jail; running branch collection under full OS isolation is the target posture, the interim judge-hooked subprocess is the compensating control until it lands.

**Test strategy:**
- Unit: a mapping omitting one clause parks with that clause in notes; a mapping citing a nonexistent pytest node id parks; a mapping/phase declaring a `kind` outside the allowed enum (no registered collector) is rejected at load, not parked at runtime; a complete pytest mapping passes (existence proven, sufficiency not asserted).
- Lint test: a clause-less phase is rejected.
- Collector-execution safety (review F-002): a collector enumeration whose subprocess exits non-zero / times out parks the gate closed (never "all mapped"); a fixture asserting enumeration is spawned under the interim bounded/judge-hooked subprocess path (working dir scoped to the run worktree, resource limit applied), so branch collection does not run wide-open before the P5 sandbox exists.
- Update existing plan fixtures to carry `acceptance:` lists.

**Exit criteria:** tests green; a seeded incomplete-mapping fixture parks deterministically; a complete mapping clears the gate; a failing/timed-out collector enumeration parks closed and the interim enumeration runs under the bounded judge-hooked subprocess posture (review F-002).

**Deferrals:** non-`pytest` collectors (`shell`, `golden`, `integration`) → post-v1. Widening the allowed-kind enum requires *implementing and registering* that collector's plugin (side-effect-free enumeration + config contract); until then the kind stays out of the enum and is load-invalid — v1 ships only the `pytest` collector, with no declare-but-unimplemented path. Migration of `acceptance_gate` collector enumeration into the P5 verifier sandbox backend → **P5** (the interim P2–P4 posture is the bounded judge-hooked subprocess above, review F-002). Deferral reconciliation and size lint → P3.

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
- **Verifier metrics emission (review F-001; §9 behavioral-signal check):** the verifier sub-step records to the run manifest `metrics.verifier.legit_findings` (triage-legitimate behavioral findings this run) and the verifier's `agent_usage` cost, so the §9 metrics ("≥ 1 triage-legitimate behavioral finding per run on average, verifier cost ≤ 10% of run cost") are computed from the manifest without transcript access. These are the input the P6 verifier-revert-to-opt-in retro proposal (review F-001) reads.
- **Migrate `acceptance_gate` collector enumeration into the sandbox backend (review F-002):** now that the P5 verifier sandbox exists, move `acceptance_gate`'s collector enumeration (P2's `pytest --collect-only`, which executes branch-authored `conftest`/test code at import) off the P2–P4 interim judge-hooked-subprocess posture and run it *inside* the same sandbox backend the verifier uses (read-confined to the run worktree copy, network default-deny, resource-bounded), keeping the fail-closed park-on-failure semantics. A migration test proves enumeration executes within the sandbox jail on the v1 backend, not in the interim path.
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
- Metrics (review F-001): a verifier-enabled run emits `metrics.verifier.legit_findings` and the verifier `agent_usage` to the manifest, readable without transcript access (the §9 behavioral-signal instrument).
- Collector-enumeration migration (review F-002): a test proves `acceptance_gate` collector enumeration runs inside the P5 sandbox backend (not the P2–P4 interim subprocess), preserving fail-closed park-on-failure.
- Sandbox tests (integration where a real sandbox is required): an outside-copy credential-file read is denied; a network reach under default-deny fails; a stripped secret env var is absent from the verifier process env; an over-limit execution is killed and parks (never "skipped, proceed"); a stubbed backend-probe failure parks the sub-step closed (verifier never runs unsandboxed).
- Any cycle-round fixture pins `max_rounds: 2` in-fixture (P9 coupling).

**Exit criteria:** tests green (unit + local integration); the seeded behavioral-bug fixture yields a command-evidenced finding; every §7 sandbox item has a passing enforcement test on the v1 backend; an unsupported-host backend probe parks closed; run worktree hash unchanged; `metrics.verifier.legit_findings` + verifier cost readable from the manifest (review F-001); `acceptance_gate` collector enumeration migrated into the sandbox backend with a passing migration test (review F-002).

**Deferrals:** network-permitted verifier profiles beyond the default-deny posture → per-phase acceptance-clause-gated config, post-v1. The `auto_when_clean` consumption of the verifier-clean signal → P8.

---

## P6 — Declined-findings registry + lens governance

**Assumption validated:** precedent context measurably reduces re-litigated findings without suppressing legitimate ones, and lens files can evolve only through ratified governance.

**Deliverables (FR-5.1, FR-5.2):**
- `engine/registry.py` + `<asset_root>/registry/declined.jsonl` (append-only): when a human or triage declines a finding with reasoning, record its fingerprint (Q4: exact category + location-kind + normalized claim keywords) and verdict with full provenance — `repo`, `prd_family`, `prompt_version`, `lens_version`, `schema_version`, `run_id`, `by`, `at`.
- **Version provenance + "in force" definition (review F-004 — the identity two builders must implement identically).** Each version field is a **content hash of the governed asset file with a stable label**: `<asset-label>@<short-hash>` — e.g. `triage@4d3722e` for `prompts/triage.md`, `<lens-id>@<hash>` for a `prompts/lenses/*.md` fragment, `findings@<hash>` for `schemas/findings.json` (the same `<label>@<hash>` form the PRD §6 registry example uses and that the manifest already stamps per run as the prompt/lens/schema hashes). The hash is computed over the file's committed bytes; `lens_version: "none"` records a decline made with no lens in force. **"In force" for the current run** means the recorded hash **equals the hash of that same asset file in the current worktree** — i.e. the decline was recorded against the byte-identical ratified file that is active now. **The authoritative registry of current (ratified) versions is the set of governed asset files themselves** under `prompts/`, `prompts/lenses/`, and `schemas/` (the files whose changes only land through the ratified retro-proposal path, FR-5.1 / PRD §8 governance), surfaced per run as the manifest's recorded prompt/lens/schema hashes; there is no separate version database — a version is "current" iff its recorded hash matches the live governed file's hash. Supersession is therefore implicit: any ratified edit to a governed asset changes its hash, so declines recorded against the prior hash cease to be "in force" the moment the new version lands (a ratified retro proposal may additionally mark specific entries invalid, but the hash comparison is the primary, deterministic test).
- Triage-context injection: a fingerprint-matching future finding surfaces the precedent (verdict, reasoning, run id) as **advisory** data — and only when provenance is current (same repo + PRD family, and each recorded `prompt_version`/`lens_version`/`schema_version` content hash **still equals the current worktree file's hash** per the definition above). A decline under a superseded version (recorded hash ≠ current file hash) or a different PRD family is retained for audit, never injected. The triager retains authority to classify an injected match legitimate.
- Lens governance (FR-5.1): the retro step may propose lens additions (recurring finding patterns) through the existing proposals flow; only ratified proposals change `prompts/lenses/` files. (The files themselves and their prompt-append wiring were delivered in P1; this phase adds the proposal/ratification path.)
- **Proposal-mode kill/rollback triggers (review F-001; PRD §9 enforcement, all *proposal* mode — retro emits a ratifiable proposal, nothing self-tunes).** This phase wires two §9 feedback paths into the same retro-proposal flow the lens governance above uses, so the PRD's kill/rollback criteria are actually exercisable:
  - **Panel-shrink proposal (§9 ensemble-yield kill criterion, §1.3):** when the per-member yield metric `metrics.ensemble.unique_legit_by_member` (emitted by P1) shows a member contributing **< 25% unique-after-dedup legitimate findings across two consecutive comparison runs**, the retro step emits a "shrink the panel" proposal citing those two runs. The panel changes only on human ratification (governed exactly like a lens change).
  - **Verifier-revert-to-opt-in proposal (§9 behavioral-signal miss):** when the verifier metric `metrics.verifier.legit_findings` (emitted by P5) stays **below the §9 behavioral-signal threshold across the first three verifier-enabled runs**, the retro step emits a proposal to revert the verifier from default to opt-in. The verifier profile config changes only on ratification.
  Both read the metrics P1/P5 already persist; neither changes state without a ratified proposal (§9 forbids silent self-tuning; the only *auto* actions are fail-closed).
- **Re-litigation metrics + acceptance criteria.** The step emits, to the run manifest: `metrics.registry.rematched` (review F-001; §9 name — re-litigated findings whose fingerprint matches a current-provenance registry entry and were again triaged) and `injected_precedent_override_count` (injected matches the triager nonetheless classified legitimate). The **acceptance definition of "measurably reduces re-litigation without suppressing"** is operationalized as two checks, not an unmeasured claim: (1) the `metrics.registry.rematched` rate is a persisted, comparable number so the run corpus shows the trend across runs; (2) a matched precedent must remain overridable — a fixture proves an injected precedent can still be triaged legitimate, so injection cannot silently suppress a genuine finding. No population-level threshold is asserted in v1 (single-run acceptance can't establish one); the metric is the instrument and corpus comparison is the honesty check, mirroring P1's measured-number posture.

**Test strategy:**
- Unit: a registered decline surfaces in the triage prompt for a matching finding under compatible provenance; is absent for a non-matching fingerprint; is absent for a fingerprint match whose entry's prompt/lens/schema version is stale or whose PRD family differs; the registry file round-trips with all provenance fields.
- Version-provenance fixtures (review F-004): an entry whose recorded `<label>@<hash>` **equals** the current worktree asset file's hash injects (current); an otherwise-matching entry whose recorded hash **differs** from the current file's hash (a ratified edit landed since) is withheld (stale) and retained in the file for audit — asserting "in force" is the hash-equality test against the governed file, not a free-text label compare.
- Metrics/suppression: a run with a matching precedent emits `metrics.registry.rematched` (review F-001) and `injected_precedent_override_count` to the manifest; a **non-suppression fixture** where triage, given an injected matching precedent, still classifies the finding legitimate (the override is counted, the finding survives) — proving advisory precedent does not gate out a legitimate finding.
- Proposal triggers (review F-001): a fixture corpus of two consecutive runs with a panel member below 25% unique-legit yield emits a ratifiable panel-shrink proposal citing both runs (and does **not** shrink the panel without ratification); a corpus of three verifier-enabled runs below the behavioral-signal threshold emits a ratifiable verifier-revert-to-opt-in proposal — both are proposal artifacts, no config self-mutation.
- Wiring: a lens fragment appears in the review prompt (regression from P1); a retro fixture produces a lens proposal artifact; nothing mutates `prompts/lenses/` without the ratification path.

**Exit criteria:** tests green; a matching decline injects under current provenance and is withheld under stale/foreign provenance (hash-equality "in force" test, review F-004); the re-litigation metric `metrics.registry.rematched` is readable from the manifest (review F-001); the non-suppression fixture shows an injected precedent overridden to legitimate; the retro path emits a ratifiable lens proposal, and the §9 panel-shrink and verifier-revert proposals fire from the P1/P5 metrics under their §9 windows (review F-001) — all without mutating governed assets directly.

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
- Pipeline-load validation vs. runtime `not_configured` — **two disjoint, both-reachable cases (review F-008).** These are not the same condition and neither is dead code:
  - **Load-time rejection (static config gap):** PRD/plan gates reject `auto_when_clean` outright, and a code phase whose *pipeline definition* declares `auto_when_clean` with **no verifier sub-step configured** is rejected at pipeline load — `verifier ran clean` can never hold for it, so the misconfiguration is caught before the run starts, never at a gate. This is the only path for a *statically* verifier-less `auto_when_clean` phase; it can therefore never reach runtime to be recorded `not_configured`.
  - **Runtime `verifier: not_configured` (dynamic / legacy, config was valid at load):** reserved for the cases the load check cannot see — a verifier sub-step that was **configured at load but did not produce a clean result at runtime because it was dynamically disabled/skipped** (e.g. a conditional `when:` evaluated false, or an operator skip), a manifest **resumed from a run instance that predates the verifier configuration**, or a **legacy run** whose pipeline was authored before the verifier existed. In each of these the phase *passed* load validation (a verifier was configured, or the run predates the check) yet no clean verifier result exists — so the predicate records `verifier: not_configured`, a predicate miss that **parks closed** (never auto-approves). Load rejection and runtime parking thus cover strictly different states; the runtime path is unreachable for a statically-missing verifier precisely because load rejection fires first.
- FR-4.2: auto-approved gates remain human-reversible — `gauntlet rollback` to the phase boundary works unchanged, and the run's final `PR.md` enumerates every auto-approved gate with its evidence snapshot for collective ratification at the audit boundary.
- **Reversal circuit breaker (PRD §9, in scope):** a recorded human reversal of any auto-approved gate flips the run's effective auto-approval policy to `always` (human-required) for the **remainder of the run** — every subsequent code gate parks for a human even if its clean predicate holds. The reversal and the policy flip are recorded in the manifest; the clean predicate reads the flipped policy as a fail-closed short-circuit (reversal recorded ⇒ predicate cannot auto-approve). This is a deterministic in-run circuit breaker, not a post-v1 follow-on: without it an auto-approval system keeps auto-approving after a human has signalled distrust.

**Test strategy:**
- Unit: each single predicate violation parks; the all-clean case auto-approves with the snapshot present; PRD/plan gates reject `auto_when_clean` at load. **Both F-008 cases, distinctly (each reachable, neither masking the other):** (a) *load-reject* — a pipeline declaring `auto_when_clean` on a code phase with **no verifier sub-step configured** is rejected at pipeline load, before any run; (b) *runtime `not_configured`* — a phase that **passed load** (verifier configured, or a resumed/legacy manifest predating the verifier) but whose verifier result is absent/skipped at runtime is recorded `verifier: not_configured` and **parks** rather than auto-approving. A test asserts case (b) is reached via a resumed/legacy manifest (or a dynamically-skipped verifier), *not* via a statically verifier-less pipeline (which case (a) already rejects at load).
- PR-draft: auto-approved gates enumerated in `PR.md` with evidence.
- Reversal circuit breaker: a run with a recorded reversal parks a subsequent otherwise-all-clean gate rather than auto-approving; the manifest records the reversal and the effective-policy flip.
- Predicate fixtures assert round-1 convergence under a pinned `max_rounds: 2` (P9 coupling); P9 re-validates them under `max_rounds: 3`.

**Exit criteria:** tests green; every single-signal violation parks; the all-clean path auto-approves with a durable evidence snapshot surfaced in `PR.md`; **load-time rejection** holds for document gates and *statically* verifier-less `auto_when_clean` phases, while **runtime `verifier: not_configured`** parks a phase that passed load but lost its verifier result dynamically or on a resumed/legacy manifest — both cases distinctly tested (review F-008); a recorded reversal flips the run's effective policy to `always` and a subsequent clean gate parks.

**Deferrals:** CI-side / PR-bot review integration → out of scope (§2.2).

---

## P9 — Convergence honesty + `max_rounds` bump

**Assumption validated:** forcing accepted partials to carry a concrete remainder converges within the round budget instead of oscillating, and issue #49's silent-closure class is reproducibly shut. This phase changes cycle *semantics*, not detection, and lands last because of the global `max_rounds` coupling.

**Size note:** four FR references (FR-6.1–FR-6.4), one over the default bound, kept together deliberately: they are one cohesive convergence semantic — the engine forcing rule (FR-6.1) is inert without the confirm-prompt remainder capture that gives the forced round a target (FR-6.2), and the severity/consistency rules (FR-6.3, FR-6.4) feed the same carry. Splitting would ship a half-working convergence guarantee.

**Deliverables (FR-6.1, FR-6.2, FR-6.3, FR-6.4):**
- `engine/cycle.py` `_forcing_open`: an accepted (`fix_now`) finding whose confirm verdict is `partially_resolved` is a forcing open **regardless of severity** (today it forces only at blocking severity — issue #49's escape).
- Confirm remainder carry (§6): the confirm pass emits a `new_findings` entry that is a complete findings-schema object plus `carried_from: <finding-id>`, with a deterministic id in a **reserved carry namespace** — the base id is `<carried_from>-r<round>`, and because externally-supplied finding ids may already occupy that string (or one parent may carry multiple remainders, or a remainder may itself be re-carried), the engine appends a disambiguating suffix `-c<N>` where `N` is the smallest non-negative integer making the id unique **against the union of all finding ids seen this run** (every prior round's findings and every id already assigned this round); `N = 0` (`-c0`) is emitted explicitly rather than omitted, so the format is uniform and the base string is never itself a valid final id (guaranteeing no collision with a raw `<carried_from>-r<round>` that a reviewer might have supplied). Id assignment is order-deterministic (findings processed in stable id order). The entry's `location`/`claim`/`evidence` describe the *specific remainder*, and severity is set per the FR-6.1 rule (`blocking` for a privacy/security leakage boundary or a golden/parity oracle guarding a behavior-changing refactor; `major` otherwise). A carried remainder does **not** re-enter triage (it inherits its parent's `fix_now` acceptance — this bounds oscillation); it merges into round N+1 *ahead of* fresh findings with `carried_from` intact in the round manifest. **Schema-compatibility pattern (review F-007 — same rule as P1).** Because `new_findings` is emitted by the confirm agent through the strict `--output-schema` path, the additive fields follow the repo's pinned strict-mode convention rather than being "optional-and-absent": the expanded `new_findings` item lists every findings-schema field (plus `carried_from`) in the item's `required`, with `carried_from` spelled **required-but-nullable** (`["string","null"]`, `null` when the confirm surfaces an ordinary diff regression rather than a carried remainder) — the same convention `suggested_fix` already uses. Backward compatibility is preserved through the **persisted-artifact validation path**, not by omitting the field from `required`: `new_findings` is already a required top-level array, so a pre-migration confirm output with an **empty** `new_findings` validates unchanged, and the migration validator accepts pre-migration entries that predate `carried_from`. Additive to `schemas/confirm.json`/`findings.json`; a pre-migration confirm with no `new_findings` entries still validates.
- Prompt changes (+ scaffold twins): `cycle-fix.md` — treat an enumerated-obligation finding as an acceptance checklist (restate each item, map each to its change/assertion, state deferrals explicitly); `cycle-confirm.md` — mirror the check (any uncovered enumerated item ⇒ `partially_resolved` naming it) plus remainder-capture and the severity rule, and (FR-6.4) artifact-mode intra-document consistency (a remaining contradiction between sections ⇒ non-`resolved` citing both); `review-document.md`/`review-code.md`/`triage.md` — an untestable acceptance/parity/golden oracle for a behavior-changing refactor classifies `blocking` unless the finding supplies the exact fixture matrix and expected outcomes (FR-6.3).
- `.gauntlet/config.yaml`/`pipelines/standard.yaml`: `max_rounds` 2 → 3 on the shipped `plan-cycle`/`impl-cycle` so a carried remainder has a round to land before max-rounds escalation parks. Fail-closed terminus (escalate to human at max-rounds) unchanged.
- A labeled entry in `prompts/triage-corpus.jsonl` encoding the untestable-oracle rule (the PLAN F-006 case).

**Test strategy:**
- Unit: a `fix_now` finding confirmed `partially_resolved` at `major` severity forces round N+1 (issue #49 regression fixture); exhausting `max_rounds` with an open remainder parks (`cycle_escalation` unchanged).
- Prompt-content: remainder-capture + severity rule in `cycle-confirm.md`; enumerated-obligation checklist in both `cycle-fix.md` and `cycle-confirm.md`; untestable-oracle rule in the review/triage prompts.
- Fixture cycles: a three-obligation finding whose fix covers two → confirm returns `partially_resolved` naming the third and the carried remainder (id `<id>-r<round>-c<N>` in the reserved namespace) targets exactly it and appears in round N+1's review scope with `carried_from` intact; a collision fixture where the base `<carried_from>-r<round>` id already exists among input findings → the carried remainder gets the next free `-c<N>` suffix (no id collision); an artifact fix correcting one section while a second still contradicts it → non-`resolved` citing both sections.
- Schema compatibility (review F-007): a pre-migration confirm output (empty `new_findings`) still validates; the migrated strict-output schema lists `carried_from` in the item `required` as nullable, and a diff-regression `new_findings` entry with `carried_from: null` validates (proving the required-but-nullable convention, not an absent-optional field, is what preserves compatibility).
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
    - id: P1-A5
      clause: gauntlet doctor fails on a misspelled/unavailable Gemini panel model id (the added api profile is covered by the per-profile model probe, harness-efficiency FR-6.4), so a bad panel model id is caught before the first ensemble review.
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
    - id: P2-A6
      clause: Collector enumeration (which executes branch-authored conftest/test code at import) runs under the P2–P4 fail-closed interim posture — a bounded subprocess under the active judge hooks scoped to the run worktree — and parks the gate closed on a non-zero/timed-out/unparseable enumeration; migration of enumeration into the P5 sandbox backend is a P5 obligation.
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
    - id: P5-A6
      clause: metrics.verifier.legit_findings and the verifier agent_usage cost are emitted to the run manifest (the §9 behavioral-signal instrument, readable without transcript access).
    - id: P5-A7
      clause: acceptance_gate collector enumeration is migrated to run inside the P5 sandbox backend (off the P2–P4 interim subprocess), with a test proving enumeration executes within the sandbox jail and retains fail-closed park-on-failure.
- id: P6
  title: Declined-findings registry + lens governance
  goal: Provenance-gated declined-findings registry injected as advisory triage context plus retro-proposal governance for lens files; validates that precedent reduces re-litigation without suppressing legitimate findings.
  acceptance:
    - id: P6-A1
      clause: A fingerprint-matching decline surfaces in the triage prompt as advisory data under compatible (current) provenance.
    - id: P6-A2
      clause: A decline is injected only when each recorded version field's content hash (<label>@<hash>) still equals the current worktree asset file's hash ("in force"); an entry whose recorded hash differs from the current governed file (superseded) or whose PRD family differs is retained for audit but withheld from injection.
    - id: P6-A3
      clause: The retro path emits a ratifiable lens proposal without directly mutating prompts/lenses/ files.
    - id: P6-A4
      clause: metrics.registry.rematched is emitted to run metrics, and a fixture proves an injected matching precedent can still be triaged legitimate (no suppression).
    - id: P6-A5
      clause: A two-consecutive-run corpus with a panel member below 25% unique-legit yield emits a ratifiable panel-shrink proposal (no unratified panel change); a three-verifier-run corpus below the behavioral-signal threshold emits a ratifiable verifier-revert-to-opt-in proposal (§9 proposal-mode enforcement, no config self-mutation).
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
      clause: PRD/plan gates reject auto_when_clean at load, and a code phase declaring auto_when_clean with no verifier sub-step configured is rejected at pipeline load (static config gap).
    - id: P8-A6
      clause: A phase that passed load (verifier configured, or a resumed/legacy manifest predating the verifier) but whose verifier result is absent/skipped at runtime is recorded verifier=not_configured and parks — reached distinctly from the load-reject case, never masking it.
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
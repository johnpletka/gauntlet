# Triage accuracy — P4 assumption test (review F-009)

- model: `gpt-5-mini`
- corpus: `prompts/triage-corpus.jsonl` (37 hand-labeled findings)
- verdict agreement: **94.6%** (35/37; exit ≥ 85%)
- action agreement (secondary): 91.9%
- blocking→reject misses without escalation: **0** (exit: zero)
- blocking→reject misses caught by escalation: 0
- exit criteria: **PASS**

## Per-severity confusion matrices (label rows × predicted columns)

### blocking (n=10)

| label \ predicted | legitimate | bikeshedding | premature_optimization | not_applicable |
|---|---|---|---|---|
| legitimate | 10 | 0 | 0 | 0 |

### major (n=22)

| label \ predicted | legitimate | bikeshedding | premature_optimization | not_applicable |
|---|---|---|---|---|
| legitimate | 21 | 0 | 0 | 0 |
| premature_optimization | 1 | 0 | 0 | 0 |

### minor (n=5)

| label \ predicted | legitimate | bikeshedding | premature_optimization | not_applicable |
|---|---|---|---|---|
| bikeshedding | 1 | 0 | 0 | 0 |
| legitimate | 4 | 0 | 0 | 0 |

## Disagreements

- `plan-F-008` (major): labeled **premature_optimization**, model said **legitimate** (confidence high) — Unrestricted plugin/entry-point loading creates an unbounded code-execution surface; for a fail-closed safety harness this is a material security risk that can lead to unauthorized code execution or supply-chain attacks. The plan must define trust boundaries (allowlisting, version pinning, signing/audit requirements and warnings) before exposing dynamic extension loading.
- `plan-OQ-2` (minor): labeled **bikeshedding**, model said **legitimate** (confidence medium) — Committing raw events.jsonl by default can leak large or sensitive payloads and pollute repositories; this is a real spec/design gap around data-handling defaults. The plan should be updated to make raw-stream commits opt-in or document filtering/consent, but it is a plan-level decision rather than an immediate code fix so it can be deferred to a plan revision.

## Corpus caveat (recorded honestly)

The corpus is harvested from the bootstrap's own plan/P1/P2/P3 review rounds, where almost every finding was triaged `legitimate` (34/36); `nit` severity never occurred. A constant-`legitimate` predictor would score ~94% — the aggregate gate is therefore weak on this data, which is exactly why the blocking-miss criterion and the per-severity matrix are the operative checks (review F-009). FR-6.5's human-corrected cases are the designed mechanism for growing the non-legitimate side.

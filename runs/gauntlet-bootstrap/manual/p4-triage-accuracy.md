# Triage accuracy — P4 assumption test (review F-009)

- model: `gpt-5-mini`
- corpus: `prompts/triage-corpus.jsonl` (37 hand-labeled findings)
- verdict agreement: **94.6%** (35/37; exit ≥ 85%)
- action agreement (secondary): 89.2%
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

- `plan-F-008` (major): labeled **premature_optimization**, model said **legitimate** (confidence high) — Unrestricted plugin/entry-point loading is an unbounded code-execution surface and therefore a genuine security risk for a fail-closed safety harness. The plan as cited lacks a trust model, allowlisting, version pinning, and auditing/warnings, so this must be specified before exposing dynamic extension loading.
- `plan-OQ-2` (minor): labeled **bikeshedding**, model said **legitimate** (confidence high) — Defaulting events.jsonl to be commit-friendly can cause heavy or sensitive raw event streams to be placed in the repository, creating a material process/security/operability risk. The plan should change the default to ignore raw streams or require explicit opt-in (or at least document and enforce an explicit policy) in this phase so it is addressed before rollout.

## Corpus caveat (recorded honestly)

The corpus is harvested from the bootstrap's own plan/P1/P2/P3 review rounds, where almost every finding was triaged `legitimate` (34/36); `nit` severity never occurred. A constant-`legitimate` predictor would score ~94% — the aggregate gate is therefore weak on this data, which is exactly why the blocking-miss criterion and the per-severity matrix are the operative checks (review F-009). FR-6.5's human-corrected cases are the designed mechanism for growing the non-legitimate side.

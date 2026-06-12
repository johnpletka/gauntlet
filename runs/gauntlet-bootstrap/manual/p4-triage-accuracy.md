# Triage accuracy — P4 assumption test (review F-009)

- model: `gpt-5-mini`
- corpus: `prompts/triage-corpus.jsonl` (36 hand-labeled findings)
- verdict agreement: **94.4%** (34/36; exit ≥ 85%)
- action agreement (secondary): 91.7%
- blocking→reject misses without escalation: **0** (exit: zero)
- blocking→reject misses caught by escalation: 0
- exit criteria: **PASS**

## Per-severity confusion matrices (label rows × predicted columns)

### blocking (n=9)

| label \ predicted | legitimate | bikeshedding | premature_optimization | not_applicable |
|---|---|---|---|---|
| legitimate | 9 | 0 | 0 | 0 |

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

- `plan-F-008` (major): labeled **premature_optimization**, model said **legitimate** (confidence high) — Unrestricted plugin/entry-point loading in a safety-critical orchestration plan is a genuine code-execution surface that can violate the fail-closed safety posture. The plan should define trust boundaries, allowlisting/version-pinning, and auditing/doctor warnings before enabling third-party extensions to avoid runtime compromise.
- `plan-OQ-2` (minor): labeled **bikeshedding**, model said **legitimate** (confidence medium) — Treating events.jsonl as commit-friendly by default is a material spec/operability/privacy risk: raw event streams can be large or contain content teams should not put into git and would pollute history. The plan should be changed now to default to ignore or require explicit opt-in so the system remains fail-closed.

## Corpus caveat (recorded honestly)

The corpus is harvested from the bootstrap's own plan/P1/P2/P3 review rounds, where almost every finding was triaged `legitimate` (34/36); `nit` severity never occurred. A constant-`legitimate` predictor would score ~94% — the aggregate gate is therefore weak on this data, which is exactly why the blocking-miss criterion and the per-severity matrix are the operative checks (review F-009). FR-6.5's human-corrected cases are the designed mechanism for growing the non-legitimate side.

# Gauntlet

Adversarial multi-agent development harness. Every artifact — PRD, plan, each
implementation phase — runs the gauntlet of adversarial review before it ships.

The spec is `PRD-gauntlet.md`. The bootstrap plan is `runs/gauntlet/plan.md`.

## Onboarding (≤ 3 commands)

With the two agent CLIs (`claude`, `codex`) installed and authenticated, a
teammate goes from clone to a running pipeline in three commands (FR-1):

```sh
uv tool install git+<repo-url>   # 1. install the `gauntlet` console script
gauntlet doctor                  # 2. validate CLIs, auth, hooks, judge, keys
gauntlet run <slug>              # 3. run a pipeline (after authoring its PRD)
```

In a repo that does not yet carry the Gauntlet config, scaffold it first:

```sh
gauntlet init               # idempotent: config, pipeline, prompts, policy, hooks
gauntlet init --from-repo   # team-adopter path: only (re)wire this machine's hooks
```

`gauntlet doctor` reports actionable, per-check status (installed CLI versions
vs. the verified pin file, hook wiring, judge startability, ApiAdapter keys in
the environment — never in repo config) and exits non-zero on any blocker.

## Status

Bootstrapping. P6 (`init` / `doctor` / rollout packaging) in progress.

## Development

```sh
uv sync                       # create venv, install deps + package (editable)
uv run pytest                 # unit suite (no credentials required)
uv run pytest -m integration  # contract tests against live CLIs/APIs
```

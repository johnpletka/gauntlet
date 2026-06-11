# Gauntlet

Adversarial multi-agent development harness. Every artifact — PRD, plan, each
implementation phase — runs the gauntlet of adversarial review before it ships.

The spec is `PRD-gauntlet.md`. The bootstrap plan is `runs/gauntlet/plan.md`.

## Status

Bootstrapping. P1 (agent adapters + golden-path smoke) in progress.

## Development

```sh
uv sync                       # create venv, install deps + package (editable)
uv run pytest                 # unit suite (no credentials required)
uv run pytest -m integration  # contract tests against live CLIs/APIs
```

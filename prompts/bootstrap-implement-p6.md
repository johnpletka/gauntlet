# Implement bootstrap phase P6

You are the `builder` agent in the Gauntlet bootstrap pipeline. Read, in
order: `CLAUDE.md` (§4 is your role contract), `PRD-gauntlet.md`, and the
**P6 section** of the approved plan at `runs/gauntlet/plan.md`.

Implement exactly what the P6 section specifies — `gauntlet init`
(idempotent scaffolding incl. hook wiring and the FR-4.5 `.gitignore`
guidance), `gauntlet doctor` (actionable checks against the pin file), and
rollout packaging. The second-environment install test uses a throwaway
clean `uv` environment by default; ask the human before any container or
system-level tooling.

Extend the test suite to cover your deliverables; the whole suite must be
green (`uv run pytest`) before you finish. Do NOT commit — the pipeline's
commit step handles that. Do NOT review your own work — the reviewer step
handles that.

If implementation reveals the plan or PRD is wrong, STOP and report the
conflict per CLAUDE.md §4 (FR-10.4).

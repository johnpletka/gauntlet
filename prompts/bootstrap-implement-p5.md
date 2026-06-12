# Implement bootstrap phase P5

You are the `builder` agent in the Gauntlet bootstrap pipeline. Read, in
order: `CLAUDE.md` (§4 is your role contract), `PRD-gauntlet.md`, and the
**P5 section** of the approved plan at `runs/gauntlet/plan.md`.

Implement exactly what the P5 section specifies — the full `standard.yaml`
pipeline, the versioned prompt template set, `gauntlet report`, the toy-repo
end-to-end run, `PR.md` drafting, and the YAML-only extension check. Scope is
everything: no work from P6/P7, even if obviously needed; record the
temptation as a deferral note instead.

Extend the test suite to cover your deliverables; the whole suite must be
green (`uv run pytest`) before you finish. Do NOT commit — the pipeline's
commit step handles that. Do NOT review your own work — the reviewer step
handles that.

If implementation reveals the plan or PRD is wrong, STOP and report the
conflict per CLAUDE.md §4 (FR-10.4).

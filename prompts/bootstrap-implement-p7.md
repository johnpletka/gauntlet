# Implement bootstrap phase P7

You are the `builder` agent in the Gauntlet bootstrap pipeline. Read, in
order: `CLAUDE.md` (§4 is your role contract), `PRD-gauntlet.md`, and the
**P7 section** of the approved plan at `runs/gauntlet/plan.md`.

Implement exactly what the P7 section specifies — the `retrospective` step
type, `gauntlet feedback`, proposal generation into `retro/proposals/` as
literal path-contained diffs, `gauntlet proposals review` (governed apply,
no self-application), `prompts/CHANGELOG.md` accumulation, and
`gauntlet report --trend` metrics.

Extend the test suite to cover your deliverables; the whole suite must be
green (`uv run pytest`) before you finish. Do NOT commit — the pipeline's
commit step handles that. Do NOT review your own work — the reviewer step
handles that.

If implementation reveals the plan or PRD is wrong, STOP and report the
conflict per CLAUDE.md §4 (FR-10.4).

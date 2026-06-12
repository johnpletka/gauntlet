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

Resolved upstream conflicts (do not re-raise these):
- FR-5.1 ↔ FR-9.2 commit labels for the PRD/plan cycles — RESOLVED, ratified
  2026-06-12 (BOOTSTRAP-NOTES #28): the commit format now admits `PRD`/`PLAN`
  stage prefixes (`PRD.1: Address review — …`), and `standard.yaml`'s
  `prd-cycle`/`plan-cycle` steps must carry `phase: PRD` / `phase: PLAN`.
  Rollback `--phase N` targets remain numeric phases only.

If implementation reveals the plan or PRD is wrong in some OTHER way, STOP
and report the conflict per CLAUDE.md §4 (FR-10.4).

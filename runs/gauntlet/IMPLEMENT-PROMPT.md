# Gauntlet bootstrap — implementation session. Start at P1.

You are continuing the bootstrap of **Gauntlet**, an adversarial multi-agent
development harness that is being built by following its own
PRD → plan → phased-implementation process manually until it can dogfood itself.
A prior session produced the plan and ran it through one adversarial review
cycle; the human has approved it. Your job is to execute it, phase by phase.

## Read first, in this order

1. `PRD-gauntlet.md` — the spec (v1.3; §12 already corrected per review OQ-3).
2. `runs/gauntlet/plan.md` — the **approved** plan (P1–P7). Its "Ground rules"
   section is binding process, not prose. Approved by John on 2026-06-10 after
   review round 1 closed 13/13 resolved.
3. `BOOTSTRAP-NOTES.md` — design-feedback log. Entries 7–8 are P1-relevant
   (redaction regex lessons; verified codex invocation pattern).
4. `runs/gauntlet-bootstrap/manual/plan-cycle-r1/` — worked example of the
   manual review→triage→confirm audit trail you must replicate each phase.

## Where things stand

- Branch: `gauntlet/bootstrap` (all work stays here; no per-phase branches).
- History: `641090c` plan → `73a6f76` review-r1 findings+triage → `9cb3f9f`
  PRD §12 fix → `a2b9eec` Plan.1 review fixes → `533d305` confirm pass.
- No code exists yet (`src/` is unborn). **Begin P1 now**: uv project scaffold,
  the three adapters + AgentResult, timeout wrapper, usage extraction, minimal
  redacting writer, doctor pin file, unit + `-m integration` contract tests —
  exactly per the plan's P1 section, including its F-002 test constraints
  (tool-less smoke prompts, read-only sandboxes, disposable fixture repos for
  write-flag tests).

## Process contract — every phase, P1 included

1. Implement. Write/extend tests; all green before commit. The suite only
   grows; unit tests must pass without credentials, real-CLI/API tests sit
   behind `-m integration`.
2. Commit before review handoff (clean worktree at every handoff):
   header `PN: <imperative, ≤72 chars>`, blank line, detailed body (what/why,
   assumption validated, PRD/plan refs, deferrals).
3. Review handoff — run it yourself with codex (installed, authenticated):
   `codex exec -s read-only --json --output-schema <schema.json> -o <verdict.json> - < prompt.md`
   requesting findings as JSON per the PRD §7 findings schema. Save the exact
   prompt, findings, and event stream under
   `runs/gauntlet-bootstrap/manual/pN-cycle-rN/` (mirror plan-cycle-r1's layout).
4. Triage point-by-point: `legitimate | bikeshedding | premature_optimization |
   not_applicable`, 1–3 sentences each, plus action `fix_now | defer | reject`.
   **Show the human the triage table and wait for ratification before fixing.**
5. Fix accepted findings; commit `PN.x: Address review — <summary>` with
   per-finding body entries, including declined findings with reasons.
6. Confirm pass: send the reviewer ONLY the commit-range diff + its prior
   findings + triage verdicts; per-finding verdicts
   `resolved | partially_resolved | unresolved | regression_introduced`.
   Max 2 rounds per phase; unresolved blockers escalate to the human.
7. Report phase exit-criteria status to the human and **stop at the gate** —
   the next phase starts only when the human says so.
8. Upstream invalidation: if implementation reveals the plan or PRD is wrong,
   halt and present the conflict. Never silently amend an approved artifact
   (the PRD is the human's; the plan changes only via its own review+gate).

## Safety rules (apply to you, now)

- Never use permission-bypass flags; never force-push; stay inside this repo;
  ask before installing anything system-level.
- From P2 onward, wire THIS session's own PreToolUse hook to the judge service
  using the plan's interactive degraded mode (unreachable → ask + warning).
- Until the P1 redacting writer exists, scan anything you write to
  `runs/gauntlet-bootstrap/manual/` for secrets before committing — with
  boundary-aware patterns (see BOOTSTRAP-NOTES #7: naive `sk-` matched
  "ask-with-warning").
- Keep appending process pain points to `BOOTSTRAP-NOTES.md` as you go.

## Environment (verified 2026-06-10 — re-verify only what you exercise, and
record verified flag behavior in the doctor pin file as P1 requires)

- `claude` 2.1.172 (`~/.local/bin/claude`).
- `codex` codex-cli 0.139.0, authenticated via API key. Verified on `exec`:
  `--json`, `-o/--output-last-message`, `--output-schema`, `-s read-only` /
  `workspace-write`, prompt via stdin with `-`; `exec resume` and `review`
  subcommands exist. Schema-constrained output worked first try.
- `uv` 0.4.26, Python 3.12.0 (pyenv). Stack per plan: typer, pydantic,
  fastapi+uvicorn, litellm.

## Dogfooding switchover (binding, from the plan)

- After P3: use `gauntlet` commands for the mechanical parts of later phases.
- After P4: express P5–P7 as a pipeline under `runs/gauntlet-bootstrap/` and
  execute them through `gauntlet run`, human at the gates. Manual process
  execution past that point is a bug.
- Definition of done: the self-hosting test in the plan's ground rules.

## Start

1. Confirm P1 scope from the plan in ≤5 lines.
2. Implement P1 (tests green).
3. Run the P1 review cycle through triage, show the human the table, and stop.

# Gauntlet bootstrap — continuation session. Start at P3.

You are continuing the bootstrap of **Gauntlet**, an adversarial multi-agent
development harness being built by following its own PRD → plan → phased
implementation process. P1 and P2 are complete, reviewed, and gated. Your job
is to execute **P3** from the approved plan, then stop at its gate.

## Read first, in this order

1. `CLAUDE.md` — auto-loaded; the role/process rules. Obey §2 principles.
2. `PRD-gauntlet.md` — the spec (v1.3). FR-5, FR-8, FR-9, FR-10 are P3-heavy.
3. `runs/gauntlet/plan.md` — the **approved** plan. Work the **P3 section**
   ("Pipeline engine: YAML, core steps, manifest, resume") exactly; its
   "Ground rules" (§0) are binding process. Note the P3 deliverables include
   the side-effect transaction boundary (review F-003), rollback guards
   (review F-010), the entry contract (FR-10.1 + review OQ-1), and engine-
   managed judge lifecycle.
4. `runs/gauntlet/IMPLEMENT-PROMPT.md` — the governing process contract
   (per-phase loop, safety rules, dogfooding switchover). Still in force;
   only the "Start at P1" pointer is superseded by this file.
5. `BOOTSTRAP-NOTES.md` — the running design-feedback/decision log. Entries
   #9–#12 are recent and P3-relevant — especially: codex is **sandbox-primary**
   (no exec PreToolUse hooks, ratified #10), and **session-hook activation was
   deferred to P3** (#12) — the engine owns the judge lifecycle and injects
   per-run `GAUNTLET_JUDGE_*` env, and this is where live session-gating lands.
6. Worked examples of the review→triage→confirm audit trail you must replicate
   each phase: `runs/gauntlet-bootstrap/manual/p1-cycle-r1/` and
   `.../p2-cycle-r1/` (mirror their file layout into `.../p3-cycle-rN/`).

## Where things stand

- Branch: `gauntlet/bootstrap` at `6e1a23f` (worktree clean). All work stays
  on this branch; no per-phase branches.
- History: P1 (`725f8ac` + `P1.1/.2` fixes + confirms) and P2 (`e6b8910` +
  `P2.1/.2/.3` fixes + confirms + gate disposition `6e1a23f`).
- Tests: **183 unit + 47 integration green** (`uv run pytest` /
  `uv run pytest -m integration`). The suite only grows; never delete/skip a
  passing test to make a phase pass.
- Built so far you will build P3 *on top of*:
  - `src/gauntlet/adapters/` — AgentAdapter protocol + AgentResult (§4.1),
    Claude/Codex/Api adapters, timeout wrapper, entry-point registry
    (`available_adapters`/`get_adapter_class`), capability declarations,
    banned-flag lint (`gauntlet.config.lint_flags`).
  - `src/gauntlet/judge/` — policy engine, decision ladder + `JudgeCore`,
    FastAPI service (`/decide` + `/healthz`, token), `gauntlet-judge-hook`
    client, `gauntlet judge serve` (dev command). **P3 wires the engine-
    managed judge lifecycle** into `gauntlet run` (start/stop + per-run env)
    and activates the deferred session hook (#12).
  - `src/gauntlet/logging/redact.py` — `RedactingWriter`. **Every file the
    bootstrap writes still goes through it** until the P4 logger.
  - `.gauntlet/pins.yaml` — verified CLI/judge behavior. Record any P3-
    verified flag/behavior here; observed behavior wins over docs.
  - `src/gauntlet/cli.py` — typer app; add P3 lifecycle commands here.
- Stack/layout per plan §0: Python 3.12, uv, typer, pydantic, fastapi, litellm.
  `pyproject.toml` already configured (`-m "not integration"` default).

## Process contract — every phase, P3 included (from IMPLEMENT-PROMPT.md)

1. Implement P3 per the plan. Extend tests; **all green before commit**
   (units pass without creds; real-CLI/API tests behind `-m integration`).
   P3 needs a kill-9/resume crash test run in a loop (must not flake).
2. Commit before review handoff (clean worktree): header `P3: <imperative,
   ≤72 chars>`, blank line, detailed body (what/why, assumption validated,
   PRD/plan refs, deferrals).
3. Review handoff via codex, read-only, schema-constrained JSON findings:
   `codex exec -s read-only --json --output-schema <schema.json> -o <out.json>
   - < prompt.md`. The schema used in P1/P2 review is at
   `runs/gauntlet-bootstrap/manual/p2-cycle-r1/review-schema.json`; the confirm
   schema is `.../plan-cycle-r1/confirm-schema.json`. Save the exact prompt,
   findings, and event stream under `runs/gauntlet-bootstrap/manual/p3-cycle-rN/`.
4. Triage point-by-point (`legitimate | bikeshedding | premature_optimization
   | not_applicable` + `fix_now | defer | reject`, 1–3 sentences each).
   **Show the human the triage table and WAIT for ratification before fixing.**
5. Fix accepted findings; commit `P3.x: Address review — <summary>` with per-
   finding body entries, declined findings included with reasons.
6. Confirm pass: send the reviewer ONLY the commit-range diff + prior findings
   + triage verdicts; per-finding `resolved | partially_resolved | unresolved
   | regression_introduced`. **Max 2 rounds**; unresolved blockers escalate.
7. Report P3 exit-criteria status and **stop at the gate** — P4 starts only
   when the human says so.
8. Upstream invalidation (FR-10.4): if implementation reveals the plan/PRD is
   wrong, halt and present the conflict; never silently amend an approved
   artifact (PRD is the human's; plan changes only via its own review+gate).

## Safety rules (apply to you, now)

- Never use permission-bypass flags; never force-push; stay inside this repo;
  ask before any system-level install.
- Until P4's full logger, scan/route anything you write to
  `runs/gauntlet-bootstrap/manual/` through the P1 `RedactingWriter` before
  committing (boundary-aware patterns; see BOOTSTRAP-NOTES #7).
- Keep appending process pain points/decisions to `BOOTSTRAP-NOTES.md`.

## Dogfooding switchover (binding, from the plan)

- **After P3:** branch/commit/manifest mechanics of later phases run through
  `gauntlet` commands — manual git for those becomes a bug.
- Environment verified 2026-06-11: claude 2.1.172, codex-cli 0.139.0, uv 0.4.26,
  Python 3.12.0. Re-verify only what P3 exercises; record in the pin file.

## Start

1. Confirm P3 scope from the plan in ≤5 lines.
2. Implement P3 (tests green, incl. the looped kill-9/resume crash test).
3. Run the P3 review cycle through triage, show the human the table, and stop.

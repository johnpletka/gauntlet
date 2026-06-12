# Gauntlet bootstrap — continuation session. Start at P4.

You are continuing the bootstrap of **Gauntlet**, an adversarial multi-agent
development harness being built by following its own PRD → plan → phased
implementation process. P1, P2, and P3 are complete, reviewed, and gated. Your
job is to execute **P4** from the approved plan, then stop at its gate.

## How to run this phase (read this first)

**Run P4 in a normal Claude Code session — NOT through `gauntlet run`.** P4 is
bootstrap *development*: you hand-author Gauntlet's own `adversarial_cycle` step
type, transcript logger, and schemas. There is no pipeline to drive it through
yet — **P4 is the phase that builds it.** The dogfooding switchover is staged:
switchover #1 (already in effect, after P3) puts the branch/commit/manifest
*mechanics* under `gauntlet`; **switchover #2 happens at the END of P4**, where
`runs/gauntlet-bootstrap/` becomes a real Gauntlet pipeline and P5–P7 execute
*through* `gauntlet run`. So P4 is the last manually-driven phase, and it is the
one that makes #2 real.

In practice there is no standalone `gauntlet commit`, so P4's own commits stay
manual `git`. That is an expected gap, not a violation — **record it in
`BOOTSTRAP-NOTES.md`** per the plan's "record any gap that forced you to fall
back to manual." Do NOT try to force P4 through a half-built pipeline. The human
at the keyboard is the interactive backstop (the running session is not
live-judge-gated; the engine only gates agents it spawns — BOOTSTRAP-NOTES #12/#15).

## Read first, in this order

1. `CLAUDE.md` — auto-loaded; the role/process rules. Obey §2 principles. Note
   §4 (builder) and §5 (reviewer) describe the two agent roles `adversarial_cycle`
   orchestrates — you are *implementing* that machinery in P4.
2. `PRD-gauntlet.md` — the spec (v1.3). P4 is heavy on **FR-4** (complete logging:
   prompt.md/transcript.md/events.jsonl/RUN.md, redaction, .gitignore guidance),
   **FR-5.2** (adversarial_cycle as configuration), **FR-9.4/9.5/9.6** (fix-round
   commits, diff-scoped confirm, reviewer-mutation guard), **FR-10.5** (escalation
   on max_rounds), and **§7/§8** (findings/triage schemas; prompt-injection
   containment — reviewer findings are *data* to the triager, never instructions).
3. `runs/gauntlet/plan.md` — the **approved** plan. Work the **P4 section**
   ("`adversarial_cycle` + schemas + transcript logger") exactly. The §0 "Ground
   rules" are binding process. Note the refinement table: fix-round commits,
   diff-scoped confirm, and the mutation guard are P4 (they are properties of the
   cycle), while commit format/identities/branch/rollback already shipped in P3.
4. `runs/gauntlet/IMPLEMENT-PROMPT.md` — the governing process contract (per-phase
   loop, safety rules, dogfooding switchover). Still in force; only the "Start at
   P1" pointer is superseded by this file.
5. `BOOTSTRAP-NOTES.md` — the running design-feedback/decision log. Several
   entries are P4-relevant: #4–#6 (review output needed structured findings, the
   `open_questions` slot, `target_artifact` for upstream routing — P4's schemas
   should resolve these), #7 (redaction false-positives — the logger's redaction
   must be boundary-aware), #9–#16 (CLI/judge facts; the run-root/bookkeeping
   model the logger writes into; max_turns rejected).
6. Worked examples that are BOTH the review→triage→confirm format you replicate
   each phase AND the **hand-labeled triage corpus P4 mines**:
   `runs/gauntlet-bootstrap/manual/{plan-cycle-r1, p1-cycle-r1, p2-cycle-r1,
   p3-cycle-r1}/` — each has `findings.json` + `triage.json` (and confirm
   verdicts). Mirror their file layout into `.../p4-cycle-rN/`.

## Where things stand

- Branch: `gauntlet/bootstrap` at `d1e529a` (worktree clean). All work stays on
  this branch; no per-phase branches.
- History: P1 (`725f8ac` + fixes/confirms), P2 (`e6b8910` + `P2.1/.2/.3` +
  confirms + gate `6e1a23f`), P3 (`77570f9` + `P3.r1` review record +
  `P3.1/.2/.3` fixes + two confirm rounds, tip `d1e529a`).
- Tests: **267 unit + 49 integration green** (`uv run pytest` /
  `uv run pytest -m integration`). The suite only grows; never delete/skip a
  passing test to make a phase pass.
- Built so far you will build P4 *on top of*:
  - `src/gauntlet/engine/` — the P3 pipeline engine: pipeline loader + load-time
    validation (`validate.py`), write-ahead atomic manifest + side-effect
    transaction boundary (`manifest.py`, `orchestrator.py`), step-type registry
    + entry point `gauntlet.step_types` (`execution.py`, `steptypes.py`: the
    built-ins `agent_task`/`shell`/`human_gate`/`commit`), run lifecycle +
    rollback (`run.py`), git wrapper (`gitops.py` — note `range_diff`,
    `diff_head`, identities, the narrow `run_bookkeeping_excludes`), commit-format
    validator (`commit_format.py`), `when/foreach` evaluator (`expr.py`),
    engine-managed judge lifecycle (`judgeproc.py`).
    **P4 registers `adversarial_cycle` as a new step type** here and adds the
    transcript logger.
  - `src/gauntlet/adapters/` — Claude/Codex/Api adapters; `codex` supports
    `--output-schema` (P4 reviewer path); `api` does schema-prompt+retry (triager).
  - `src/gauntlet/logging/redact.py` — `RedactingWriter` (boundary-aware). The
    **P4 transcript logger is its full home** (FR-4.4 configurable redaction list);
    until then every write still routes through it.
  - `.gauntlet/config.yaml` — agent profiles (`builder`, `reviewer`, `triage`,
    `judge_llm`) + identities. P4's `adversarial_cycle` binds `reviewer`/`triager`/
    `fixer`/`confirmer` to these. Reviewer profile is `sandbox: read-only` (FR-9.6).
  - `.gauntlet/pins.yaml` — verified CLI/judge behavior; record any P4-verified
    flag/behavior (e.g. codex `--output-schema` on the real review path).

## P4 scope (confirm from the plan in ≤5 lines before you build)

- **Schemas:** normative `schemas/findings.json` + `schemas/triage.json` exactly
  per §7 (severity/category enums; verdict `legitimate|bikeshedding|
  premature_optimization|not_applicable`; action `fix_now|defer|reject`).
- **`adversarial_cycle` step type (FR-5.2):** review (structured findings via
  codex `--output-schema` / schema-prompt+retry elsewhere) → point-by-point
  triage (1–3 sentence reasoning) → fixer applies accepted → fix-round commit
  `PN.x: Address review — …` with declined-with-reason bodies (FR-9.4) →
  **diff-scoped confirm** (`<handoff-sha>..<fix-sha>` + prior findings + verdicts,
  FR-9.5) → loop within `max_rounds`; on exhaustion with open blockers escalate
  to a `human_gate` (FR-10.5).
- **Reviewer-mutation guard (FR-9.6):** review steps request read-only; engine
  checks `git status` after each review; `reviewer_mutation: commit|revert|halt`
  (default `commit`, reviewer-attributed `PN.rX:` author identity). Plus
  prompt-injection containment (§8): reviewer findings wrap as data to the triager.
- **Transcript logger (FR-4):** per-step `prompt.md`/`transcript.md`/`events.jsonl`,
  `RUN.md` index (verdict/duration/cost), FR-4.1 layout, default-on configurable
  redaction before any write, `.gitignore` guidance text (shipped by `init` in P6).
- **Triage corpus + escalation:** rubric-first few-shot triage prompt for small
  models (FR-3.4) + the hand-labeled ~30-finding corpus harvested from the
  bootstrap's own P1–P3 review rounds, **stratified across the severity enum**
  (review F-009); severity-aware escalation (blocking/low-confidence → stronger
  model or human gate). **Plus** a minimal bootstrap pipeline + prompt stubs
  expressing the P5–P7 loop (review F-006) — just enough to make switchover #2 real.

## Process contract — every phase, P4 included (from IMPLEMENT-PROMPT.md)

1. Confirm P4 scope from the plan (≤5 lines). Implement per the plan. Extend
   tests; **all green before commit** (units pass without creds; real-CLI/API
   behind `-m integration`).
2. **Triage-accuracy is the P4 assumption test.** Exit needs **≥ 85% overall
   agreement AND zero blocking-severity findings misclassified into a reject
   category without escalation**, reported as a **per-severity confusion matrix**
   (review F-009), measured with the configured cheap model over the hand-labeled
   corpus (`-m integration`). If it fails, iterate the rubric/few-shots first —
   that is the assumption test working; a model upgrade is the recorded-deviation
   fallback and goes to the human gate (it changes the FR-3 cost story).
3. Commit before review handoff (clean worktree): `P4: <imperative, ≤72 chars>`
   + detailed body (what/why, assumption validated, PRD/plan refs, deferrals).
   Manual `git` is expected for P4's own commits (record the switchover gap).
4. Review handoff via codex, read-only, schema-constrained JSON findings:
   `codex exec -s read-only --json --output-schema <schema.json> -o <out.json>
   - < prompt.md`. Reuse `runs/gauntlet-bootstrap/manual/p3-cycle-r1/
   review-schema.json` and `.../confirm-schema.json`. Save the exact prompt,
   findings, and event stream under `runs/gauntlet-bootstrap/manual/p4-cycle-rN/`.
5. Triage point-by-point (`legitimate|bikeshedding|premature_optimization|
   not_applicable` + `fix_now|defer|reject`, 1–3 sentences each). **Show the human
   the triage table and WAIT for ratification before fixing.**
6. Fix accepted findings; commit `P4.x: Address review — <summary>` with
   per-finding body entries, declined findings included with reasons.
7. Diff-scoped confirm pass (range diff + prior findings + verdicts only);
   per-finding `resolved|partially_resolved|unresolved|regression_introduced`.
   **Max 2 rounds**; unresolved blockers escalate.
8. Report P4 exit-criteria status and **stop at the gate** — P5 starts only when
   the human says so.
9. Upstream invalidation (FR-10.4): if implementation reveals the plan/PRD is
   wrong, halt and present the conflict; never silently amend an approved
   artifact. (Tempting in P4: the schemas may want an `open_questions` slot and a
   triage `target_artifact` field — BOOTSTRAP-NOTES #5/#6. Adding those to the
   *new* P4 schemas is in-scope; changing the PRD's §7 excerpt is not — surface it.)

## Safety rules (apply to you, now)

- Never use permission-bypass flags; never force-push; stay inside this repo; ask
  before any system-level install.
- Until P4's logger is the system of record, keep routing anything you write to
  `runs/gauntlet-bootstrap/manual/` through the P1 `RedactingWriter` before
  committing (boundary-aware patterns; see BOOTSTRAP-NOTES #7). The P4 logger
  then *becomes* that system of record (FR-4.4).
- Keep appending process pain points/decisions to `BOOTSTRAP-NOTES.md`.

## Dogfooding switchover (binding, from the plan)

- **Switchover #1 (already in effect, after P3):** branch/commit/manifest
  mechanics run through `gauntlet` where a command exists. P4's own commits have
  no `gauntlet` command yet → manual `git`, gap recorded.
- **Switchover #2 (END of P4):** create `runs/gauntlet-bootstrap/` with P5–P7
  expressed as a Gauntlet pipeline (implement → tests → commit →
  `adversarial_cycle` → human gate). **From P5 onward, manual process execution
  is a bug** — P5+ run *through* `gauntlet run` with the human at the gates.
- Environment verified 2026-06-11 (claude 2.1.172, codex-cli 0.139.0, uv 0.4.26,
  Python 3.12.0). Re-verify only what P4 exercises (codex `--output-schema` on
  the live review path; cheap-model triage); record in the pin file.

## Start

1. Confirm P4 scope from the plan in ≤5 lines.
2. Build the schemas + `adversarial_cycle` + logger; extend tests (units green;
   the triage-accuracy run is `-m integration`).
3. Run the P4 review cycle through triage, show the human the table, and stop.

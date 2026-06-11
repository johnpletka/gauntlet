# Adversarial review — Gauntlet bootstrap, Phase P3, round 1

You are the adversarial reviewer in Gauntlet's bootstrap pipeline. Find
problems, not praise. Shipping a broken or incomplete pipeline engine is the
only unacceptable outcome. You are in a read-only sandbox: do NOT run the test
suite or any writing command (review statically). Read-only commands are fine
(`git show`, `git log`, `cat`, `rg`, `ls`).

## What you are reviewing

Commit `77570f9` ("P3: Add pipeline engine: YAML, steps, manifest, resume") on
branch `gauntlet/bootstrap`. Inspect with `git show 77570f9` and by reading the
files it adds/changes:

- `src/gauntlet/engine/` — `pipeline.py`, `validate.py`, `config.py`,
  `manifest.py`, `execution.py`, `steptypes.py`, `orchestrator.py`, `run.py`,
  `gitops.py`, `commit_format.py`, `expr.py`, `judgeproc.py`, `__init__.py`
- `src/gauntlet/cli.py` — the lifecycle commands; `src/gauntlet/__main__.py`
- `.gauntlet/config.yaml` — agent profiles (FR-2.1)
- `tests/unit/` — `test_pipeline_loader.py`, `test_orchestrator.py`,
  `test_manifest.py`, `test_run_lifecycle.py`, `test_steptypes.py`,
  `test_gitops.py`, `test_commit_format.py`, `test_expr.py`,
  `test_resume_crash.py` (+ `_crash_child.py`), `conftest.py`
- `tests/integration/test_pipeline_contract.py`
- `.gauntlet/pins.yaml` and `BOOTSTRAP-NOTES.md` entries #13-16 (P3 decisions)

This is P3 of a bootstrap. P1 (`725f8ac`) shipped the adapters/timeout/redactor/
pins; P2 (`e6b8910`) the judge/policy/hook/red-team. The adversarial_cycle step
type + findings/triage schemas + transcript logger + reviewer-mutation guard
(P4), the full `standard.yaml` + prompt set (P5), and `init`/`doctor` (P6)
intentionally do not exist yet — do NOT flag their absence as a P3 defect, but
DO flag P3 work that silently does a later phase's job or contradicts the plan.

## Critical context you must account for (not defects)

- The engine's live run-instance dir is deliberately kept out of the worktree
  state (self-`.gitignore` + an `exclude=[run_root]` pathspec on every engine
  git op) so the engine's own manifest/transcripts never dirty the worktree or
  land in phase commits. This is a ratified design choice (BOOTSTRAP-NOTES #13)
  that preserves the clean-handoff invariant and the F-003 base-SHA boundary —
  do not re-litigate it; DO scrutinize whether it is implemented soundly (e.g.
  can a real work file ever be wrongly excluded, or run bookkeeping wrongly
  committed?).
- `codex exec` does not fire PreToolUse hooks on 0.139.0; codex is
  sandbox-primary (BOOTSTRAP-NOTES #10, ratified). Engine-managed live gating
  was verified for claude only.
- `interrupted_step` defaults to `park` (do not re-run a builder over a dirty
  mid-edit tree); `reset_to_base` is the opt-in recover-and-rerun policy.

## Review against, in priority order

1. **The spec** `PRD-gauntlet.md`: FR-5 (pipeline YAML, adversarial_cycle as
   config, extensibility, when/foreach/on_fail/overrides, versioning+hash),
   FR-2.1-2.4 (profiles, role swap, capability validation, entry-point
   adapters), FR-8.1/8.2 (lifecycle CLI; write-ahead manifest, resume reusing
   session ids), FR-9.1/9.2/9.7/9.9 (branch, commit format, identities,
   rollback), FR-10.1-10.4 (entry contract, strict stage gating, sequential
   phases, upstream invalidation), FR-3.3 (budget/timeout halt), FR-7.1
   (engine-managed judge lifecycle), §7 (manifest shape), §8 (no agent text in
   command lines; redact before write).
2. **The approved plan, P3 section** (`runs/gauntlet/plan.md`): every listed
   deliverable, the test strategy (incl. the kill-9 crash cases: die after
   worktree edits / die mid-commit), and the exit criteria.
3. **The guiding principles** in `CLAUDE.md` §2 (determinism over cleverness;
   fail closed; separation of concerns; data over inference; approved artifacts
   change only through their own gate).

Findings not tracing to one of these three are likely bikeshedding — report
them but label severity honestly (`nit`).

## Hunt especially for

- **Resumability holes (the headline assumption).** Any kill -9 timing the
  write-ahead manifest + base-SHA boundary does NOT cover: a torn manifest read,
  a step re-run that duplicates an effect, a mid-commit kill that double-commits
  or loses the SHA, a dirty-base agent step that gets silently re-run instead of
  parked. Is `os.replace` + fsync actually atomic as used? Does the
  `is_dirty_vs(base, exclude=run_root)` logic ever misclassify (real partial
  work hidden because it landed under run_root; or pre-existing dirt from an
  earlier uncommitted step misread as this step's interruption)?
- **Trust-model bypass (review F-001 / §8).** Can agent-authored text ever reach
  a shell command line? `render_shell_command` only allows `{{config.*}}` — is
  the rejection complete (nested tokens, partial matches, `{{config.x}}{{y}}`)?
  Does the commit message (model-authored) ever hit argv unvalidated? Does any
  step interpolate artifact contents into `run:`?
- **Entry contract & stage gating (FR-10).** Can `run` proceed on a stub PRD
  (marker stripped but otherwise unchanged)? Any look-ahead or speculative work
  before a gate? Is upstream invalidation (FR-10.4) actually expressible, or
  merely claimed?
- **Rollback safety (FR-9.9 / review F-010).** Can rollback corrupt the branch
  or leave branch and manifest disagreeing? Are the dirty/divergence guards
  sound? Is the backup ref + manifest snapshot written BEFORE any destructive
  op? Does `git clean` during reset_to_base ever delete tracked work or the
  authored prd.md?
- **Capability/flag validation (FR-2.3, §8).** Does load-time validation catch
  repo-write-on-`api`, best-effort-schema, banned flags in profile base_flags,
  dangling artifacts, unknown step types / agents / on_fail targets? Any
  validation that passes something it should reject, or rejects something valid?
- **Budget/timeout halt (FR-3.3).** Does an over-budget or timed-out step halt
  at a checkpoint, or can it burn on? Is `max_turns` enforced or silently
  ignored (claimed but not wired)?
- **Manifest fidelity (§7, data-over-inference).** Does the manifest record
  everything needed to reconstruct the run (status, session ids, base SHAs,
  commits, usage), or must a debugger infer? Any field in the §7 shape missing?
- **Judge lifecycle (FR-7.1).** Does `gauntlet run` start AND reliably stop the
  judge (no orphan process / leaked env) on the happy path AND on failure/exit?
  Could the per-run env leak across runs or into the parent session unintended?
- **Test rigor.** Do the tests pin behavior or mirror the implementation? Does
  the crash test actually kill mid-step (not just at start)? Are there
  load-bearing paths with no test (rollback divergence guard, foreach+on_fail,
  reset_to_base backup, judge stop-on-failure)?

## Output contract

Return ONLY JSON conforming to the provided schema:
- `findings[]`: `id` (sequential `F-001`...), `severity`
  (blocking|major|minor|nit), `category` (correctness|spec-gap|security|
  performance|principle-violation|style), `location` (file:line or section),
  `claim`, `evidence` (cite spec/plan text or code behavior), `suggested_fix`.
- `open_questions[]`: `id` (`OQ-1`...), `question`.
- `summary`: 2-4 sentences.

No prose outside the JSON; the triage step consumes it programmatically.

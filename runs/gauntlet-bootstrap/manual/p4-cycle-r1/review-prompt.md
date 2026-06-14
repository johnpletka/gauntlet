# Adversarial review — Gauntlet bootstrap, Phase P4, round 1

You are the adversarial reviewer in Gauntlet's bootstrap pipeline. Find
problems, not praise. Shipping a broken adversarial-review primitive is the
only unacceptable outcome — P4 is the machinery every later phase will be
reviewed BY, so a defect here compounds. You are in a read-only sandbox: do
NOT run the test suite or any writing command (review statically). Read-only
commands are fine (`git show`, `git log`, `cat`, `rg`, `ls`).

## What you are reviewing

Commit `472a644` ("P4: ...") on branch `gauntlet/bootstrap`. Inspect with
`git show 472a644` and by reading the files it adds/changes:

- `schemas/` — `findings.json`, `triage.json`, `confirm.json` (normative, §7)
- `src/gauntlet/engine/cycle.py` — the `adversarial_cycle` step type (FR-5.2,
  FR-9.4/9.5/9.6, FR-10.5, §8 containment, F-009 escalation)
- `src/gauntlet/logging/transcript.py` — transcript logger (FR-4) +
  `src/gauntlet/logging/redact.py` (configurable redaction, FR-4.4)
- `src/gauntlet/engine/triage_eval.py` — triage-accuracy harness (P4
  assumption test) + `prompts/triage-corpus.jsonl` (hand-labeled corpus) +
  `prompts/triage.md` (rubric-first cheap-model prompt, FR-3.4)
- `pipelines/bootstrap.yaml` + `prompts/bootstrap-implement-p[567].md` +
  `prompts/cycle-{review,fix,confirm}.md` + `prompts/commit-message.md` +
  `runs/gauntlet-bootstrap/prd.md` — switchover #2 (plan §0, review F-006)
- Engine integration diffs: `steptypes.py`, `execution.py`, `orchestrator.py`,
  `validate.py`, `run.py`, `engine/config.py`, `logging/__init__.py`
- Tests: `tests/unit/test_cycle.py`, `test_transcript.py`, `test_schemas.py`,
  `test_triage_eval.py`, `test_bootstrap_pipeline.py`;
  `tests/integration/test_cycle_contract.py`, `test_triage_accuracy.py`
- `.gauntlet/config.yaml` (verified model swap + escalation profile),
  `.gauntlet/pins.yaml` (P4 pins), `BOOTSTRAP-NOTES.md` #17–#22
- The recorded assumption-test artifact:
  `runs/gauntlet-bootstrap/manual/p4-triage-accuracy.md`

This is P4 of a bootstrap. P1 (`725f8ac`) shipped adapters/redactor/pins; P2
(`e6b8910`) judge/policy/hooks; P3 (`77570f9`, tip `d1e529a` after fixes) the
pipeline engine. The full `standard.yaml` + polished prompt set + toy-repo
end-to-end run (P5), `init`/`doctor` (P6), and retro/proposals (P7)
intentionally do not exist yet — do NOT flag their absence as a P4 defect,
but DO flag P4 work that silently does a later phase's job or contradicts the
approved plan.

## Critical context you must account for (ratified, not defects)

- **codex `exec` does not fire PreToolUse hooks on 0.139.0** (BOOTSTRAP-NOTES
  #10): codex review steps are controlled by the read-only sandbox, not the
  judge. Reviewer-mutation defense (FR-9.6) is therefore the engine-side
  `git status` guard — scrutinize THAT, not the missing hook.
- **codex `--output-schema` enforces OpenAI strict mode** (notes #20, pinned):
  every property must be in `required`; §7-"optional" fields are spelled
  required-but-nullable in the normative schemas. The PRD §7 excerpt is
  deliberately unchanged.
- **Round outputs (findings/triage/confirm JSON) are run bookkeeping** under
  `run_dir/artifacts/`, NOT tracked commit payload (notes #22): writing them
  to the tracked artifact root would dirty the tree between the fix commit
  and the next handoff. Do not re-litigate; DO scrutinize soundness (can a
  real work file be wrongly excluded, or bookkeeping wrongly committed?).
- **Triage/judge models swapped to `gpt-5-mini`/`gpt-5`** (notes #19): the
  prior `claude-haiku-latest` could not run here (no Anthropic key; alias
  unresolvable). Verified live and pinned.
- **The corpus verdict-skew (34/36 legitimate) is recorded** in the accuracy
  report and notes #21, with the blocking-miss criterion + per-severity
  matrix as the operative checks. The hand-labels are the bootstrap's own
  human-ratified triage verdicts.
- **The handoff/branch discrepancy** (work was on `main`, `gauntlet/bootstrap`
  created at P4 start) is recorded in notes #17.

## Review against, in priority order

1. **The spec** `PRD-gauntlet.md`: FR-5.2 (adversarial_cycle as
   configuration: review → point-by-point triage → apply accepted → confirm),
   FR-9.3 (clean worktree at every reviewer handoff, including fix rounds),
   FR-9.4 (fix-round commits `PN.x: Address review — …`, per-finding bodies,
   declined-with-reason), FR-9.5 (confirm sees ONLY `<handoff>..<fix>` diff +
   prior findings + verdicts), FR-9.6 (mutation guard commit|revert|halt,
   reviewer-attributed `PN.rX:` identity), FR-9.7 (identities), FR-10.5
   (max_rounds exhaustion with open blockers escalates to a human gate),
   FR-4.1–4.5 (layout, lossless transcripts, RUN.md, default-on configurable
   redaction, .gitignore guidance), FR-3.4 (rubric-first cheap-model prompt),
   §7 (schema enums exactly), §8 (findings are DATA to the triager; redact
   before write).
2. **The approved plan, P4 section** (`runs/gauntlet/plan.md`): every listed
   deliverable, the unit test list (converge 1/2 rounds, escalation,
   mutation policies, fix-commit body, confirm-diff scoping, schema retry,
   redaction), the triage-accuracy exit criteria (≥85% AND zero unescalated
   blocking-into-reject, per-severity confusion matrix), and switchover #2.
3. **The guiding principles** in `CLAUDE.md` §2.

Findings not tracing to one of these are likely bikeshedding — report them
but label severity honestly (`nit`).

## Hunt especially for

- **Cycle-loop holes.** Round/handoff bookkeeping: is `handoff` advanced
  correctly between rounds? Can a converged-with-declines exit hide an open
  blocker (e.g. blocking finding triaged `defer` post-escalation)? Is the
  "fixer made no changes" guard sound when accepted fixes legitimately need
  no tree change? What happens when the confirmer returns verdicts for
  finding IDs that don't exist, or omits some?
- **Mutation-guard soundness (FR-9.6).** `revert` does reset+clean — can it
  destroy anything beyond the reviewer's mutation (pre-existing untracked
  files at handoff are impossible by the clean-check, but is that airtight)?
  Is the backup ref written before EVERY destructive path? `commit` policy:
  the reviewer's edits ride into the confirm diff — is that diff still
  attributable?
- **Containment gaps (§8).** Findings text reaches: the triager (wrapped),
  the FIXER prompt, the CONFIRM prompt, commit-message bodies — is every
  agent-authored string treated as data everywhere it lands? The
  engine-composed fix-commit body embeds claims/reasoning — can crafted
  finding text break the commit-format validation or smuggle anything?
- **Escalation correctness (F-009).** The rule is severity==blocking OR
  confidence==low. Post-escalation low confidence parks. Can a blocking
  finding end up rejected purely by the CHEAP model in any path? Is the
  accuracy harness measuring the SAME prompt/schema/rule the cycle ships?
  Few-shot leakage between prompt examples and corpus?
- **Logger fidelity (FR-4.2).** Is anything summarized away in
  `render_transcript`? Are events lossless (every raw event a JSONL line)?
  Is redaction applied before EVERY write path (append_jsonl included)? Does
  RUN.md regeneration on every checkpoint stay consistent on crash?
- **Resume semantics.** An adversarial_cycle killed mid-round: the
  orchestrator parks dirty resumes — but a kill AFTER a fix commit and
  BEFORE finalize leaves unmanifested `PN.x` commits; what does re-run do?
  Does the rollback guard (HEAD == last recorded) interact safely with
  cycle multi-commits?
- **Schema/normativity drift.** Do the schema enums match §7 exactly? Are
  the P4 additions (open_questions, target_artifact, confidence, escalated)
  additive-only, with the upstream §7 conflict surfaced rather than the PRD
  amended? Does `_verdict_schema` stripping `escalated` actually prevent the
  model from asserting it?
- **Pipeline/prompt stubs (switchover #2).** Does `pipelines/bootstrap.yaml`
  actually validate and express the plan's loop? Are the commit steps'
  `phase:` hints consistent with the enforced format? Anything in the stubs
  that contradicts CLAUDE.md §4/§5 role contracts?
- **Test rigor.** Do tests pin behavior or mirror implementation? Are there
  load-bearing paths with no test (e.g. mutation during a confirm pass,
  artifact-mode cycle inside foreach, RUN.md content after a parked run,
  redaction of cycle sub-step logs)?

## Output contract

Return ONLY JSON conforming to the provided schema:
- `findings[]`: `id` (sequential `F-001`...), `severity`
  (blocking|major|minor|nit), `category` (correctness|spec-gap|security|
  performance|principle-violation|style), `location` (file:line or section),
  `claim`, `evidence` (cite spec/plan text or code behavior),
  `suggested_fix` (string or null).
- `open_questions[]`: `id` (`OQ-1`...), `question`.
- `summary`: 2-4 sentences.

No prose outside the JSON; the triage step consumes it programmatically.

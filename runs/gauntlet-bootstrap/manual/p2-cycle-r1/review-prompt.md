# Adversarial review — Gauntlet bootstrap, Phase P2, round 1

You are the adversarial reviewer in Gauntlet's bootstrap pipeline. Find
problems, not praise. Shipping a broken or incomplete safety layer is the only
unacceptable outcome. You are in a read-only sandbox: do NOT run the test suite
or any writing command (review statically). Read-only commands are fine
(`git show`, `git log`, `cat`, `rg`, `ls`).

## What you are reviewing

Commit `e6b8910` ("P2: Add judge service, policy fast path, hook wiring,
red-team suite") on branch `gauntlet/bootstrap`. Inspect with
`git show e6b8910` and by reading the files it adds/changes:

- `src/gauntlet/judge/` — `policy.py`, `core.py`, `classifier.py`,
  `service.py`, `hook_client.py`, `runner.py`, `decision.py`, `__init__.py`
- `policy.yaml` — the default fast-path policy
- `src/gauntlet/cli.py` — `gauntlet judge serve`
- `tests/unit/test_policy.py`, `test_judge_core.py`, `test_judge_service.py`,
  `test_hook_client.py`
- `tests/integration/test_judge_live.py`, `test_codex_sandbox.py`
- `.gauntlet/pins.yaml` and `BOOTSTRAP-NOTES.md` entry #10 (P2 hook finding +
  the ratified sandbox-primary decision)

This is P2 of a bootstrap. P1 (commit `725f8ac`/`af6332f`) shipped the agent
adapters, timeout wrapper, redacting writer, and pin file. The pipeline engine
(P3), adversarial_cycle (P4), and `init`/`doctor` (P6) intentionally do not
exist yet — do not flag their absence as a P2 defect, but DO flag P2 work that
silently does a later phase's job or contradicts the plan.

## Critical context you must account for (not defects)

`codex exec` on 0.139.0 does NOT fire PreToolUse hooks (verified every which
way — see BOOTSTRAP-NOTES #10). The human RATIFIED a "sandbox-primary for
codex" decision: claude gets the full judge+hook+audit path (FR-7 met there);
codex's pre-execution control is its sandbox (read-only / workspace-write),
with the hook client wired but inert on this build. FR-7's "100% blocked
pre-execution + audit through EACH agent" is therefore scoped to claude by
ratified decision. Do not re-litigate that decision; DO scrutinize whether the
implementation faithfully delivers it and whether the codex sandbox claims are
actually proven.

## Review against, in priority order

1. **The spec** `PRD-gauntlet.md`: FR-7 (all sub-reqs: /decide endpoint +
   context, decision ladder deny-first, fail-closed, deny rationale to agent,
   audit log, default deny/ask lists), §8 (127.0.0.1 bind, per-run token,
   fail-closed, redact-before-write, prompt-injection containment: reviewer/
   agent text is data not instructions), FR-3.3/3.4 (bounded judge LLM,
   rubric-first), FR-2 (judge_llm profile).
2. **The approved plan, P2 section** (`runs/gauntlet/plan.md`): deliverables
   (FastAPI judge, decision ladder, default policy, hook clients + wiring,
   audit log, operational/degraded model per review F-004, red-team suite
   extended beyond Bash per review F-007), test strategy, exit criteria.
3. **The guiding principles** in `CLAUDE.md` §2 (fail closed; determinism;
   data over inference; separation of concerns; process fidelity).

Findings not tracing to one of these three are likely bikeshedding — report
them but label severity honestly (`nit`).

## Hunt especially for

- **Fail-open holes:** any path through the policy engine, judge core, service,
  or hook client where a timeout, parse error, malformed payload, unexpected
  exit code, or unmatched command results in `allow` rather than `deny`/`ask`.
- **Policy bypasses:** red-team commands that evade the deny rules via
  quoting, env indirection, alternative binaries, path tricks, encoding, or
  command chaining; benign commands wrongly denied. The structural
  path-escape / credential checks: can they be fooled (symlinks, `..`,
  unresolved paths, relative paths, case)? Are the regexes anchored correctly?
- **Token/auth:** is the 127.0.0.1 bind + token actually enforced; constant-time
  compare; any unauthenticated decision path; token leakage into logs.
- **Redaction:** does every judge write (audit log especially) go through the
  redacting writer? Any tool_input that could carry a secret onto disk
  unredacted?
- **Prompt-injection containment (§8):** the classifier embeds tool_input into
  the LLM prompt — can crafted tool_input flip the judge's decision? Is it
  framed as data?
- **Hook contract correctness:** does the emitted JSON match what claude
  actually honors? Is the exit-2 deny path correct? Does interactive degraded
  mode genuinely avoid deadlock while unattended fails closed?
- **Test rigor:** do the live tests actually prove pre-execution blocking, or
  could a passing test coexist with a broken hook? Is the codex sandbox claim
  proven against a real escape target (not a system-temp path the sandbox
  allows by design)? Any test that mirrors the implementation instead of
  pinning behavior?

## Output contract

Return ONLY JSON conforming to the provided schema:
- `findings[]`: `id` (sequential `F-001`...), `severity`
  (blocking|major|minor|nit), `category` (correctness|spec-gap|security|
  performance|principle-violation|style), `location` (file:line or section),
  `claim`, `evidence` (cite spec/plan text or code behavior), `suggested_fix`.
- `open_questions[]`: `id` (`OQ-1`...), `question`.
- `summary`: 2-4 sentences.

No prose outside the JSON; the triage step consumes it programmatically.

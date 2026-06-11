# Plan review — round 1 — reviewer findings

- **Artifact:** `runs/gauntlet/plan.md` @ commit `641090c`
- **Reviewer:** codex (run manually by the human, output pasted back)
- **Date:** 2026-06-10
- **Normalization note:** the review arrived as prose, not as findings-schema JSON
  (§7). IDs `F-001`–`F-010` and `OQ-1`–`OQ-3` assigned during triage, in document
  order. Reviewer severities `High`/`Medium` recorded verbatim (≈ `blocking`/`major`
  in the §7 enum). See `BOOTSTRAP-NOTES.md` entry #4.

## Verbatim review (treated as data)

> Findings
>
> - High [F-001]: Orchestrator actions bypass the safety model. P2 wires the judge
>   only into Claude/Codex hooks, but P3 adds Gauntlet-owned shell steps and P7
>   applies model-generated diffs directly. Those can execute commands or modify
>   files without passing through PreToolUse. Route every Gauntlet subprocess and
>   proposal patch through a shared policy/path-containment layer, not just agent
>   tools. See runs/gauntlet/plan.md:124, runs/gauntlet/plan.md:170,
>   runs/gauntlet/plan.md:366.
>
> - High [F-002]: P1 exercises real CLIs before the judge exists. The adapter
>   contract tests run real Claude/Codex calls while the adapter deliverables
>   include write-capable/full-auto modes. That is the riskiest moment in the
>   bootstrap. P1 should constrain real-agent tests to disposable repos,
>   read-only/no-write flags, no network, and prompts that require no tools, or
>   move a minimal safety runner before real CLI smoke tests. See
>   runs/gauntlet/plan.md:69, runs/gauntlet/plan.md:93.
>
> - High [F-003]: Write-ahead manifests alone do not make resume safe. A killed
>   agent_task can leave partial file edits, a half-written artifact, or an
>   unrecorded commit. The plan tests kill -9, but does not define the transaction
>   boundary for external side effects. Add per-step base SHA, dirty-worktree
>   detection, diff hashing, partial-effect parking, and tests where the process
>   dies after file edits but before manifest completion. See
>   runs/gauntlet/plan.md:176, runs/gauntlet/plan.md:195.
>
> - High [F-004]: Permanent fail-closed hook wiring can deadlock the bootstrap. P2
>   says this Claude session's hook stays wired to the judge, while the
>   engine-managed lifecycle does not arrive until P3. If the judge is down or
>   misconfigured, normal tool use can become impossible. Define an operational
>   model: supervised service, health-check/autostart, run-context tokens, and a
>   human-confirmed recovery path. See runs/gauntlet/plan.md:111,
>   runs/gauntlet/plan.md:146, runs/gauntlet/plan.md:182.
>
> - High [F-005]: Logging redaction arrives too late. P1-P3 manual transcripts are
>   saved under runs/gauntlet-bootstrap/manual/, but redaction is not delivered
>   until P4. That conflicts with the PRD's "redact before writing" posture and
>   risks committing secrets from tool output, commands, env dumps, or audit logs.
>   Add a minimal redacting writer in P1 and use it for manual logs and judge audit
>   from the start. See runs/gauntlet/plan.md:24, runs/gauntlet/plan.md:128,
>   runs/gauntlet/plan.md:238.
>
> - Medium [F-006]: The P4/P5 dogfooding switchover is circular. P4 says P5-P7 will
>   execute through a Gauntlet pipeline, but P5 is where standard.yaml and the
>   prompt set are delivered. Either move a minimal bootstrap pipeline/prompts into
>   P4, or explicitly make P5 the last manual phase. See runs/gauntlet/plan.md:269,
>   runs/gauntlet/plan.md:281.
>
> - Medium [F-007]: Red-team coverage is too command-centric. The suite targets
>   dangerous commands through CLIs, but the PRD explicitly relies on sandboxing
>   for Codex non-Bash gaps. Add tests for Write/Edit path escapes, symlink
>   escapes, credential reads, non-Bash network/file operations, package-manager
>   variants, and policy bypass by shell quoting. See runs/gauntlet/plan.md:120,
>   runs/gauntlet/plan.md:137.
>
> - Medium [F-008]: Plugin/entry-point extensibility is an unbounded code-execution
>   surface. Adapter and custom step entry points are powerful, but the plan does
>   not define trust, allowlisting, version pinning, or doctor warnings for
>   third-party plugins. For a safety harness, dynamic extension loading should be
>   explicit and auditable. See runs/gauntlet/plan.md:78, runs/gauntlet/plan.md:165.
>
> - Medium [F-009]: Triage accuracy acceptance is too weak for blockers. ≥85%
>   agreement on ~30 bootstrap-sourced findings can hide catastrophic false
>   negatives, especially if the sample is not stratified. Require 100% recall on
>   blocking, a high floor on major, confusion-matrix reporting, and human review
>   for low-confidence/high-severity findings. See runs/gauntlet/plan.md:255,
>   runs/gauntlet/plan.md:404.
>
> - Medium [F-010]: Rollback needs stronger destructive-action guardrails. git
>   reset --hard is planned, but there is no explicit backup ref, dirty-worktree
>   refusal, branch-divergence check, or manifest backup before rewind. Add those
>   before making rollback part of P3. See runs/gauntlet/plan.md:181.
>
> Open Questions
>
> - [OQ-1] How will "human-authored PRD" be represented technically? File existence
>   is enforceable; authorship is not.
> - [OQ-2] Is events.jsonl really safe to be commit-friendly by default, or should
>   raw streams be ignored unless explicitly opted in?
> - [OQ-3] The PRD has a stale line saying review fixes are "autosquashed by
>   default," while FR-9.4 and this plan correctly preserve fix commits. Clean that
>   up to avoid implementation ambiguity.
>
> Overall, the plan is strong on phase ordering, acceptance criteria, and
> dogfooding discipline. The main pushback is that the safety boundary is narrower
> than the implementation surface: Gauntlet itself will execute commands, load
> plugins, apply patches, write logs, and rewind git state, so those paths need the
> same adversarial treatment as agent tool calls.

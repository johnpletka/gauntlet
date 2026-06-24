# Bootstrap notes — process pain points as design feedback

Running log, newest at the bottom. Each entry: what happened while following the
process, and what it suggests for Gauntlet's design.

## 2026-06-10 — Planning stage

1. **PRD has an unlabeled section.** The CLI command reference (between FR-10's
   acceptance and §7) has no section header — references like "§6" resolve only by
   guessing. Footer also says "*End of PRD v1.0*" while the header says v1.3.
   *Design feedback:* the PRD adversarial-review prompt should explicitly hunt for
   structural defects (numbering, dangling refs, stale footers), not just semantic
   ones — cheap to catch, and downstream prompts cite sections by number.

2. **Two slugs for one effort.** The bootstrap places the plan at
   `runs/gauntlet/plan.md` but manual transcripts at
   `runs/gauntlet-bootstrap/manual/`, while FR-4.1 puts run artifacts under
   `.gauntlet/runs/<prd-slug>/`. Three layouts for one project invites confusion.
   *Design feedback:* FR-4.1's "configurable to an external path" should be a single
   run-root setting that everything (plan, transcripts, manifests) lives under; a
   run should never split across roots.

3. **Plan gate ordering: bootstrap prompt vs. PRD.** FR-5.1's pipeline runs
   `plan-cycle` (adversarial review) *before* `plan-approve` (human gate), but
   bootstrap rule 2 says stop for human approval immediately after authoring the
   plan, with no review cycle. Followed the bootstrap prompt (it owns the process)
   and surfaced the option of a manual codex plan review at the stop point.
   *Design feedback:* none for Gauntlet itself — the pipeline YAML makes this
   ordering explicit, which is exactly the property the bootstrap prompt lacked.

## 2026-06-10 — Plan review cycle, round 1

4. **Unstructured review output required manual normalization.** The human ran
   codex directly, so the review came back as prose — no finding IDs, severities
   `High`/`Medium` instead of the §7 enum, and triage had to assign IDs and map
   severities by hand before point-by-point verdicts were possible.
   *Design feedback:* this is the concrete cost `findings_schema` +
   `--output-schema` enforcement (FR-5.2, §7) exists to remove; also, the triage
   step should tolerate/normalize schema-violating reviewer output as a fallback
   path rather than failing, since ad-hoc human-run reviews will happen.

5. **Reviewers naturally emit open questions; the findings schema has no slot for
   them.** Codex produced three "Open Questions" alongside findings — one of which
   (stale PRD text) was a real defect. Forcing these through the
   `legitimate|bikeshedding|...` verdict enum is awkward (they're questions, not
   claims).
   *Design feedback:* consider an `open_questions` array in `findings.json` (or
   instruct reviewers to express questions as `spec-gap` findings) so the triage
   prompt and transcripts handle them first-class.

6. **A finding can implicate an upstream artifact, not the reviewed one.** OQ-3
   flagged a defect in the PRD while reviewing the plan. The process answer
   (FR-10.4: halt, propose, human amends) worked, but the triage schema has no way
   to mark "fix lands in a different artifact owned by someone else."
   *Design feedback:* triage verdicts could use an optional `target_artifact`
   field so upstream-invalidation routing (FR-10.4) can be automated instead of
   inferred from reasoning text.

7. **Naive credential regexes false-positive on ordinary prose.** A pre-commit
   scan of the confirm-pass event log for `sk-…` matched "a**sk-with**-warning"
   inside a verdict note. No real secret, but it cost a manual inspection.
   *Design feedback:* the P1 redacting writer's patterns need word boundaries +
   minimum length/entropy (e.g. `\bsk-[A-Za-z0-9]{20,}\b`), and the redactor
   should log *what pattern* fired so false positives are diagnosable. Exact
   env-var **value** matching (known secrets) stays the primary mechanism;
   regexes are the fallback.

8. **Schema-constrained confirm pass worked end-to-end.** `codex exec -s
   read-only --output-schema … -o …` returned valid JSON on the first try (13/13
   verdicts, parseable, no retry needed), with usage in the event stream
   (21.5k in / 1.2k out tokens) and a session/thread id. The PRD's adapter
   assumptions (§4.1 CodexAdapter, FR-9.5 confirm mapping) look sound on
   codex-cli 0.139.0 — first real datapoint for the P1 pin file.

## 2026-06-11 — P2 hook discovery (the riskiest assumption, tested first)

10. **`codex exec` does not fire PreToolUse hooks on 0.139.0 — the PRD's
    codex-side hook control is non-functional headless.** Verified by direct
    probe before writing any P2 code (plan risk #1: "hook semantics on
    installed CLI versions"). Findings:
    - **claude 2.1.172 — hook path fully works.** `claude -p` fires the
      `PreToolUse` hook from `.claude/settings.json`; the payload is
      `{session_id, cwd, hook_event_name, tool_name, tool_input{command,...},
      tool_use_id, permission_mode, transcript_path}`. Emitting
      `{"hookSpecificOutput":{"hookEventName":"PreToolUse",
      "permissionDecision":"deny","permissionDecisionReason":"..."}}` blocks
      the tool **pre-execution** and surfaces the reason to the agent
      (verified: a denied bash `touch` never created its file; the agent
      reported the deny reason). FR-7.3/7.4 hold on claude.
    - **codex 0.139.0 — `exec` PreToolUse hook never fires.** Tried project
      `.codex/hooks.json`, user `~/.codex/hooks.json`, `--enable codex_hooks`,
      `[features].hooks=true` (already globally stable+true per
      `codex features list`), and even `--dangerously-bypass-hook-trust`. The
      hook command (a `tee` to a tmp file) never ran on a shell tool call.
      The hook runtime (`hooks/list`, `hook/started`/`hook/completed`
      notifications, trust review) lives in the **app-server/TUI** path; the
      standalone `codex exec` we drive headlessly does not execute it.
    - **codex sandbox backstop works and is the only headless codex control.**
      `-s read-only` blocks ALL writes (`operation not permitted`, verified);
      `-s workspace-write` confines writes to the workspace **and system temp**
      (writing `/tmp/...` succeeded — `/tmp` is a default writable root, so it
      is not an "outside" escape). The sandbox is a coarse filesystem/network
      boundary, not the judge's semantic allow/deny, and produces no
      judge-audit line or rationale.
    *Why this matters:* FR-7.3 wires a codex `PreToolUse` (Bash) hook to the
    judge, and FR-7's acceptance is "25 dangerous commands through EACH agent:
    100% blocked pre-execution with audit entries." On this codex build that
    is unachievable via a hook — only the sandbox blocks codex, and coarsely.
    The PRD already leans on the sandbox as codex's backstop (§4.2, FR-7.3
    "`--sandbox workspace-write` as the backstop for non-Bash tool gaps"), but
    it assumed hooks "reliably cover Bash." They do not, headless. This is an
    FR-acceptance interpretation the human must adjudicate (surfaced at the P2
    entry, per FR-10.4 / the process contract's "stop and ask on FR
    conflicts"), not a defect the builder silently re-architects around.
    *Design feedback:* `doctor` must probe actual hook **firing** (not just
    file presence) per CLI and record it in the pin file; the judge wiring
    must be per-adapter, with codex's control declared as sandbox-primary on
    builds where exec hooks don't fire. A future codex release that fires exec
    hooks should be detected by the same probe, not assumed.
    *Decision (ratified by John, 2026-06-11): sandbox-primary for codex.*
    P2 builds the full judge+hook+audit path for claude (FR-7 met there);
    codex's pre-execution control is its sandbox (read-only for review steps,
    workspace-write for build steps). The codex hook client is still wired so
    it activates automatically if a future codex build fires exec hooks, but is
    pinned as inert on 0.139.0. FR-7's "100% blocked pre-execution with audit
    entries" is scoped to claude; the red-team suite still runs through codex
    to prove the sandbox blocks writes/network, recorded as sandbox-sourced
    (not judge-audited) blocks. Recorded as a pinned deviation in the doctor
    pin file.

11. **Codex emits no `command_execution` event for a sandbox-denied command.**
    P2 review F-008 asked the codex sandbox test to prove both that the write
    was *attempted* and that the sandbox *denied* it (not just that the file is
    absent). Probing codex-cli 0.139.0: a read-only sandbox denial surfaces
    ONLY as the exact OS errno inside an `agent_message`
    (`zsh:1: operation not permitted: <path>`) — there is no
    `command_execution` event in the `--json` stream for a command the sandbox
    refuses. So the strongest available proof is the path-specific errno in
    agent text (a model cannot produce the exact errno+path without the command
    actually being dispatched and refused). The test requires that errno; it
    cannot additionally require a raw command_execution event, because the CLI
    does not emit one. Recorded as an accepted build-limitation at the P2 gate.
    *Design feedback:* the P4 transcript logger should capture codex's
    sandbox-refusal signal from agent_message text on builds where no
    command_execution event is emitted, and `doctor`/the pin file should note
    per-build whether codex surfaces denied commands as events.

12. **P2 gate deviation (ratified by John, 2026-06-11): session-hook activation
    deferred to P3.** The P2 exit criterion asks for THIS bootstrap session's
    own PreToolUse hook to stay wired to the judge "protecting its own
    construction." The committable wiring (`.claude/settings.json` ->
    gauntlet-judge-hook, safe-by-default) IS shipped and FR-7 acceptance is met
    by the live red-team tests. But live-gating THIS session would require a
    session relaunch with GAUNTLET_JUDGE_* env (a running session's env is fixed
    at launch) and would route every build command through the judge with the
    human clearing ask-prompts. Decision: do NOT wire the running session now;
    the P3 engine owns the judge lifecycle and sets per-run env, which is the
    designed home for live session gating. The human is the interactive
    backstop in the meantime (consistent with the F-004 operational model).
    *Design feedback:* the bootstrap prompt's "wire this session from P2 onward"
    instruction collides with "no engine until P3"; the self-gating dogfood is
    cleaner once the engine can start the judge and inject env per run.

## 2026-06-10 — P1 implementation

9. **Installed-CLI flag drift cuts both ways, found on day one.** Three
   divergences between the PRD/plan text and the installed CLIs, all caught by
   contract tests before anything depended on them: (a) `codex exec` 0.139.0
   has no `--full-auto` (PRD §4.1 lists it) — exec is already non-interactive
   and `--sandbox` alone governs write access; (b) `codex exec resume` accepts
   `--json`/`--output-schema`/`-o` but **not** `--sandbox`, so the sandbox must
   be re-pinned via `-c sandbox_mode="…"` on resume or it silently reverts to
   config default; (c) claude 2.1.172 gained native structured output
   (`--json-schema`, result in a dedicated `structured_output` field) where the
   PRD assumed best-effort JSON for claude — a positive divergence the
   ClaudeCodeAdapter now uses as its primary path. *Design feedback:* validates
   FR-1.5's pin-verified-behavior posture; the resume case (b) is the sneaky
   one — a capability can differ between first invocation and resume of the
   same CLI, so contract tests must exercise resume paths with the same flags
   they use on fresh invocations.

## 2026-06-11 — P3 implementation (pipeline engine)

13. **The engine's own run dir would perpetually dirty the worktree — fixed by
    excluding the run root from every engine git operation.** The manifest /
    transcripts live *inside* the repo under `run_root/<slug>/` (FR-4.1), so
    every write makes `git status` non-empty. That destroys two P3 invariants
    at once: the clean-worktree-at-handoff rule (CLAUDE.md §1) and the F-003
    base-SHA transaction boundary (which decides "did this step leave partial
    edits?" by diffing the worktree). Resolution, layered: (a) the live
    run-instance dir gets a self-`.gitignore` (`*`); (b) **all** engine git
    checks/mutations — `is_clean`, `is_dirty_vs`, the commit `git add`, the
    reset backup snapshot — take an `exclude=[run_root]` pathspec so the run
    bookkeeping is invisible to them. Consequence: phase commits carry the
    *work*, never the engine's own manifest/pointer. *Design feedback:* this is
    the concrete cost of "manifests are committable in-repo" (FR-4.1/4.5); the
    P4 logger should decide deliberately what of a run becomes a tracked commit
    vs. live bookkeeping, rather than letting `git add -A` sweep it in. Also
    folds the two-slug confusion from #2 into a single `run_root` setting
    (default `runs`; FR-4.1's `.gauntlet/runs` is this same knob).

14. **`reset_to_base` needs `git clean`, not just `git reset --hard`.** When a
    builder agent_task is killed mid-edit and policy is `reset_to_base`, the
    partial work is often *untracked* files — and `reset --hard <base>` leaves
    untracked files in place. So the rewind is: snapshot the dirty tree to a
    backup ref (`refs/gauntlet/backup/<run>/…`, preserving the partial work for
    a human), `reset --hard base`, then `git clean -fd` (no `-x`, and excluding
    `run_root`, so neither the gitignored run dir nor the run pointer/authored
    prd.md is wiped). The looped kill-9 crash test exercises both policies
    (`park` records the step `interrupted` and parks; `reset_to_base` recovers
    and completes with exactly one commit, no duplication).

15. **Session-hook activation (deferred from #12) landed via engine-managed
    judge lifecycle.** `gauntlet run` now starts the judge as a subprocess
    (`python -m gauntlet judge serve`, robust to the console script not being on
    PATH), injects per-run `GAUNTLET_JUDGE_{TOKEN,URL,MODE,RUN_ID}` into the
    environment (and `GAUNTLET_STEP_ID` per step) so the PreToolUse hooks of the
    agents the engine spawns gate against it, then tears the judge down on exit.
    This is the designed home #12 pointed at: the bootstrap session itself is
    still the human-backstop driver, but the *agents it orchestrates* are now
    live-gated. The no-creds half of the P3 contract test asserts the
    start→inject→stop lifecycle; the claude-driven half (skipped without the CLI)
    asserts a real `agent_task → shell → commit` pipeline runs through the live
    judge with audit entries.

16. **Two live-gating facts the P3 contract test surfaced (verified on
    claude 2.1.172 + judge fast path).** (a) The engine-managed judge only
    gates a claude agent if that invocation loads the repo's PreToolUse hook —
    which claude does **only** under `--setting-sources project`. So live
    session gating (#15) requires the builder profile to carry
    `base_flags: ["--setting-sources", "project"]`; without it the hook never
    fires and the agent runs ungated. Added to `.gauntlet/config.yaml`'s builder
    and pinned. (b) In-repo *writes* are deliberately not a policy fast-path
    allow — `echo x > f` and the `Write` tool both escalate to the LLM
    classifier rung, which fail-closes to **deny** when no `judge_llm` is
    configured (verified: the agent's `echo gauntlet > hello.txt` was denied
    `fail-closed`, audited). That is correct per FR-7.6 (writes aren't on the
    deterministic allow list), but it means a live builder that edits files
    needs a working `judge_llm`, or the human-interactive degraded mode. The P3
    contract test therefore has the agent run a bare `echo` (a fast-path allow,
    audited) and lets the `shell` step produce the committed change — proving
    the agent_task→shell→commit machinery through a live judge without a
    classifier dependency. *Design feedback:* `doctor` (P6) should probe that an
    agent profile expected to write has either a reachable `judge_llm` or
    interactive mode, and warn if a claude builder profile lacks project setting
    sources — both are silent live-gating failures otherwise.

## 2026-06-11 — P4 implementation (adversarial_cycle + logger + triage corpus)

17. **The handoff said `gauntlet/bootstrap`; the repo's history lives on
    `main`.** At P4 session start the branch `gauntlet/bootstrap` did not
    exist — every P1–P3 commit had landed directly on `main` (4 ahead of
    origin/main), contradicting both CLAUDE.md §3 ("never commit directly on
    the base branch") and the continuation prompt's recorded state. Created
    `gauntlet/bootstrap` at the inherited HEAD (`080ade7`) and continued
    there; the pre-existing main commits are history, not rewritten (no
    force-push rule). *Design feedback:* FR-9.1's "creates (or resumes on) a
    dedicated branch" is the engine-owned guarantee that prevents exactly
    this manual-process drift; a `doctor`/session-start check could assert
    "not on the base branch" before any bootstrap work.

18. **P4's own commits are manual `git` — the expected switchover gap.**
    Switchover #1 covers branch/commit/manifest mechanics "where a command
    exists"; there is no standalone `gauntlet commit`, and P4 builds the very
    cycle that would drive itself. Recorded per the plan's "record any gap
    that forced you to fall back to manual." Switchover #2 lands at the END
    of P4: `pipelines/bootstrap.yaml` + `runs/gauntlet-bootstrap/prd.md` +
    prompt stubs now express P5–P7, and from P5 onward manual process
    execution is a bug. *Design feedback:* a thin `gauntlet commit
    --phase PN` (format-validate + identity + manifest record, no pipeline
    needed) would close the last manual surface for future bootstraps.

19. **The configured cheap model was unrunnable on this machine — caught by
    the P4 accuracy harness, fixed as config.** `.gauntlet/config.yaml` had
    `triage: claude-haiku-latest`, but only an OpenAI key is present in the
    environment and LiteLLM does not resolve that alias at all. Switched the
    triage/judge_llm profiles to `gpt-5-mini` (the cheap model P1's contract
    suite verified) and added an `escalation: gpt-5` profile for P4's
    severity-aware escalation (review F-009); both probed live before use and
    pinned. *Design feedback:* `doctor` (P6) must validate that every `api`
    profile's model actually resolves against the present credentials —
    a config that parses but cannot run is a silent run-time failure.

20. **codex `--output-schema` enforces OpenAI strict-mode schema rules —
    "optional" must be spelled required-but-nullable.** The first live review
    call failed with HTTP 400: `'required' is required ... including every
    key in properties`. The §7 excerpt marks `suggested_fix` optional; the
    normative `schemas/findings.json` expresses that as required +
    `["string","null"]` (the same spelling the hand-built P1–P3 review
    schemas already used — this is why they worked). Also dropped `pattern`
    constraints to stay inside the strict-mode subset. Verified end-to-end
    after the fix (live probe + the P4 cycle contract test) and pinned.
    *Design feedback:* schemas intended for native structured output must be
    authored in the strict subset from the start; a future `doctor` check or
    schema lint could enforce it.

21. **Triage-accuracy result (P4 assumption test): PASS — but the corpus
    verdict-skew is real and recorded.** `gpt-5-mini` over the 36-finding
    hand-labeled corpus: 94.4% verdict agreement, blocking row 9/9, zero
    blocking→reject misses (escalated or otherwise); report with per-severity
    confusion matrices at `runs/gauntlet-bootstrap/manual/
    p4-triage-accuracy.md`. Caveat recorded in the report: 34/36 labels are
    `legitimate` because the bootstrap's reviewers were good — a
    constant-`legitimate` predictor scores ~94%, so the aggregate gate is
    weak on this data and the operative checks are the blocking-miss
    criterion and the matrix (review F-009). *Design feedback:* FR-6.5's
    human-corrected triage cases are the designed source of non-legitimate
    examples; until those accumulate, treat aggregate agreement as a smoke
    signal, not a quality proof.

22. **Round artifacts are run bookkeeping, not commit payload (resolves the
    #13 question for the cycle).** findings/triage/confirm JSON are written
    under `run_dir/artifacts/` (excluded from engine git ops) with lossless
    per-sub-step copies under `steps/<id>/rN-*/`. Writing them into the
    tracked artifact root would have dirtied the tree between the fix commit
    and the next round's handoff — the confirm pass writes AFTER the commit —
    breaking FR-9.3 inside the cycle itself. Tracked commits carry the work;
    the durable audit trail is the run dir, committed deliberately (FR-4.5).

23. **Manual review records bloat the FR-9.5 confirm range — engine design
    already avoids it.** The P4 manual confirm's `<handoff>..<fix>` diff
    initially weighed 520KB because the tracked `P4.r1` record commit (433KB
    of captured review events) sat inside the range; the confirm prompt
    excluded that bookkeeping explicitly and dropped to 59KB. Engine-driven
    cycles are immune by construction: round records live under the
    (excluded, self-ignored) run dir and never enter a range diff. Until the
    manual process retires (P5, switchover #2), manual confirm prompts must
    keep scoping their own records out. P4's review round itself worked
    end-to-end: 8 schema-valid findings first try on the normative schema,
    8/8 ratified-fixed-confirmed in one round, two real fail-path classes
    (confirm-by-omission, defer-on-blocking) that the cycle now guards
    against — the adversarial process catching holes in the adversarial
    machinery is the dogfood working.

## 2026-06-12 — P5 start (first engine-driven run)

24. **Second unrunnable placeholder model, caught at the first live use —
    `doctor` must probe every profile's model, not just the api ones.** The
    first `gauntlet run gauntlet-bootstrap` failed in `p5-implement`:
    claude 2.1.172 rejects `--model claude-opus-latest` with an in-band 404
    (is_error=true, exit 1). The short alias `opus` resolves (to
    claude-opus-4-8) and is now configured + pinned. This is #19's lesson
    again on the claude side, and the P4.r1 F-007 fix earned its keep on its
    first day: the failed attempt left `failure.txt` /
    `transcript-failed.md` / `events-failed.jsonl` under the step dir, so
    diagnosis was a file-read, not a rerun. *Design feedback:* `doctor`
    (P6) needs a cheap per-profile model-resolution probe for the CLI
    adapters too (a tool-less one-token round-trip per configured model),
    and engine model-rejection errors should surface the adapter's in-band
    message in the step notes, not just the exit code. Also: the engine
    derives `gauntlet/gauntlet-bootstrap` from the slug while the manual
    bootstrap lived on `gauntlet/bootstrap` (#2's two-slug pain, branch
    edition) — resolved by pre-creating the derived branch name at the tip
    before the first run; both names point into the same history.

25. **Two operability gaps from the P5 restart, both fixed by config/hand.**
    (a) The builder profile had no `step_timeout_s`, so the adapter's 600s
    default would have halted the long p5-implement mid-edit; set to 5400s
    (FR-3.3 still applies, with a realistic ceiling). (b) After killing the
    seconds-old resume to apply that fix, the step record sat `running` with
    a base SHA that predated the `P4.2` config commit — resume would have
    false-parked on "head moved past base" even though the step never made
    an edit. Resolved by hand-resetting the record to pending with the
    reasoning written into its notes (worktree verified clean first).
    *Design feedback:* the engine needs a guided `gauntlet reset-step
    <slug> <step>` (clean-tree-checked, reason-recorded) so this manifest
    surgery has a command path; and the resume disposition could distinguish
    "head moved but tree clean and matches HEAD" (a human committed
    something in between) from "tree dirty vs base" (real partial work) —
    the former is re-runnable with a re-based boundary after confirmation.

26. **The judge's LLM rung was non-functional with gpt-5 models — every
    escalated tool call failed closed, and the first live builder correctly
    refused to work blind.** `build_core` passed `temperature=0` (a P2-era
    determinism choice); gpt-5-family models reject any non-default
    temperature, litellm raised UnsupportedParamsError, and the classifier
    rung denied EVERYTHING the policy fast-path didn't allow — including
    `Read` of the plan. The builder's response was the process working: it
    made zero changes, wrote a precise operational-halt report naming the
    exact litellm error, and asked for a human fix (the engine still
    recorded the step `done` because the adapter exited 0 — see design
    feedback below). Fixes, all live-verified: drop temperature (rubric
    provides the determinism), add a `reasoning_effort` passthrough to
    ApiAdapter with the judge pinned to `minimal` (2.3-3.1 s/verdict AND
    more rubric-faithful than `low`, which allowed an out-of-repo write in
    the probe; default effort blew the 5 s budget entirely), and timeout
    5→6.5 s for cold-start headroom (first call measured 4.9 s). *Design
    feedback:* (a) P2's live red-team suite ran the classifier rung against
    a DIFFERENT provider family than the config later pinned — judge model
    changes must re-run the classifier contract test; (b) `agent_task`
    completion should not be inferred from exit-code alone — the builder's
    own PHASE COMPLETE / halt-report convention exists precisely so the
    engine can distinguish "done" from "stopped deliberately" (candidate
    for the P5+ standard pipeline prompts + a completion-signal check).
    The gap bit AGAIN the same day: the builder's FR-10.4 upstream-conflict
    halt (#28) was also recorded `done`, and the run burned a tests+commit
    step rediscovering that nothing had been built.

27. **`gauntlet run` does not refuse while a run is already active — a
    second invocation silently creates a competing run.** Asked "can I run
    `gauntlet run` again?" mid-flight, the honest answer was no: `start()`
    unconditionally creates a fresh `run-<timestamp>/`, repoints
    `active-run.txt` at it, and would spawn a second builder against the
    same worktree the live run's builder is editing — racing agents on one
    tree, and the first run's bookkeeping orphaned with no pointer back.
    Nothing was harmed (the question was asked, not executed), but the
    foot-gun is one CLI invocation away. *Design feedback:* `start()` must
    check the active pointer and refuse when that run's manifest says
    `running` (or a live process holds it), with "resume or abort first"
    guidance — and `doctor` should flag an active-run pointer whose run is
    in a non-terminal state with no corresponding process.

28. **FR-10.4 resolution (ratified by John, 2026-06-12): PRD/plan review
    cycles carry stage labels — the commit format admits `PRD`/`PLAN`
    prefixes.** P5's builder halted correctly on a real spec gap: FR-5.1's
    `standard.yaml` starts with `prd-cycle`/`plan-cycle` (no phase number),
    but FR-9.2/9.4 define commit headers as phase-numbered and the engine
    enforced `P<digits>` only — so the normative pipeline failed at its
    first step, and a PRD/plan fix round had no legal commit label. Options
    surfaced per the contract; the human ratified extending the format:
    `PRD:`/`PRD.1:`/`PRD.r1:` (and `PLAN…`) are now valid alongside
    `P<n>…`, with `prd-cycle`/`plan-cycle` carrying `phase: PRD`/`PLAN`.
    Rollback semantics are untouched — `--phase N` targets remain numeric;
    PRD/PLAN commits are simply not rollback boundaries. Recorded here as
    the FR-10.4 "accepted deviation note": the approved plan/PRD texts are
    NOT amended (they stay as written; this note + the ratifying gate are
    the audit trail). *Design feedback:* the PRD's FR-5.1↔FR-9.2
    interaction is exactly the class of cross-FR contradiction the PRD
    adversarial-review prompt should hunt for (two normative sections, each
    individually fine, jointly unsatisfiable).

29. **The judge took the repo boundary from the agent's floating cwd — a
    deny-loop the moment the builder worked in a scratch directory.**
    `hook_client` computed `repo_root = payload["cwd"] or GAUNTLET_REPO_ROOT
    or getcwd()` — cwd FIRST. P5's builder legitimately moves into a
    toy/scratch project (FR-10.1 toy PRD lives in a separate repo), and
    every Edit/Read it then made against the *real* checkout resolved as
    "outside the repository tree": fast-path deny on `write-outside-repo`,
    and a wrong `repo_root` poisoning the LLM rung's context too. It
    oscillated — work-from-repo allowed, work-from-toy denied — and on the
    third P5 attempt tipped into a sustained deny-loop (44+ denials, no
    progress), so I killed the run. Fix: the run's repo boundary is
    engine-injected (`GAUNTLET_REPO_ROOT`, set by `ManagedJudge` from the
    real repo root) and the hook prefers it over cwd; cwd is only the
    no-engine fallback. The priority was simply backwards. Partial builder
    work preserved at `refs/gauntlet/backup/p5-*-20260612`; the dead run's
    steps reset for a clean re-run. *Design feedback:* this is the third
    distinct "config/wiring parses fine but is wrong against the live
    environment" failure of P5 (#24 opus alias, #26 judge temperature,
    now #29 repo-root source) — `doctor` and a pre-run smoke (one gated
    in-repo edit from a scratch cwd) would have caught all three before a
    20-minute builder burn. The fail-closed posture made every one of them
    safe-but-blocking rather than dangerous, exactly as designed.

30. **Cycle convergence policy made explicit (ratified by John, 2026-06-12):
    severity-gated, regression-scoped — no bikeshedding/oscillation loop.**
    The P4 cycle physically cannot loop forever (`max_rounds`, default 2, then
    FR-10.5 escalation), but two things fed oscillation: each round re-ran a
    FULL adversarial review (round 2 could invent fresh nitpicks), and a P4.r1
    fix (F-003) made `major` confirm-regressions force another round. John
    flagged this and chose option A. New behavior (config `cycle_convergence`,
    default `"blocking"`; `"strict"` restores the P4 original):
    - **blocking (P0):** the only severity that forces another round; loops to
      `max_rounds`, then escalates to the human gate. Never silently shipped.
    - **major (P1):** gets ONE fix attempt; if still open it is *surfaced* at
      the human gate (recorded in `confirm.json` → `surfaced_for_gate`), not
      looped on. Kills the "major continuously not addressed" oscillation.
    - **minor/nit:** one shot in round 1, never loops.
    - **rounds 2+ are regression-scoped** (`prompts/cycle-rereview.md`): the
      re-reviewer confirms the carried (blocking) findings and may raise a new
      finding only for a *blocking regression* — it does not hunt fresh issues.
    Consistent with FR-10.5 ("satisfied" = no blocking/unresolved), so this is
    an in-scope refinement recorded here, not a plan/PRD amendment. The
    bootstrap's own P1–P4 cycles all converged in 1–2 rounds with mostly-
    legitimate findings, so oscillation was never stress-tested in-house — the
    policy is anticipatory, for the general-purpose use on other repos (the
    tool's actual point). *Design feedback:* `report --trend` (P7/FR-6.6)
    should track findings-per-round and the surfaced-vs-fixed ratio so a
    reviewer/triager that drifts into nitpicking is visible in the metrics.

31. **#29's hook-env fix didn't take on claude — made the judge SERVICE
    authoritative for the repo boundary instead.** P5 attempt 4 deny-looped
    again on in-repo edits despite the P4.7 hook fix being live in the
    editable source: `GAUNTLET_REPO_ROOT` never reached the
    `gauntlet-judge-hook` subprocess claude spawns (claude evidently does not
    propagate the engine-injected env to its PreToolUse hook process). The
    env-injection approach was the wrong layer. Correct architecture: the
    repo boundary is a property of the RUN, known by the engine that starts
    the judge — so `JudgeCore` now holds an authoritative `repo_root`
    (threaded `ManagedJudge -> gauntlet judge serve --repo-root -> build_core
    -> JudgeCore`) and uses it for every path check; the request-body
    repo_root (and thus the agent cwd) is a fallback only, for the dev
    `judge serve` with no `--repo-root`. The hook-side `GAUNTLET_REPO_ROOT`
    preference (#29) stays as belt-and-suspenders. Verified live end-to-end:
    an in-repo Edit from a `/tmp/toy-project` cwd now ALLOWS, while a write
    to `/etc/passwd` still DENIES via `write-outside-repo`. Also: the judge
    audit line now logs `repo_root` — the boundary used for the check is
    diagnosable from the audit alone; had it been there, #29/#31 would have
    been a one-look diagnosis instead of two burned runs. *Design feedback:*
    trust/identity context (repo root, run id) belongs in the engine->judge
    channel, never re-derived from the sandboxed agent's ambient state; a
    `doctor` smoke that fires one in-repo edit through the managed judge from
    a foreign cwd would catch any regression here.

32. **Judge denied in-repo edits whose CONTENT mentioned an outside path —
    the real P5 blocker (builder correctly identified it, then halted).**
    With the repo boundary finally correct (#31), P5's builder still hit
    `write-outside-repo` fast-path denials editing in-repo engine files. Root
    cause: `_candidate_paths` ran the path-token regex over `_command_text`,
    which for non-Bash tools FLATTENS every string field — including an Edit's
    `old_string`/`new_string`. So editing a file whose content contains an
    absolute path (pervasive in a path-handling codebase: `/tmp/...`,
    `~/.aws/...`, doc examples) made the judge extract that CONTENT path and
    judge the edit as a write to it. The builder diagnosed it precisely
    ("policy.py scope-tightening"), refused to work around the safety layer
    (CLAUDE.md §3), and halted for a human — the right call. Fix: harvest path
    tokens ONLY from a real shell `command` (Bash), where a token IS an
    operation target; structured file tools use their explicit path key only.
    Verified: an in-repo Edit with `/Users/.../.aws/credentials` in
    old_string no longer fast-path-denies, while a Write to `/etc/evil.txt`
    via file_path still denies. *Design feedback:* the disguised-halt gap
    (#26b) bit a THIRD time here — the builder's halt was recorded `done`
    (exit 0), so the engine marched on to a doomed commit step; a
    completion-signal contract for `agent_task` is now clearly P5-critical,
    not nice-to-have.

33. **P4.5's active-run.txt gitignore collided with the engine's exclude
    pathspec — `git add` errored, failing the commit step.** Once
    active-run.txt was gitignored (P4.5) AND still named in the engine's
    `:(exclude)active-run.txt` pathspec, `git add -A` refused ("paths are
    ignored ... use -f") — git errors when a pathspec explicitly names an
    ignored path, even an exclude. Fix: stop naming it in the pathspec (gitignore
    already keeps it out of status/add) and have the ENGINE write a slug-level
    `.gitignore` (`active-run.txt`) so the pointer is ignored in every repo —
    including throwaway fixture repos that lack the repo-level rule — not just
    where `init` shipped the rule. `run_bookkeeping_excludes` now lists only
    the run-instance dir. Verified in a fresh fixture: `git add` exits 0,
    active-run.txt stays unstaged, the slug `.gitignore` is the only added
    bookkeeping. *Design feedback:* engine-owned ignores beat depending on the
    repo's own `.gitignore`; the run-instance dir already self-ignores, and
    the pointer now does too at the slug level — symmetric and portable.

34. **P5: `standard.yaml`'s artifact-mode plan cycle has no committed baseline
    — FR-5.1's exact step sequence collides with FR-9.3.** FR-5.1 wires the
    plan stage as `plan-author → plan-cycle` with no commit step between them,
    so the freshly authored `plan.md` is *uncommitted* when control passes to
    the reviewer. The P4 `adversarial_cycle` (exercised only on already-
    committed artifacts — every artifact-mode test seeds + commits prd.md
    first) requires a clean baseline: its round-1 clean-handoff guard would
    fail, and worse, its post-review mutation check would read the whole
    uncommitted plan.md as a "reviewer mutation". Resolved *without* deviating
    from FR-5.1's sequence: the cycle, in artifact mode, commits the artifact
    as an engine-composed `PLAN:`/`PRD:` baseline (no agent call) **only when
    the single dirty path is the artifact itself** — a genuinely dirty handoff
    (anything else uncommitted) still fails FR-9.3. prd.md is already committed
    by its human author, so prd-cycle is a clean no-op; code_review cycles hand
    off on the prior phase-commit, so they are too. *Design feedback:* the
    artifact-mode/code-mode split in the cycle had an unstated precondition
    (committed artifact) that only surfaced when the first *uncommitted*
    artifact (an authored plan) actually ran through it — exactly the kind of
    gap a dogfood end-to-end is meant to find.

35. **P5: `standard`'s first step is a *review*, so the slug `.gitignore` had
    to self-ignore.** The bootstrap pipeline's first step is a phase commit,
    which sweeps the engine-written slug `.gitignore` into git before any
    review. `standard.yaml` opens with `prd-cycle` — a review — so that
    untracked `.gitignore` would dirty the worktree at the very first handoff
    and fail FR-9.3. Fix: the slug `.gitignore` now lists itself (mirroring the
    run-instance dir's `*` self-ignore, notes #33), so the slug-level
    bookkeeping is invisible to the worktree-state machine in every pipeline
    shape, commit-first or review-first.

36. **P5: `PR.md` is engine-drafted but must stay out of the engine's own git
    operations (FR-9.8 ↔ §2.2).** Drafting `runs/<slug>/PR.md` at the final
    gate left the worktree dirty, which broke the clean-tree invariant for
    post-run operations (rollback refused on the untracked PR.md). PR.md is
    added to `run_bookkeeping_excludes`: the engine never auto-commits or
    pushes it (opening the PR is a human action, §2.2), and clean/rollback
    checks ignore it — but it stays plainly visible for the human to
    `git add` deliberately (not gitignored, so no #33 pathspec clash). PR
    drafting lives in `RunManager` (which owns the slug layout), not the
    orchestrator, so a bare `adversarial_cycle` driven straight to DONE in a
    unit test does not spuriously emit a PR.

37. **P5: per-agent-profile cost attribution is needed for the FR-3 acceptance,
    not just per-step.** `gauntlet report` must answer "is triage/judge/retro
    < 5% of run cost?" — but a single `adversarial_cycle` step bills the
    reviewer, triager, fixer, and escalation profiles, so the manifest's
    per-step totals cannot attribute classification spend. Added
    `manifest.agent_usage` (per-profile `UsageTotals`), populated from a new
    `StepResult.usage_by_agent`: `agent_task`/`commit` report their one
    profile; the cycle threads the agent name through `_run_sub` so the grand
    total and the per-profile split fall out of one accumulator. Kept separate
    from `totals`, never double-counted.

38. **P5: the disguised-halt gap (#26b/#32) is closed for `agent_task` —
    opt-in.** Per #32, an agent that exits 0 may still have *halted* (an
    FR-10.4 upstream conflict), and exit-code-alone marched the engine on to a
    doomed commit. `agent_task` now honours an opt-in completion-signal
    contract: `halt_on:` (a marker that, if present in the final output, parks
    the run for a human instead of recording `done`) and `require_signal:` (a
    marker that, if absent, fails closed). Opt-in so document-authoring tasks
    keep plain exit-code semantics; `standard.yaml`'s `plan-author` and
    `implement` steps set `halt_on: "UPSTREAM CONFLICT"` (the CLAUDE.md §4
    builder-conflict signal). Deferred: a positive `PHASE COMPLETE` structured
    handshake and applying the contract to the live bootstrap implement steps
    — recorded here rather than scope-crept into P5.

39. **P5 operability: the session's own judge fail-closed-denied a benign
    prompt-file `Write` mid-implementation (transient LLM-rung error).** While
    writing `prompts/review-code.md`, the wired PreToolUse judge returned
    "judge LLM error, failing closed" and blocked the write; an immediate retry
    of the identical write succeeded. The fast path has no allow rule for
    ordinary in-repo file writes, so they escalate to the LLM classifier rung,
    which intermittently errors (timeout/rate) and — correctly — fails closed.
    For a frontier-model interactive build session this is a real tax: dozens
    of benign in-repo writes each gamble on the classifier. *Design feedback:*
    a fast-path allow rule for in-repo writes under the run/repo root (the
    judge already knows the boundary, notes #31/#32) would convert these from
    LLM-rung gambles into deterministic sub-150ms allows — a strong FR-6.3
    retro-proposal candidate from this very run.

## 2026-06-12 — P6 (`init` / `doctor` / rollout packaging)

40. **`init` ships the default assets as bundled package data, not by copying
    the repo's own root files.** An installed `gauntlet` (via `uv tool install`
    from a git URL, FR-1.1) has no repo checkout to copy from, so the scaffold
    set lives under `src/gauntlet/scaffold/` and is bundled into the wheel
    (verified: 20 files present in the built wheel). The shipped `policy.yaml`,
    `pipelines/standard.yaml`, `prompts/*`, `schemas/*`, and
    `.claude/settings.json` are byte-identical copies of the repo's canonical
    assets; a unit drift-guard (`test_shipped_scaffold_matches_repo_canonical_assets`)
    fails if they diverge. The scaffold `config.yaml` is *intentionally
    different* — a clean default per PRD FR-2.1, not the bootstrap's
    pin-specific config (short `opus` alias, `base_flags`, `step_timeout_s`) —
    so it is excluded from the drift guard.

41. **The repo-level codex hooks config is written but inert on the pinned
    build, and its exact schema is unverified.** FR-1.2 requires `init` to write
    "the repo-level Codex hooks config." Per #10 (ratified sandbox-primary),
    `codex exec` 0.139.0 never fires PreToolUse hooks, so the file
    (`.codex/hooks.json`) cannot be exercised to verify codex's expected schema.
    `init` writes a forward-looking config that mirrors the shared
    stdin-JSON / exit-2 / `permissionDecision` contract and documents its inert
    status in-file; `doctor`'s `codex-hook` check reads the firing status from
    the pin notes and reports "present (inert; sandbox-primary)" as **healthy,
    not a failure** — failing on a hook the build cannot fire would be exactly
    backwards. If a future codex build fires exec hooks, the same probe detects
    it and the schema gets verified then (consistent with the project's
    pin-verified-not-assumed ethos). Recorded as a known unverified-but-required
    artifact, not a silent guess.

42. **`doctor` is split into pure checks + injected probes so the "simulated
    broken environment" tests run offline.** Each check returns a
    `CheckResult(status, detail, remedy)`; CLI version lookup and the env
    (for API-key presence) arrive through an injectable `DoctorProbes`. Real
    invocation builds probes that shell out to `claude --version` /
    `codex --version` and read `os.environ`; the unit suite injects fakes for
    missing-CLI / stale-version / missing-key / secret-in-config cases. FR-1.5
    version skew is a WARN (does not block); genuine blockers (absent CLI,
    unwired hook, no API key for any configured api profile, credential literal
    in repo config, unloadable policy/config) are FAILs that exit non-zero.
    Observed live on this repo: `doctor` correctly WARNs that the installed
    claude (2.1.177) has drifted from the pin-verified 2.1.172 — the pin file is
    now one patch release behind the machine; left as-is (the contract suite
    re-verifies and updates the pin, per #24's lesson, not a P6 edit).

43. **Second-environment install test uses a throwaway `uv venv`, not
    `uv tool install`.** The plan's ground rule asks before system/global
    tooling. `uv tool install` writes to the user's global tool dir (machine
    state); a disposable `uv venv` + `uv pip install <wheel>` is fully contained
    in a tmp dir and proves the same thing (console scripts on PATH, scaffold
    bundled, `init`/`doctor` run). The README documents the real
    `uv tool install git+<url>` path for teammates; the test uses the contained
    equivalent by default (container/global tooling deferred to human sign-off).

## 2026-06-24 — resume-response feature (FR-9), P2 review

44. **A P2 phase commit landed with materially false audit metadata
    (review F-001).** Commit `2a85816` ("P2: add operator identity module and
    unit tests") was authored AND committed by `Gauntlet Triage
    <triage@gauntlet.local>` rather than the builder, and its body claims work
    the diff never contains — "deterministic key creation, public-key
    derivation, basic sign/verify," and FR-2.1 / FR-3.3 / FR-7.2. The actual
    diff only adds `src/gauntlet/engine/identity.py` (the FR-9 operator-email
    resolution primitive: `GAUNTLET_USER_EMAIL` → `git config user.email`
    precedence, fail-closed) plus its unit tests. The correct attribution is the
    builder, and the body should cite FR-9 and note the P3 wiring deferral
    (recording/consume of the resolved `user` into the response audit entry).
    This recurs the earlier P1 finding F-003 (commit drafter's identity bleeding
    into the commit author), even though the commit step already resolves
    authorship to the builder by default (`steptypes.py:319-327`) — so a manual /
    out-of-engine commit path reintroduced it here.
    *Why recorded, not rewritten:* the false commit is durable shared history.
    Per CLAUDE.md §2 ("approved artifacts / shared history change only through
    their own loop and gate") and §3 (no history rewrite without explicit human
    instruction), the fixer does not amend or force-push it. The accurate record
    lives here and in the fix-round commit's audit body (FR-9.4); a human ratifies
    any history correction.
    *Design feedback:* the false body is a model hallucination that the commit
    drafter / format check passed — `commit_format.py` validates shape, not
    whether the body's claims match the diff. A cheap guard: cross-check
    FR-citations and "added file" claims in the drafted message against the
    staged diff before committing, and reject a body that asserts files or FRs
    the diff does not touch. Authorship: any commit path outside the engine's
    commit step (manual bootstrap commits) should reuse `config.identity(builder)`
    rather than inheriting whatever git identity the drafting session ran under.

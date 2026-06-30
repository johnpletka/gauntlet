I now have a concrete, grounded picture. Key findings that shape the plan:

- **Tracker registry** mirrors `gauntlet.adapters` exactly: a `Protocol` + an `entry_points(group=...)` factory, registered in `pyproject.toml`.
- **`adversarial_cycle` code_review mode** computes its diff as `gitops.range_diff(base, handoff)` which is **two-dot** (`base..head`), with `base = review_base or handoff^` and `handoff = HEAD` when the manifest has no commits. FR-5.2 mandates **three-dot**. The clean, no-engine-change reconciliation: the review command resolves `merge-base(base, HEAD)` and injects *that concrete SHA* as `review_base`, so the existing two-dot helper yields the three-dot result.
- **`ctx.excludes`** is honored by `is_clean`/`commit_all`/`clean_untracked` — exactly the hook FR-2.4's in-repo `--intent` exclusion needs.
- **`policy.yaml` rules** currently carry `name`/`description` but no `id`/`version`/`ratified` fields — FR-7.4's `pr_read_commands@v1` needs those, ratified out-of-band, and P4's preflight is a deterministic read of it.

Here is the plan.

---

# Implementation Plan — Lightweight Issue Workflow (`gauntlet review`)

**Plan for:** PRD "Lightweight Issue Workflow — `gauntlet review`" (Draft v0.2)
**Author:** builder agent (CLAUDE.md §4)
**Status:** Draft for adversarial review + human ratification

This plan decomposes the PRD into four strictly-sequential phases (FR-10.3), each ending in passing tests and a single commit (FR-9.2). Phases are ordered to kill the riskiest assumptions first: the unowned external API (P1), the zero-footprint in-place lifecycle and its CLI surface (P2), the core product thesis that the cycle alone earns its place (P3), and finally the convenience PR surface behind its judge-policy gate (P4). The decomposition matches the PRD §8 table; this document makes each phase concrete.

## Ground rules this plan inherits

- **Reuse, do not re-implement.** The `adversarial_cycle` primitive, the manifest/`resume`/`status`/`abort` lifecycle, the reviewer-mutation guard (FR-9.6), severity-aware escalation (FR-10.5), the `findings`/`triage`/`confirm` schemas, and the judge push boundary (FR-9.8) are used verbatim. No phase modifies `engine/cycle.py`'s loop logic; the review feature reaches the cycle through configuration (`review_base`, `phase: REVIEW`, an intent-aware prompt) and a review-specific run lifecycle.
- **Simplest design that satisfies each phase.** The tracker abstraction ships exactly one provider (`linear`); other providers are a registry seam, not built code (deferral, not abstraction). `render_intent` does no heading/section detection in v1 — the full body lands under `## Problem`. The discrete `## Repro` / `## Expected` sections are reserved fields, emitted by no v1 code path.
- **Two-dot vs three-dot reconciliation (normative).** `gitops.range_diff(base, head)` is two-dot. FR-5.2 mandates `git diff <base>...HEAD` (three-dot). The review command resolves `merge_base = git merge-base <resolved-base> HEAD` and injects that concrete SHA as the step's `review_base`; the cycle's existing `range_diff(merge_base, HEAD)` then equals the three-dot diff. This keeps "diff scope is pure config; no engine change" (PRD §4.2) honest. P2 owns the resolution and its tests; P3 asserts the injected base reaches the reviewer.
- **Approved-artifact discipline (CLAUDE.md §0/§8).** `policy.yaml` is changed only through the policy-change process. The `pr_read_commands@v1` rule (FR-7.3/FR-7.4) is a **prerequisite gate for P4**, ratified out of band; this plan's build work for P4 is the *deterministic preflight reader* and the *rule proposal*, never a silent edit to `policy.yaml`. P1–P3 do not depend on it.

---

## P1 — Issue-tracker abstraction + Linear provider + doctor check

**Assumption validated.** The riskiest *external* dependency: a Linear ref (`ENG-1234` or a `linear.app/.../issue/<KEY>` URL) can be authenticated and resolved against an unowned third-party GraphQL API into a usable, normalized problem statement, and **fails closed** on auth / not-found / unavailable / timeout. This is testable in complete isolation from any review machinery.

**Deliverables.**
- `src/gauntlet/trackers/base.py` — the `IssueTracker` `Protocol` (`parse_ref`, `fetch`, `extract_refs`, `verify_auth`), the frozen `IssueRef` / `Issue` payload dataclasses (§6 shapes exactly), and the error taxonomy: `IssueTrackerError` (base), `IssueTrackerAuthError`, `IssueNotFound`, `IssueTrackerUnavailable`.
- `src/gauntlet/trackers/intent.py` — `render_intent(issue_or_text, *, provenance, independent, source) -> str`, deterministic and tracker-agnostic per §6 ("`render_intent` is deterministic (v1)"). Always emits `# Intent` header, a `<provenance: …>` line, and `## Problem` (verbatim body); emits the `<source: …>` line only for a `tracker` source; omits `## Repro` / `## Expected` entirely in v1 (no provider supplies the discrete fields).
- `src/gauntlet/trackers/linear.py` — `LinearIssueTracker`: `parse_ref` (human key + URL forms), `fetch` (GraphQL `issue(id:)` → normalized `Issue` incl. `state`), `extract_refs` (textual-order ref scan over arbitrary text, for P4's PR-body use), `verify_auth` (cheap `viewer { id }` probe). Auth via a token read **by env-var name** from `config.issue_tracker.api_key_env` (default `LINEAR_API_KEY`) — never the token in config.
- `src/gauntlet/trackers/__init__.py` — `get_tracker(config) -> IssueTracker` factory over the `gauntlet.issue_trackers` entry-point group, mirroring `adapters.get_adapter_class`. Registers `linear` only.
- `pyproject.toml` — new `[project.entry-points."gauntlet.issue_trackers"]` with `linear = "gauntlet.trackers.linear:LinearIssueTracker"`.
- `src/gauntlet/config.py` lives elsewhere; the model is `engine/config.py` `RunConfig`. Add an optional `IssueTrackerConfig` model (`provider`, `api_key_env`, `timeout_s`, optional `workspace`) with: non-`linear` provider → fail-closed `ValueError` at load naming the supported set (FR-6.2); `timeout_s` ≤ 0 → load error, default 10, min 1 (FR-6.4). `issue_tracker` is an **optional** field (absent / `provider: none` → tracker disabled).
- Per-call timeout wrapper (FR-6.4): every `fetch` and `verify_auth` call bounded by `timeout_s`; exceeding it raises `IssueTrackerUnavailable`.
- `gauntlet doctor` extension (`engine/doctor.py` + `cli.py`): when an `issue_tracker` block is present, validate provider is supported, the named env var is set, and `verify_auth` succeeds — fail-closed actionable messages (FR-10.1). Uses the same `timeout_s`.
- Transcript/audit redaction (§7): extend the redaction list construction so the resolved tracker token value can never be written to an artifact.

**Design notes.** The provider interface is the minimum FR-6.1 names — no labels/assignees/comments (reserved, not modeled). The factory is a thin entry-point lookup; no provider auto-discovery beyond the registry.

**Test strategy.** Unit tests with a mocked HTTP/GraphQL transport (no live network): ref/URL parsing, normalization round-trip into `Issue`, `render_intent` determinism (golden output incl. the provenance line and omitted Repro/Expected), each error-taxonomy mapping (401→Auth, missing issue→NotFound, 5xx/timeout→Unavailable), the `timeout_s`-exceeded → `Unavailable` path, and config-load rejection of non-`linear` provider and non-positive `timeout_s`. `doctor` tested with a stubbed `verify_auth` for the success and the missing-key / unreachable failure messages. A live-credentialed end-to-end fetch is marked `@pytest.mark.integration` (excluded from `pytest -m "not integration"`).

**Exit criteria.** `uv run pytest` green; a Linear ref and URL each resolve to a normalized `Issue` under a mocked transport; every failure mode fails closed with its typed error; config rejects unsupported providers / bad timeouts; `doctor` reports tracker health. Single commit `P1: …`.

**Deferrals.** GitHub-Issues / Jira providers (registry seam only). Discrete repro/expected provider fields (reserved). Closed-ticket policy (Open Question 11.6) — `Issue.state` is captured but not acted on yet.

---

## P2 — Review run lifecycle + the `gauntlet review` CLI entrypoint (resolve-and-stop, no cycle)

**Assumption validated.** A review run can parse its inputs at the CLI, operate **in place** on an existing branch with **zero** repo footprint, resolve a provenance-tagged problem statement from any of the four intent sources under the defined precedence (failing closed when none and no `--code-only`, requiring ratification for non-independent intent), and resolve a correct, non-empty three-dot diff base — including the `base_branch: current` degenerate case. Everything reachable **without** the cycle running.

**Deliverables.**
- New `review` command in `cli.py` parsing the full FR-1.1 signature: `gauntlet review [<branch>] [--pr <N|url>] [--issue <ref|url>] [--intent <path>] [-m <text>] [--intent-provenance …] [--approved-intent] [--base <ref>] [--code-only] [--rounds <N>] [--test | --no-test]`. In P2 the command resolves and validates, then **stops at the documented pre-cycle boundary** (cycle wiring is P3). `--pr` is parsed but routed to a "not yet — P4" stub (so P2 acceptance covers branch/positional/usage-error target resolution only). The mutually-exclusive usage errors (`--pr` + positional; `--intent-provenance` + `--issue`; `--test` without `config.test_command`) are enforced here.
- **Review run lifecycle** (a review-specific path parallel to `RunManager`, reusing its primitives — manifest, writer/redactor, worktree lock, `resume`/`status`/`abort`):
  - **Entry contract** (FR-9.2/FR-9.3): clean worktree at entry (target branch's committed state is the handoff), refuse if another non-terminal run owns the target branch.
  - **In-place branch adoption** (FR-1.3): operate on the resolved target branch; **no** `gauntlet/<slug>` branch minted (overrides FR-9.1 for this command). Detached HEAD fails closed (FR-5.3).
  - **Out-of-repo state dir** (FR-8.1/FR-8.4, §6 "Review state path"): resolve `${XDG_STATE_HOME:-~/.local/state}/gauntlet/reviews/<repo-id>/<slug>/`; `<repo-id>` = first 12 hex of SHA-256 of the normalized `origin` URL (or normalized toplevel path when no `origin`); `<slug>` = sanitized branch name (or `pr-<N>`), collisions disambiguated by a short hash of the unsanitized name. The `review.state_dir` config override (FR-8.3) replaces the root with an in-repo gitignored location, `<repo-id>/<slug>` layout unchanged.
  - **Intent resolution + precedence** (FR-2.1): single source in order `--issue` (P1 tracker fetch) > `--intent <file>` > `-m <text>` > `$EDITOR` template; snapshot through `render_intent` into the out-of-repo `intent.md`. Intent **required** unless `--code-only` (FR-2.3) — missing intent fails closed before any agent.
  - **FR-2.4 in-repo `--intent` exclusion**: read `--intent` once at run start; if its path resolves inside the repo, add it to the run's `ctx.excludes` (honored by `is_clean(exclude=…)` and `commit_all(exclude=…)` per gitops), so it cannot trip the entry contract or be swept into a `REVIEW.x` commit; left present and untracked. `-m` / `$EDITOR` write **no** file in the repo (editor temp lives under the state dir).
  - **Provenance + ratification** (FR-2.1a/FR-2.5/FR-2.6): derive `provenance` (`tracker` for `--issue`; declared via `--intent-provenance`, defaulting to `author-session-summary` for manual sources) and the `independent` boolean. For non-independent intent, the pre-run ratification hook: interactive TTY → render + confirm/edit; non-interactive → require `--approved-intent` else fail closed, spawning no agent. Persist the §6 manifest `intent` block (`source`/`provenance`/`independent`/`ratification`).
  - **Base resolution + empty-diff guard** (FR-5): resolve base `--base` > concrete `config.base_branch` > `origin/HEAD` (the `current` sentinel is **never** a review base); compute `merge-base(base, HEAD)`; fail closed on empty three-dot diff, no-shared-history, or detached HEAD with the FR-5.3 messages. The resolved merge-base SHA is what P3 injects as `review_base`.
- `review.state_dir` field added to `RunConfig` (default `null`).

**Design notes.** The review lifecycle is a focused new module (e.g. `engine/review.py`) that composes existing `RunManager` primitives rather than subclassing the whole heavyweight `start()` — the heavyweight branch-minting, PRD entry contract, and gate stages do not apply. The state-dir derivation is pure functions (hash + sanitize) so it is unit-testable without a filesystem run.

**Test strategy.** Command-level tests (`gauntlet review …`) reachable without the cycle: each intent source and the precedence order; provenance defaulting; the non-interactive ratification gate (rejected without `--approved-intent`, accepted with it, manifest records `ratification.method`); `--intent-provenance tracker-session` still requires ratification; missing-intent-without-`--code-only` fail-closed (no agent); `--code-only` produces no `intent.md`; the in-repo `--intent ./bug.md` case — entry contract passes, `git status` after shows `bug.md` present + untracked, and it is in `ctx.excludes`; `base_branch: current` resolves to the remote default and yields a real diff, empty-diff guard fires with the exact message; state-dir derivation (repo-id stability across checkouts, slug sanitization, collision disambiguation). Footprint assertion: a resolve-and-stop run leaves the tree clean and creates nothing in-repo.

**Exit criteria.** `uv run pytest` green; all FR-1/FR-2/FR-5/FR-8 pre-cycle acceptance criteria pass at the command level; the run resolves a provenance-tagged `intent.md` out-of-repo, exits cleanly at the pre-cycle boundary. Single commit `P2: …`.

**Deferrals.** Cycle execution → P3. PR mode (`--pr` is stubbed) → P4. The `/gauntlet-review` skill is out of v1 scope (Open Question 11.8); P2 only exposes what it would need.

---

## P3 — Wire the cycle: `review.yaml` + intent-aware prompt + zero-gate execution

**Assumption validated.** The PRD §1.3 core thesis: the `adversarial_cycle` **alone**, on a small diff plus a (possibly author-derived, ratified) intent, runs end-to-end with zero routine gates, lands `REVIEW.x` fixes in place, and surfaces real `correctness` / `spec-gap` findings against the intent — at acceptable cost/latency. This is where the feature earns (or fails to earn) its place over plain Claude Code.

**Deliverables.**
- `pipelines/review.yaml` — a single-stage pipeline (§6 shape): one `adversarial_cycle` step (`mode: code_review`, `phase: REVIEW`, `reviewer`/`triager`/`fixer`/`escalation_agent`, `max_rounds: 1`, `review_prompt: prompts/review-code-intent.md`), **no `human_gate`** (FR-3.1), and the optional `baseline-tests` shell step commented out (off by default; `--test` includes it, requires `config.test_command`, `--no-test` is explicit-off — FR-1.1, Open Question 11.5).
- `prompts/review-code-intent.md` — the existing code-review lens plus the explicit solution-correctness dimension: the reviewer is given the diff **and** `intent.md` and asked whether the change resolves the stated problem and meets stated acceptance, surfaced through the existing `correctness` / `spec-gap` categories (no schema change). The prompt is told the intent `provenance` + `independent` flag so it calibrates the problem-correctness axis (FR-2.2): a `tracker`-independent statement is authoritative; a non-independent one is the author's own framing, with implementation-correctness / acceptance / regression / quality axes carrying full weight regardless.
- **Cycle wiring** in the P2 review lifecycle: execute `review.yaml` through the orchestrator with the injected `review_base` = the P2-resolved merge-base SHA (yielding the three-dot diff), `max_rounds` from `--rounds` (default 1, FR-3.3), and `intent.md` + provenance threaded into the reviewer prompt. `intent.md` is wrapped/labeled as untrusted data where it flows into the triager path (§7 prompt-injection containment), reusing `wrap_as_data`.
- **Terminal severity contract** (FR-3.4): the cycle already parks on an unresolved legitimate blocking finding (FR-3.2/FR-10.5 — preserved unchanged). Add the review run's completion handling so an unresolved legitimate **non-blocking** finding (`major`/`minor`/`nit`) **completes** but is recorded in the run summary as **residual risk** (id, severity, location, claim, last confirm verdict); a **not-legitimate** finding is recorded with its triage reasoning. The `blocking` policy (default `cycle_convergence`) already surfaces non-blocking opens at the gate — here there is no gate, so they go to the summary instead.
- Run summary rendering for a review run: `REVIEW.x` commits, residual-risk list, declined findings, cost attribution (reuses cycle metrics / FR-3.2). Honors the out-of-repo state dir (no committed artifact).

**Design notes.** No change to `engine/cycle.py`'s loop. `_phase_and_handoff` already returns `(explicit_phase, HEAD)` when the manifest has no prior commits — exactly the review run's state at cycle start, so `phase: REVIEW` and the handoff = branch tip resolve correctly. The only new logic is the review-run completion/summary mapping of the cycle's terminal `StepResult`.

**Test strategy.** End-to-end with a stub adapter driving the cycle: (1) a clean/correct fix → completes with zero human interaction, lands `REVIEW.x` commits or none, leaves the tree clean (footprint assertion: no `intent.md`/`findings.json`/run dir anywhere in the repo — tracked, ignored, or untracked); (2) a seeded unresolved **blocking** finding → parks (does not silently pass), resumable via `gauntlet resume --response`; (3) a seeded unresolved legitimate **major** finding → completes (does not park) and appears in the summary as residual risk. Assert the injected `review_base` reaches the reviewer as the three-dot diff and that `intent.md` + provenance are present in the review prompt and wrapped as data in the triage prompt.

**Exit criteria.** `uv run pytest` green; the three FR-3.4 acceptance scenarios pass; the footprint guarantee holds (§9 hard check); `--rounds`/`--test` behave per FR-1.1/FR-3.3. Single commit `P3: …`.

**Deferrals.** PR mode → P4. Tier-2 write-back (post findings as PR comments) is a Non-Goal (Open Question 11.2). The autonomous implement-from-ticket variant is a Non-Goal (Open Question 11.1).

---

## P4 — GitHub PR mode (`--pr`) behind the FR-7.4 policy preflight gate

**Assumption validated.** A PR can be pulled down, reviewed against its base and linked ticket, and have fixes landed locally for a human to push — including the fork (no-push) case — **without** widening the harness's autonomy or the judge boundary.

**Prerequisite gate (hard, FR-7.3/FR-7.4).** P4 must not begin until the `pr_read_commands@v1` allow rule for `gh pr view` / `gh pr checkout` / `git fetch` is ratified in `policy.yaml` through the policy-change process (Open Question 11.4). The build delivers the deterministic preflight reader and proposes the rule's exact form; it never edits `policy.yaml` itself. If P4 execution is reached with the rule absent/unratified/version-mismatched, the run **halts and escalates** with the exact FR-7.4 message — it never relies on the LLM fallback.

**Deliverables.**
- **Machine-checkable preflight** (FR-7.4): before any `gh`/`git fetch`, load the active `policy.yaml`, look up the `pr_read_commands` rule, and verify present + ratified + version `v1` (a deterministic config read — no network, no agent). On absent/unratified/version-mismatch, fail closed with the exact message. Branch-mode reviews skip the preflight entirely. (This requires the policy rule to carry a stable `id`/`version`/`ratified` marker; the rule proposal introduces those fields for this rule — ratified out of band, not committed by this phase.)
- **PR checkout contract** (FR-4.5), in order: (1) clean-tree preflight **before** `gh pr checkout`; (2) `gh pr checkout <N>` leaving HEAD on a named branch (never detached); (3) refuse a non-fast-forward update to an existing diverged local branch; (4) fork / cross-repo PRs checked out for read+local-fix; (5) any checkout failure or detached result fails closed before any agent.
- **PR metadata resolution** (FR-4.1): `gh pr view` → `headRefName`, `baseRefName`, `isCrossRepository`, `title`, `body`, `url`; review proceeds as a local-branch review of the head branch with `review_base` = the PR's base ref's merge-base (FR-4.2, three-dot per FR-5.2).
- **Linked-ticket auto-derive** (FR-4.3): `extract_refs(pr_body)` (P1) in textual order; **first resolved** ref supplies the intent (`provenance: tracker`); when >1 distinct ref present, emit a warning naming the chosen ref and list **all** detected refs in the summary as ignored secondary refs; explicit `--issue` overrides. PR body is **secondary context only**, never the problem statement. No linked ref + no explicit intent + no `--code-only` → fail closed (FR-4.3), does not fall back to the PR body.
- **No push-back** (FR-4.4): fixes land locally on the checked-out head branch; the run never pushes. Fork PR completes locally with a summary noting push-back is manual (and may need maintainer-edit). The cycle carries a `step_id`, so the existing FR-9.8 judge rule keeps denying in-step `git push` / `gh pr create` (FR-7.1) — no new latitude.
- `gauntlet init` (FR-10.2): scaffold a commented-out `issue_tracker` block (Linear example + env-var name) and ship `pipelines/review.yaml` + the review prompt in the standard asset set.

**Design notes.** The orchestrator-level tracker fetch and `gh`/`git fetch` reads are Gauntlet's own process calls, not hooked-agent calls, so they are not routed through the PreToolUse judge (FR-7.2); the preflight is the deterministic gate, not a try-it-and-see probe.

**Test strategy.** With a `policy.yaml` fixture **missing** `pr_read_commands@v1`: `gauntlet review --pr <N>` exits non-zero with the exact preflight message, issues no `gh`/`git fetch`, spawns no agent; a branch-mode review with the rule absent is unaffected. With the ratified-rule fixture and a stubbed `gh`: same-repo PR whose body says "Fixes ENG-1234" → `intent.md` from ENG-1234, full-diff-against-base review, accepted fixes as local commits, nothing pushed; multi-ref body → warning + chosen ref + ignored-secondary list; no-ref body without explicit intent / `--code-only` → fail closed; fork PR → completes locally with manual-push summary; diverged existing local branch → fail closed; checkout-leaves-detached → fail closed. `init` produces a repo whose `pipelines/review.yaml` loads/validates and whose config carries the commented `issue_tracker` example. Live `gh` paths marked `@pytest.mark.integration`.

**Exit criteria.** `uv run pytest` green (preflight gate respected); all FR-4 / FR-7.4 / FR-10.2 acceptance criteria pass; the `pr_read_commands@v1` rule is ratified before execution. Single commit `P4: …`.

**Deferrals.** Tier-2 write-back / PR review comments (Open Question 11.2). Fork-PR push-back (impossible; Mode A only). Multi-ticket concatenation into one intent (FR-4.3: v1 takes the first ref only).

---

## Ordering rationale

P1 attacks the external-API risk in isolation — testable with no review machinery. P2 establishes the no-footprint, in-place lifecycle and the CLI entrypoint that exposes it, including all manual intent sources, provenance + ratification, and precedence, so its acceptance is command-level without the cycle. P3 needs P1 (tracker intent) + P2 (lifecycle + CLI + manual intent) and adds only cycle execution, proving the product thesis. P4 is a convenience surface over P1–P3, gated by the FR-7.4 policy preflight, and so comes last. No phase depends on work a later phase delivers.

```gauntlet-phases
- id: P1
  title: Issue-tracker abstraction + Linear provider + doctor check
  goal: Ship the IssueTracker protocol/payloads/errors, the deterministic intent renderer, the entry-point registry, the Linear provider, IssueTrackerConfig, and the doctor tracker probe. Validates that an unowned third-party API and its auth can resolve a ref to a normalized problem statement and fail closed on auth/not-found/unavailable/timeout.
- id: P2
  title: Review run lifecycle + gauntlet review CLI (resolve-and-stop, no cycle)
  goal: Ship the gauntlet review command and the zero-footprint in-place review lifecycle — entry contract, in-place branch adoption, out-of-repo state dir, four-source intent resolution with precedence + the FR-2.4 in-repo exclusion, provenance tagging + ratification + manifest record, and base resolution with the empty-diff/merge-base guard — stopping at the pre-cycle boundary. Validates that a review parses its inputs, leaves zero repo footprint, resolves a provenance-tagged intent, and resolves a correct non-empty three-dot base.
- id: P3
  title: Wire the cycle — review.yaml, intent-aware prompt, zero-gate execution
  goal: Add pipelines/review.yaml and prompts/review-code-intent.md and wire the P2 entrypoint through to executing the adversarial_cycle in code_review mode with injected review_base, intent.md, and provenance, zero gates, and the FR-3.4 terminal-severity contract (park on blocking, complete-with-residual-risk on non-blocking). Validates the core thesis: the cycle alone surfaces real correctness/spec-gap findings on a small diff at acceptable cost.
- id: P4
  title: GitHub PR mode behind the FR-7.4 policy preflight gate
  goal: Add --pr mode (gh resolution + checkout contract, base-from-PR, linked-ticket auto-derive with multi-ref handling, fork no-push case) behind the deterministic FR-7.4 preflight that verifies pr_read_commands@v1 is ratified in policy.yaml, plus gauntlet init scaffolding. Validates that a PR can be reviewed against its base and linked ticket with fixes landed locally, without widening the judge boundary.
```
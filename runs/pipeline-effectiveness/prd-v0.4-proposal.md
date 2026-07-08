# PRD v0.4 amendment proposal — pipeline-effectiveness

**Status:** DRAFT — awaiting human ratification (this file proposes; `prd.md`
changes only through its own revision process, CLAUDE.md §2/§8)
**Author:** Claude (PR #59 review-fix pass, 2026-07-08), for John Pletka
**Scope:** Reconcile `prd.md` (v0.3) with the system as actually built and
hardened by the PR #59 review fixes. Two kinds of change: (a) places where a
plan-level ratification (the P5 §7 amendment) or the review fixes made the
implementation deliberately diverge from the PRD's letter — the PRD is the
superior artifact and must either absorb or reverse them; (b) places where the
review fixes strengthened machinery in ways the PRD should now state
normatively. Nothing here weakens a guarantee silently: every weakening is
named as such with its compensating control.

## A. §7 — verifier sandbox contract (items 1, 2, 4, 5, 6)

1. **Items 1/4 (confinement, no credential reads) — mechanism update.**
   Replace "workspace-write-scoped … under the judge's hooks" with the
   implemented mechanism: a **server-authoritative per-step boundary** — the
   engine registers the disposable-copy root against the verifier's own step id
   on the run's judge before launch (one-shot; clearing requires an engine-held
   lease key), the boundary **wins over the pinned repo root**, and a rung-0
   confinement deny covers reads, writes, Bash path tokens, and relative `..`
   escapes, with symlink-resolved paths. The engine proves enforcement on the
   live judge (an outside-copy read must return the deterministic confinement
   deny) before every launch; unproven confinement parks.
2. **Item 2 (network default-deny) — split the statement.** The RUN-wide
   posture is deny-to-non-allowlisted-hosts (github/pypi/npm, needed by the
   builder); the VERIFIER-boundary posture is default-deny with **no
   allowlist**, enforced in the confinement rung. The PRD currently implies
   default-deny everywhere; state both layers.
3. **Item 5 (resource bounds) — now true; state the platform caveat.**
   Wall-clock kill (hard) + rlimit CPU/AS caps applied at spawn and inherited
   by forked children (best-effort; macOS does not honor RLIMIT_AS).
4. **Item 6 (subprocess inheritance) — absorb the ratified plan amendment
   honestly.** The hook gates tool calls, not forked grandchildren; OS-level
   jailing is out of v1 scope. Compensating controls, now all real: stripped
   env (allowlist rebuild), **scratch HOME** (children cannot discover
   `~/.aws`, `~/.ssh`, `~/.config/gh`; the claude CLI finds its login via
   `CLAUDE_CONFIG_DIR`), rlimit inheritance, disposable-copy cwd, and the
   ref-mutation git deny (the copy shares the real repo's refs). **Named
   residual:** a forked child inherits `CLAUDE_CONFIG_DIR` (the CLI's own
   credential) and is not network-gated below the tool-call surface.

## B. FR-3.2 / §4.2 — acceptance-gate enumeration posture

Replace the P5 "enumeration runs inside the sandbox backend" wording with the
implemented posture: a **bounded engine subprocess in a disposable copy** with
a stripped env — deterministic, no LLM in the evidence path (the echo design
could truncate a large id list into chronic false parks or fabricate ids into
a false pass). The enumeration **command is project-resolved**: explicit
`collectors.<kind>.command` config, else a pytest-shaped `test_command`
(adopter repos get their own test env; the engine interpreter is dev-layout
fallback only). **Named residual:** the subprocess is not hook-gated;
import-time egress by branch conftest code is bounded by strip + copy +
rlimits only.

Add to FR-3.2: the gate runs **twice per phase** — before the review cycle
(cheap structural park before reviewer budget is spent) and **after it**
(`acceptance-recheck`), because fix commits can rename cited tests and author
deferrals after the first pass blessed the map. The fixer is obliged
(cycle-fix.md) to update the map in the same fix when renaming a cited test.

## C. §6 — normative-shape corrections

1. **Remainder id.** The shipped, plan-ratified shape is
   `<carried_from>-r<round>-c<N>` (explicit `-c0`; the bare `-r<round>` string
   is deliberately never a final id, so a reviewer-supplied collision is
   impossible). Replace the `F-003-r2` example with `F-003-r2-c0`.
2. **Invalid/unparseable location (dedup).** The shipped, plan-ratified rule:
   an invalid location overlaps **nothing**, even when the file is known —
   strictly the keep-findings direction. Replace the "treated as whole-file
   for that file" sentence.
3. **Carry validation (new, from B2).** State normatively: the engine grants
   the triage bypass only after validating all three legs — the `carried_from`
   parent exists in the round, was accepted `fix_now`, and was confirmed
   `partially_resolved`; anything else demotes the entry to an ordinary
   confirm regression (recorded in `engine_reconciliation.demoted_carries`).
   A restatement of a carried remainder's id by the round-N+1 reviewer is
   dropped by the engine (the pre-accepted obligation stands).
4. **Confirm-record compatibility.** The persisted `confirm.json`
   `new_findings` item requires only the true pre-migration trio
   (`severity`, `claim`, `location`); the strict confirmer-output schema
   (derived in code) requires the full shape natively.
5. **Registry.** Add `registry/supersessions.jsonl` (append-only, on the
   proposals allowlist with an append-only diff guard) as the FR-5.2 targeted
   invalidation mechanism: a ratified supersession retires a fingerprint from
   injection; `declined.jsonl` is pure audit and never proposal-editable.

## D. FR-4 — evidence-tiered gates

1. **Predicate addition (evidence freshness).** Add an eighth conjunct to the
   §4.2 clean conjunction: tests/acceptance evidence recorded before the cycle
   is a predicate miss when the cycle landed `P<N>.x` fix commits, unless a
   same-type step re-proved the signal after the cycle (the shipped
   `acceptance-recheck` does this for the acceptance signal; a pre-cycle tests
   record with fix commits therefore parks).
2. **Zero-findings convergence** persists an explicit empty verdict set so the
   archetypal clean gate is actually evaluable (previously it parked forever
   on a missing artifact).
3. **Notification channel.** "A notification is sent" = a pushed advisory
   (`gate-auto-approved` kind) at approval time, distinct from `gauntlet
   status` visibility and from PR-level ratification.

## E. FR-1.3 / §9 — yield metric semantics

Define `unique_after_dedup` / `unique_legit` as **sole-source** counts: a
primary raised by more than one member counts toward **neither** member
(ownership of the merged phrasing is not unique yield). Restrict
`unique_legit_by_member` to actual panel members (verifier findings and
carried remainders are excluded). §9 panel-shrink: a zero unique-legit total
with members that raised findings reads "below any threshold" (the
full-overlap kill case), not "cannot judge".

## F. §7 — governed learning assets

Replace "no agent-writable path mutates them" (previously convention) with
the enforced statement: an in-pipeline write to `prompts/lenses/*` or
`registry/*.jsonl` is judge-denied (`governed-learning-assets-in-pipeline`,
matching operation-target paths, never file content); they change only via
the ratified proposals flow.

## Ratification

Approving this proposal authorizes folding sections A–F into `prd.md` as
**v0.4** with a changelog line; declining any section reverts the
corresponding implementation choice through its own loop. Until ratified,
`prd.md` v0.3 remains the artifact of record and this file documents the
known divergences.

# Authoring a Gauntlet PRD — instructions for Claude

**What this is.** A reusable playbook for the *interactive* step where a human
and Claude co-author a `prd.md` before `gauntlet run` is invoked. Gauntlet does
not author PRDs from scratch (FR-10.1); this is the "authoring is a human
activity, optionally assisted interactively" step that produces the seed the
pipeline then reviews. Paste or `@`-reference this at the start of a PRD
discussion. It is project-agnostic: it ships in every Gauntlet install, so the
"this project" it refers to is whatever repo you are authoring a PRD *in* —
Gauntlet's own repo, or any project that adopted Gauntlet via `gauntlet init`.

## 0. Your role and the stakes

You are my **co-author and interrogator**, not the decider. I own the PRD; you
extract, sharpen, challenge, and draft it. "Done" means *I* am satisfied —
because the moment I am, this document leaves us and faces two unforgiving
consumers:

1. **An adversarial reviewer** (`prompts/review-document.md`) whose entire job
   is to find ambiguity, untestable requirements, missing failure modes,
   forward dependencies, and assumptions-stated-as-facts. Write every line to
   survive that reviewer. If something is unclear to a hostile reader, it is a
   defect — fix it now, not in review round 2.
2. **A plan-author agent** (`prompts/plan-author.md`) that decomposes this PRD
   into sequential, assumption-validating phases. If the PRD cannot be phased —
   vague, unordered, or untestable requirements — the plan inherits the rot.

The PRD is the **root artifact**: the plan traces to it, every implementation
phase traces to it, and when in doubt the PRD wins. Treat its precision as
load-bearing.

**Three hard rules, non-negotiable:**

- **Never amend an already-approved artifact.** If this feature implies changing
  an approved `prd.md`, `plan.md`, `policy.yaml`, or this project's canonical
  spec (in Gauntlet's own repo that is `PRD-gauntlet.md`; in another project it
  is whatever doc is the source of truth), **STOP and surface it.** That is a
  separate ratification process, not something a new PRD absorbs silently.
  A feature PRD states its *relationship* to existing artifacts — almost always
  "does not amend them; builds on <named machinery>."
- **Never resolve ambiguity by guessing.** If you do not know, you ask me or you
  record it as an Open Question. Inventing a plausible-sounding requirement to
  fill a gap is the single worst thing you can do here — it launders an unmade
  decision into a fact the planner will build on. Data over inference.
- **Do not write the document before the thinking is done.** Interview first
  (§3). A wall of polished prose generated on turn one is a liability, not
  progress.

## 1. The process

Work in this order. Do not skip ahead to drafting.

1. **Lock the spine first.** Before any section exists, force two sentences:
   - *Problem:* what hurts today, concretely, for whom.
   - *Solution:* what we will build, in one sentence.
   If we cannot state both crisply, we are not ready to write — keep
   interviewing.
2. **Grill to resolve ambiguity** (§3). This is the bulk of the work. Drive the
   conversation; do not wait for me to volunteer completeness.
3. **Draft incrementally.** Produce one or two sections, get my reaction,
   continue. Never dump the whole document and ask "good?" — that hides the gaps
   we are trying to surface.
4. **Record every unknown as an Open Question** the instant it appears. Do not
   lose it in chat; it goes in the doc (§2 §11).
5. **Converge and hand off.** When I say I am satisfied, write the final
   `prd.md`, list the still-open questions plainly, and remind me the next step
   is `gauntlet run <slug>` (adversarial review), not implementation.

**What you decide vs. what you ask me:**
- *Decide yourself:* section structure, wording, naming, how to phrase an
  acceptance test, which template sections a small feature can fold together.
- *Ask me:* scope boundaries, every functional requirement, priorities,
  acceptance thresholds (the actual numbers), phasing order, and anything that
  changes *what gets built*.

## 2. The PRD structure — required sections and level of detail

This is the canonical superset, drawn from Gauntlet's own PRDs. **Mandatory**
sections appear in every PRD regardless of size. **Scale-with-size** sections
may be compressed or folded for a small feature — but decide that consciously
and say so; never drop one by omission.

**Header block** *(mandatory)* — `Status: Draft vX.Y` · `Author` · `Date`
(absolute, e.g. 2026-06-23) · `Working name` · **`Relationship to existing
artifacts`**: one line stating whether this amends any approved artifact
(default, and almost always: it does **not**) and what existing machinery it
builds on.

**§1 Overview** *(mandatory)*
- **1.1 Problem statement** — what is painful today, for whom, why it matters.
  Concrete, not aspirational. 1–2 paragraphs.
- **1.2 Solution summary** — what we are building, the shape of the approach.
  1–2 paragraphs.
- **1.3 The assumption this validates** — the riskiest belief the feature rests
  on, stated so a phase can later prove or disprove it. This is what makes the
  work assumption-validating rather than speculative. One paragraph.

**§2 Goals and Non-Goals** *(mandatory — Non-Goals especially)*
- **2.1 Goals** — a table of `G1, G2, …`, each a single *outcome* (not a
  feature), each mapped to the need it serves.
- **2.2 Non-Goals (v1)** — explicit, bulleted, slightly painful to write. This
  is your sharpest weapon against scope creep and the reviewer's first probe.
  Every "we are not doing X (yet)" you record now is a finding you preempt. If I
  have not given you Non-Goals, **interrogate for them** (§3.1).

**§3 Users and Personas** *(scale-with-size)* — who touches this and what they
do with it. For a small internal feature, a 3-bullet list of reader-roles is
enough; for anything user-facing, a persona table.

**§4 System Architecture** *(scale-with-size, usually present)*
- **4.1 Components** — name the *real* modules/files this will add or touch
  (e.g. in Gauntlet's repo `src/gauntlet/engine/summary.py`; in your project,
  your real paths), what each does, and which are new vs. reused. Naming real
  components is what lets the reviewer check feasibility and the planner check
  sequencing.
- **4.2 Key design decisions** — a table of *decision / choice / rationale*.
  This is where genuine design choices live **with their reasoning**, not buried
  as implicit assumptions. Anticipate "why not the obvious alternative?" and
  answer it here.

**§5 Functional Requirements** *(mandatory — the core)*
- Number them `FR-1, FR-2, …` with sub-requirements `FR-N.M`. One requirement
  per item; if an item has an "and," consider splitting it.
- State what must be **true**, not how to code it. "The step halts on backbone
  failure" — not "wrap it in try/except."
- **Every FR ends with `Acceptance:`** — a concrete, testable criterion. If you
  cannot write the acceptance test, the requirement is not yet a requirement; it
  is a wish — grill me until it becomes testable (§3). This is the
  highest-leverage discipline in the whole document.
- Cross-reference by ID (`per FR-3`, `§4.2`) and keep those references
  consistent.

**§6 Data & Schemas (normative excerpts)** *(present whenever there is
structured I/O)* — the literal shape of any structured artifact the feature
produces or consumes (JSON schema sketch, table columns, file layout). The
reviewer treats an under-specified schema as a spec-gap.

**§7 Security & Privacy** *(present whenever the feature touches execution,
secrets, network, or the judge)* — apply fail-closed thinking explicitly: what
is the deny/halt default on timeout, parse error, unexpected exit. Secrets
handling. Anything the judge policy must allow or deny.

**§8 Implementation Plan (phased, assumption-validating)** *(mandatory)* — a
table of phases `P1, P2, …`, each with its **deliverable** and **the assumption
it validates**, **ordered to kill the riskiest assumptions first.** Hard
constraint: **no phase may depend on work a later phase delivers** (forward
dependencies are the reviewer's favorite catch). Each phase ends in passing
tests and a commit. This table is what the plan-author expands — get the
ordering right here and the plan writes itself.

**§9 Success Metrics** *(mandatory)* — measurable outcomes that tell us the
feature worked. Numbers and thresholds, not adjectives. "Classification steps
≤ 5% of run cost," not "cost is low."

**§10 Risks & Mitigations** *(scale-with-size)* — a table. Each risk paired with
a concrete mitigation, ideally one that traces to an FR or a phase.

**§11 Open Questions** *(mandatory if any exist — and some always do)* — every
unresolved decision, stated plainly. Mark resolutions inline with strikethrough
+ **Resolved:** so the audit trail of *how* we decided survives. An honest open
question is strength; a guessed answer is a latent bug.

## 3. How to grill me — the interrogation playbook

This is the part that matters most. Be relentless but efficient. Each category
maps to something the adversarial reviewer *will* attack — grilling me on it now
is how you pre-harden the PRD.

**The categories (what to probe):**

1. **Scope boundaries → forces Non-Goals.** "What is explicitly *not* in this?
   Where is the edge?" Offer the tempting-adjacent features and make me reject
   them out loud: "Should this also handle X, or is X a Non-Goal?"
2. **Testability → forces Acceptance criteria.** For *every* requirement: "How
   would we know this works? What is the observable pass/fail?" If my answer is
   not observable, keep pushing until it is — or downgrade it to an Open
   Question.
3. **Two-way-door ambiguity.** "A builder could read this as A *or* B. Which?"
   Read my requirement back in the most adversarial-but-plausible alternative
   interpretation and make me disambiguate.
4. **Failure modes & fail-closed posture.** "What happens on timeout? Parse
   error? Unexpected exit code? Empty input? `kill -9` mid-step? Two of these
   running at once?" For each external call, what is the deny/halt default. A
   PRD that only specifies the happy path is half-written.
5. **Assumptions stated as facts.** When I assert something as given — "the data
   is already there," "that always succeeds" — challenge it: "Is that verified
   or assumed? What is true if it is false?" Either it gets evidence, or it
   becomes §1.3 / an Open Question.
6. **Edge cases & boundaries.** Empty, max, partial, duplicate, resumed,
   concurrent, first-run, already-exists. Walk the boundaries explicitly.
7. **Dependencies & sequencing.** "What must already exist for this to work?
   Does any part of the plan need a later part?" This is where
   forward-dependency bugs die.
8. **Upstream conflict check.** "Does this require changing an approved artifact
   or this project's canonical spec?" If yes — **stop and surface it** (§0). Do
   not design around it.
9. **Success definition.** "What measurable outcome means we won? What would
   make us say this was a mistake?"
10. **Phasing & minimum shippable.** "Riskiest assumption — which is it, and can
    P1 attack it? If you could ship only half, which half?"
11. **Internal consistency.** As the doc grows, cross-check: does any
    requirement contradict another? Does the schema in §6 match the FRs in §5?
    Does §8's order respect §5's dependencies?

**How to grill (the mechanics — they matter as much as the categories):**

- **Batch by theme, do not interrogate at random.** Group 2–4 related questions;
  let me answer a cluster, then move on. A 20-question wall is as useless as no
  questions.
- **Prefer concrete options over open prompts.** "Should the failure mode be
  (a) halt the run, (b) degrade to a partial result + warning, or (c) retry N
  times then halt?" is far faster to answer than "how should failures work?"
  Give me a strawman to react to — even a wrong one surfaces the real decision.
  (If you have a structured-question tool available, this is the place for it.)
- **Distinguish "must resolve now" from "record and move on."** Not everything
  needs an answer today. A genuine judgment call that is cheap to defer becomes
  an Open Question (§11). A decision the *plan depends on* must be resolved
  before handoff. Tell me which bucket each one is in.
- **Push back on vague answers.** If I answer ambiguously, the answer is itself
  a finding — re-ask, sharper. "You said 'handle it gracefully' — concretely,
  what does the system *do*?"
- **Default to assuming I have under-specified, not over-specified.** I will give
  you the happy path and the interesting parts; you are responsible for dragging
  out the boundaries, failures, and the unglamorous Non-Goals.
- **Know when to stop.** When the remaining ambiguity is a true judgment call
  with no cheap way to decide, and it is recorded as an Open Question, *stop
  grilling that thread.* Diminishing returns are real; the adversarial review is
  a second net. Do not manufacture questions to seem thorough.

## 4. The quality bar — write to survive the reviewer

Before you call a draft ready, check it against what the reviewer hunts for:

- **Every FR has a testable `Acceptance:` line.** No exceptions.
- **No forward dependencies** in the §8 phase table; phases ordered
  riskiest-assumption-first.
- **Assumptions are labeled as assumptions,** not smuggled in as facts.
- **Non-Goals are explicit** and slightly uncomfortable.
- **Failure / fail-closed behavior is specified** for every external call or
  risky operation.
- **Cross-references are consistent** (FR numbers, § numbers, schema fields).
- **Guiding principles invoked by name where they apply** — determinism over
  cleverness, fail closed, separation of concerns, data over inference, process
  fidelity, and "approved artifacts change only through their own loop and
  gate." (These are Gauntlet's review lens; the reviewer also checks against
  this project's own `CLAUDE.md` principles where present.) When a design
  decision is really one of these, say so — it signals the reviewer it was
  deliberate.
- **Honest open questions beat false precision.** A recorded unknown is fine; a
  confident wrong answer is a finding waiting to happen.

## 5. Anti-patterns — do not do these

- **Drafting before interviewing.** Polished prose is not progress if the
  decisions underneath it are unmade.
- **Guessing to fill a gap.** Record the unknown; never invent the answer.
- **Untestable requirements.** "Robust," "efficient," "user-friendly" with no
  metric. Every quality claim needs a number or an observable.
- **Confusing PRD altitude with plan altitude.** The PRD says *what must be true
  and why* (and names components + key decisions). It does **not** contain
  step-by-step build instructions beyond the assumption-validating phase table —
  that is the plan's job. Keep "what/why" here; push "how, in detail" to the
  plan.
- **Silent scope creep.** If it is not in Goals or Non-Goals, ask — do not
  quietly add it.
- **Amending approved artifacts.** Covered in §0. It bears repeating because it
  is the most consequential mistake.
- **Padding.** The reviewer treats vague filler as a finding. If a sentence does
  not constrain the build or justify a decision, cut it.

## 6. Output mechanics and handoff

- **Location:** `runs/<slug>/prd.md`, resolved under your config's `asset_root`
  — so `.gauntlet/runs/<slug>/prd.md` in a project that adopted Gauntlet via
  `gauntlet init`, or `runs/<slug>/prd.md` in Gauntlet's own repo. `<slug>` is
  short kebab-case (e.g. `invoice-export`). `gauntlet new <slug>` scaffolds the
  stub; you fill it.
- **Status:** start `Draft v0.1`; bump as we iterate.
- **Open Questions stay live** until resolved; mark each resolution with
  strikethrough + **Resolved:** plus the reasoning, so the decision history is
  in the document, not just our chat.
- **At handoff,** state plainly: which questions remain open, what the riskiest
  assumption is, and that the next step is `gauntlet run <slug>` — which begins
  with *adversarial review*, not implementation. I ratify; the pipeline
  executes.

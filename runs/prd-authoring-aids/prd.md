# PRD: PRD-authoring aids — teach the repo its own PRD conventions

**Status:** Draft v0.1
**Author:** John (with Claude)
**Date:** 2026-06-23
**Working name:** PRD-authoring aids (the repo teaches you how to author a PRD for it)
**Relationship to existing artifacts:** Extends `gauntlet init` scaffolding (FR-1.2) and the `gauntlet new` PRD stub (FR-10.1). It adds two committable aids alongside existing assets and **does not amend `PRD-gauntlet.md`**, the entry contract's *intent* (a human still authors the PRD), or any pipeline prompt. Builds on: the asset-copy machinery in `engine/init.py`, the `.claude/settings.json` hook-wiring pattern, the `_PRD_STUB` / `check_entry_contract` mechanism in `engine/run.py`, and the `prompts/prd-author.md` playbook.

---

## 1. Overview

### 1.1 Problem statement

Gauntlet's PRD-authoring conventions — the `prd-author.md` playbook, the section structure, *where* a PRD lives, and the fact that you run `gauntlet run <slug>` next — only help if the human already knows them. A fresh Claude session in a Gauntlet repo has no idea `prd-author.md` exists; `gauntlet new` emits a near-empty stub; and both common entry paths depend on tribal knowledge:

1. `gauntlet new <slug>` → author the stub. The author must know the structure and conventions unaided.
2. A fresh session → "enter plan mode, write a PRD, save it to `<run_root>/<slug>/prd.md`." This depends entirely on the human remembering the path and the workflow.

The `prd-author.md` playbook now ships into every install, but it is **inert**: nothing surfaces it to a session, so it is used only when the human manually points at it.

### 1.2 Solution summary

Make the repo *teach itself* by installing two committable aids:

- **A Claude Code skill** (`.claude/skills/gauntlet-prd-author/SKILL.md`), installed by `gauntlet init`, that triggers on PRD-authoring intent and routes the session to the playbook and the conventions (where the PRD goes, scaffold with `gauntlet new`, run `gauntlet run` after). It is a **thin pointer** to `prd-author.md`, not a copy.
- **A structured `gauntlet new` stub**: the playbook's section skeleton plus a one-line guidance hint per section, so authoring starts from the right shape — while **keeping the FR-10.1 marker** so an unfilled skeleton still cannot start a run.

The skill covers the Claude-session entry path; the structured stub covers the `gauntlet new` path and is CLI-agnostic. Both are committable, so a teammate who clones the repo inherits them (the FR-1.2 "identical workflow" goal).

### 1.3 The assumption this validates

That a **per-repo, committable Claude Code skill is reliably discovered and triggered** from a natural-language PRD request on the pinned Claude Code version — and that a richer stub improves authoring **without weakening** the FR-10.1 human-author gate. Skills are a moving Claude Code feature; this is the riskiest external dependency, so it is validated first (§8 P1), mirroring how the core PRD front-loaded hook-semantics risk.

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal | Addresses |
|---|------|-----------|
| G1 | A Claude session in an init'd repo auto-discovers how to author a PRD (skill → playbook + conventions) | "the playbook is inert; conventions are tribal knowledge" |
| G2 | `gauntlet new` produces a structured, guidance-bearing stub instead of a near-empty one | both entry paths start from the right structure |
| G3 | Single source of truth for the instructions — the skill references `prd-author.md`, never copies it | drift avoidance |
| G4 | Both aids are committable and propagate via `gauntlet init` like every other asset | teammates inherit the workflow (FR-1.2) |
| G5 | The FR-10.1 human-author gate is preserved exactly | a richer stub must not become a runnable non-PRD |

### 2.2 Non-Goals (v1)

- **No Codex / `AGENTS.md` pointer.** Skills are Claude-specific; the structured stub is the CLI-agnostic path for non-Claude sessions. A Codex equivalent is a future consideration.
- **The skill never authors the PRD autonomously.** It routes and teaches; the human still writes and ratifies (FR-10.1 unchanged).
- **No changes to pipeline prompts or the review/plan loop.** This is pre-`gauntlet run` authoring support only.
- **Project-level skill only**, not a user-level (`~/.claude/skills`) global install — the aid must travel with the repo.
- **No new CLI command.** This rides on existing `init` and `new`.

## 3. Users and Personas

- **Pipeline author** writing a PRD in Gauntlet's own repo or an adopter repo — the primary beneficiary; gets the skill prompt and the structured stub.
- **Team adopter** who clones a repo that already carries the committed skill + stub — inherits the workflow with no extra setup.
- **Pipeline maintainer** who may customize the skill description or the stub template — must not have customizations clobbered by a re-run.

## 4. System Architecture

### 4.1 Components

- **`src/gauntlet/scaffold/skills/gauntlet-prd-author/SKILL.md` (new)** — the committable skill template; installed to `.claude/skills/gauntlet-prd-author/SKILL.md`.
- **`src/gauntlet/scaffold/prd-stub.md` (new)** — the structured stub template (section headers + guidance comments + the FR-10.1 marker). Replaces the inline `_PRD_STUB` constant as the single stub source.
- **`src/gauntlet/engine/init.py` (modified)** — installs the skill (create-if-absent, idempotent, never-clobber, asset_root-aware reference path); ensures `.gitignore` guidance keeps `.claude/skills/` committable.
- **`src/gauntlet/engine/run.py` (modified)** — sources the stub from `prd-stub.md`; `check_entry_contract` compares against that same source.
- **`prompts/prd-author.md` + scaffold twin (already shipped)** — the single instruction source the skill points to.

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Skill ↔ playbook | Skill **references** `prd-author.md`; no embedded copy | One instruction source; SKILL.md stays a thin pointer; no drift |
| Stub source | A single committable template installed into the adopter repo (see §4.3); `new` and the entry contract read the **installed repo copy**, falling back to package data only when it is absent | Teams can customize one file; one source means the "still a stub" check and the scaffold never disagree |
| Stub ↔ playbook headers | A drift-guard test compares the stub against the playbook's machine-readable section manifest (§6) | The one residual drift risk the "reference" choice leaves |
| Customizable-template safety | `new` and the entry contract validate the installed stub against template invariants (§4.4) and fail closed if violated | A customizable gate-input template must not be able to silently disable the human-author gate |
| Skill scope | Project-level, committable | Teammates inherit it on clone (FR-1.2) |
| FR-10.1 gate | Keep the stub marker; richer stub, same gate | A better starting structure must not weaken the human-author requirement |
| Install posture | Mirror the judge-hook wiring: create-if-absent, idempotent, fail-closed on malformed state | Consistent, safe re-runs; never destroy a customized skill |
| Provenance / stale handling | Generated aids carry provenance metadata (§4.5); re-run may refresh an **unmodified** generated file to the current template, never a customized one | Reconciles "never-clobber" with config-dependent generated paths going stale |
| Reference path | A **repository-relative** path (e.g. `prompts/prd-author.md` for asset_root `"."`, `.gauntlet/prompts/prd-author.md` for an adopter) — never an absolute filesystem path | The committed skill must keep working after the repo is cloned or copied to a different absolute location (FR-1.3, G4) |

### 4.3 Stub template: destination, source, and lookup precedence

- **Package source:** `src/gauntlet/scaffold/prd-stub.md` ships in the package.
- **Adopter destination:** `gauntlet init` installs the template to `<asset_root>/prd-stub.md` (i.e. `prd-stub.md` at the repo root for asset_root `"."`, `.gauntlet/prd-stub.md` for an adopter). This installed copy is the customizable, committable file.
- **Lookup precedence — both `gauntlet new` and `check_entry_contract` resolve the stub identically:**
  1. If `<asset_root>/prd-stub.md` exists in the repo, read **that** file.
  2. Otherwise fall back to the packaged `scaffold/prd-stub.md`.
- **Single source per repo:** within one repo, `new` (which writes the scaffold) and `check_entry_contract` (which decides whether the scaffold is "still a stub") read the *same* resolved file, so they can never disagree about what an unfilled stub is.
- **Missing-file behavior:** neither tool errors merely because the repo copy is absent — the packaged fallback always exists, so the stub source is always resolvable. (A missing packaged resource is a packaging bug, surfaced by `gauntlet doctor`, not a runtime branch.)

### 4.4 Stub-template invariants (fail-closed validation)

Because the same template both seeds new PRDs and defines what the entry-contract gate rejects, a malformed customization could silently weaken the FR-10.1 human-author gate. Before either `gauntlet new` or `check_entry_contract` uses the resolved stub, it validates these invariants:

- The template contains **exactly one** FR-10.1 stub marker (not zero, not duplicated).
- The template contains every mandatory section header from the §6 manifest.
- The template is non-empty after normalization.

If any invariant is violated, the tool **fails closed**: `gauntlet new` raises (refuses to scaffold from a broken template) and `check_entry_contract` raises rather than passing — a malformed gate input is treated as "cannot prove this is human-authored," never as "authored." The error names the offending invariant and file path. This mirrors FR-3.2's fail-closed posture for malformed skill state.

### 4.5 Provenance and stale-generated-file handling

Generated aids (the skill, and the installed stub template) carry provenance metadata so `init` can distinguish an untouched generated file from a user customization:

- The generated `SKILL.md` frontmatter carries `x-gauntlet-generated: true` and `x-gauntlet-template-version: <N>`; the generated stub carries an equivalent provenance comment.
- **On re-run, per file:**
  - **Absent** → create at the current template version.
  - **Present, byte-identical to a checksum Gauntlet has shipped** (i.e. an unmodified prior-generated file) → it is *not* a customization. If the current template or the resolved config-dependent path differs, `init` **refreshes** it to the current template (the only overwrite `init` ever performs) and logs the refresh; otherwise it reports it unchanged.
  - **Present and matching no shipped checksum** (a customization) → **never overwritten.** If it carries provenance and looks stale (e.g. references a playbook path that no longer matches the current asset_root), `init` emits a **warning** naming the file and the drift, but does not modify it.

This keeps the never-clobber guarantee for customizations while preventing an unmodified generated file from silently carrying obsolete config-dependent paths forever.

## 5. Functional Requirements

### FR-1: Skill template + install

- **FR-1.1** A committable skill template ships in the scaffold; `gauntlet init` installs it to `.claude/skills/gauntlet-prd-author/SKILL.md` when absent and skips it when present (idempotent). A **customized** skill (provenance absent or content matching no shipped checksum) is never overwritten; an **unmodified generated** skill may be refreshed per §4.5. **Acceptance:** a fresh `init` creates the skill file with provenance metadata; a re-run with the current template reports it unchanged; a re-run after a template/config change refreshes only a byte-identical generated file and logs the refresh; a pre-existing customized `SKILL.md` is left byte-for-byte intact (and warned about if §4.5 detects staleness).
- **FR-1.2** `SKILL.md` frontmatter carries a `name` and a `description` worded to trigger on PRD-authoring intent (e.g. "write/draft/author a PRD", "start a Gauntlet run", "plan a PRD"). **Acceptance:** the description contains the documented trigger phrases. Natural-language *trigger* reliability is validated separately by the recorded trigger test in FR-1.6 (not by metadata inspection, which proves only discovery).
- **FR-1.3** The skill body is a thin pointer: it routes the session to read the playbook at a **repository-relative** path resolved under the repo's `asset_root` (e.g. `prompts/prd-author.md` or `.gauntlet/prompts/prd-author.md`) — never an absolute filesystem path — and states the conventions: the PRD lives at `<run_root>/<slug>/prd.md`, scaffold it with `gauntlet new <slug>`, run `gauntlet run <slug>` afterward. **Acceptance:** (a) the installed `SKILL.md` references the correct repository-relative playbook path for the repo's asset_root and contains no absolute path; (b) after copying or cloning the repo to a *different absolute location*, the committed skill's playbook reference still resolves to the playbook (no embedded source-machine path); (c) verbatim-duplication is bounded per FR-1.3a.
- **FR-1.3a** (measurable no-duplication rule, replaces "contains none of the prose verbatim") The skill must not copy the playbook's *prose*. Concretely: after normalizing whitespace to single spaces and lowercasing, no normalized substring of **12 or more consecutive words** may appear in both `SKILL.md` and `prd-author.md`. Section headings, command names (`gauntlet new`, `gauntlet run`), file paths, and the literal conventions sentence are exempt (they are shared vocabulary, not copied prose). **Acceptance:** a test computes the longest shared normalized word-run excluding the exempted tokens and asserts it is `< 12`.
- **FR-1.4** The installed skill is committable. `init`'s own `.gitignore` guidance does not exclude `.claude/skills/`, **and** `init` checks whether any effective ignore source — repo `.gitignore`, parent-directory `.gitignore`, `.git/info/exclude`, or the global core.excludesFile — would exclude the installed skill (via `git check-ignore`). If one would, `init` does **not** silently override the maintainer's rule: it emits a **warning** naming the ignoring rule's source and the remediation (commit with `git add -f` or amend the rule), and proceeds without editing the foreign rule. **Acceptance:** after `init` with no pre-existing exclusion, `git check-ignore` does not match the skill; with a pre-existing parent-directory or `info/exclude` rule that matches `.claude/skills/`, `init` warns (naming the source) and does not modify that rule.
- **FR-1.5** (format pin + doctor — resolves OQ-3 as **in scope, warn-only**) The skill's required frontmatter shape is pinned by the normative schema fixed in P1 (FR-1.6 / §6). `gauntlet doctor` validates that the installed skill exists and parses against that pinned shape and emits a **warning** (never a hard error — the skill gates nothing) when it is missing, unparseable, or its provenance looks stale (§4.5). **Acceptance:** `doctor` warns on an absent or malformed installed skill and is silent on a well-formed one.
- **FR-1.6** (recorded natural-language trigger test — the riskiest external dependency) P1 produces a reproducible trigger check on the **named pinned Claude Code version** (recorded explicitly, e.g. in `BOOTSTRAP-NOTES.md` or an `@pytest.mark.integration` test). It specifies: the exact prompt(s) (at minimum "help me write a PRD"), the expected observation (the `gauntlet-prd-author` skill is selected/invoked, not merely enumerated), and the failure criterion (skill not selected). **Acceptance:** the recorded artifact names the Claude Code version, the prompts, the expected selection observation, and the pass/fail criterion, and was run with its result recorded. Enumeration or metadata inspection alone does **not** satisfy this requirement.

### FR-2: Structured PRD stub

- **FR-2.1** `gauntlet new <slug>` writes a stub containing the playbook's section skeleton (the mandatory and scale-with-size headers enumerated in the §6 manifest), each with a one-line guidance comment, plus the FR-10.1 stub marker. The stub source and lookup precedence are per §4.3; the resolved template is validated per §4.4 before scaffolding. **Acceptance:** a freshly scaffolded `prd.md` contains every mandatory section header from the §6 manifest and exactly one marker; scaffolding from a template that violates a §4.4 invariant raises rather than writing.
- **FR-2.2** The stub is sourced from one resolved template (§4.3); `_PRD_STUB` and `check_entry_contract` derive from that same resolved source (no second copy). A drift-guard test compares the stub against the playbook's **machine-readable section manifest** (§6) — extracted from `prd-author.md` by the documented parser, not a hand-restated list. **Acceptance:** the scaffolded file and the entry-contract comparison string are the same bytes; the drift test fails if **adding, renaming, or removing** a mandatory section in `prd-author.md` is not mirrored in the stub (i.e. the test is driven off the parsed playbook, so a change to the playbook alone trips it).
- **FR-2.3** The FR-10.1 entry contract is preserved, using a deterministic **authored-content predicate** (FR-2.4). **Acceptance:** `check_entry_contract` raises on the fresh structured stub (marker present), raises on a marker-stripped-but-otherwise-unchanged stub, and passes once substantive content is authored and the marker removed.
- **FR-2.4** (deterministic authored-content predicate — closes the "what counts as authored" gap) A PRD passes the entry contract iff **both**: (1) it contains **no** FR-10.1 marker, **and** (2) it has **authored content** relative to the resolved stub template. "Authored content" is defined by normalizing both the candidate and the template — strip the marker, drop HTML/Markdown guidance comments, drop section-heading lines, collapse whitespace — and requiring the normalized candidate to be **non-empty and not equal to** the normalized template. **Acceptance cases:**
  - whitespace-only change to the stub → **reject** (normalized form unchanged);
  - comment-only edit (editing/adding guidance comments) → **reject** (comments dropped);
  - heading-only edit (renaming/adding a heading, no body) → **reject** (headings dropped);
  - duplicating the marker, or stub with marker present → **reject** (marker present);
  - adding substantive body prose under any section, marker removed → **accept**.

### FR-3: Propagation & idempotency

- **FR-3.1** Both aids — the skill (`.claude/skills/gauntlet-prd-author/SKILL.md`) and the stub template (`<asset_root>/prd-stub.md`, §4.3) — propagate via `gauntlet init`: a fresh repo gets both created; a re-run on an existing adopter creates whichever is missing, refreshes an unmodified generated file per §4.5, and otherwise skips a present/customized one; `--from-repo` reports present/missing/customized for both without writing. **Acceptance:** the three init modes behave as stated for *both* aids in a temp-repo test, including the refresh-vs-preserve distinction of §4.5.
- **FR-3.2** Installs fail closed on malformed pre-existing state at either generated path (e.g. the path exists but is not a regular file), mirroring the `settings.json` guard. **Acceptance:** `init` raises `InitError` rather than clobbering unexpected state at `.claude/skills/gauntlet-prd-author/` or `<asset_root>/prd-stub.md`.
- **FR-3.3** The resolved stub template is validated against the §4.4 invariants at the point of use: `gauntlet new` and `check_entry_contract` fail closed (raise) when the installed template has zero or duplicate markers, is missing a mandatory §6 header, or is empty after normalization — so a broken customization cannot disable the human-author gate. **Acceptance:** with an installed `prd-stub.md` whose marker has been deleted (or duplicated, or a mandatory header removed), both `gauntlet new` and `check_entry_contract` raise a validation error naming the violated invariant.

## 6. Data & Schemas (normative excerpts)

- **`SKILL.md` frontmatter:** `name` (kebab id) + `description` (trigger sentence) + provenance fields `x-gauntlet-generated` / `x-gauntlet-template-version` (§4.5). The exact required field set/limits are fixed as the **normative schema produced in P1** (FR-1.6) against the named pinned Claude Code version; until P1 records it, that schema is the single source the format pin and `doctor` (FR-1.5) check against.
- **Canonical mandatory-section manifest (machine-readable, the drift-guard source of truth).** The manifest is **extracted from `prompts/prd-author.md` by a documented parser**, not hand-restated, so a change to the playbook changes the manifest. Extraction rule: the parser collects every ATX Markdown heading (`#`…`######`) in `prd-author.md` and classifies each by a marker the playbook carries inline — a heading tagged **mandatory** is required in the stub; a heading tagged **scale-with-size** is present in the stub but not required to be filled. The manifest is the ordered list of `(level, normalized-heading-text, class)` tuples the parser returns.
  - **"Header block" disambiguation:** the PRD metadata block (Status / Author / Date lines) is **not** a Markdown heading and is classified as the synthetic mandatory entry `header-block`, matched by the presence of the `Status:` / `Author:` labels rather than by a heading. This removes the ambiguity of calling a non-heading a "header."
  - As of this playbook revision the mandatory set is: `header-block`, §1 Overview (1.1/1.2/1.3), §2 Goals & Non-Goals, §5 Functional Requirements (with an `Acceptance:` label), §8 Implementation Plan, §9 Success Metrics, §11 Open Questions; scale-with-size: §3, §4, §6, §7, §10. **This prose list is informative only** — the test asserts against the parser's output, so adding, renaming, or removing a mandatory heading in `prd-author.md` trips the drift guard even if this paragraph is not updated.

## 7. Security & Privacy

The skill is inert instruction data — no execution, no new tool grants, no widening of the judge surface; installing under `.claude/` is the same trust boundary as the existing hook wiring. No secrets are read or written. The skill body must not instruct the agent to bypass any gate or the judge (it routes to authoring guidance only).

## 8. Implementation Plan (phased, assumption-validating)

| Phase | Deliverable | Assumption validated |
|-------|-------------|----------------------|
| **P1** | Skill template (repo-relative reference path) + `init` install (idempotent, asset_root-aware, provenance/refresh per §4.5, fail-closed) + format pin + normative frontmatter schema (resolves OQ-2) + **recorded NL trigger test (FR-1.6)** | A per-repo committable Claude Code skill is discovered **and triggers** from NL PRD intent on the named pinned version (the riskiest external dependency) |
| **P2** | Structured stub as a single committable template (§4.3 destination/precedence); refactor `_PRD_STUB`/entry contract to that resolved source; §4.4 template-invariant validation; FR-2.4 authored-content predicate; machine-readable manifest + drift-guard test (§6) | A richer, customizable stub preserves the FR-10.1 human-author gate (fail-closed on malformed templates) and never diverges from the playbook's parsed mandatory sections |
| **P3** | Fresh / re-run / `--from-repo` propagation of *both* aids + clone-to-different-path portability test (FR-1.3 acceptance (b)) + `.gitignore` committability incl. foreign-ignore-rule warning (FR-1.4) + `doctor` warn-only check (FR-1.5) + second-repo install check + README/CHANGELOG | An adopter re-running `init` picks up both aids without clobbering customizations, and the committed aids survive cloning |

**Pre-approval decisions (resolves F-006).** OQ-1 and OQ-3 are decided in §11 below and are not deferred. OQ-2 is genuinely empirical (depends on the pinned Claude Code): **P1 must halt for human ratification** of the recorded frontmatter schema before P2/P3 build on it — the phase produces the schema, a human ratifies it, and only then does dependent work proceed. No phase carries an unresolved open question that shapes its own deliverables.

Each phase ends with passing tests and a commit. P2 and P1 are independent; P1 is sequenced first because it carries the riskiest assumption (and its trigger test, FR-1.6). P3 polishes both and therefore follows them.

## 9. Success Metrics

- A fresh `gauntlet init` produces both aids; a re-run is a no-op for an unchanged config, refreshes only an unmodified generated file when the template/config changed (§4.5), and never overwrites a customized copy of either.
- The entry contract still rejects an unfilled structured stub and every FR-2.4 trivial-edit case (whitespace-/comment-/heading-only).
- The recorded FR-1.6 trigger test passes on the named pinned Claude Code version: the prompt "help me write a PRD" *selects* the skill, which routes to `prd-author.md`. (Metadata enumeration alone does not count.)
- No prose duplication, measured per FR-1.3a: the longest normalized shared word-run between `SKILL.md` and `prd-author.md` (excluding headings, command names, and paths) is under 12 words; the drift test keeps the stub's headers aligned to the parsed playbook manifest.

## 10. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Skill format/trigger semantics drift across Claude Code releases | Pin the format + `doctor` warn (FR-1.5); P1's recorded trigger test (FR-1.6) validates on the named installed version; the structured stub is the format-independent fallback |
| Stub headers drift from the playbook's sections | Drift-guard test (FR-2.2) |
| Skill triggers too eagerly or not at all | Tune the `description`; it is advisory only (gates nothing), so a missed trigger degrades to today's manual behavior |
| A team customized the skill or stub | Never-clobber idempotency (FR-1.1, FR-3.1) |

## 11. Open Questions

Items below marked **Resolved** are decisions, not open work; they shaped the requirements above (F-006). Only OQ-4 remains genuinely optional.

- **OQ-1 — Resolved:** Gauntlet's *own* repo (asset_root `"."`) carries a committed skill, generated by the same template and committed directly into this repo; `init` installs the skill into adopter repos. (P1 produces the template and commits Gauntlet's own copy.)
- **OQ-2 — Resolved-by-gate:** the exact `SKILL.md` frontmatter fields/limits are empirical on the pinned Claude Code version. P1 produces the normative schema (§6) and **halts for human ratification** before P2/P3 depend on it (§8). It is not left open through approval — it is an explicit phase gate.
- **OQ-3 — Resolved:** `gauntlet doctor` **is** in v1 scope, as a **warn-only** presence/format check (FR-1.5), consistent with the §10 mitigation. It never hard-fails, since the skill gates nothing.
- **OQ-4 (still open, non-blocking):** Should `gauntlet new`'s CLI output also print a pointer to the skill/playbook (cheap, CLI-agnostic reinforcement of the convention)? This shapes no required deliverable or acceptance test and may be decided during P3 or deferred post-v1.

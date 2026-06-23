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
| Stub source | A single committable template, shared by `new` and the entry contract | Teams can customize; one source means the "still a stub" check and the scaffold never disagree |
| Stub ↔ playbook headers | A drift-guard test asserts the stub covers the playbook's mandatory sections | The one residual drift risk the "reference" choice leaves |
| Skill scope | Project-level, committable | Teammates inherit it on clone (FR-1.2) |
| FR-10.1 gate | Keep the stub marker; richer stub, same gate | A better starting structure must not weaken the human-author requirement |
| Install posture | Mirror the judge-hook wiring: create-if-absent, idempotent, fail-closed on malformed state | Consistent, safe re-runs; never destroy a customized skill |
| Reference path | Resolved under the repo's `asset_root` at install time | `prompts/prd-author.md` (asset_root ".") vs `.gauntlet/prompts/prd-author.md` (adopter) must both be correct |

## 5. Functional Requirements

### FR-1: Skill template + install

- **FR-1.1** A committable skill template ships in the scaffold; `gauntlet init` installs it to `.claude/skills/gauntlet-prd-author/SKILL.md` when absent and skips it when present (idempotent; a customized skill is never overwritten). **Acceptance:** a fresh `init` creates the skill file; a re-run reports it skipped/unchanged; a pre-existing customized `SKILL.md` is left byte-for-byte intact.
- **FR-1.2** `SKILL.md` frontmatter carries a `name` and a `description` worded to trigger on PRD-authoring intent (e.g. "write/draft/author a PRD", "start a Gauntlet run", "plan a PRD"). **Acceptance:** the description contains the documented trigger phrases; the pinned Claude Code enumerates the skill as available (integration-marked or a documented manual check, since NL-trigger reliability is not unit-testable).
- **FR-1.3** The skill body is a thin pointer: it routes the session to read the playbook at the path resolved under the repo's `asset_root`, and states the conventions — the PRD lives at `<run_root>/<slug>/prd.md`, scaffold it with `gauntlet new <slug>`, run `gauntlet run <slug>` afterward. **Acceptance:** the installed `SKILL.md` references the correct asset_root-resolved playbook path for the repo it was installed in, and contains none of the playbook's prose verbatim.
- **FR-1.4** The installed skill is committable — `init`'s `.gitignore` guidance does not exclude `.claude/skills/`. **Acceptance:** after `init`, `git check-ignore` does not match `.claude/skills/gauntlet-prd-author/SKILL.md`.

### FR-2: Structured PRD stub

- **FR-2.1** `gauntlet new <slug>` writes a stub containing the playbook's section skeleton (Header block, §1–§11, mandatory and scale-with-size headers), each with a one-line guidance comment, plus the FR-10.1 stub marker. **Acceptance:** a freshly scaffolded `prd.md` contains every mandatory section header and the marker.
- **FR-2.2** The stub is sourced from one committable template (`scaffold/prd-stub.md`); `_PRD_STUB` and `check_entry_contract` derive from that same source (no second copy). A drift-guard test asserts the stub's section headers cover the playbook's mandatory sections. **Acceptance:** the scaffolded file and the entry-contract comparison string are the same bytes; the drift test fails if a mandatory playbook section is missing from the stub.
- **FR-2.3** The FR-10.1 entry contract is preserved. **Acceptance:** `check_entry_contract` raises on the fresh structured stub (marker present), raises on a marker-stripped-but-otherwise-unchanged stub, and passes once a section is authored and the marker removed.

### FR-3: Propagation & idempotency

- **FR-3.1** Both aids propagate via `gauntlet init`: a fresh repo gets them created; a re-run on an existing adopter creates whichever is missing and skips whichever is present; `--from-repo` reports present/missing without writing. **Acceptance:** the three init modes behave as stated in a temp-repo test.
- **FR-3.2** Installs fail closed on malformed pre-existing state at the skill path (e.g. the path exists but is not a regular file), mirroring the `settings.json` guard. **Acceptance:** `init` raises `InitError` rather than clobbering unexpected state at `.claude/skills/gauntlet-prd-author/`.

## 6. Data & Schemas (normative excerpts)

- **`SKILL.md` frontmatter:** `name` (kebab id) + `description` (trigger sentence). Exact field set/limits confirmed against the pinned Claude Code in P1 (OQ-2).
- **Stub template — mandatory section headers** the drift test enforces: Header block, §1 Overview (1.1/1.2/1.3), §2 Goals & Non-Goals, §5 Functional Requirements (with `Acceptance:`), §8 Implementation Plan, §9 Success Metrics, §11 Open Questions. Scale-with-size headers (§3, §4, §6, §7, §10) are present but the test does not require them filled.

## 7. Security & Privacy

The skill is inert instruction data — no execution, no new tool grants, no widening of the judge surface; installing under `.claude/` is the same trust boundary as the existing hook wiring. No secrets are read or written. The skill body must not instruct the agent to bypass any gate or the judge (it routes to authoring guidance only).

## 8. Implementation Plan (phased, assumption-validating)

| Phase | Deliverable | Assumption validated |
|-------|-------------|----------------------|
| **P1** | Skill template + `init` install (idempotent, asset_root-aware, fail-closed) + format pin | A per-repo committable Claude Code skill is discovered and triggers from NL PRD intent on the pinned version (the riskiest external dependency) |
| **P2** | Structured stub as a single committable template; refactor `_PRD_STUB`/entry contract to that source; drift-guard test | A richer stub preserves the FR-10.1 human-author gate and never diverges from the playbook's mandatory sections |
| **P3** | Fresh / re-run / `--from-repo` propagation + `.gitignore` committability + second-repo install check + README/CHANGELOG | An adopter re-running `init` picks up both aids without clobbering customizations |

Each phase ends with passing tests and a commit. P2 and P1 are independent; P1 is sequenced first because it carries the riskiest assumption. P3 polishes both and therefore follows them.

## 9. Success Metrics

- A fresh `gauntlet init` produces both aids; a re-run is a no-op; a customized copy of either is never overwritten.
- The entry contract still rejects an unfilled structured stub.
- In a fresh Claude session in an init'd repo, a "help me write a PRD" request surfaces the skill and routes to `prd-author.md` (manual/integration check).
- Zero duplication of the playbook text (the skill references it; the drift test keeps the stub's headers aligned).

## 10. Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| Skill format/trigger semantics drift across Claude Code releases | Pin the format + `doctor` warn (mirror FR-1.5); P1 validates on the installed version; the structured stub is the format-independent fallback |
| Stub headers drift from the playbook's sections | Drift-guard test (FR-2.2) |
| Skill triggers too eagerly or not at all | Tune the `description`; it is advisory only (gates nothing), so a missed trigger degrades to today's manual behavior |
| A team customized the skill or stub | Never-clobber idempotency (FR-1.1, FR-3.1) |

## 11. Open Questions

- **OQ-1:** Does Gauntlet's *own* repo (asset_root `"."`) carry a committed skill, or is skill-install adopter-only via `init`? (Lean: Gauntlet commits its own skill directly; `init` handles adopters.)
- **OQ-2:** Exact `SKILL.md` frontmatter fields/limits on the pinned Claude Code version — resolved in P1.
- **OQ-3:** Should `gauntlet doctor` validate skill presence/format (as it does hooks), or is that v1 scope creep?
- **OQ-4:** Should `gauntlet new`'s CLI output also print a pointer to the skill/playbook (cheap, CLI-agnostic reinforcement of the convention)?

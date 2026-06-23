# Implementation Plan: PRD-authoring aids

**For PRD:** PRD-authoring aids — teach the repo its own PRD conventions (Draft v0.1, 2026-06-23)
**Author:** builder agent
**Status:** Draft for adversarial review → human ratification
**Base branch this plan executes on:** `gauntlet/prd-authoring-aids`

---

## 0. Plan summary and sequencing rationale

The PRD's own §8 already orders the work by descending external risk; this plan keeps that ordering and makes each phase concrete against the existing code (`engine/init.py`, `engine/run.py`, `engine/doctor.py`, the `scaffold/` tree, and `prompts/prd-author.md`).

Three phases, strictly sequential (FR-10.3), each ending in passing tests and one **primary implementation commit** (`PN:`, FR-9.2) — plus any `PN.x` review-fix commits the adversarial cycle requires (FR-9.4); the worktree is clean and committed at every reviewer handoff (review F-008):

- **P1 — Skill template + install + the recorded NL trigger test.** Front-loaded because the riskiest assumption in the whole PRD (§1.3) is *"a per-repo committable Claude Code skill is reliably discovered **and triggered** from natural-language PRD intent on the pinned Claude Code version."* That is an empirical, externally-dependent fact; if it is false, the skill half of the feature is worthless and we want to know on day one. P1 also produces the normative `SKILL.md` frontmatter schema and **halts for human ratification of that schema (OQ-2)** before any later phase depends on it.
- **P2 — Structured stub as a single committable template + fail-closed gate.** The second-riskiest assumption: *a richer, customizable stub can replace the near-empty `_PRD_STUB` without weakening the FR-10.1 human-author gate.* This is the safety-critical phase — a customizable template is now a gate input, so it must fail closed. Independent of P1 in principle (the stub path is CLI-agnostic), but sequenced second because its assumption is lower-risk than P1's.
- **P3 — Propagation, portability, committability, doctor, docs.** Polishes both aids: the three `init` modes (fresh / re-run / `--from-repo`), provenance refresh-vs-preserve, clone-to-different-path portability, foreign-ignore-rule warnings, the `doctor` warn-only check, and README/CHANGELOG. It can only run once both aids exist, so it is last.

**Why these are the right cut points.** Each phase ends at a state where the worktree is clean, tests pass, and a reviewer has a meaningful diff (CLAUDE.md central invariant). P1 delivers a working, committed, tested install of one aid plus the ratified schema; P2 delivers the other aid plus the hardened gate; P3 delivers cross-cutting propagation behavior that needs both aids present to test. No phase reaches forward into work a prior phase has not delivered.

---

## P1 — Skill template, idempotent install, normative frontmatter schema, recorded trigger test

### Assumption this phase validates

A **per-repo, committable Claude Code skill** placed at `.claude/skills/gauntlet-prd-author/SKILL.md` is **discovered and actually triggered** by a natural-language PRD request ("help me write a PRD") on the **named, pinned Claude Code version** — and the install machinery can place, refresh, and protect that file with the same create-if-absent / idempotent / fail-closed posture as the existing judge-hook wiring. This is the PRD's stated riskiest external dependency (§1.3, §10); it is killed first.

### Deliverables

1. **Skill template** — `src/gauntlet/scaffold/skills/gauntlet-prd-author/SKILL.md` (new), a *thin pointer* (FR-1.3):
   - Frontmatter: `name` (kebab id `gauntlet-prd-author`), `description` worded with the documented trigger phrases ("write/draft/author a PRD", "start a Gauntlet run", "plan a PRD") (FR-1.2), and provenance fields `x-gauntlet-generated: true` + `x-gauntlet-template-version: 1` (§4.5).
   - Body routes the session to read the playbook at a **repository-relative** path resolved under the repo's `asset_root` (`prompts/prd-author.md` for asset_root `"."`, `.gauntlet/prompts/prd-author.md` for an adopter) — never an absolute path (FR-1.3) — and states the conventions: PRD lives at `<run_root>/<slug>/prd.md`, scaffold with `gauntlet new <slug>`, run `gauntlet run <slug>` after.
   - Body contains no embedded copy of the playbook prose, bounded by FR-1.3a (no shared normalized run of ≥12 words excluding exempt tokens).
2. **Normative `SKILL.md` frontmatter schema** — a committed schema artifact (`schemas/skill-frontmatter.json` or equivalent documented location) fixing the required field set/limits validated against the pinned Claude Code version (§6, OQ-2). This is the single source the format pin (FR-1.5) and `doctor` check against.
3. **`init` install of the skill** — extend `src/gauntlet/engine/init.py`:
   - A `_scaffold_skill()`-style installer (paralleling `_scaffold_config()` / `_wire_claude_hook()`) that resolves the repo's `asset_root`, renders the template with the correct repository-relative playbook reference path, and writes to `.claude/skills/gauntlet-prd-author/SKILL.md`.
   - Posture: create-if-absent, idempotent, **never-clobber a customization**, fail-closed via `InitError` on malformed pre-existing state (path exists but is not a regular file) — mirroring `_wire_claude_hook`'s malformed-state guard (FR-1.1, FR-3.2).
   - Provenance/refresh logic (§4.5), made deterministically implementable (review F-004): recognition of a prior-generated file is **not** a flat finite checksum list — because the rendered playbook path is configuration-dependent (it varies by `asset_root`), a single shipped checksum cannot identify every legitimate prior variant. Instead:
     - The package ships an **append-only registry of every historical generated-template version**, keyed by the integer `x-gauntlet-template-version` (stored under `src/gauntlet/scaffold/skills/_versions/<N>/` as version N's template plus its render rule; nothing is ever removed — retirement policy is *never retire*, so any file Gauntlet has ever generated stays recognizable).
     - To classify an existing `SKILL.md`: read its provenance frontmatter. If `x-gauntlet-generated: true` and `x-gauntlet-template-version: N` is present **and N is in the registry**, **re-render** version N under *this repo's* resolved `asset_root` and compare byte-for-byte. Match → unmodified generated file. Mismatch, missing provenance, or unknown version → treat as a **customization** (fail safe toward never-clobber).
     - Absent → create at the current version. An unmodified generated file whose re-render under the current template/version or resolved path now differs → **refresh** to the current template (the only overwrite `init` performs) and log it; otherwise report unchanged. A customization → never overwritten (warn only when §4.5 staleness is detected).
   - This single version-keyed **re-render-and-compare** scheme is the one provenance algorithm; the stub's provenance (P2) and the both-aids propagation (P3) reuse it rather than defining their own.
   - Ship Gauntlet's **own** committed `.claude/skills/gauntlet-prd-author/SKILL.md`, generated by this template with asset_root `"."` (OQ-1).
4. **Recorded natural-language trigger test (FR-1.6)** — a reproducible artifact (an `@pytest.mark.integration` test and/or a `BOOTSTRAP-NOTES.md` entry) naming: the pinned Claude Code version, the exact prompt(s) (at minimum "help me write a PRD"), the expected observation (the `gauntlet-prd-author` skill is *selected/invoked*, not merely enumerated), and the failure criterion. Run it and record the result. **A passing trigger observation is required for P1 to complete (review F-002):** if the skill is not selected, P1 does **not** commit-and-proceed — it halts with an `UPSTREAM CONFLICT` (the PRD §1.3 riskiest assumption is falsified) and **P2/P3 are prohibited** until a human resolves it (FR-10.4/10.5). Recording a *failure* result is not a passing exit; the kill-the-assumption-first ordering means a failed trigger blocks the dependent phases rather than being logged and bypassed.

### Test strategy

- **Unit (`tests/unit/test_init.py`, offline, `fixture_repo`):**
  - Fresh `init` creates the skill file with provenance frontmatter and the correct repo-relative playbook path for the repo's asset_root.
  - Re-run with unchanged template/config reports the skill unchanged (no write).
  - Re-run after a template-version/asset_root change refreshes a byte-identical generated file and logs the refresh.
  - A pre-existing **customized** `SKILL.md` (provenance stripped or content altered) is left byte-for-byte intact.
  - Malformed pre-existing state at `.claude/skills/gauntlet-prd-author/` (e.g. a non-regular file) raises `InitError`.
  - **FR-1.3a duplication test:** compute the longest shared normalized word-run between the installed `SKILL.md` and `prd-author.md`, excluding exempt tokens (headings, command names, paths, the literal conventions sentence); assert `< 12`.
  - Frontmatter parses against the P1 normative schema and contains the documented trigger phrases (FR-1.2 — this proves *discovery*, not *trigger*).
- **Integration / recorded (FR-1.6):** the trigger test above, run on the pinned version with its result recorded. Enumeration or metadata inspection alone does **not** satisfy it.

### Exit criteria

- `uv run pytest` (unit) green; **`uv run pytest -m integration` green on this machine** before the reviewer handoff — inability to run it (missing CLI credentials/environment) is a **stop-and-ask**, never a silent skip (review F-007).
- The FR-1.6 trigger test was run and **passed** (the `gauntlet-prd-author` skill was selected) on the named pinned version, with the result recorded. A *failed* trigger halts P1 with `UPSTREAM CONFLICT` and blocks P2/P3 pending human resolution (review F-002); P1 is not complete and does not gate forward.
- Gauntlet's own committed `SKILL.md` present and valid.
- **STOP-AND-ASK gate (OQ-2 / §8):** P1 **halts for human ratification of the normative frontmatter schema** before P2/P3 proceed. The phase produces the schema; a human ratifies it; only then does dependent work start. This is an explicit phase gate, not a deferral.
- Single commit `P1: …` with body naming the PRD assumption validated and FR refs (FR-1.1–FR-1.6, FR-3.2).

### Deferrals (named, not smuggled)

- The structured stub, the §6 section manifest/parser, and the FR-2.4 authored-content predicate → **P2**.
- `--from-repo` reporting, foreign-ignore-rule warnings, `doctor` check, README/CHANGELOG → **P3**. (P1 wires the install path; P1 does **not** add the `doctor` warn-only check — that is P3.)

---

## P2 — Structured stub as a single committable template, fail-closed gate, manifest drift-guard

### Assumption this phase validates

A **richer, customizable PRD stub** — installed as one committable template and read by *both* `gauntlet new` and `check_entry_contract` — can replace the inline `_PRD_STUB` **without weakening the FR-10.1 human-author gate**, and never silently diverges from the playbook's parsed mandatory sections. The safety claim is that a *malformed customization of a gate-input template* cannot disable the gate, because both consumers validate template invariants and fail closed.

### Deliverables

1. **Structured stub template** — `src/gauntlet/scaffold/prd-stub.md` (new): the §6 mandatory **and** scale-with-size section skeleton (both classes present, not mandatory-only; review F-003), each header with a one-line guidance comment, plus exactly one FR-10.1 marker (the existing `PRD_STUB_MARKER`, `<!-- GAUNTLET-PRD-STUB: replace this file with a real PRD -->`) and a provenance comment (§4.5). This **replaces** the inline `_PRD_STUB` constant as the single stub source.
2. **Install destination + lookup precedence (§4.3)** — `gauntlet init` installs the template to `<asset_root>/prd-stub.md`; both `gauntlet new` and `check_entry_contract` resolve the stub identically: repo copy `<asset_root>/prd-stub.md` if present, else the packaged `scaffold/prd-stub.md`. Within one repo both consumers read the *same* resolved file (they can never disagree about what an unfilled stub is). Missing repo copy is not an error (packaged fallback always resolves). **Fail-closed install guard (FR-3.2; review F-005):** before writing `<asset_root>/prd-stub.md`, the installer applies the same malformed-pre-existing-state check as the skill installer — if the destination exists but is **not a regular file** (a directory, a symlink where disallowed, or any other non-regular node), `init` raises `InitError` and mutates nothing. This assigns FR-3.2's stub-path acceptance case to P2 (it was previously stated only for the skill path in P1).
3. **Refactor `engine/run.py`** — `RunManager.new()` writes the resolved template instead of the `_PRD_STUB` literal; `check_entry_contract()` compares against the same resolved source. Remove the now-redundant inline stub literal as a second copy.
4. **§4.4 template-invariant validation (fail-closed)** — before either consumer uses the resolved template, validate: exactly one FR-10.1 marker; every mandatory §6 **manifest entry** present; non-empty after normalization. The mandatory entries are of two kinds and are validated by kind (review F-006): a normal section entry is satisfied by its parsed header line; the synthetic **`header-block`** entry is **not** a heading and is satisfied only by the presence of its required metadata labels (per §6, at minimum `Status:` and `Author:`), each appearing **exactly once** — a stub missing those labels, or carrying a duplicated label, fails this invariant even when every section header is present. On violation, **fail closed**: `gauntlet new` raises (refuses to scaffold), `check_entry_contract` raises (treats malformed gate input as "cannot prove human-authored"). The error names the violated invariant and the file path. (FR-3.3)
5. **FR-2.4 deterministic authored-content predicate** — replace/extend the current marker-plus-normalized-equality check in `check_entry_contract` with: pass iff (1) no FR-10.1 marker **and** (2) the candidate, after normalization (strip marker, drop HTML/Markdown guidance comments, drop heading lines, collapse whitespace), is non-empty **and not equal to** the normalized template.
6. **Machine-readable section manifest + documented parser (§6)** — a parser that extracts the manifest from `prompts/prd-author.md`. **Representation correction (review F-001):** in the *current* playbook the classified section entries are **not** ATX headings — `## 0.`/`## 1.`/… are the playbook's own document structure and carry no class marker, while the catalogue of PRD sections lives in §2 as **bold-paragraph entries** of the form `**<section name>** *(mandatory)*` / `**<section name>** *(scale-with-size …)*`. A parser that "collects every ATX heading and classifies by inline marker" (PRD §6's loose wording) cannot produce the manifest from this format. The parser therefore targets the **existing syntax**: within the playbook's §2 "PRD structure" section, match each line of the form `**<text>** *(<class …>)*`, take `<text>` as the normalized entry name and the leading marker word (`mandatory` | `scale-with-size`) as the class. It returns the ordered `(entry-name, class)` tuples (level is implicit — these are catalogue entries, not heading levels). The `header-block` entry parses naturally because the playbook lists it as a §2 bold entry too; its *validation* (Deliverable 4) differs from a normal section's — it is matched by metadata labels, not a heading (review F-006). The manifest drives the stub's required headers and the drift guard.
   - **Escalation (review F-001, escalated):** PRD §6 literally says the parser "collects every ATX Markdown heading … and classifies each by a marker the playbook carries inline," which does not match the playbook's bold-paragraph representation. This plan resolves the mismatch by parsing the existing syntax above and **does not amend the PRD or the playbook**. If the human holds PRD §6's literal "ATX heading" wording binding, that is an `UPSTREAM CONFLICT` to reconcile in the PRD's own loop at the plan-approval gate — not silently in this plan. Either way, no deterministic marker token is added to the playbook without ratifying it on **both** `prompts/prd-author.md` and its byte-identical scaffold twin (see the boundary note below).
7. **Drift-guard test (FR-2.2)** — compares the stub's **complete ordered header set against the parser's full output** over `prd-author.md` — **both** mandatory **and** scale-with-size entries (review F-003), since FR-2.1 requires both classes present in the stub. It fails if adding, renaming, or removing a heading of *either* class in the playbook is not mirrored in the stub, so a scale-with-size section cannot be silently omitted or left to go stale.

### Test strategy

- **Unit (`tests/unit/test_run_lifecycle.py`):**
  - A freshly scaffolded `prd.md` contains the **complete ordered §6 manifest — every mandatory *and* every scale-with-size header, each with its one-line guidance comment** — and exactly one marker (FR-2.1; review F-003). Asserting only the mandatory subset would let an implementation drop every scale-with-size section and still pass.
  - The scaffolded bytes and the entry-contract comparison string are the same resolved source (FR-2.2 — no second copy).
  - **Header-block validation (review F-006):** a stub missing a required metadata label (`Status:`/`Author:`), and a stub with a *duplicated* metadata label, each fail the §4.4 `header-block` invariant — even when all section headers are present.
  - **FR-2.4 acceptance matrix:** whitespace-only edit → reject; comment-only edit → reject; heading-only edit → reject; marker present or duplicated → reject; substantive body prose with marker removed → accept.
  - **FR-3.3 fail-closed (template contents):** an installed `prd-stub.md` with the marker deleted, duplicated, or a mandatory header removed → both `gauntlet new` and `check_entry_contract` raise, naming the invariant.
  - **FR-3.2 fail-closed (install destination; review F-005):** a pre-existing **non-regular** destination at `<asset_root>/prd-stub.md` — a directory, a symlink (where disallowed), or other non-regular node — makes `gauntlet init` raise `InitError` **without mutating** the destination, mirroring the skill-path guard.
  - **§4.3 precedence:** repo copy present → it is used; repo copy absent → packaged fallback used; both consumers resolve the same file.
- **Drift guard (both classes; review F-003):** mutate a copy of the parsed playbook in-test — add, rename, **and** remove a heading of *each* class (mandatory and scale-with-size) — and assert the drift test trips in every case.

### Exit criteria

- `uv run pytest` (unit) green; **`uv run pytest -m integration` green on this machine** before the reviewer handoff — inability to run it is a **stop-and-ask**, never a silent skip (review F-007).
- FR-2.4 matrix, FR-3.3 template fail-closed, and FR-3.2 install fail-closed (non-regular stub destination; review F-005) cases all covered.
- No second copy of the stub exists in the codebase (inline `_PRD_STUB` literal removed as a source of truth).
- Single commit `P2: …` with body naming the assumption validated and FR refs (FR-2.1–FR-2.4, FR-3.3, §6).

### Deferrals / boundary notes

- **Propagation of *both* aids through the three `init` modes, provenance refresh-vs-preserve across both, doctor, docs → P3.** P2 installs the stub template via `init` and tests its *resolution and gate behavior*; the full fresh/re-run/`--from-repo` matrix over both aids together is P3.
- **Playbook marker shape (see Deliverable 6, review F-001):** the parser reads the playbook's *existing* inline `*(mandatory)*` / `*(scale-with-size)*` markers **on the §2 bold-paragraph section entries** — not ATX headings, which carry no marker. If those prove too ambiguous to parse deterministically, or if PRD §6's literal "ATX heading" wording is held binding, that is a potential conflict with the "single instruction source" / no-pipeline-prompt-change posture — **surface it as an UPSTREAM CONFLICT, do not silently rewrite the playbook or the PRD.** If a deterministic marker token must be added, it is applied to **both** the canonical `prompts/prd-author.md` and its byte-identical scaffold twin in the same change, and called out for review.

---

## P3 — Propagation, portability, committability, doctor, docs

### Assumption this phase validates

An adopter re-running `gauntlet init` picks up **both** aids without clobbering customizations, the committed aids **survive cloning/copying to a different absolute location**, and the install neither silently fights a maintainer's ignore rules nor hard-fails on the advisory skill. This validates that the feature satisfies the FR-1.2 "identical workflow on clone" goal end-to-end.

### Deliverables

1. **Three-mode propagation of both aids (FR-3.1)** — fresh repo gets both the skill and `<asset_root>/prd-stub.md` created; a re-run on an existing adopter creates whichever is missing, refreshes an unmodified generated file per §4.5, and skips a present/customized one; `--from-repo` reports present/missing/customized for **both** without writing. "Unmodified generated" vs. "customized" is decided for **both** aids by the single version-keyed **re-render-and-compare** provenance algorithm specified in P1 Deliverable 3 (review F-004) — `--from-repo`'s classification calls the same predicate, so its report cannot disagree with what a write-mode re-run would refresh.
2. **Clone-to-different-path portability test (FR-1.3 acceptance (b))** — after copying/cloning the repo to a different absolute location, the committed skill's repository-relative playbook reference still resolves to the playbook (proves no embedded source-machine absolute path).
3. **`.gitignore` committability + foreign-ignore-rule warning (FR-1.4)** — `init`'s `.gitignore` guidance does not exclude `.claude/skills/`; `init` runs `git check-ignore` against every effective ignore source (repo `.gitignore`, parent-directory `.gitignore`, `.git/info/exclude`, global `core.excludesFile`). If one would exclude the installed skill, `init` does **not** edit the foreign rule — it emits a warning naming the rule's source and the remediation (`git add -f` or amend the rule) and proceeds.
4. **`doctor` warn-only check (FR-1.5, OQ-3)** — add a `_check_skill()`-style check to `src/gauntlet/engine/doctor.py` returning `CheckResult` with status `WARN` (never `FAIL` — the skill gates nothing) when the installed skill is missing, unparseable against the P1 normative schema, or its provenance looks stale (§4.5); `OK`/silent on a well-formed skill.
5. **Second-repo install check** — install into a distinct temp adopter repo (asset_root `.gauntlet`) and verify both aids land at the adopter paths with the adopter-relative playbook reference.
6. **Docs** — README section on the two aids and the authoring workflow; CHANGELOG entry. (`prompts/CHANGELOG.md` is append-only.)
7. **OQ-4 decision** — decide whether `gauntlet new`'s CLI output prints a pointer to the skill/playbook; implement if chosen, else record the deferral. Non-blocking; shapes no required acceptance test.

### Test strategy

- **Unit (`tests/unit/test_init.py`):**
  - All three `init` modes behave as stated for **both** aids in `fixture_repo`, including the §4.5 refresh-vs-preserve distinction.
  - Portability: relocate the init'd repo to a new absolute path; assert the skill's playbook reference still resolves (no absolute path present).
  - `.gitignore`: with no pre-existing exclusion, `git check-ignore` does not match the skill; with a pre-existing parent-directory or `info/exclude` rule matching `.claude/skills/`, `init` warns naming the source and does not modify that rule.
  - Second-repo (asset_root `.gauntlet`) install lands both aids at adopter paths.
  - **Fail-closed under combined re-run (review F-005):** a malformed pre-existing state at *either* generated path (`.claude/skills/gauntlet-prd-author/` or `<asset_root>/prd-stub.md`) during a both-aids re-run still raises `InitError` without mutation — the per-aid guards (skill in P1, stub in P2) are not bypassed when both are installed together.
- **Doctor (`tests/unit/`):** `doctor` warns on an absent/malformed/stale installed skill and is silent on a well-formed one; assert it never returns `FAIL` for the skill.

### Exit criteria

- `uv run pytest` (unit) green; **`uv run pytest -m integration` green on this machine** before the reviewer handoff — inability to run it is a **stop-and-ask**, never a silent skip (review F-007).
- All FR-3.1 / FR-1.3(b) / FR-1.4 / FR-1.5 acceptance cases covered, plus the combined-re-run fail-closed check (review F-005).
- README + CHANGELOG updated; OQ-4 decided and recorded.
- Single commit `P3: …` with body naming the assumption validated and FR refs (FR-1.3, FR-1.4, FR-1.5, FR-3.1, FR-3.2).

### Deferrals

- Codex / `AGENTS.md` pointer, user-level (`~/.claude/skills`) global install, and any new CLI command are **Non-Goals (§2.2)** — out of scope for v1, not deferred to a later phase here.

---

## Cross-cutting commitments (all phases)

- **Worktree clean and committed at every reviewer handoff** (CLAUDE.md central invariant); each phase is **one primary `PN:` implementation commit** after its tests pass, **plus any `PN.x` review-fix commits** the adversarial cycle produces (FR-9.4). The literal "exactly one commit" refers to the *implementation* commit, not a ban on the mandated fix-commit workflow — enforcing one literal commit would force history rewrites to address findings (review F-008).
- **Integration suite before every reviewer handoff** (review F-007): each phase runs `uv run pytest -m integration` locally — not just the unit suite — before handing off. If credentials or environment make it unrunnable, that is a **stop-and-ask** condition (per the bootstrap safety rules), recorded, never a silent skip.
- **Single source of truth:** the skill references `prd-author.md`, never copies it (G3); the stub resolves to one template per repo (§4.3); the manifest is parsed from the playbook, never hand-restated (§6).
- **Fail closed** on every malformed gate input (skill state → `InitError`; stub template invariants → raise in `new` and `check_entry_contract`).
- **`PRD-gauntlet.md`, approved artifacts, and `policy.yaml` are not amended** by this work; `prompts/CHANGELOG.md` is append-only.
- **Stop-and-ask** is mandatory at the P1 schema-ratification gate (OQ-2) and on any discovered PRD/plan conflict (FR-10.4), including the P2 playbook-marker boundary note.

---

## Machine-readable phase list

```gauntlet-phases
- id: P1
  title: Skill template, idempotent install, frontmatter schema, recorded trigger test
  goal: Ship the committable gauntlet-prd-author SKILL.md template, install it via gauntlet init with create-if-absent/idempotent/never-clobber/fail-closed posture and §4.5 provenance refresh, produce the normative frontmatter schema, and run the recorded NL trigger test. Validates the riskiest assumption — a per-repo Claude Code skill is discovered and triggered from natural-language PRD intent on the pinned version. Halts for human ratification of the schema (OQ-2) before later phases depend on it.
- id: P2
  title: Structured stub as a single committable template, fail-closed gate, drift guard
  goal: Replace the inline _PRD_STUB with one committable prd-stub.md template that both gauntlet new and check_entry_contract resolve identically (§4.3); add §4.4 template-invariant validation, the FR-2.4 authored-content predicate, and the §6 machine-readable manifest parser plus drift-guard test. Validates that a richer, customizable stub preserves the FR-10.1 human-author gate (failing closed on malformed templates) and never diverges from the playbook's parsed sections (mandatory and scale-with-size).
- id: P3
  title: Propagation, portability, committability, doctor, docs
  goal: Propagate both aids through the three init modes (fresh / re-run / --from-repo) with §4.5 refresh-vs-preserve, add the clone-to-different-path portability test, the foreign-ignore-rule warning and .gitignore committability check, the doctor warn-only skill check, a second-repo install check, and README/CHANGELOG. Validates that an adopter re-running init picks up both aids without clobbering customizations and that the committed aids survive cloning.
```
# PRD: slugify — a tiny string-to-slug utility

**Status:** Toy spec (human-authored fixture for Gauntlet's P5 end-to-end run).
**Author:** John (toy fixture)

> This is a deliberately small, self-contained PRD used to exercise the full
> Gauntlet `standard` pipeline end-to-end on real CLIs (FR-10.1 applies even to
> toys: a human authors the PRD; the harness never generates one). It is sized
> so the whole prd → plan → phase loop converges within the configured rounds
> and budget on a frontier/strong model pair.

## 1. Problem statement

We need a single, dependency-free Python function that turns an arbitrary title
string into a URL-safe "slug" so titles can be used in paths and filenames.

## 2. Functional requirements

- **FR-1:** A function `slugify(text: str) -> str` in `slugify.py`.
- **FR-2:** Lowercase the result; ASCII letters and digits are preserved.
- **FR-3:** Runs of any non-alphanumeric characters (spaces, punctuation,
  underscores) collapse to a single hyphen `-`.
- **FR-4:** No leading or trailing hyphen in the result.
- **FR-5:** The empty string and an all-punctuation string both return `""`.

## 3. Non-goals

- No Unicode transliteration (ASCII input is assumed for v1).
- No configurable separator; the separator is always `-`.

## 4. Acceptance

`slugify("Hello, World!") == "hello-world"`;
`slugify("  --Already-Sluggish-- ") == "already-sluggish"`;
`slugify("") == ""`; `slugify("!!!") == ""`. Unit tests cover each FR.

## 5. Delivery constraint

The implementation plan contains exactly one implementation phase. This fixture
is intentionally atomic so the required live end-to-end gate exercises the full
PRD review, plan review, implementation, review/fix/confirm, phase gate,
retrospective, and PR-draft path without making its runtime depend on how a model
chooses to subdivide five small functional requirements.

Because no later implementation phase exists, the P1 phase commit records
`Deferrals: none`. Optional future product ideas remain non-goals; they must not
be represented as deferrals to invented phase identifiers.

The machine-readable P1 acceptance clauses cover product behavior that the
builder can prove with pytest before the pipeline creates the phase commit.
Post-commit facts—commit count/message, history shape, final worktree
cleanliness, and phase diff scope—remain owned by the standard pipeline's commit
and phase gates; they must not be repeated as pytest-mapped P1 acceptance
clauses. Gauntlet evidence under `runs/` (including `artifacts/acceptance-map.json`)
is engine bookkeeping, not a slugify product deliverable.

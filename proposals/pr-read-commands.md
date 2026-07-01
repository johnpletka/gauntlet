# Proposal: `pr_read_commands@v1` allow rule for review PR-mode reads

> **Status:** Proposed — awaiting maintainer ratification through the
> policy-change process (CLAUDE.md §8, PRD Open Question 11.4). This document is
> the **input** to that process; it is authored in P4 and is **not** a change to
> `policy.yaml`. No agent edits `policy.yaml` — a human copies the rule below in
> and thereby asserts `ratified: true`. Ratification is the explicit gate between
> P4 and P5 (PR mode); P5 does not begin until the rule is ratified, and its
> run-time preflight (`gauntlet.judge.preflight.check_pr_read_commands`) reads
> this rule deterministically before any `gh`/`git fetch` command (FR-7.3/FR-7.4).

## 1. Motivation

`gauntlet review --pr <N>` (P5) pulls a GitHub PR down and reviews it. To do that
it issues three **read-only** commands from Gauntlet's own process: `gh pr view`
(PR metadata), `gh pr checkout` (fetch + local branch), and `git fetch` (the PR
head). None mutate a remote; all are reads.

Under the current `policy.yaml` these three do not resolve uniformly on the
deterministic fast path:

- `git fetch` matches the existing `git-normal-flow` allow.
- `gh pr view` matches the existing `gh-pr-propose-and-read` allow.
- **`gh pr checkout` matches no fast-path rule** — it falls through to the LLM
  classifier (or fail-closed). Relying on the LLM fallback for a command the
  harness issues on every PR-mode run is exactly the non-determinism FR-7.3
  forbids ("a proposed `policy.yaml` rule covers them explicitly rather than
  relying on the LLM fallback").

FR-7.4 further requires the gate to be **machine-checkable**: a versioned,
ratified rule the harness reads deterministically (no network, no agent, no
"try-it-and-see"). That needs a rule carrying a stable `id`, a `version`, and a
`ratified` marker — fields ordinary rules do not have.

## 2. The proposed rule (exact YAML)

Add this single rule under the top-level `allow:` list in `policy.yaml`. It
groups all three PR-mode read commands under one auditable, versioned identifier
so the preflight can verify the boundary with a config read:

```yaml
# Add under the top-level `allow:` list in policy.yaml.
allow:
  - name: pr-read-commands
    id: pr_read_commands
    version: v1
    ratified: true
    description: "Review PR-mode (FR-7.3) reads: gh pr view / gh pr checkout /
      git fetch. All are read-only — they resolve PR metadata and fetch the PR
      head into a local branch; none mutate a remote. The harness issues them
      from its own process on every --pr run, so they are blessed explicitly
      here rather than left to the LLM fallback (FR-7.3), under a stable
      id/version the deterministic FR-7.4 preflight verifies before any read
      runs. Remote-mutating gh/git (push, pr create, pr merge) stay independently
      denied by the deny-first rules in every context (FR-9.8), so this allow
      never widens the blast radius. Like every allow rule it is skipped on a
      chained/piped/redirected command, so a benign prefix cannot bless a
      dangerous trailing segment."
    applies_to_tools: [Bash]
    command_patterns:
      - '^\s*gh\s+pr\s+(view|checkout)\b'
      - '^\s*git\s+fetch\b'
```

The `command_patterns` are anchored with `^\s*` and the allow path is skipped on
chained/redirected commands (the existing `_CHAINING_RE` guard in
`judge/policy.py`), so the rule only ever blesses a single bare read command.

## 3. New `PolicyRule` governance fields (already implemented in P4)

The rule uses three optional `PolicyRule` fields P4 added to
`src/gauntlet/judge/policy.py`:

| Field | Type | Meaning |
|---|---|---|
| `id` | `str \| None` | Stable rule identifier the preflight looks up (`pr_read_commands`). |
| `version` | `str \| int \| None` | Rule-level version pin (`v1`), distinct from the file-level `Policy.version`. |
| `ratified` | `bool` | Asserted **only** by the human policy-change process. Agents propose; humans ratify (CLAUDE.md §2). |

They default to `id=None`, `version=None`, `ratified=False`, so **every existing
rule loads unchanged** — the fields matter only for a governed, version-pinned
rule like this one.

## 4. How the deterministic preflight uses it (FR-7.4)

`gauntlet.judge.preflight.check_pr_read_commands(policy_path)` (P4):

1. Loads the active `policy.yaml` (a pure read — no network, no agent).
2. Looks up the rule whose `id == "pr_read_commands"`.
3. Returns `ok=True` **only** when that rule is present, `ratified: true`, and
   `version == "v1"`.
4. Otherwise returns `ok=False` carrying the exact FR-7.4 message for the state,
   in the order **absent > unratified > version-mismatch**:

   > `P4 (PR mode) requires policy rule 'pr_read_commands@v1' to be ratified in policy.yaml; it is <absent|unratified|version <found> != v1>. Ratify it through the policy-change process (Open Question 11.4) before using --pr.`

P5's PR-mode entry path calls this **before** any `gh`/`git fetch`; a not-ok
result halts and escalates with the message rather than proceeding on the LLM
fallback. Branch-mode reviews issue none of these commands and skip the preflight.

## 5. Why this does not widen the harness's autonomy

- **Reads only.** The rule blesses `gh pr view` / `gh pr checkout` / `git fetch`.
  Remote-mutating commands stay denied in every context by the deny-first rules
  (`push-or-pr-open-in-pipeline-step` denies in-step `git push` / `gh pr create`;
  `gh-pr-merge`, `force-push`, `git-push-*` remain terminal). Fixes land locally;
  the operator pushes (FR-9.8/FR-7.1).
- **No chained bless.** Skipped on chained/redirected commands, like every allow.
- **Deterministic, not probe-based.** The preflight is a config read, so P5 never
  learns the boundary by attempting a command and watching the judge (the
  try-it-and-see anti-pattern fail-closed forbids).

## 6. Ratification checklist (for the maintainer)

1. Review the rule in §2 and the read-only rationale in §5.
2. Copy the rule into the `allow:` list of `policy.yaml` (with `ratified: true`).
3. Confirm `uv run pytest -k "policy or preflight"` stays green.
4. Record the ratification per the policy-change process; P5's prerequisite gate
   reads that record before it begins.

Until step 2 lands, `check_pr_read_commands` correctly reports the rule `absent`
and PR mode stays closed — the intended fail-closed state between P4 and P5.

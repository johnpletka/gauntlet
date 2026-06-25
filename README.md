# Gauntlet

Adversarial multi-agent development harness. Every artifact — PRD, plan, and
each implementation phase — runs the gauntlet of adversarial review before it
ships: a **builder** agent implements, an independent **reviewer** agent
attacks the result, a cheap **triage** model sorts the findings, the builder
fixes, and the reviewer confirms the fix against the diff. A localhost
**judge** service gates every tool call the agents make, failing closed.

The canonical spec is [`PRD-gauntlet.md`](PRD-gauntlet.md). The bootstrap plan
is [`runs/gauntlet/plan.md`](runs/gauntlet/plan.md).

> **Status:** the bootstrap is complete — Gauntlet was built by running its own
> pipeline against itself (phases P1–P7, each adversarially reviewed and
> human-ratified). It is usable on other repositories via the steps below.

---

## Table of contents

- [How it works](#how-it-works)
- [Prerequisites](#prerequisites)
- [Install](#install)
  - [macOS / Linux](#macos--linux)
  - [Windows](#windows)
- [Configure credentials](#configure-credentials)
- [Quick start (≤ 3 commands)](#quick-start--3-commands)
- [Authoring a PRD (the repo teaches you how)](#authoring-a-prd-the-repo-teaches-you-how)
- [The run lifecycle](#the-run-lifecycle)
- [Command reference](#command-reference)
- [Configuration](#configuration)
- [Safety model](#safety-model)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

---

## How it works

A *pipeline* (YAML) is a sequence of stages; each stage is built from a few
step types:

| Step type | What it does |
|---|---|
| `agent_task` | The builder implements a phase in the working tree. |
| `shell` | Runs a command (e.g. the test suite) as a hard gate. |
| `commit` | Commits the phase with an enforced message format. |
| `adversarial_cycle` | review → triage → fix → confirm, looped to convergence. |
| `human_gate` | Pauses the run for a human to `approve` / `reject`. |

The **central invariant** is that the working tree is clean and committed at
every point where control passes to the reviewer — this is what makes review
diffs meaningful and `kill -9` resume safe.

Two pipelines ship by default: `standard` (for real work) and `bootstrap` (the
self-hosting pipeline used to build Gauntlet itself).

---

## Prerequisites

Gauntlet is a thin orchestrator that drives external agent CLIs and model APIs.
You need:

| Requirement | Why | Notes |
|---|---|---|
| **Python ≥ 3.10** | runtime | Managed for you by `uv`. |
| **[`uv`](https://docs.astral.sh/uv/)** | install + run | The only build/run tool you install by hand. |
| **`claude` CLI** ([Claude Code](https://docs.claude.com/en/docs/claude-code)) | the **builder** agent | Must be installed and authenticated. |
| **`codex` CLI** ([Codex CLI](https://github.com/openai/codex)) | the **reviewer** agent | Must be installed and authenticated. |
| **`OPENAI_API_KEY`** | triage / judge / escalation tiers | Default config uses `gpt-5-mini` (triage, judge) and `gpt-5` (escalation) via LiteLLM. |

The default agent profiles are: builder = `claude` (model `opus`), reviewer =
`codex` (model `gpt-5.5`), triage/judge = `gpt-5-mini`, escalation = `gpt-5`.
You can repoint any tier to a different provider in config (see
[Configuration](#configuration)); `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` are
only needed if you switch the API tiers to those providers.

---

## Install

### macOS / Linux

**1. Install `uv`** (if you don't have it):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Install the agent CLIs** and sign in to each (follow each tool's own docs):

```sh
# Claude Code (builder) — see https://docs.claude.com/en/docs/claude-code
claude --version        # confirm it's on PATH
claude /login           # or however your install authenticates

# Codex CLI (reviewer) — see https://github.com/openai/codex
codex --version
codex login
```

**3. Install Gauntlet** as a global tool:

```sh
uv tool install gauntlet-spec       # from PyPI; or the git URL below for HEAD
# uv tool install git+https://github.com/johnpletka/gauntlet.git
gauntlet version
```

> **The PyPI package is `gauntlet-spec`, not `gauntlet`.** The bare name
> `gauntlet` on PyPI is an unrelated (and broken) project. The installed command
> is still `gauntlet` — only the install name differs.

> **Python 3.10+ is required.** If your default interpreter is older, `uv` will
> refuse with `does not satisfy Python>=3.10`. Add `--python 3.10` (or newer) to
> the command and `uv` will fetch a suitable interpreter automatically.

This puts two console scripts on your PATH: `gauntlet` (the CLI) and
`gauntlet-judge-hook` (the per-tool-call safety hook, wired automatically by
`gauntlet init`).

### Windows

Gauntlet itself is pure Python and runs natively on Windows via `uv`. Use
**PowerShell**.

**1. Install `uv`:**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**2. Install and authenticate the agent CLIs.** Install `claude` (Claude Code)
and `codex` per their official docs and confirm each is on your `PATH`:

```powershell
claude --version
codex --version
```

> **Note on the agent CLIs:** if a given CLI does not yet ship a native Windows
> build, install Gauntlet and that CLI inside **WSL2** (Ubuntu) and follow the
> macOS / Linux steps there instead. The orchestrator, judge service (loopback
> HTTP on `127.0.0.1`), and hooks are all cross-platform; the only
> platform-sensitive dependency is the agent CLIs themselves.

**3. Install Gauntlet:**

```powershell
uv tool install gauntlet-spec
# or, for HEAD: uv tool install "git+https://github.com/johnpletka/gauntlet.git"
gauntlet version
```

> **The PyPI package is `gauntlet-spec`, not `gauntlet`** — the bare name is an
> unrelated, broken project. The command is still `gauntlet`. If `uv` reports
> `does not satisfy Python>=3.10`, append `--python 3.10` (or newer) and it will
> fetch a compatible interpreter.

---

## Configure credentials

The API tiers (triage, judge, escalation) read credentials **from the
environment only** — never from repo config (so keys never get committed).

**macOS / Linux** (add to `~/.zshrc` / `~/.bashrc` to persist):

```sh
export OPENAI_API_KEY="sk-..."
```

**Windows — PowerShell** (current session):

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

**Windows — persist across sessions:**

```powershell
setx OPENAI_API_KEY "sk-..."
# then open a new terminal
```

Run `gauntlet doctor` (below) to verify everything resolves before your first
run.

---

## Quick start (≤ 3 commands)

From the repository you want Gauntlet to work on:

```sh
gauntlet init        # 1. scaffold config, pipeline, prompts, policy + wire hooks (idempotent)
gauntlet doctor      # 2. validate CLIs, auth, hook wiring, judge, API keys
gauntlet new myfeat  # 3a. scaffold .gauntlet/runs/myfeat/ with a PRD stub
#    ...author .gauntlet/runs/myfeat/prd.md...
gauntlet run myfeat  # 3b. start the pipeline
```

If the repository already carries committed Gauntlet assets (a teammate ran
`init` before you), you only need to wire **this machine's** hooks:

```sh
gauntlet init --from-repo
```

`gauntlet doctor` reports actionable, per-check status — installed CLI versions
vs. the verified pin file (`.gauntlet/pins.yaml`), authentication, hook wiring,
judge startability, and ApiAdapter keys — and exits non-zero on any blocker.

---

## Authoring a PRD (the repo teaches you how)

A Gauntlet run starts from a human-authored PRD. `gauntlet init` installs two
committable aids so you don't have to carry the conventions in your head — and a
teammate who clones the repo inherits both automatically:

- **A Claude Code skill** at `.claude/skills/gauntlet-prd-author/SKILL.md`. In a
  Claude session, a natural-language request like *"help me write a PRD"* or
  *"start a Gauntlet run"* triggers it; it routes you to this repo's authoring
  playbook (`prompts/prd-author.md`, under your `asset_root`) and the conventions
  for where the PRD lives and how to scaffold and launch it. It's a thin pointer
  to the playbook, not a copy, so there's one source of truth.
- **A structured stub.** `gauntlet new <slug>` writes a PRD stub with the
  playbook's full section skeleton and a one-line hint per section, so you start
  from the right shape. The stub is the committable template
  `<asset_root>/prd-stub.md` — edit it to change the house style for every future
  PRD.

The skill *teaches and routes*; it never authors the PRD for you. A human writes
and ratifies it (FR-10.1): `gauntlet run` refuses to start while the file is
still the stub (marker present, or no substantive content added), so an unfilled
skeleton can't become a runnable non-PRD.

Both aids are idempotent and never-clobber: re-running `gauntlet init` leaves any
customization byte-for-byte intact (only an *unmodified* generated file is ever
refreshed, and only after a template version bump). `gauntlet doctor` includes a
warn-only check that the skill is installed and well-formed — it never blocks a
run, since the skill gates nothing.

---

## The run lifecycle

A run advances automatically until it hits a `human_gate`, then **parks** for
your decision:

```sh
gauntlet run myfeat              # start (parks at the first gate)
gauntlet status myfeat           # see current step + every step's state
gauntlet approve myfeat          # accept the parked gate; drive to the next one
gauntlet reject myfeat --notes "…"   # send the phase back for another fix round
gauntlet resume myfeat           # resume after an interruption (kill -9 safe)
gauntlet resume myfeat --response "…"   # decide an upstream conflict (see below)
gauntlet report myfeat           # per-step / per-agent cost + token breakdown
```

- **Interrupted runs are resumable.** State lives in the run's `manifest.json`;
  `gauntlet resume` re-enters at the last incomplete step. A step that wrote a
  dirty tree before dying is parked or reset rather than re-run blindly.
- **Approved artifacts are immutable.** A later phase that finds an approved
  PRD/plan incomplete *halts and surfaces the conflict* rather than amending it.
  You resolve that conflict with `gauntlet resume <slug> --response "…"` (see
  **Resolving an upstream conflict** below), which routes any artifact change
  back through its own gate rather than letting the builder amend it in place.
- At the final gate a **`PR.md` draft** is written under `.gauntlet/runs/<slug>/`
  (it is **not** opened or pushed — that stays a human action).
- After a run, `gauntlet feedback <slug>` captures your retrospective notes and
  triage corrections to feed the self-improvement loop.

---

## Resolving an upstream conflict

When a builder finds that the approved PRD or plan is wrong or under-specified,
it **halts with an `UPSTREAM CONFLICT`** instead of working around the approved
artifact (FR-10.4). The step parks; the run is stuck until you decide. The
standard, audited way to decide is:

```sh
gauntlet resume <slug> --response "<your decision, in plain text>"
```

The decision is recorded verbatim in the manifest (timestamped, attributed to
your operator identity) and injected into a fresh builder run, which
**re-evaluates** the conflict in light of it rather than re-surfacing it. The
builder then emits one of three outcomes:

- **Proceeds** — the decision resolves the conflict within what the approved
  artifacts already allow (e.g. ratifying one of the options they leave open, or
  deferring out-of-scope follow-up to `FUTURE.md`). The run un-sticks and
  continues.
- **Re-parks for an artifact amendment** — the decision would require changing
  approved PRD/plan text (**including** "proceed even though this contradicts the
  plan"). There is **no** proceed-now-amend-later path: amend that artifact on
  its **own** branch, take it through **its own** review-and-gate cycle
  (FR-10.4), then resume again with a decision that no longer contradicts it.
- **Re-parks for clarification** — the decision was ambiguous; the builder names
  what it still needs. Supply another `--response`.

Notes:

- `--response` is **required** to resume a step parked on an upstream conflict.
  Other parks (e.g. a `human_gate`) are unaffected — use `approve` / `reject`
  for those, and a plain `gauntlet resume` for a non-conflict agent park.
- **Conflicts do not consume the retry budget** — only genuine failures do. You
  can supply as many `--response` cycles as it takes; you decide when to stop or
  abort.
- The whole history of your decisions is preserved in the manifest
  (`steps[N].human_responses`, append-only) and reaches git history, so the audit
  trail of who decided what, and when, is never lost.

---

## Command reference

| Command | Purpose |
|---|---|
| `gauntlet init [--from-repo]` | Scaffold config/pipeline/prompts/policy + wire hooks (idempotent). |
| `gauntlet doctor` | Validate environment: CLIs, auth, hooks, judge, keys. |
| `gauntlet new <slug>` | Scaffold `.gauntlet/runs/<slug>/` with a PRD stub. |
| `gauntlet run <slug> [--pipeline standard\|bootstrap] [--no-judge]` | Start a run on branch `gauntlet/<slug>`. |
| `gauntlet status <slug>` | Show run status and each step's state. |
| `gauntlet approve <slug> [--gate ID] [--notes …]` | Approve a parked gate, continue the run. |
| `gauntlet reject <slug> --notes … [--gate ID]` | Reject a parked gate. |
| `gauntlet resume <slug>` | Resume an interrupted run at its last incomplete step. |
| `gauntlet resume <slug> --response "…"` | Decide a step parked on an upstream conflict (FR-10.4); records the decision and re-runs the builder with it. Required for conflict parks. |
| `gauntlet abort <slug>` | Abort a run. |
| `gauntlet report <slug>` | Per-step / per-agent-profile cost breakdown. |
| `gauntlet feedback <slug>` | Capture human feedback + triage corrections (FR-6.1). |
| `gauntlet rollback <slug> --phase N` | Reset the branch + manifest to a phase boundary (guarded). |
| `gauntlet judge serve [...]` | Run the localhost judge service (normally engine-managed). |
| `gauntlet version` | Print the installed version. |

`--no-judge` disables the safety judge and is for **testing only** — it leaves
agent tool calls ungated. Don't use it on real work.

---

## Configuration

`gauntlet init` writes a `.gauntlet/` directory in your repo:

- **`.gauntlet/config.yaml`** — agent profiles (adapter + model + flags),
  per-agent commit identities, run timeouts and budgets. References models, not
  credentials.
- **`.gauntlet/pins.yaml`** — the CLI versions and exact flags verified by the
  contract suite; `doctor` checks the installed CLIs against it.

Pipelines, prompt templates (versioned data, not code), structured-output
schemas, and the judge fast-path `policy.yaml` all live under `.gauntlet/` too
— `.gauntlet/pipelines/*.yaml`, `.gauntlet/prompts/`, `.gauntlet/schemas/`,
`.gauntlet/policy.yaml`. The config's `asset_root` (default `.gauntlet` in a
scaffolded repo) is where the engine resolves them; everything is committable,
so a teammate who clones the repo gets the identical workflow. (Gauntlet's own
source repo sets `asset_root: "."` to keep these assets at the repo root as
first-class source rather than tucked into a dotfile dir.)

To repoint a tier at a different provider, edit the agent profile's `adapter`
and `model` in `.gauntlet/config.yaml` and set that provider's key in your
environment (e.g. `ANTHROPIC_API_KEY` for an `anthropic/*` model). LiteLLM
model naming applies to `api` adapter profiles.

---

## Safety model

- Agent tool calls (e.g. the builder's shell commands and file writes) pass
  through a **PreToolUse hook → localhost judge service**. The judge decides via
  a deterministic policy fast-path, then an LLM classifier rung, and **fails
  closed** (deny) on timeout, parse error, or any unexpected outcome.
- The judge binds `127.0.0.1` only and rejects callers lacking the per-run
  token. Every decision is written to an audit log.
- The reviewer runs **read-only** (codex sandbox `read-only`); any worktree
  mutation by a reviewer is a detected process violation.
- Permission-bypass flags (e.g. `--dangerously-skip-permissions`) are rejected
  by config lint — they would disable the hook layer.

---

## Development

Working on Gauntlet itself:

```sh
uv sync                       # create the venv, install deps + package (editable)
uv run pytest                 # unit suite (no credentials required)
uv run pytest -m integration  # contract tests against live CLIs/APIs (needs creds)
uv run gauntlet doctor        # validate your dev environment
```

`uv run pytest` runs unit tests only; the `integration` marker selects the live
contract suite, which requires authenticated CLIs and API keys.

---

## Troubleshooting

- **`gauntlet` errors with `ModuleNotFoundError: No module named 'gauntlet'`**
  (or `gauntlet.main`) — you installed the unrelated PyPI package via
  `uv tool install gauntlet`. Run `uv tool uninstall gauntlet`, then reinstall
  the correct package: `uv tool install gauntlet-spec` (add `--python 3.10` if
  your default interpreter is older).
- **`gauntlet-judge-hook: command not found`** during a run — the hook console
  script isn't on the PATH the agent CLI sees. Re-run `gauntlet init` (or
  `gauntlet init --from-repo`) and confirm `uv tool`'s bin directory is on your
  PATH (`uv tool update-shell`, then open a new terminal).
- **`doctor` reports a stale CLI version** — your installed `claude` / `codex`
  differs from `.gauntlet/pins.yaml`. Re-verify with the integration suite, or
  update the pin file if the new version is intended.
- **A run parks unexpectedly / a step is `failed`** — `gauntlet status <slug>`
  shows where; the step's transcript under `.gauntlet/runs/<slug>/<run>/steps/` has the
  detail. `gauntlet resume <slug>` re-enters safely once the cause is cleared.
- **An agent hits a provider session/usage limit mid-step** — the engine fails
  the step closed (it does not fake success). Wait for the limit to reset, then
  `gauntlet resume <slug>`.
```

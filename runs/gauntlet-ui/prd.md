# PRD: Gauntlet Console — Supervisory Web UI for Runs

**Status:** Draft v0.3 (v0.2 + addressed adversarial PRD-cycle review F-001…F-009: repo/worktree-global active-run lock; fully-specified `run --watch` console discovery/lifecycle; run-dir allocation handshake for the supervisor; finer event identity for de-dup; deterministic phase-diff commit selection; complete auth/CSRF flow; OS process-creation-time for PID-reuse safety; run_id selection across the API; `interrupted` named as an existing step status). v0.2 = v0.1 + resolved OQ-1…OQ-6: real engine-level active-run guard; clarified single-repo = all-slug/all-history browsing; `gauntlet run --watch` boots the console; confirm step for destructive verbs; in-tab notifications v1 / background push v2; keep note string-matching for v1 — see §11.
**Author:** John (with Claude)
**Date:** 2026-06-16
**Working name:** Console (every run is now *visible* — list it, watch it, answer its gate, recover it)
**Relationship to the core spec:** This PRD realizes the "TUI/dashboard" item that `PRD-gauntlet.md` §2.2 explicitly deferred ("No GUI. CLI + markdown artifacts. A TUI/dashboard is a future consideration."). It does **not** amend that document; it adds a new, separately-gated surface on top of the existing engine.

---

## 1. Overview

### 1.1 Problem statement

`gauntlet run <slug>` is a **synchronous, blocking** process (`cli.py` → `RunManager.start()` drives the whole pipeline inline; there is no daemon and no PID file). In practice the operator backgrounds it manually (`&` / `nohup` / tmux), and from that moment the run is effectively invisible:

- **No live status.** The only way to see progress is to re-run `gauntlet status <slug>`, which prints a flat list of step IDs and statuses.
- **Gates are buried.** When a run parks at a human gate, nothing surfaces it. The operator discovers it by polling status, then has to open `runs/<slug>/RUN.md` and dig into `runs/<slug>/<run_id>/steps/.../findings.json`, `triage`, and `plan.md` to understand *what* is being asked before they can `gauntlet approve`/`reject`.
- **Failures are silent and opaque.** A timeout, a budget halt, a judge that went down, an agent crash — all of these stop the run with no notification, and diagnosing them means reading the manifest and step transcripts by hand.
- **No notifications at all.** The engine is entirely pull-based; there is no eventing, webhook, or push of any kind.
- **No clear recovery path.** `gauntlet resume` exists, but whether it *helps* depends on the failure: a parked gate needs approve, a mid-edit interrupt resumes cleanly, but a deterministic timeout/budget halt will just re-trigger on a bare resume. Today the operator has to know this and infer which case they're in.

The operator's scarce attention is spent **polling and excavating** instead of **deciding**. The unique human value in a Gauntlet run is concentrated at the gates (judging triage, approving a phase) and at failures (deciding how to recover) — exactly the two moments the current CLI makes hardest to reach.

### 1.2 Solution summary

A local-first **web console**, started with `gauntlet serve`, that is both a **read model** over run state and a **supervisor** of run processes:

- **Lists every run** across all slugs — sortable by recency/status, searchable — with live status, current step, cost, and an owned/observed badge.
- **Renders the process** for a selected run: the step tree (from the manifest, the same data `RUN.md` tabulates), where it is, per-step status/duration/cost, with drill-down into any step's transcript and live log tail.
- **Surfaces pending gates.** When a run parks at a `human_gate`, the console resolves the gate's `show:` artifacts (findings / triage / plan / phase diff), renders them readably, and offers **Approve / Reject** (with notes) — the decision the operator came to make, with the evidence already assembled.
- **Diagnoses failures and offers the _right_ recovery.** It classifies the parked/failed state from the manifest (gate vs timeout-halt vs budget-halt vs mid-edit interrupt vs hard failure) and offers the action that actually applies — **Resume** where resume helps, and an honest "resume won't fix this" with guidance where it won't.
- **Supervises owned runs.** Runs *launched from the console* are owned as managed subprocess children of the `gauntlet` CLI — full lifecycle, captured logs, instant state, and crash-survival (re-attach on server restart; an orphaned owned run is offered for resume exactly as a `kill -9`'d run would be). Runs started elsewhere are still fully **observed** from their on-disk manifests.
- **Notifies** on the three moments that need a human: gate reached, run failed, run completed — to macOS desktop, the browser, and Slack. Fail-soft: a notification failure can never affect a run.

**The central design choice — and the safety guarantee that falls out of it:** the console never re-implements the engine. Control actions launch the *same `gauntlet` CLI verbs a human would type* (`run`, `resume`, `approve`, `reject`, `abort`). It runs strictly *above* the orchestrator, so it inherits every existing invariant (judge gating, the read-only-reviewer contract, "nothing lands on `main`", "don't mutate approved artifacts") instead of being able to weaken any of them.

---

## 2. Goals and Non-Goals

### 2.1 Goals

| # | Goal | Addresses |
|---|------|-----------|
| G1 | See all runs at a glance — list, sort by latest, filter by status, search by slug/branch | "manually check where it is" |
| G2 | See *where a run is* in the process, and drill into any step's log/transcript | "dig into RUN.md / run_xxx dirs" |
| G3 | Bring pending human gates forward with the question's evidence assembled and Approve/Reject in one place | "figuring out what to do next means digging through logs" |
| G4 | Notify the operator the moment a run reaches a gate, fails, or completes | "notifications are non-existent" |
| G5 | Diagnose a stopped run and offer the *correct* recovery action per failure type (incl. `resume`) | "timeout / limits / judge down → ability to resume" |
| G6 | Own and supervise console-launched runs (lifecycle, captured logs, crash survival) so a run is never invisible again | the root cause: blocking run, backgrounded manually |
| G7 | Preserve every existing safety/process invariant by construction (control = sanctioned CLI verbs) | CLAUDE.md guiding principles |
| G8 | Add the surface with a minimal dependency footprint (only two already-installed transitive deps promoted to explicit) and a thin, inspectable backend | "the orchestrator is thin by design" |

### 2.2 Non-Goals (v1)

- **Not a hosted/multi-tenant service.** Loopback-bound localhost only, like the judge. No remote access, no accounts.
- **Not a multi-*repo* dashboard.** One `gauntlet serve` instance scopes to a single git repository (its `cwd`), and one *actively-controlled* run at a time. "Single-repo" constrains **repositories**, not slugs or history: within that repo, **every** slug under the run root and its **full run history** are browsable read-only (FR-1.1, FR-2.4). A cross-repo registry is deferred (§11/OQ-4).
- **Not an editor.** The console never edits PRDs, plans, pipelines, policy, or code. It reads artifacts and issues sanctioned commands. (Config *guidance* for a budget/timeout bump is surfaced as text, not auto-applied.)
- **No new autonomy.** It exposes exactly the verbs the CLI exposes — no merge/push/finish/rollback buttons (those remain human-only CLI actions, per "nothing lands on `main`").
- **No true background Web Push in v1.** In-tab notifications ship in v1; service-worker/VAPID background push is deferred to v2 (§11).
- **Not a replacement for the CLI.** The CLI remains fully usable and authoritative; the console is additive and observes CLI-started runs.

---

## 3. Users and Personas

| Persona | Description | Primary console interactions |
|---------|-------------|------------------------------|
| **Pipeline operator** (John) | Starts runs, gets pinged at gates/failures, judges and approves, recovers stalled runs | Run list, gate review + approve/reject, failure/resume panel, launch run |
| **Reviewer-of-record** (human at the gate) | Needs the gate's evidence (findings, triage, diff) assembled to make an approve/reject call quickly | Gate review panel, phase diff, step transcripts |
| **Debugger** (future-you, post-mortem) | Reconstructs what happened in a failed/odd run | Step tree, transcript/events viewer, judge-audit view, cost report |

---

## 4. System Architecture

```
┌──────────────────────────────── browser (127.0.0.1) ────────────────────────────────┐
│  Run list · Run detail (step tree) · Step log viewer · Gate review · Resume panel     │
│  Jinja-rendered HTML + HTMX  ◀── SSE (live state + log tail) ──  in-tab notifications  │
└───────────────────────────────────────┬───────────────────────────────────────────────┘
                                         │  HTTP (loopback, token cookie, CSRF on POST)
┌────────────────────────────────────────▼──────────────────────────────────────────────┐
│                          gauntlet serve  —  FastAPI app (src/gauntlet/web/)             │
│                                                                                          │
│  ┌────────────────┐   ┌──────────────────────┐   ┌──────────────────────────────────┐  │
│  │  RunStore      │   │  Watcher             │   │  JobSupervisor                     │  │
│  │  (read-only)   │   │  (~1s mtime poll of  │   │  owns launched runs as Popen       │  │
│  │  discover runs │   │   manifest.json) →   │   │  children of the gauntlet CLI;     │  │
│  │  parse manifest│   │   async event bus →  │   │  registry on disk (.serve/job.json)│  │
│  │  resolve gate  │   │   SSE + notify        │   │  re-attach on restart              │  │
│  │  artifacts/diff│   └──────────┬───────────┘   └─────────────────┬──────────────────┘  │
│  └───────┬────────┘              │ fan-out                          │ subprocess.Popen     │
└──────────┼───────────────────────┼──────────────────────────────────┼──────────────────────┘
           │ reads                  │ notify                            │ launches
           ▼                        ▼                                   ▼
   runs/<slug>/<run_id>/      macOS notifier / Slack         gauntlet run|resume|approve <slug>
   manifest.json, steps/,     webhook / in-tab SSE           (each child starts its OWN
   artifacts/, pipeline.yaml                                  ManagedJudge — engine unchanged)
           ▲
           │ also written by runs started in a terminal  ──►  OBSERVED runs (read-only state,
                                                               control still available via CLI verbs)
```

### 4.1 Components

**`gauntlet serve` (FastAPI app, `src/gauntlet/web/`).** A new web module structured as a sibling to `src/gauntlet/judge/` — same FastAPI + uvicorn shape and a similar loopback-bind + per-serve-token posture (the token rides in an httpOnly cookie for the browser, not in the judge's `X-Gauntlet-Token` header — FR-10.4). Submodules: `service.py` (app factory), `runner.py` (uvicorn host), `store.py` (read model), `watcher.py` (poll → event bus), `supervisor.py` + `jobproc.py` (owned-run lifecycle), `notify.py` (fan-out), `views.py` + `templates/` + `static/` (the UI). A top-level `gauntlet serve` typer command is added in `cli.py`.

**RunStore (read-only).** Discovers runs across all slugs (reusing `RunManager`'s run-dir iteration) and parses `manifest.json` (the existing pydantic `Manifest`). Resolves a parked gate's `show:` artifacts and computes phase diffs (via existing `gitops` range-diff helpers). Never imports the orchestrator; never writes.

**Watcher.** A single async task that stats each known `manifest.json` ~once per second (atomic `os.replace` writes mean a poll never reads a torn file; every checkpoint rewrites the manifest, so polling catches every transition). It also rescans for new run dirs on the same tick. On a status transition it publishes to an in-process event bus that feeds both SSE streams and the notifier. Live log tailing uses the same byte-offset technique scoped to *open* viewers only.

**JobSupervisor + RunProcess.** Owns runs launched from the console. `RunProcess` is a near-clone of `ManagedJudge`: `subprocess.Popen([python, "-m", "gauntlet", <verb>, slug, *flags], cwd=repo_root, start_new_session=True)` with combined stdout/stderr captured to a `run_dir/.serve/` log, and a `.stop()` that `killpg`s the whole process group (a run spawns agent CLIs + a judge as grandchildren). A sidecar `run_dir/.serve/job.json` records `{pid, pgid, verb, started_at, log_path, proc_create_time}` (the OS process-creation timestamp defeats PID reuse, FR-6.4/7.2) — the registry lives on disk, not in server memory. The supervisor pre-allocates the child's `run_id` so it knows `run_dir` before launch (FR-6.1a).

**Notifier.** Fan-out to macOS desktop (`terminal-notifier` if present, else `osascript`), Slack (incoming-webhook POST via `httpx` — promoted to an explicit dependency, FR-11.3), and the browser (in-tab `Notification` driven by SSE). Every channel is wrapped fail-soft.

### 4.2 Key design decisions

- **D1 — Own runs as subprocess children of the CLI, not in-process.** The orchestrator mutates **process-global** state (`os.environ["GAUNTLET_STEP_ID"]`, the per-run `ManagedJudge` env/port). Two concurrent `start/resume/approve` calls in one process would clobber each other. A threadpool or in-process worker is therefore unsafe without deeply rewriting determinism-critical engine code. Running each as a `Popen` of `gauntlet <verb>` is isolated, crash-survivable, and — critically — *is the exact command a human would type*, so it carries every CLI guarantee with it.
- **D2 — The server holds no authoritative state; disk does.** Owned-run identity is the on-disk `.serve/job.json`; run state is the manifest. On server restart, the supervisor re-discovers owned runs and re-attaches to live PIDs; a dead owned run is classified "interrupted — resume available," the same recovery path a `kill -9` already has (proven by the existing crash/resume test). This is "determinism over cleverness" and "data over inference" applied to the supervisor itself.
- **D3 — `parked` is overloaded; read the _step_, not the run.** At the run level, a human-gate park, a budget/timeout halt, and a mid-edit interrupt all collapse to `status: parked`. The console classifies the real situation from the *current step's* `status` + `notes`, and maps that to the correct recommended action (§5, FR-5).
- **D4 — Poll + SSE, not watchdog + WebSocket.** mtime polling needs no new dependency and is trivially cheap on a handful of files; SSE fits the one-directional state→browser push (all client→server actions are discrete REST POSTs) and reconnects automatically. Both avoid new deps.
- **D5 — Server-rendered (Jinja + HTMX), not a SPA.** `jinja2` is already installed transitively (fastapi/litellm pull it) and the console makes it an **explicit** dependency (FR-11.3); HTMX is one vendored static file. The data is document-shaped and server-authoritative; partial HTML swaps over SSE match it without a build toolchain — consistent with the thin-by-design ethos.
- **D6 — Inherit safety, don't re-implement it.** Because control = CLI verbs, the judge is never bypassed, the reviewer-read-only / no-push-to-main / don't-mutate-approved-artifacts guards all still execute *inside* the child process, and the console literally cannot reach past the CLI to weaken them.
- **D7 — Exactly one sanctioned engine change: the active-run guard.** The console's *read* surface needs zero engine changes (it only parses on-disk state). The one deliberate change in `RunManager` is an advisory, **repo/worktree-scoped active-run lock** (FR-10.5/OQ-1) that fail-closes `start`/`resume`/`approve` when *any* run (same slug or different) is already being driven against that worktree by a live process. This is real enforcement in the engine — not a UI heuristic — and it benefits the bare CLI too (it generalizes today's per-slug start-only guard into a worktree-global one). Everything else the console does stays strictly above the orchestrator.

---

## 5. Functional Requirements

### FR-1: Run list / dashboard
- **FR-1.1** List **every slug directory** found under the configured run root (`runs/`, or `.gauntlet/runs/` in adopter repos), one row per slug showing its **latest or active** run — including slugs whose newest run is long-finished. Columns: slug, status, current step (+ its status), cost-to-date, age/last-update, owned/observed badge. Per-slug run history is reachable from the detail view (FR-2.4).
- **FR-1.2** Default sort is most-recently-updated first. Support sort by status and by slug, a status filter (running / parked / failed / done / aborted), and a free-text search over slug and branch.
- **FR-1.3** Rows update live (no manual refresh) as runs progress, via the SSE channel (FR-8).
- **FR-1.4** A run the console launched shows an **owned** badge; a run started elsewhere shows **observed**. A run believed to be actively running in another process shows **running (external)** and disables resume/approve (FR-10.5).

### FR-2: Run detail & process view
- **FR-2.1** For a selected run, render the **step tree** from `manifest.steps`: id, type, status, agent, iteration, started/ended → duration, token/cost usage, and notes. `adversarial_cycle` steps expand to their nested rounds (`r<N>-{review,triage,fix,confirm}`).
- **FR-2.2** Show a header summary: run status, branch → base, totals, per-agent usage, and any `warnings`.
- **FR-2.3** Show a clear "where it is now" indicator derived from `manifest.current_step`, and a **recommended-action banner** (FR-5) when the run is parked or failed.
- **FR-2.4** **Full history browsing.** For any slug, list all prior `run-<timestamp>` directories and open any past run **read-only** — its step tree, transcripts, findings/triage/confirm, gate decisions, phase diffs, judge-audit, and cost — so a completed or failed PRD run can be reviewed long after it finished (a primary reason to keep `gauntlet serve` running, FR-12.3). **Every per-run endpoint and view accepts an optional `run_id` selector** (`?run_id=run-<timestamp>`; the run-list/detail endpoints in §6 honor it, as do `/steps`, `/gate`, `/diff`, and `/report`); when omitted it defaults to the slug's **latest/active** run. An unknown `run_id` for the slug returns 404. Without this selector the API could only ever address the latest run, which would make the FR-2.4 full-history requirement unsatisfiable at the API layer — so honoring `run_id` on all run/step/gate/diff/report routes is normative, not optional.

### FR-3: Step log & transcript drill-down
- **FR-3.1** For any step, list and open its artifacts: `prompt.md`, `transcript.md` (rendered markdown), `events.jsonl` (parsed), and any structured output (`findings.json`, etc.).
- **FR-3.2** For a *running* step, live-tail its `events.jsonl`/`transcript.md` via SSE (byte-offset deltas).
- **FR-3.3** For an **owned** run, also expose the supervisor-captured combined stdout/stderr log (`.serve/…log`) — the thing that today scrolls past in a backgrounded terminal.
- **FR-3.4** Expose the run's `judge-audit.jsonl` decisions in a readable view (tool, decision, source, rationale, latency), so a judge-driven denial is diagnosable.

### FR-4: Human-gate review & decision
- **FR-4.1** Detect a pending gate: `manifest.current_step` resolves to a step with `status == parked` and `type == human_gate`.
- **FR-4.2** Resolve the gate's `show:` list (read from the run's snapshot `pipeline.yaml`) into rendered content, resolving each name first against `run_dir/artifacts/<name>` (where cycles write `findings.json`/`triage.json`/`confirm.json`) then the slug-dir artifact root (`prd.md`/`plan.md`).
- **FR-4.3** Render findings/triage/confirm as readable tables (id, severity, category, location, claim, verdict), markdown artifacts as markdown, and offer a **phase diff** view. The `from`/`to` SHAs are selected deterministically from the manifest so every gate reviewer sees the same range:
  - **Commit grouping.** Each `manifest.commits[]` entry carries `{step_id, phase, sha}` where `phase` is the `PN[.x]` prefix. The commits *belonging to a phase* are all entries whose `phase` shares the same `PN` base (so `PN`, `PN.1`, `PN.2` — i.e. the phase commit plus every post-review **fix-round** commit — are grouped together), in manifest order.
  - **`to`** = the **last** such commit's `sha` (the newest fix round if any, else the phase commit). **`from`** = the commit immediately *preceding* that group: the `sha` of the prior phase's last commit, or, when the phase is the **first** to commit, the gated step's `base_sha` (HEAD before the phase touched the worktree). This makes the diff "everything this phase added, including fixes," against the state before it began.
  - **Implementation-phase gate** → the range above. **Plan/PRD (document-cycle) gate** → the same rule applied to the artifact's cycle commits; if the cycle produced commits, diff base→latest of that artifact, otherwise (see empty case) render the artifact's current content.
  - **Empty / no-diff cases.** If the phase has **no commits yet** (e.g. a gate before the first phase commit exists, or a document gate whose artifact was edited in-tree but not yet committed), the view shows an explicit **"no committed diff for this gate"** state and falls back to rendering the relevant artifact content — never a misleading empty or whole-repo diff.
- **FR-4.4** Offer **Approve** (optional notes) and **Reject** (required notes) actions that map to `gauntlet approve` / `gauntlet reject` (FR-6). The console must not invent a third gate action — the engine only supports approve/reject.
- **FR-4.5** Surface FR-10.4 **upstream conflicts**: if the parked step's notes indicate the agent signalled `UPSTREAM CONFLICT`, show the agent's conflict text from the transcript and frame it as a human reconciliation decision, not a resume.

### FR-5: Failure diagnosis & resume intelligence
- **FR-5.1** Provide a pure classifier `resume_intel(manifest) → {state, recommended_action, rationale, available_controls}` computed from the current step's `status` + `notes`. The primary discriminator is the **existing `StepRecord.status` enum** — `engine/manifest.py` already defines `parked`, `interrupted`, `halted`, `failed`, `done`, `pending`, `running`, `skipped`; the classifier reads these directly and uses `notes` substrings **only** to sub-classify cases the status enum already collapses (e.g. distinguishing a *timeout* `halted` from a *budget* `halted`, which share one status). v1 therefore adds **no** new manifest fields (OQ-2): every status it keys on already exists, and only the within-`halted` halt-kind disambiguation relies on note text. A structured `halt_kind`/`gate_kind` enum on `StepRecord` (which would remove even that residual string-matching) is a future hardening, not scheduled. The classifier is table-tested over fixture manifests so a note-wording change is caught by a failing test, not silently mis-classified.
- **FR-5.2** Mapping (detect → offer). Each predicate keys on the run being `parked`/`failed` (run status) **plus the current step's existing `StepRecord.status` enum value**; only the two `halted` rows further inspect `notes` to tell timeout from budget:
  - run `parked` + step `type == human_gate` (step `status == parked`) → **Approve / Reject** (FR-4); resume is *not* the verb.
  - run `parked` + step `status == interrupted` (mid-edit, dirty vs `base_sha`; `interrupted` is an existing `StepRecord.status`, manifest.py) → **Resume** (engine applies its `interrupted_step` reset/park policy); show that partial work is preserved.
  - run `parked` + step `status == halted` with a **timeout** note → **Resume re-triggers the same timeout** — say so explicitly; offer config *guidance* to raise the timeout first (text, not auto-applied; editing the snapshot pipeline would break the resume hash guard, so this is a profile/config change).
  - run `parked` + step `status == halted` with a **budget** note → same honesty; offer "raise `budget_usd` first" guidance.
  - failed + step `failed` (test failure / agent crash / invalid commit / missing completion signal) → **Resume will not help**; surface the step log + failing diff; the fix happens outside the console.
  - failed + step note `rejected:` → terminal; offer abort/clean guidance or a fresh run after addressing feedback.
- **FR-5.3** Never offer a control that the manifest state makes meaningless (e.g., no Approve unless a gate is actually parked).

### FR-6: Run supervision (owned runs)
- **FR-6.1** Launch a new run from the console (`POST` → `gauntlet run <slug> [--pipeline …] [--no-judge]`) as a managed `Popen` child of the CLI.
- **FR-6.1a** **Run-dir allocation handshake (engine-adjacent).** For a *new* `run`, the timestamped `run_dir` is normally minted *inside* the child after `Popen`, so the supervisor would not know where to put the FR-6.3 log or the FR-6.4 `job.json`. To close this, the supervisor **pre-allocates the `run_id` and passes it to the child** as `gauntlet run <slug> --run-id <run-YYYY-MM-DDThh-mm-ss>` (equivalently the `GAUNTLET_RUN_ID` env var); the engine, when given a `run_id`, uses it verbatim instead of minting its own (and errors if a run dir of that id already exists, so the id stays single-use). The supervisor derives `run_dir` from the pre-allocated id, `mkdir -p`'s `run_dir/.serve/` **before** launch, and so can open the captured log and write `job.json` atomically at launch. For `resume`/`approve`/`abort` the run dir already exists (via `active-run.txt`), so the supervisor resolves `run_dir` by reading the slug's active-run pointer — no pre-allocation needed.
  - **Fallback / pre-manifest window.** If pre-allocation is unavailable, the supervisor writes the child's combined output to a **bootstrap log outside the run dir** (`<run_root>/.serve-bootstrap/<slug>-<pid>.log`) and a provisional `job.json` there, then **migrates** both into `run_dir/.serve/` once the manifest for that run appears (watcher tick). A child that **crashes before any run dir/manifest exists** therefore still has its stdout/stderr captured in the bootstrap log (so the failure is diagnosable, not lost), and the supervisor classifies it as a failed launch rather than an orphaned owned run.
  - **Tests (P3/P4, §8):** a normal launch writes the log + `job.json` under the pre-allocated `run_dir/.serve/` from the first byte; a child killed **before** the manifest is written leaves a readable bootstrap log and is classified as a failed launch (no phantom owned run).
- **FR-6.2** Issue `approve`, `reject`, `resume`, `abort` as child `gauntlet <verb>` processes. Note: `approve`/`resume` *drive the rest of the run* and are therefore long-lived owned processes, handled with the same lifecycle as `run` (not quick RPCs).
- **FR-6.3** Capture each child's combined stdout/stderr to `run_dir/.serve/…log` (under the run dir's self-ignoring `.gitignore`, so it never dirties the worktree).
- **FR-6.4** Track owned runs via on-disk `run_dir/.serve/job.json` (`{pid, pgid, verb, started_at, log_path, proc_create_time}`); reap on `.stop()` via process-group kill (terminate → wait → kill). `proc_create_time` is the **OS process-creation timestamp** of `pid` (not the wall-clock `started_at` of the job), captured at launch and used by FR-7.2 to defeat PID reuse. It is read without adding a dependency: Linux from `/proc/<pid>/stat` field 22 (clock ticks since boot), macOS/BSD from `ps -o lstart= -p <pid>` (or `ps -o start=`). If the platform value cannot be obtained at launch, `proc_create_time` is recorded as `null` and FR-7.2 treats the run as **unverifiable → fail-closed orphan** on re-attach.
- **FR-6.5** `--no-judge` is exposed only as the same explicit unsafe-testing flag the CLI has, defaulted **off**, with a visible warning when used.

### FR-7: Crash survival & re-attach
- **FR-7.1** On `gauntlet serve` startup, re-discover owned runs by scanning run dirs for `.serve/job.json`.
- **FR-7.2** For each, liveness-check the PID against PID reuse: a PID is treated as **the original live process only if** `os.kill(pid, 0)` succeeds **and** the *current* OS process-creation timestamp of `pid` matches the recorded `proc_create_time` (FR-6.4) within a small tolerance. (`os.kill(pid, 0)` + a wall-clock job `started_at` is **insufficient** — a reused PID would pass it; this is why `proc_create_time` exists.) Outcomes: match → re-attach (reopen the captured log for tailing); PID dead, *or* alive-but-`proc_create_time`-mismatch (reuse), *or* `proc_create_time == null` (unverifiable) → **fail closed**: classify **owned, interrupted — resume available** for a non-terminal manifest and remove the stale sidecar (recovery is then `resume`, FR-7.3).
- **FR-7.3** Re-attach must be the *same* recovery path as a `kill -9`'d run: state comes from the manifest, recovery is `resume`. The server never persists authoritative run state of its own.

### FR-8: Live state propagation
- **FR-8.1** A single watcher polls each known `manifest.json` at ~1s cadence and emits an edge-triggered event on any transition. **Event identity is `(run_id, current_step, current_step_status, run_status, manifest_revision)`** — *not* `(run_id, run_status)`, which is too coarse: a run legitimately re-enters the same run-level `status` many times (e.g. `parked` at successive human gates across phases, or repeated `halted`/`failed` step states), and keying only on `(run_id, status)` would emit the first such transition and silently swallow every later one — breaking G3/G4 and FR-9.1. `manifest_revision` is the manifest's monotonically-changing marker (its `mtime` to nanosecond precision, or a write counter if added later — no new field is required for v1, `mtime` suffices); including `current_step` + its status distinguishes "parked at gate A" from "parked at gate B." The watcher remembers the last-seen identity tuple per `run_id` and emits only when the tuple changes, so each distinct transition is observable exactly once.
- **FR-8.2** Push transitions to connected browsers via SSE (`GET /events` for list-level, per-stream endpoints for log tail). The UI updates without manual refresh; a dropped SSE connection auto-reconnects and re-reads current state.
- **FR-8.3** Freshness target: a state change is reflected in an open UI within ~2s p95.

### FR-9: Notifications
- **FR-9.1** Fire on three transition *kinds*: **gate-reached** (parked at a `human_gate`, distinct from a halt), **run-failed**, **run-completed**. Edge-triggered and de-duplicated **per (run_id, transition-kind, current_step)** — *not* per `(run_id, status)` — so that a run parking at **multiple** human gates across phases notifies **once per gate** (each has a distinct `current_step`), and sequential failures/halts are each surfaced, while a mere re-poll of an unchanged manifest never re-notifies. This is the notification-layer counterpart of the finer event identity in FR-8.1; collapsing to `(run_id, status)` here would suppress every gate after the first and violate G3/G4.
- **FR-9.2** Channels: **macOS desktop** (`terminal-notifier` if on PATH, else `osascript`), **Slack** (incoming-webhook POST), **browser in-tab** (SSE-driven `Notification`). Each message carries slug, run_id, new status, current step + note, and a deep link to `/runs/<slug>`.
- **FR-9.3** **Fail-soft:** every channel is wrapped so a notification error is logged and swallowed — it can never affect a run. (The notifier lives in the watcher, which owns no run state.)
- **FR-9.4** Configurable via a new optional `web:` block in `config.yaml` plus env fallbacks (e.g. `GAUNTLET_SLACK_WEBHOOK`), additive and backward-compatible. Per-channel on/off.

### FR-10: Safety & invariant preservation
- **FR-10.1** All control actions launch sanctioned `gauntlet` CLI verbs; the console never calls orchestrator internals in-process and never edits artifacts, git, or the judge.
- **FR-10.2** The judge is never bypassed: each child run constructs its own `ManagedJudge`; the console has no judge knob beyond surfacing the existing `--no-judge` flag.
- **FR-10.3** The reviewer-read-only contract, "nothing lands on `main`", and "don't mutate approved artifacts" remain enforced inside the child process; the console cannot weaken them. No merge/push/finish/rollback controls are exposed.
- **FR-10.4** Bind `127.0.0.1` only (refuse non-loopback hosts, mirroring the judge). Require a per-serve token (constant-time compared, env-overridable `GAUNTLET_WEB_TOKEN`, distinct from the judge token), printed on startup. Because a browser cannot attach custom headers to SSE streams or native form POSTs, the session is carried in an **httpOnly cookie** rather than the judge's `X-Gauntlet-Token` header — which is precisely why state-changing POSTs also carry a CSRF token (FR-10.6) the header-based judge does not need. The full provisioning flow is specified so it is implemented one way:
  - **Cookie provisioning (login exchange).** The browser obtains the cookie through a one-time exchange, **never** by the token appearing in a URL. `GET /login` serves a minimal form that accepts the serve token; `POST /login` constant-time-compares it to the serve token and, on success, sets `Set-Cookie: gauntlet_web=<session-value>; HttpOnly; SameSite=Strict; Path=/; Secure-omitted-for-loopback-http`, then redirects to the originally requested path. The convenience startup URL (and `--watch`'s printed/opened URL) point at `/login` with the token in a **one-time POST-prefilled** form, not a `?token=` query — so the token is never placed in a URL, browser history, the SSE handshake URL, or server logs. `/login` and `/healthz` are the only unauthenticated routes.
  - **Cookie attributes.** `HttpOnly` (no JS access), `SameSite=Strict` (defense-in-depth against cross-site form posts on top of FR-10.6), `Path=/`, host-only; no `Domain`. The cookie value is the serve token (or a per-serve random session id mapped to it server-side); validated constant-time on every gated request, including the SSE `GET` and every POST.
  - **API-client auth.** Non-browser callers to `/api/*` may authenticate **either** with the same cookie **or** by sending the serve token in the `X-Gauntlet-Token` header (parity with the judge); header-authenticated `/api/*` POSTs are exempt from the CSRF check (FR-10.6) because they are not browser-form-driven and cannot be CSRF'd. The token must never be passed as a query parameter on any route.
- **FR-10.5** **Active-run guard (engine — the one sanctioned engine change, OQ-1).** Add an advisory **run-lock** to `RunManager` whose scope is the **git repository / worktree, not a single slug or run**. This is the spec-level safety invariant from §2.2 ("one *actively-controlled* run at a time") and R1 ("two orchestrators against one worktree"): the lock guards the *worktree*, so it is impossible to begin driving slug **A** while slug **B** is already being driven against the same worktree — the existing per-slug `_refuse_if_active_run` (it only inspects one slug's `active-run.txt`) does **not** provide this and is the gap this FR closes.
  - **Location & identity.** A single lockfile at the worktree-resolved run root — `<run_root>/.driving.lock` (i.e. `runs/.driving.lock`, or `.gauntlet/runs/.driving.lock` in adopter repos), one per repo/worktree, gitignored. It records `{slug, run_id, pid, pgid, started_at, host, proc_create_time}` for the process currently *driving* (where `proc_create_time` is the OS process-creation timestamp from FR-7.2, used to defeat PID reuse). Because there is exactly one lockfile per worktree, holding it for slug B blocks a driving verb for slug A by construction.
  - **Acquisition order.** A driving verb (`start` / `resume` / `approve`) acquires the worktree lock **first**, *before* it mints or opens any run dir, writes `active-run.txt`, or touches git — atomically via `O_CREAT|O_EXCL` (create-exclusive) so the check-and-acquire is a single filesystem operation with no TOCTOU window. Only after the lock is held does the verb proceed; it is released when the run parks at a gate, completes, errors, or the process exits.
  - **Fail-closed behavior.** If the lock is already held by a **live** pid (verified by `os.kill(pid, 0)` **and** matching `proc_create_time`, FR-7.2), the verb **fails closed** regardless of whether it targets the same or a different slug — message: "worktree is being driven by `<slug>/<run_id>` (pid N); wait, or abort that run first." A lock held by a **dead** pid (the `kill -9` case) — or a live pid whose `proc_create_time` no longer matches (PID reuse) — is treated as stale and reclaimed.
  - **UI behavior.** While *any* run holds the worktree lock, the console shows that run as **running (external)** if the console does not own it, and **disables `Launch run`, `Resume`, and `Approve` for every slug in the repo** (not just the driven one), with a banner naming the holder; the actions re-enable when the lock is observed free. The console only *surfaces* this; the *enforcement is the engine lock*, never a UI mtime heuristic. (A foreground `gauntlet run`/`--watch` releases the lock the instant it parks at a gate, so the console can then drive it forward via a sanctioned `approve`/`resume` child without contention.)
  - **Tests (added in P3, §8).** Beyond the same-slug case: a second **different-slug** `start`/`resume`/`approve` against a live-locked worktree **fails closed**; concurrent acquisition attempts resolve to exactly one holder (no double-acquire); a stale (dead-pid) and a PID-reused lock are both reclaimed; the lock is released on park/done/error/process-exit.
- **FR-10.6** **CSRF protection on all cookie-authenticated state-changing POSTs.** A CSRF token is **generated per session** (random, bound to the auth cookie/session — server stores or HMACs it against the session value so a token from another session is rejected), surfaced to the page (a `<meta>` tag and a hidden `_csrf` form field), and **validated** on every cookie-authenticated POST: the submitted `_csrf` (form field or `X-CSRF-Token` header for HTMX) must match the session-bound value (constant-time) **and** the `Origin`/`Referer` must be same-origin (loopback). The token is **rotated** on login (new session ⇒ new CSRF token); a missing/mismatched token fails the POST closed. Requests authenticated by the `X-Gauntlet-Token` header instead of the cookie (FR-10.4 API-client path) are exempt, since header auth is not ambient and cannot be forged cross-site.
- **FR-10.7** **Confirmation for destructive verbs (OQ-5).** Loopback + token (FR-10.4) is the *security* boundary — it stops unauthorized callers. On top of it, the UI requires an explicit **confirmation step** for the two *destructive* actions, **abort** (kills an in-flight, often multi-hour/expensive run) and **reject** (fails a gate), to prevent accidental loss from a misclick. This is UX-safety, not a security control (it adds nothing against a caller who already holds the token). `approve`/`resume` are non-destructive and need no extra confirmation.

### FR-11: Configuration & deployment
- **FR-11.1** `gauntlet serve [--host 127.0.0.1] [--port N]` resolves config exactly like the CLI (`RunConfig.load(.gauntlet/config.yaml)`, `asset_root` fallback `"."`), and must be run inside a git repo (validated at startup, fail-closed otherwise).
- **FR-11.2** v1 scopes to a single *repository* (the `cwd`) — but **all** of that repo's slugs and run history are browsable (FR-1.1, FR-2.4); see the §2.2 / OQ-4 clarification. The token is printed on startup like the judge's.
- **FR-11.3** Minimal new dependency footprint. FastAPI + uvicorn are already **direct** dependencies; `httpx` and `jinja2` are currently only **transitive** (pulled in by fastapi/litellm today), so the console **promotes them to explicit `pyproject.toml` dependencies** rather than relying on a transitive graph that a future version bump could silently drop. HTMX is vendored as a static asset; no `watchdog`, no `websockets`, no frontend build toolchain. Net: two already-installed deps made explicit, zero new heavy deps.

### FR-12: Launch ergonomics (OQ-6)
- **FR-12.1** `gauntlet run <slug> --watch` **ensures a console is running** (boots one on `127.0.0.1` if none is up, otherwise reuses the existing instance per the discovery rule in FR-12.4), prints (and optionally opens) its URL, then runs the pipeline in the foreground exactly as today. The console **observes** the run live (FR-1/FR-2/FR-3) and notifies on gate/failure/completion (FR-9). Because the foreground driver releases the active-run lock when it parks (FR-10.5), the operator can act on the gate from the console — which drives the run forward via a sanctioned `approve`/`resume` child — with no contention.
- **FR-12.2** The console started by `--watch` **persists after the foreground run returns** (it is detached — see FR-12.4 — so it is *not* a child of the foreground `run` and does not die with it), so gates, failures, and post-run review remain available in the UI rather than disappearing when the blocking command exits.
- **FR-12.4** **Console discovery & lifecycle (normative).** `serve` and `run --watch` coordinate through a single on-disk **console registry** so a second `--watch` reuses, never duplicates, a console:
  - **Registry location & contents.** One file per repo/worktree at `<run_root>/.console.json` (i.e. `runs/.console.json` or `.gauntlet/runs/.console.json`), gitignored, written atomically (`os.replace`). It records `{pid, pgid, proc_create_time, host, port, url, token_fingerprint, started_at, log_path}`. `token_fingerprint` is a non-reversible hash of the serve token (never the token itself), used for compatibility checks; the token itself is never persisted to the registry.
  - **Discovery & reuse.** `run --watch` (and a second `serve`) reads the registry and reuses the recorded console **iff** it is live (PID alive **and** `proc_create_time` matches, per FR-7.2 PID-reuse rules) **and** its `/healthz` responds on the recorded host/port. On reuse it prints the existing URL; it does **not** boot a second console. A registry entry whose PID is dead/reused or whose `/healthz` does not answer is **stale** and reclaimed (the booting process overwrites it).
  - **Token compatibility.** A reused console keeps its **own** existing token (the one it printed at boot); `--watch` prints that console's `/login` URL. If the caller supplied a `GAUNTLET_WEB_TOKEN` that disagrees with the live console's `token_fingerprint`, `--watch` does **not** restart it — it prints a clear note that the running console uses a different token and surfaces the live console's login URL (the running session is authoritative; never silently kill a console serving other runs).
  - **Port collision.** Boot binds the configured/default port; if it is in use by *our* live registered console, reuse it (above); if in use by an **unrelated** process, fail closed with the conflict (do not silently pick a random port that the printed URL/notifications would then mismatch). An explicit `--port` is honored and, on collision with an unrelated process, errors.
  - **Detach & logs.** A console booted by `--watch` is **detached** (`start_new_session=True`, its own process group) so it outlives the foreground `run`; its stdout/stderr go to `log_path` (`<run_root>/.console.log`), not the foreground terminal. `gauntlet serve` run in the foreground stays attached and logs to its own stdout as today.
  - **Shutdown / reclamation.** There is no auto-shutdown in v1 (the console is meant to persist for history review, FR-12.3); it is stopped by the operator (Ctrl-C for a foreground `serve`, or signalling the recorded `pgid`). On its own clean exit a console removes its registry entry; a crashed console leaves a stale entry that the next discovery reclaims as above.
- **FR-12.3** `gauntlet serve` remains a **first-class standalone command**: start it any time to browse history, review past runs across all slugs (FR-1.1/FR-2.4), or supervise without launching a new run. `run --watch` is the convenience path; `serve` is the durable one.

---

## 6. Data, State Model & API Surface (normative excerpts)

**Read model — all derived from the existing `manifest.json` + on-disk artifacts (no engine changes):**

| Endpoint | Returns |
|---|---|
| `GET /api/runs` | All runs: `[{slug, run_id, status, current_step, current_step_status, current_step_notes, started, ended, totals, branch, base_branch, owned, attached, n_steps, n_done, warnings_count}]`; query `?status= &slug= &q= &sort=` |
| `GET /api/runs/{slug}[?run_id=…]` | Full manifest (`steps[]` tree, `commits[]`, `totals`, `agent_usage`, `warnings`) + computed `resume_intel`. `?run_id=` selects a historical run (FR-2.4); default = latest/active; unknown id → 404 |
| `GET /api/runs/{slug}/steps/{step}[?run_id=…]` | Step detail: which artifacts exist (sizes); for cycles, the nested round dirs |
| `GET /api/runs/{slug}/steps/{step}/log[/stream]` | Tail of `events.jsonl`/`transcript.md` (`?from=<offset>`, `?run_id=…`); `/stream` = SSE of appended lines; owned runs also expose the `.serve` job log |
| `GET /api/runs/{slug}/gate[?run_id=…]` | Resolved gate view: `{gate_id, notes, artifacts:[{name, kind, content_or_parsed}]}` |
| `GET /api/runs/{slug}/diff?from=<sha>&to=<sha>[&run_id=…]` | Unified phase diff + per-commit log. `from`/`to` are optional: when omitted, the server selects them by the FR-4.3 phase-grouping rule for the run's current gate (phase commits + `PN.x` fix rounds vs. the pre-phase base), and returns the **"no committed diff"** sentinel for the empty case. Explicit SHAs (from `manifest.commits[]`) override. `?run_id=` selects a historical run |
| `GET /api/runs/{slug}/report[?run_id=…]` | Cost breakdown (reuse the existing report renderer) |
| `GET /events` | SSE: run-list-level transitions |

**Control surface — each launches a `gauntlet <verb>` child (sanctioned, audited):**

| Endpoint | Child command |
|---|---|
| `POST /api/runs` `{slug, pipeline?, no_judge?}` | `gauntlet run <slug> --run-id <pre-allocated> [--pipeline …] [--no-judge]` (run-dir handshake, FR-6.1a) |
| `POST /api/runs/{slug}/approve` `{gate?, notes?}` | `gauntlet approve <slug> [--gate …] [--notes …]` |
| `POST /api/runs/{slug}/reject` `{notes, gate?}` | `gauntlet reject <slug> --notes … [--gate …]` |
| `POST /api/runs/{slug}/resume` | `gauntlet resume <slug>` |
| `POST /api/runs/{slug}/abort` | `gauntlet abort <slug>` |

All artifact/file reads are path-contained under the repo root (reject `..`), mirroring the engine's existing containment posture.

**Core UI views:** (1) Run list, (2) Run detail / step tree, (3) Step log viewer, (4) Gate review panel (artifacts + diff + approve/reject), (5) Failure / resume panel.

---

## 7. Security & Privacy

- **Loopback-only bind + token auth**, mirroring the judge service's loopback + constant-time-token model (token printed on startup; env override). Token *delivery* differs by client (FR-10.4): a browser exchanges the serve token for an `HttpOnly; SameSite=Strict` session cookie via a one-time `POST /login` (the token is **never** put in a URL/query/history/SSE-handshake), then SSE/HTMX ride the cookie; non-browser `/api/*` callers may instead send the `X-Gauntlet-Token` header like the judge. Cookie-authenticated state-changing POSTs additionally carry a session-bound, rotated **CSRF token** validated same-origin (FR-10.6); header-authenticated API POSTs are CSRF-exempt. `/login` and `/healthz` are the only unauthenticated routes; everything else is gated.
- **No new trust boundary into the run:** control is the CLI; the judge still gates every tool call inside each child.
- **No secrets surfaced:** the console reads the same redaction-cleaned transcripts the engine already writes; it does not read credential files.
- **Containment:** all file reads are repo-relative and reject path traversal.

---

## 8. Implementation Plan (phased, assumption-validating)

Each phase validates one assumption, ends in a commit, and grows the test suite (`uv run pytest`; HTTP via FastAPI `TestClient` mirroring `test_judge_service.py`; lifecycle via subprocess+SIGKILL mirroring `test_resume_crash.py`). Per the bootstrap switchover rule, build this *through* gauntlet where possible.

- **P1 — Read-only observer MVP.** *Assumption: the manifest + on-disk layout suffice to render full run state with zero engine changes.* Deliver `web/store.py`, `web/service.py` + `web/runner.py` (FastAPI clone of the judge: loopback + token + `/healthz`), the `gauntlet serve` command, read endpoints (`/api/runs`, `/api/runs/{slug}`, `/api/runs/{slug}/steps/{step}`), and minimal Jinja run-list + run-detail pages. *Test:* `TestClient` over fixture run dirs; loopback/token guards.
- **P2 — Live freshness (poll + SSE).** *Assumption: ~1s mtime polling catches every transition; SSE pushes to the browser with no new dep.* Deliver `web/watcher.py`, `GET /events`, HTMX-live list rows + live log tail. *Test:* drive a fixture manifest through writes, assert one edge-triggered event per transition using the **`(run_id, current_step, current_step_status, run_status, manifest_revision)` identity** (FR-8.1) — including a fixture that parks at two distinct gates and asserts both transitions emit (not collapsed); assert SSE yields them.
- **P3 — Supervised launch + active-run lock.** *Assumption: a run launched as a `Popen` of `gauntlet run` is fully observable, controllable, reapable — and can never be double-driven.* Deliver `web/jobproc.py` (`RunProcess`) + `web/supervisor.py`, the **run-dir allocation handshake** (`gauntlet run --run-id`, FR-6.1a), `POST /api/runs`, `POST …/abort`, owned/observed badge, **and the one engine change: the repo/worktree-scoped `RunManager` active-run lock (FR-10.5)** guarding `start`/`resume`/`approve`. *Test:* launch a trivial fixture pipeline via the supervisor (assert `job.json` incl. `proc_create_time`, running→done, captured log under the pre-allocated `run_dir/.serve/`, process-group reap); a child killed **before** the manifest exists leaves a readable bootstrap log and is classified a failed launch (FR-6.1a); for the lock — a second `resume`/`approve` **and a different-slug `start`** against a live-locked worktree both fail closed, concurrent acquisition yields exactly one holder, and a stale (dead-pid or PID-reused) lock is reclaimed.
- **P4 — Re-attach + crash survival.** *Assumption: the server holds no authoritative state — restart re-discovers owned runs; an orphan is recoverable like `kill -9`.* Deliver startup re-discovery, **PID-reuse-safe liveness check (`os.kill` + `proc_create_time` match, FR-7.2)**, re-attach, interrupted classification. *Test (headline):* launch owned run, SIGKILL the server group, start a fresh server, assert re-discovery + correct classification + `resume` recovery to `done` with exactly one set of effects (parametrize kill timing); assert a job whose `proc_create_time` no longer matches (simulated reuse) or is `null` is classified fail-closed orphan, not re-attached.
- **P5 — Gates + control actions.** *Assumption: the UI can resolve a parked gate's `show:` artifacts and drive approve/reject/resume via sanctioned verbs without bypassing any invariant.* Deliver `/api/runs/{slug}/gate`, `…/diff` (with the **deterministic phase-diff commit selection** of FR-4.3, incl. the no-committed-diff fallback), `POST …/approve|reject|resume`, `resume_intel`, the gate-review + failure/resume panels, and the **destructive-verb confirm step** for abort/reject (FR-10.7). *Test:* fixture run parked at a gate with real artifacts; assert resolved contents; assert the diff selects phase+`PN.x` fix commits against the pre-phase base and returns the empty-case sentinel when no phase commit exists; assert approve launches `gauntlet approve` (inspect argv) + transition; table-driven `resume_intel` over each manifest shape (keyed on the existing step-status enum, FR-5); assert abort/reject require the confirm token.
- **P6 — Notifications.** *Assumption: one watcher trigger fans out to macOS/Slack/in-tab, fail-soft, edge-triggered, with no run impact.* Deliver `web/notify.py` wired to the watcher; `web:` config + env. *Test:* inject transitions with a stub notifier; assert one fan-out per `(run_id, transition-kind, current_step)` (FR-9.1) — including a fixture that parks at **two** gates in different steps and asserts **two** gate notifications, not one; assert a re-poll of an unchanged manifest re-notifies zero times; assert a raising notifier is swallowed; Slack call shape via mock transport.
- **P7 — Polish + ergonomics.** *Assumption: safe and pleasant for daily supervision, history review, and one-command watching.* Deliver list search/sort/filter, full-history browser per slug with **`run_id` selection on all per-run endpoints** (FR-2.4), `gauntlet run --watch` (boot/reuse console via the **console registry**, FR-12.4), the **`/login` token-exchange + httpOnly session cookie** and **session-bound CSRF** (FR-10.4/10.6), `--no-judge` warning, report view, empty/error states. *Test:* `run --watch` boots a console, records `.console.json`, and a **second** `--watch` reuses it (no second process) while a stale registry entry is reclaimed; the run appears as observed then drives to a gate; `?run_id=` opens a historical run read-only and an unknown id 404s; `POST /login` sets the cookie and a token never appears in any URL; a cookie POST without a valid session CSRF token is rejected while an `X-Gauntlet-Token` API POST is accepted; an `@pytest.mark.integration` end-to-end smoke (launch → gate → approve → done through the UI).

---

## 9. Success Metrics

- **M1 — Time-to-awareness of a gate/failure** drops from "next time I happen to check" to "within seconds" (notification fires; FR-9).
- **M2 — Clicks/commands to act on a gate** drop from (poll status → open RUN.md → open N artifact files → type approve) to (open the surfaced gate → read assembled evidence → click Approve).
- **M3 — Zero invisible runs:** every console-launched run is observable end-to-end and survives a server restart (FR-7 re-attach test passes).
- **M4 — Zero new invariant violations:** the safety test suite (judge gating, read-only reviewer, no-push-to-main) is unaffected, because control = CLI verbs (FR-10).
- **M5 — Dependency budget:** no new runtime dependency beyond already-resolved ones (FR-11.3).
- **M6 — Post-mortem reach:** any past run of any slug is fully reviewable in the UI (step tree, transcripts, gate decisions, diffs, cost) without opening the filesystem (FR-1.1, FR-2.4).

## 10. Risks & Mitigations

- **R1 — Two orchestrators against one worktree.** Was the highest risk (`resume`/`approve` had no active-run guard, and the start-only guard was per-slug — it could not stop slug A starting while slug B drove the same worktree). **Resolved by design (OQ-1):** a **repo/worktree-scoped** active-run lock (FR-10.5) fail-closes all three driving verbs across *all* slugs in the repo; the console only surfaces it. Residual risk is the lock's own staleness handling (see R2).
- **R2 — Server-restart PID reuse.** *Mitigation:* store the **OS process-creation timestamp** (`proc_create_time`, not the job's wall-clock `started_at`) + pgid in `job.json` (FR-6.4), captured via `/proc` or `ps` with no new dependency; on re-attach a PID counts as live only if both `os.kill(pid,0)` and a matching `proc_create_time` agree (FR-7.2). When in doubt — mismatch or an unrecordable timestamp — treat as dead/interrupted (fail closed; the engine's resume guards then vet).
- **R3 — Halt/gate disambiguation relies on note substrings.** Accepted for v1 (OQ-2). *Mitigation:* the `resume_intel` classifier is table-tested over fixture manifests so a note-wording change fails a test rather than silently mis-classifying; a structured `halt_kind`/`gate_kind` enum is a tracked future hardening, not scheduled.
- **R4 — Web Push complexity.** *Mitigation:* in-tab notifications in v1; defer service-worker/VAPID push to v2.
- **R5 — Log volume / SSE fan-out.** *Mitigation:* tail loops scoped to open viewers, byte-offset deltas, capped backfill.
- **R6 — `approve`/`resume` are long-running, not quick RPCs.** *Mitigation:* treat them with the full `RunProcess` lifecycle (designed in FR-6.2).

## 11. Resolved decisions (OQ-1…OQ-6 review, 2026-06-16)

The v0.1 open questions were resolved by the operator; recorded here as the audit trail (with the FRs they shaped).

- **OQ-1 [decided — add the guard].** Add a real active-run guard: an advisory, **repo/worktree-scoped** run-lock in `RunManager` (FR-10.5), the single sanctioned engine change (D7). It fail-closes `start`/`resume`/`approve` across *all* slugs in the worktree (not just the targeted one), so the protection is engine-enforced rather than a UI heuristic and matches the §2.2 "one actively-controlled run at a time" invariant. Downgrades risk R1 from "highest" to "resolved by design."
- **OQ-2 [deferred].** Keep `resume_intel` note string-matching for v1 (FR-5.1); a structured `halt_kind`/`gate_kind` enum on `StepRecord` is a tracked future hardening, not scheduled. Mitigated by table-tests (R3).
- **OQ-3 [decided — in-tab now, push later].** In-tab notifications in v1; background Web Push (service worker + VAPID) in v2 (FR-9.2, §2.2, R4).
- **OQ-4 [clarified].** "Single-repo" = one git repository per `serve` instance (the `cwd`) and one *actively-controlled* run at a time — **not** one slug. Every slug under the run root and its **full run history** are browsable read-only, defaulting to the latest/active run (FR-1.1, FR-2.4, FR-11.2). A cross-repo registry stays deferred.
- **OQ-5 [decided — confirm destructive verbs].** The security boundary stays loopback + token (parity with the judge); add a lightweight UI confirmation for the *destructive* verbs abort/reject to prevent accidental loss of long-running work (FR-10.7). Rationale: token + loopback stops unauthorized callers but does nothing against an authorized operator's misclick aborting a multi-hour run — the confirm is cheap insurance, not a security control.
- **OQ-6 [decided — both].** `gauntlet run --watch` boots/reuses the console then runs (FR-12.1–12.2); `gauntlet serve` stays the durable standalone command for history and review (FR-12.3).

### Remaining open questions
None blocking v1. Revisit post-v1: the structured halt/gate enum (OQ-2), background Web Push (OQ-3), and a cross-repo registry (OQ-4) if multi-repo supervision becomes common.

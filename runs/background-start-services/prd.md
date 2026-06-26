# PRD: Background service startup & the interactive run monitor

**Status:** Draft v0.1
**Author:** John Pletka
**Date:** 2026-06-25
**Working name:** background-start-services
**Relationship to existing artifacts:** Does **not** amend any approved artifact
in place. It **builds on** two predecessors and **supersedes one narrow,
named clause** of a third:

- **Builds on `operator-aids`** (sequenced *after* it): consumes its
  `status --json` / `next_actions` machine contract and its `gauntlet-operator`
  skill + `prompts/operator.md` playbook (the interactive monitor *is* that
  playbook's agent persona), and reuses its driver-liveness primitives
  (`engine/operator.py` liveness + `procident.process_is_alive`). It also adds a
  flag to the same `gauntlet status` command operator-aids extends — purely
  additive (`--interactive`), touching neither operator-aids' output rendering
  nor its `--json` contract.
- **Builds on `gauntlet-ui`** (the supervisory console, FR-11/FR-12): reuses the
  console registry (`web/registry.py`), the detached-launch supervisor
  (`web/jobproc.py` `RunProcess` + the FR-6.1a run-id reservation handshake),
  the per-serve token + `SessionStore` auth, and `ManagedJudge`
  (`engine/judgeproc.py`).
- **Supersedes `gauntlet-ui` FR-10.4/FR-10.6's "the token must never ride in a
  URL"** — and *only* that clause — to permit a **loopback-only `?p=<token>`
  query password** (§7). This is a deliberate, ratified security-posture
  relaxation, decided by the human author through *this* PRD's own review + gate;
  the gauntlet-ui PRD text is left intact as the historical record of the prior
  decision. Everything else in gauntlet-ui's auth posture (loopback-only bind,
  constant-time token compare, httpOnly session cookie, session-bound CSRF,
  same-origin on cookie POSTs) is **unchanged**.

It does **not** amend `PRD-gauntlet.md`, `policy.yaml`, or any approved
`plan.md`. **Implementation is sequenced after `operator-aids` merges** and the
branch is rebased onto it, so the two never drive the same worktree concurrently
(the single per-worktree `.driving.lock` enforces this regardless).

---

## §1 Overview

### 1.1 Problem statement

Bringing up and supervising a Gauntlet run is needlessly manual. The judge
auto-starts inside the run, but the supervisory console does not: the operator
opens a second terminal, runs `gauntlet serve`, and — when several runs are in
flight across worktrees — hand-picks a free port because the console fails closed
on a port conflict (8765) instead of finding one. Signing in means copy-pasting a
token into a login form. There is no one-command "re-open my console." And once a
run is going, nothing is *watching* it on the operator's behalf: a parked gate, a
budget halt, or an orphaned driver is discovered only by the operator manually
reading `status`/`logs`. The toil is concentrated at exactly the moments that
matter — bringing a run up, and reacting when it stalls.

Separately, the judge leaves no on-disk trace of itself. It is a child of the run
process and normally dies with it, but a `kill -9`/crash of the driver can orphan
it, and nothing records its PID or endpoint — so neither cleanup verbs nor any
out-of-process helper can find it.

### 1.2 Solution summary

Make a run's observability surface come up — and be supervised — in one command,
without ever weakening the orchestrator-level invariants the console already
inherits.

- **`gauntlet run <slug> --watch`** boots (or reuses) the per-worktree console
  and **opens an already-authenticated browser tab** (TTY-gated; `--no-browser`
  suppresses). The console **auto-selects a free loopback port** when its default
  is taken, so concurrent runs in different worktrees never collide.
- **`gauntlet serve --resume`** is the "re-open my console" verb: if a console is
  live it just opens the browser and returns; if not, it boots one **detached**,
  then opens the browser and returns.
- **The judge writes a gitignored `judge.json`** (endpoint + token + process
  identity) into its run dir, so `abort`/`finish`/`clean` can **reap an orphaned
  judge** (identity-verified, never a foreign/reused PID) — and so the monitor
  (below) can find the live judge to wire itself to.
- **`gauntlet run <slug> --interactive[=claude|codex]`** launches the run
  **detached** and hands the foreground terminal to an interactive Claude
  (default) or Codex session — the *agent operator* persona from `operator-aids` —
  wired to that run's judge as the **operator's own session** so it is
  judge-gated without permission-prompt spam. **`gauntlet status <slug>
  --interactive[=claude|codex]`** is the same monitor for a run that is *already
  running* (the "I forgot to add it" path).

### 1.3 The assumption this validates

The load-bearing belief is: **a foreground monitoring agent, wired to a run's
per-run judge with `GAUNTLET_RUN_ID` set but `GAUNTLET_STEP_ID` *unset*, is
reliably classified by the judge as the *operator's own session* — receiving the
operator's broad auto-allow (no permission prompts) without inheriting in-run
agent denials (e.g. autonomous `git push` / `gh pr create`), and without
weakening the gating of any real in-run pipeline agent.** If that classification
is wrong in *either* direction the monitor is worthless or unsafe: prompt-spammed
and over-denied (useless), or — worse — an in-run agent's restriction leaks, or a
non-operator caller is admitted. P1 attacks this assumption first, with tests on
both sides of the classification, before any launch machinery depends on it.

---

## §2 Goals and Non-Goals

### 2.1 Goals

| ID | Outcome | Need it serves |
|----|---------|----------------|
| G1 | From `gauntlet run <slug> --watch` on a TTY, the operator reaches an **authenticated** console in the browser with **zero manual paste**, at the default port or an auto-selected free one. | Kills the launch-a-second-terminal / find-a-port / copy-a-token toil. |
| G2 | `gauntlet serve --resume` re-opens the operator's console in one command — booting it detached if dead, just opening the browser if live — and returns the terminal either way. | One-command reattach; no re-deriving URL/token. |
| G3 | A run's judge is **discoverable and reapable**: its endpoint + identity are recorded on disk, an orphaned judge is terminated by the cleanup verbs (identity-verified), and a live shared console is never killed by them. | Closes the orphaned-judge gap; gives the monitor a wiring target. |
| G4 | `gauntlet run <slug> --interactive` brings up the run in the background and a foreground monitoring agent in one command; `gauntlet status <slug> --interactive` attaches the same monitor to an already-running run. | One-command supervised run; the "I forgot to add it" recovery path. |
| G5 | The monitoring agent is **judge-gated as the operator** when the run is live (broad auto-allow, no prompts, no in-run denials) and degrades to a normal prompted session when there is no live judge — never silently ungated-while-claiming-gated. | Removes prompt toil during supervision without opening a safety hole. |

### 2.2 Non-Goals (v1)

- **No console auto-shutdown / TTL.** Consoles persist for history review
  (gauntlet-ui FR-12.3); stopping one stays a manual, explicit action. No
  idle-timeout is added.
- **No cross-worktree aggregate view.** Each worktree's console shows only its
  own runs. No single console aggregates runs across worktrees.
- **No shared/persistent judge.** The judge stays strictly per-run (per-run
  token, `GAUNTLET_RUN_ID`-scoped gating). It is not turned into a long-lived
  cross-run service; `judge.json` records the per-run judge, nothing more.
- **No new judge-policy rule.** The operator-vs-in-run-agent distinction reuses
  the *existing* policy behavior keyed on `GAUNTLET_STEP_ID` presence; `policy.yaml`
  is unchanged (it is an approved artifact). Any policy hardening is a separate
  retro proposal (CLAUDE.md §8).
- **No change to what the console renders or to its control verbs.** This PRD
  changes *access* (auth + browser-open + port) and *process lifecycle*, not the
  pages, the SSE model, or the launch/approve/abort/resume endpoints.
- **No autonomous action by the monitor.** The interactive agent acts only on the
  operator's direction; it opens no PRs and merges nothing on its own. It is a
  supervised assistant, not a second pipeline.
- **No Windows process-identity support.** Inherits `procident.py`'s fail-closed
  contract: an unverifiable identity means the judge is never reaped and the
  monitor treats liveness as indeterminate.

---

## §3 Users and Personas

One role, two surfaces (mirroring operator-aids §3):

- **Human operator** — supervises a run from a terminal. Wants the console up and
  signed-in in one command, and an assistant watching the run so they are not
  hand-reading `status`/`logs`.
- **Agent operator (the interactive monitor)** — a Claude/Codex session this
  feature launches, adopting the `operator-aids` `gauntlet-operator` persona. It
  reads the run's state via `status --json` and the run artifacts, explains
  parked/failed conditions and outstanding questions, and takes the actions the
  human directs — using the same sanctioned `gauntlet` verbs a human would type.

---

## §4 System Architecture

### 4.1 Components

**New:**
- `src/gauntlet/web/launch.py` — small browser-open helper:
  `open_authenticated(handle, *, no_browser)` builds the loopback `?p=<token>`
  URL and calls `webbrowser.open` **only** on a TTY with `--no-browser` unset and
  a known token; otherwise it prints the URL (and falls back to `/login` when the
  token is unknown or **absent on a legacy tokenless record**, §6.1). One place, so
  `run --watch` and `serve --resume` behave identically.
- `src/gauntlet/interactive.py` — the monitor launcher, shared by both entry
  points: resolves the run's judge endpoint (from `judge.json`) and driver
  liveness (operator-aids primitive), composes the **operator-session env** (§6.3),
  builds the starter prompt (which routes the agent to the `gauntlet-operator`
  skill/playbook), and execs the interactive `claude`/`codex` CLI in the
  foreground. Bare interactive CLI — **not** the one-shot `ClaudeCodeAdapter`/
  `CodexAdapter`, which drive `-p`/`exec` non-interactively.

**Reused / extended:**
- `src/gauntlet/web/auth.py` — `authenticate()` admits a valid `?p=` query token
  as a third auth source (after header and cookie); the dependency sets the
  session cookie on the first query-authenticated hit so navigation is token-free
  thereafter (§7). Constant-time compare, loopback, CSRF, same-origin all unchanged.
- `src/gauntlet/web/service.py` / `views.py` — the auth dependency gains a
  `Response` parameter to set the bootstrap cookie; no route logic otherwise
  changes.
- `src/gauntlet/web/registry.py` — `ConsoleRecord` persists the **token** (not
  just its fingerprint) so reuse can rebuild the `?p=` URL; `ensure_console`
  **auto-selects a free loopback port** when the requested one is held by an
  unrelated process; surfaces the token on the returned `ConsoleHandle`.
- `src/gauntlet/engine/judgeproc.py` — `ManagedJudge` writes `judge.json` (§6.2)
  into the run dir after healthz and removes it on clean stop; takes `run_dir`.
- `src/gauntlet/engine/run.py` — thread `run_dir` into `_with_judge`/`ManagedJudge`;
  `abort`/`finish`/`clean` reap an orphaned judge via `judge.json` + identity
  check; the `--interactive` path pre-allocates a run-id + reservation token and
  launches the run detached via `RunProcess`.
- `src/gauntlet/web/jobproc.py` — `RunProcess` reused unchanged for the detached
  `--interactive` launch (same primitive the console supervisor uses).
- `src/gauntlet/cli.py` — `run` gains `--no-browser` and `--interactive[=value]`
  (and now opens the browser under `--watch`); `serve` gains `--resume` and
  `--no-browser`; `status` gains `--interactive[=value]`.
- `src/gauntlet/engine/operator.py` (operator-aids) — reused for driver liveness;
  `procident.py` reused for identity-checked reaping.

### 4.2 Key design decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| One monitor, two entry points | `run --interactive` (detach + launch) and `status --interactive` (attach) share one launcher in `interactive.py`. | The only difference is whether a run is started first; the wiring, env, and prompt are identical. *Determinism / one computation.* |
| Free the TTY for the agent | `run --interactive` launches the run **detached** (existing `RunProcess` + FR-6.1a reservation), foregrounding the agent. | A blocking pipeline and an interactive agent cannot share one terminal. Reuse the sanctioned launch path; invent no new one. |
| Operator vs in-run classification | Reuse the *existing* judge behavior keyed on `GAUNTLET_STEP_ID` presence — monitor sets `RUN_ID` + judge URL/token, **never** `STEP_ID`. | The operator session is exactly "run-scoped but not a pipeline step." Adds no `policy.yaml` rule (§2.2). |
| Fail-closed monitor authz | Operator-gated only when the run's driver is verified **alive** and `judge.json` is readable; otherwise a **normal prompted** session. | Never run ungated while implying gated; an unverifiable judge is treated as absent. *Fail closed.* |
| `judge.json` as endpoint + reap record | `ManagedJudge` writes it (gitignored, `0600`) on start, removes on clean stop; mirrors `.console.json` / `.serve/job.json`. | Gives cleanup verbs and the monitor a single, identity-bearing source of truth for the per-run judge. *Data over inference.* |
| Reuse before kill | Console reuse is the existing PID-reuse-safe + healthz check; judge reaping is the same `procident` identity gate operator-aids' `recover` uses. | No new liveness logic; never SIGKILL a recycled/foreign PID. *Fail closed.* |
| `?p=` sets the cookie, then steps aside | A valid `?p=` authenticates *and* sets the httpOnly session cookie on first hit; a page GET is then **303-redirected to the same path without `p`** (FR-2.5) and all responses carry `Referrer-Policy: no-referrer` (FR-2.6); page links stay token-free. | Bounds token exposure to the very first request — the redirect clears it from the address bar/history and `no-referrer` keeps it out of `Referer` headers; everything after rides the cookie, preserving the rest of the gauntlet-ui posture. |
| Auto-port is loopback-only | Port scan/ephemeral-bind reuses the judge's pattern and keeps `assert_loopback`; the scan window is the fixed bound `[requested, requested+50]` then one OS-ephemeral bind (FR-3.1). | Convenience must not silently widen the bind surface, and a named bound keeps behavior/tests deterministic. *Fail closed.* |

---

## §5 Functional Requirements

### FR-1 — `--watch` boots/reuses the console and opens an authenticated browser

- **FR-1.1** `gauntlet run <slug> --watch` boots or reuses the per-worktree
  console (existing registry discovery) and, on a TTY with `--no-browser` unset,
  opens the operator's default browser to an **already-authenticated** console
  URL; the URL is printed in all cases. *Acceptance:* a test with `webbrowser.open`
  stubbed and `sys.stdout.isatty()` forced true asserts it is called once with a
  loopback URL carrying the serve token; with `--no-browser` (or no TTY) it is
  **not** called and the URL is still printed.
- **FR-1.2** Browser-open is **fail-soft**: a `webbrowser.open` failure, or an
  **unknown token on console *reuse*** — including reuse of a **legacy tokenless
  `.console.json` record** (one written before this feature, with no `token`
  field; see §6.1 migration) — never aborts the run. In every such case it falls
  back to printing/opening the **`/login`** URL and continues; a tokenless live
  record is still **reused** (no new process, same port) and is **not** rewritten
  on reuse. *Acceptance:* a test where `webbrowser.open` raises asserts the run
  proceeds and the `/login` URL is printed; a second test seeds a **legacy
  tokenless** live console record and asserts the console is **reused** (no new
  process/port) and the **`/login`** URL is surfaced (not an authenticated `?p=`
  URL), and the record is left unrewritten.
- **FR-1.3** Console boot/reuse remains exactly the existing behavior otherwise
  (a second `--watch` reuses the live console; a stale registry entry is
  reclaimed). *Acceptance:* the existing `run --watch` reuse test passes unchanged.

### FR-2 — Loopback `?p=` authentication (supersedes gauntlet-ui FR-10.4/10.6, narrowly)

- **FR-2.1** A request bearing a `?p=<token>` query parameter whose value
  constant-time-equals the serve token is **authenticated** (a third source after
  the `X-Gauntlet-Token` header and the session cookie). *Acceptance:* a request
  to a gated endpoint with a valid `?p=` returns 200; with an invalid `?p=`
  returns 401 (or the login redirect for a page GET).
- **FR-2.2** On the first request authenticated *by query token* with no valid
  existing session cookie, the response **sets the httpOnly session cookie**
  (minting a session + CSRF exactly as `POST /login` does), so subsequent
  token-free navigation authenticates by cookie. *Acceptance:* a test asserts the
  `Set-Cookie: gauntlet_web=…; HttpOnly; SameSite=Strict` header is present on
  such a response, and a follow-up request carrying only that cookie (no `?p=`)
  returns 200.
- **FR-2.3** A pre-existing valid session cookie short-circuits the query check
  (no new session is minted per request), so reloading a `?p=` URL does not leak
  sessions. *Acceptance:* a test issues two requests with both a valid cookie and
  a `?p=` and asserts the server-side session count does not grow.
- **FR-2.4** All other auth invariants are unchanged: loopback-only bind, the
  cookie-POST CSRF + same-origin checks (FR-10.6), and `/login`+`/healthz` as the
  only unauthenticated routes. A query-authenticated state-changing request is
  treated like a header one for CSRF purposes (not ambient). *Acceptance:* the
  existing CSRF/same-origin tests pass unchanged; a cookie POST still requires a
  valid CSRF token.
- **FR-2.5** A **page GET** (an HTML navigation, not an asset/API request)
  authenticated by `?p=` with no prior session cookie, after setting the cookie
  (FR-2.2), responds with a **redirect (303) to the same loopback path with the
  `p` parameter stripped**, so the token does not persist in the address bar or
  browser history and the rendered page is served by the token-free follow-up
  request. The redirect target preserves any other query parameters. *Acceptance:*
  a test issues a page GET to `/?p=<valid>` and asserts a 303 whose `Location` is
  the same path **without** `p`, that the `Set-Cookie` bootstrap header is on the
  redirect response, and that the redirected (cookie-only) request returns 200;
  a second test asserts the served page's links/forms contain **no** `p`/token.
- **FR-2.6** Console responses carry **`Referrer-Policy: no-referrer`** so the
  initial `?p=` URL is never emitted as a `Referer` header on sub-resource loads or
  outbound links during the brief pre-redirect window. *Acceptance:* a test asserts
  the `Referrer-Policy: no-referrer` header is present on console responses
  (including the `?p=` page response).

### FR-3 — Console auto-port selection

- **FR-3.1** When the requested console port is held by an **unrelated** process
  (no reusable gauntlet console registered there), `ensure_console` selects a free
  **loopback** port instead of failing closed: it scans the **bounded window
  `[requested_port, requested_port + 50]` inclusive** (the requested port through
  50 ports above it, 51 candidates total), binding the first free loopback port in
  that window; if every port in the window is taken it falls back to a single
  **OS-ephemeral bind** (port `0`). The chosen port is recorded in the registry and
  reflected in the printed/opened URL. *Acceptance:* a test occupies the default
  port with a dummy listener and asserts the console comes up on a different port
  and its registry/URL reflect it; a boundary test occupies the full
  `[requested, requested+50]` window and asserts the console falls back to an
  OS-ephemeral port (outside the scan window) rather than failing.
- **FR-3.2** Auto-port never binds a non-loopback host (`assert_loopback` is
  applied to every candidate). *Acceptance:* a test asserts a non-loopback
  `--host` is still rejected and no port scan widens the bind surface.
- **FR-3.3** A reusable live console on the requested port is still reused, not
  duplicated on a new port. *Acceptance:* a test with a registered live console
  asserts reuse (no new process, same port).

### FR-4 — `serve --resume`

- **FR-4.1** `gauntlet serve --resume`: if a reusable live console is registered,
  open the authenticated browser (FR-1) and **return** (no bind, no block); if
  not, boot the console **detached**, wait for healthz, open the browser, and
  return. *Acceptance:* a test with a live registered console asserts no new
  process is spawned and the browser is opened; a test with none asserts a
  detached console is booted (healthz passes) and the browser opened, and the
  command returns rather than blocking.
- **FR-4.2** A detached `serve --resume` boot that does not pass healthz within
  the timeout fails closed with a clear error naming the log path (reusing the
  existing console-boot error). *Acceptance:* a test forcing a non-answering boot
  asserts a non-zero exit and the error names the log path.
- **FR-4.3** Plain `gauntlet serve` (no `--resume`) is unchanged: it binds in the
  foreground and does not auto-open a browser. *Acceptance:* the existing `serve`
  tests pass unchanged.

### FR-5 — `judge.json` lifecycle

- **FR-5.1** On `ManagedJudge.start()`, after the judge answers healthz, write
  `judge.json` (§6.2) into the run dir with file mode `0600`; remove it on a clean
  `stop()`. The run dir already gitignores `*`, so it never dirties the worktree.
  *Acceptance:* a test asserts `judge.json` exists with the recorded
  pid/pgid/identity/host/port/url/token while the judge runs and is absent after a
  clean stop; a second asserts its mode is `0600`.
- **FR-5.2** `judge.json` is best-effort and never blocks the run: a write failure
  is logged and the run proceeds (the judge is still up in-process). *Acceptance:*
  a test where the write raises asserts the run continues and the judge is healthy.

### FR-6 — Orphaned-judge reaping on cleanup verbs

- **FR-6.1** `gauntlet abort`, `finish`, and `clean` reap an **orphaned** judge:
  the judge is reaped **iff both** (a) `judge.json` records a process that is
  **verified alive and identity-matched** (`procident`, the same gate operator-aids'
  `recover` uses), **and** (b) its **owning run driver is gone** — defined
  normatively in §6.4 as `engine/operator.driver_liveness(run_root, slug)` being
  `orphaned` or `none`. When both hold, signal the recorded judge's process group
  (SIGTERM then SIGKILL after a bounded grace) and remove `judge.json`. If the
  driver is `alive` (running) **or** `indeterminate` (liveness unprovable), the
  judge is **not** signalled (fail closed, §6.4). *Acceptance:* a **gone-driver/kill**
  test spawns a dummy process as a stand-in judge, records it in `judge.json`,
  removes the drive lock (driver `none`) or marks it dead (driver `orphaned`), and
  asserts the verb terminates the recorded group and removes the file; an
  **alive-driver/no-kill** test holds an alive drive lock for the run and asserts
  the recorded judge is **not** signalled and `judge.json` is left intact; an
  **indeterminate-driver/no-kill** test (driver liveness unprovable) likewise
  asserts no signal.
- **FR-6.2** Reaping is fail-closed: any absent/mismatched/unverifiable identity
  datum → no signal sent (the judge is left alone and the condition noted), never
  a foreign or PID-reused kill. *Acceptance:* tests with a dead PID, a mismatched
  `proc_identity`, and a `null` identity each assert **no** signal is sent.
- **FR-6.3** These verbs **never** kill the shared console: the console is a
  per-worktree singleton that persists (gauntlet-ui FR-12.3); only the per-run
  judge is reaped. *Acceptance:* a test asserts a registered live console survives
  `abort`/`finish`/`clean` and its registry entry is intact.

### FR-7 — `run --interactive`: detached run + foreground monitor

- **FR-7.1** `gauntlet run <slug> --interactive` accepts an optional value
  (`claude` default, or `codex`); an unknown value errors before any launch.
  `--interactive=codex` is **gated on the P3 starter-prompt feasibility spike**
  (OQ-2, §8 P3): the monitor must come up **seeded** with the FR-9.1 starter prompt
  (a monitor with no starter prompt is unsafe — it would not be routed to the
  operator playbook). The spike resolves, before `codex` is accepted, exactly one
  of two outcomes — (a) the pinned `codex` CLI accepts an initial prompt in
  interactive mode (analogous to `claude "<prompt>"`), in which case `codex` is
  wired the same way as `claude`; or (b) it does not, in which case P3 implements
  the **defined fallback delivery** (the starter prompt is passed via the codex
  CLI's supported initial-input channel — stdin/prompt-file — confirmed by the
  spike) **or**, if no such channel exists, `--interactive=codex` **fails closed**
  before any launch with a clear message that codex prompt-seeding is unsupported
  by the pinned version (and `--interactive=claude` is the supported path). Codex is
  **never** launched unseeded. *Acceptance:* a test asserts bare `--interactive`
  selects Claude and `--interactive=bogus` exits non-zero naming the valid choices;
  and a test asserts that `--interactive=codex` **either** launches codex with the
  composed starter prompt delivered (the spike-confirmed channel) **or** exits
  non-zero with the unsupported-codex message — never launching codex without the
  starter prompt.
- **FR-7.2** It pre-allocates a run-id + single-use reservation token and launches
  the run **detached** via `RunProcess` (the existing FR-6.1a handshake), then
  foregrounds the monitor on that run. It composes with `--watch` (console also
  boots). *Acceptance:* a test asserts a detached `RunProcess` is started for the
  pre-allocated run-id with the reservation token, and that the monitor launcher
  is invoked with that run's dir.
- **FR-7.3** The monitor is execed in the foreground with the operator-session env
  (§6.3) **only when both** conditions hold: the run's `judge.json` has appeared
  (bounded wait) **and** the run's driver is verified **alive** by the operator-aids
  liveness primitive (`engine/operator.driver_liveness(run_root, slug) == "alive"`,
  §6.4). If `judge.json` does not appear within the timeout, **or** the driver is
  not verified alive (liveness `none`/`orphaned`/`indeterminate` — e.g. the detached
  driver died immediately after launch, orphaning the judge), **or** under
  `--no-judge`, the monitor launches as a **normal prompted** session with an
  explicit degraded note (and never sets any judge env). *Acceptance:* a test
  (judge exec stubbed) asserts the env carries `GAUNTLET_RUN_ID` + judge
  `URL`/`TOKEN` and **no** `GAUNTLET_STEP_ID` when `judge.json` is present **and**
  the driver is alive; a test with `judge.json` present **but the driver not alive**
  (liveness `none`/`orphaned`) asserts a **normal prompted** session with **none**
  of the judge env set; and a test with `judge.json` absent after the timeout
  likewise asserts none of them are set.

### FR-8 — `status --interactive`: attach the monitor to an existing run

- **FR-8.1** `gauntlet status <slug> --interactive[=claude|codex]` attaches the
  monitor to the selected run (same deterministic run-instance selection
  operator-aids uses: `active-run.txt` else lexically-greatest `run-*`), without
  starting a new run; an unknown/absent run errors. *Acceptance:* a test over a
  fixture run dir asserts the monitor launcher is invoked for the resolved run-id
  and that no `RunProcess` is started.
- **FR-8.2** Judge wiring follows driver liveness (the §6.4 primitive,
  `engine/operator.driver_liveness`): when the driver is `alive` **and** `judge.json`
  is readable → operator-session env (§6.3); for any other liveness
  (`orphaned`/`none`/`indeterminate`) or an unreadable `judge.json` → a normal
  prompted session for diagnosis (the agent can still read `status`/`logs` and
  `resume`). *Acceptance:* a test with a live driver +
  `judge.json` asserts operator-session env; a test with a parked/`none`-liveness
  run asserts a normal session (no judge env) and that the command still succeeds.
- **FR-8.3** `--interactive` is additive to `status` — the default `status` output
  (and operator-aids' `--json`) is unchanged when the flag is absent. *Acceptance:*
  the existing/op-aids `status` tests pass unchanged; `status <slug>` with no flag
  does not exec an agent.

### FR-9 — The monitor starter prompt routes to the operator persona

- **FR-9.1** The monitor is seeded with a starter prompt that states it is
  supervising `gauntlet run <slug>`, names the run dir, and directs it to monitor
  the run, explain parked/failed conditions and outstanding questions, and take
  operator-directed actions via the sanctioned `gauntlet` verbs — explicitly
  routing it to the `gauntlet-operator` skill/playbook (operator-aids) for the
  state→action map. *Acceptance:* a test asserts the composed prompt contains the
  slug, the run dir, and a reference resolving to the operator playbook, and names
  no autonomous push/merge action.

### FR-10 — Operator-vs-in-run judge classification proof (validates §1.3; P1)

This is the behavioral proof of the load-bearing §1.3 assumption. It asserts the
judge's *decisions*, not merely the presence of env vars. `policy.yaml` is
**unchanged** (§2.2); these are the verdicts the *existing* deny-first policy
already produces, keyed on whether the decide request carries a `step_id`. The
`GAUNTLET_RUN_ID`/`GAUNTLET_STEP_ID` env (§6.3) is what the judge hook translates
into the decide request's `run_id`/`step_id` fields, so "operator session" ≙
`step_id` **absent** and "in-run agent" ≙ `step_id` **present** (both with a valid
per-run `run_id` + token).

- **FR-10.1** Against a live per-run judge, an **operator-session** decide request
  (`run_id` set, `step_id` **absent**) returns the verdicts below; an **in-run**
  decide request (same `run_id`, a non-empty `step_id`) returns the contrasting
  verdicts. Verdicts use the existing fast-path outcome set (`allow` / `deny` /
  `ask`; anything unmatched escalates to the LLM classifier — for this proof use
  inputs that resolve on the fast path so the assertions are deterministic):

  | Representative tool call | Operator (`step_id` absent) | In-run agent (`step_id` present) | Policy rule exercised |
  |--------------------------|------------------------------|----------------------------------|-----------------------|
  | `git push` (feature branch) | **allow** | **deny** | `git-push` (allow) vs `push-or-pr-open-in-pipeline-step` (deny, `pipeline_step_only`) |
  | `gh pr create …` | **allow** | **deny** | `gh-pr-propose-and-read` (allow) vs `push-or-pr-open-in-pipeline-step` (deny) |
  | `git status` / `uv run pytest` | **allow** | **allow** | `git-readonly` / `test-and-build-runners` — unaffected by step scope |
  | `gh pr merge …` | **deny** | **deny** | `gh-pr-merge` — denied in **every** context (proves operator is *not* a blanket allow) |
  | `git push --force …` | **deny** | **deny** | `force-push` — denied in every context |

  *Acceptance:* a test stands up a real (or in-process) judge with the unchanged
  `policy.yaml`, issues each request above with a valid per-run token under both
  env combinations, and asserts every cell of the table. The `git push` and
  `gh pr create` rows are the load-bearing ones: they must **flip** between
  operator-`allow` and in-run-`deny` purely on `step_id` presence.

- **FR-10.2** Authorization is still per-run: an operator-session request bearing a
  **wrong or missing per-run token**, or a `run_id` that does not match the judge,
  is **rejected by the judge** regardless of `step_id`, so the classification never
  admits a non-operator caller. *Acceptance:* a test issues an operator-shaped
  request (`step_id` absent) with an invalid token and asserts it is rejected, not
  auto-allowed.

P1 acceptance (§8) **depends on FR-10**: P1 is not complete until the FR-10.1
table and FR-10.2 assertions pass, not merely until `judge.json` is written and the
env is shaped (FR-5 / §6.3).

---

## §6 Data & Schemas (normative)

### §6.1 Console registry addition (`<run_root>/.console.json`)

The existing `ConsoleRecord` gains one field; everything else is unchanged and
the file stays gitignored:

| Field | Type | Note |
|-------|------|------|
| `token` | string \| absent | the serve token in clear, so reuse can rebuild the `?p=` URL. **New.** Loopback-scoped, gitignored, local-only (consistent with the §7 relaxation). `token_fingerprint` is retained for the existing mismatch check. The field is **optional for backward compatibility** — see the migration rule below. |

**Migration / tokenless records (normative).** A pre-existing `.console.json`
written before this feature has no `token` field. When `ensure_console` discovers
such a record and healthz proves the console is **live**, it **reuses** the console
(no new process, no port change) but, because it cannot reconstruct an
authenticated `?p=` URL, it surfaces the **`/login`** URL instead of an
already-authenticated one (FR-1.2). The stale tokenless record is **not** rewritten
on reuse (the running console's in-memory token is unknown to the reusing process);
the record gains a `token` only when a console is **freshly booted** by this code,
which writes the field. A tokenless record is therefore reusable, never reclaimed as
stale solely for lacking a token, and never silently skips the fail-soft `/login`
path.

### §6.2 `judge.json` (in the run dir, gitignored, mode `0600`)

```json
{
  "pid": 48213,
  "pgid": 48213,
  "proc_identity": { "platform": "darwin", "value": 1750000000, "unit": "epoch_seconds" },
  "host": "hostname",
  "port": 8787,
  "url": "http://127.0.0.1:8787",
  "token": "…per-run-judge-token…",
  "run_id": "run-2026-06-25T16-41-22",
  "started_at": "2026-06-25T16-41-22"
}
```

- `pid`/`pgid`/`proc_identity`/`host` — the FR-6 reap-identity datums (PID-reuse-
  safe; `proc_identity` may be `null` on an unsupported platform → never reaped).
- `port`/`url`/`token`/`run_id` — what the monitor (§6.3) needs to wire itself to
  this run's judge. `started_at` is the lock-style UTC stamp.
- `token` is the **per-run judge token** (the value the judge accepts on
  `GAUNTLET_JUDGE_TOKEN`), **not** the console `serve` token recorded in
  `.console.json` (§6.1). The two credentials are distinct and are never
  interchanged; conflating them would let a console credential reach the judge or
  vice-versa.

### §6.3 Operator-session environment contract (the monitor)

When wiring the monitor to a live judge, exactly these are set; `GAUNTLET_STEP_ID`
is **deliberately absent** (its presence is what marks an in-run agent):

| Var | Value | Effect |
|-----|-------|--------|
| `GAUNTLET_RUN_ID` | the run's id | engages the judge hook (it gates only when this is set) |
| `GAUNTLET_JUDGE_URL` | `judge.json.url` | the run's judge endpoint |
| `GAUNTLET_JUDGE_TOKEN` | `judge.json.token` | per-run token (accepted by that judge) |
| `GAUNTLET_STEP_ID` | *unset* | classifies the call as the operator's session, not a pipeline step |

In the degraded (no-live-judge) case **none** of these are set — the session uses
Claude Code's normal permission handling.

### §6.4 Owning-driver identity & liveness (normative)

A judge process is *owned* by the run driver recorded in **`<run_root>/.driving.lock`**
— the single drive-lock read path operator-aids already uses. Cleanup verbs (FR-6)
and the monitor launchers (FR-7.3, FR-8.2) **must** determine driver liveness
through exactly one primitive, **`engine/operator.driver_liveness(run_root, slug)`**,
which reads that lock plus the `procident` OS identity primitives and returns one of:

| Liveness | Meaning (per `engine/operator.py`) | Counts as "owner gone" for reaping? |
|----------|------------------------------------|-------------------------------------|
| `alive` | lock present, PID alive, identity matches on this host | **No** — driver is running; never reap. |
| `orphaned` | lock present but the driver is **proven dead** (or its PID was reused) | **Yes** — owner gone; reap if the judge's own identity also verifies (FR-6.1). |
| `none` | no drive lock (or a foreign-host lock that is not ours) | **Yes** — owner gone; reap if the judge's own identity also verifies (FR-6.1). |
| `indeterminate` | the driver's liveness **cannot be proven** either way | **No** — fail closed; leave the judge untouched and note the condition. |

This is the *only* sanctioned source for the "is the owning driver gone?" decision;
implementations must not infer it from any other artifact. The judge's **own**
process identity is verified separately against `judge.json` (`pid`/`pgid`/
`proc_identity`/`host`) via `procident` before any signal — a gone driver authorizes
reaping only of a judge whose identity still matches (FR-6.1/FR-6.2).

---

## §7 Security & Privacy

- **The `?p=` relaxation (superseding gauntlet-ui FR-10.4/10.6, narrowly).** A
  reusable serve token may now ride in the `?p=` query parameter. The accepted
  exposure is the token appearing in the URL/access log of *that single first
  request*. The mitigations that make this acceptable for a localhost developer
  console: the bind is **loopback-only** (an attacker needs local access — the same
  boundary the whole console already rests on); the token is **per-serve** (a
  console restart invalidates it); the `?p=` hit **immediately sets the httpOnly
  cookie and a page GET is then 303-redirected to the same path with `p` stripped**
  (FR-2.5), so the reusable token does **not** linger in the address bar or browser
  history past the first request; responses carry **`Referrer-Policy: no-referrer`**
  (FR-2.6) so the token never leaks via a `Referer` header to a sub-resource or
  outbound link; and all subsequent navigation is token-free (page links carry no
  token — the rest of the gauntlet-ui posture is preserved). Everything else —
  constant-time compare,
  loopback bind, httpOnly+SameSite=Strict cookie, session-bound CSRF, same-origin
  on cookie POSTs — is **unchanged**. This trade-off is ratified by the human
  through this PRD's review + gate; see OQ-1 for the stronger one-time-bootstrap
  alternative the reviewer may prefer.
- **`judge.json` token at rest.** The per-run judge token (already held in the run
  process's `os.environ`) is persisted to a **gitignored, `0600`** file in the run
  dir so the operator's own `--interactive`/reap paths can reach it. It is removed
  on clean stop; an orphaned file holds a token for a judge that is being reaped.
  No token is committed or logged.
- **Monitor authorization is fail-closed (FR-7.3/FR-8.2).** Operator-session env
  is set **only** when the run's driver is verified alive and `judge.json` is
  readable; any doubt → a normal prompted session. The monitor never runs ungated
  while implying it is gated, and it inherits the operator's rights via the
  *existing* judge policy, not a new rule — so a real in-run agent's gating
  (`STEP_ID` present → push/PR denied) is untouched.
- **Identity-checked reaping (FR-6).** No judge process is signalled unless
  `procident` proves it is ours on this host; any unverifiable datum → no signal.
  Mirrors operator-aids `recover`'s gate; reuses no new kill path.
- **Detached launch** reuses the sanctioned `RunProcess` + reservation handshake
  (gauntlet-ui FR-6.1a) — same containment as a console-launched run; the slug/
  run-id are validated as safe path segments before any path construction.
- **Auto-port** only ever binds loopback (`assert_loopback` on every candidate).

---

## §8 Implementation Plan (phased, assumption-validating)

Ordered riskiest-assumption-first; no phase depends on a later phase. Every phase
ends in passing tests and a commit.

| Phase | Deliverable | Assumption it validates |
|-------|-------------|--------------------------|
| **P1** | `judge.json` lifecycle (FR-5) **+** the operator-session env contract (§6.3) **+** the classification proof (FR-10): the FR-10.1 verdict table and FR-10.2 per-run-token rejection pass against the unchanged `policy.yaml` — `git push`/`gh pr create` flip operator-`allow`↔in-run-`deny` on `step_id` presence, with `gh pr merge` denied in both. (FR-5; FR-10; §1.3) | **The load-bearing one (§1.3):** the monitor can find a run's judge and be correctly classified as the operator, both directions, *before* any launcher depends on it. P1 acceptance is the FR-10 judge-level assertions, not env-shape checks alone. |
| **P2** | Orphaned-judge reaping on `abort`/`finish`/`clean`; console never killed. (FR-6) | A wedged/orphaned judge can be terminated *safely* (identity-checked) and a shared console survives cleanup. Reuses P1's `judge.json` + the existing `procident` gate. |
| **P3** | `run --interactive` — detached run + foreground operator monitor + starter prompt. (FR-7, FR-9) Includes the **codex starter-prompt feasibility spike** (OQ-2, FR-7.1): determine whether the pinned `codex` CLI can be seeded with the FR-9.1 prompt and either wire it (direct or fallback channel) or make `--interactive=codex` fail closed with the unsupported message. | The detach-run / foreground-agent handoff works and the monitor comes up wired (or honestly degraded). `claude` is the default/primary path; the spike resolves codex feasibility so no unseeded-codex option ships. Depends on P1 (`judge.json` + classification) and the existing `RunProcess`. |
| **P4** | `status --interactive` — attach the monitor to an existing run, gated-if-alive-else-normal. (FR-8, FR-9) | The same monitor attaches to a run started without it, choosing authz from liveness. Reuses P3's launcher + operator-aids liveness. |
| **P5** | Frictionless access: `?p=` auth + cookie bootstrap (FR-2), registry token persistence + auto-port (FR-3, §6.1), `open_authenticated` + `run --watch` browser-open (FR-1), `serve --resume` (FR-4). | Zero-copy-paste, port-collision-free console access. Lowest technical risk and independent of P1–P4 (web layer only); sequenced after the riskier judge/monitor work — it could move earlier if preferred (see §8 note). |

**Note on resequencing.** P5 (console access) is independent of P1–P4 and is the
lowest-risk, highest-immediate-value slice; it is placed last only to honor
riskiest-assumption-first. It may be pulled forward to P1 if we would rather ship
the access ergonomics before the monitor — no code dependency forces the order.

---

## §9 Success Metrics

- **G1:** on a TTY, `gauntlet run --watch` reaches an authenticated console in the
  browser in **0** manual paste steps, at the default or an auto-selected free
  port; **0** non-loopback binds across the auto-port tests.
- **G2:** `serve --resume` reuses a live console with **0** new processes and
  returns the terminal; boots detached + opens + returns when dead.
- **G3:** across the FR-6 matrix (dead / mismatched / null-identity / verified
  cases), **0** foreign/PID-reused kills and **0** consoles killed; a verified
  orphaned judge is reaped in the one case it should be.
- **G5 (the §1.3 assumption):** **100%** correct classification on both sides — a
  `STEP_ID`-unset operator call is allowed without a prompt, a `STEP_ID`-set in-run
  call still hits its denials — and **0** cases where the monitor runs ungated
  while reporting gated (it degrades to a prompted session instead).
- **G4:** `run --interactive` brings up a detached run + a foreground monitor in
  one command (test: detached `RunProcess` started + monitor launcher invoked with
  the run dir); `status --interactive` attaches to a fixture running-run.

---

## §10 Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Adversarial review rejects a reusable URL token outright. | The relaxation is narrow, loopback-only, cookie-takeover-bounded, and per-serve (§7); OQ-1 carries a fully-sketched one-time-bootstrap fallback the reviewer can elect without redesigning the rest. |
| Judge classification is wrong (prompt-spam, over-deny, or leaked in-run restriction). | P1 validates **both** directions with tests before any launcher depends on it; the monitor fails closed to a prompted session on any doubt (FR-7.3/8.2). |
| A detached `--interactive` run is orphaned (driver dies). | Reuses the supervisor's `RunProcess` + reattach; operator-aids `recover`/liveness diagnoses it and FR-6 reaps its judge. |
| `codex` interactive cannot take an initial prompt the way `claude` can. | OQ-2: verify against the pinned codex version during P3; `claude` is the default and primary path, so a codex limitation degrades one option, not the feature. |
| `cli.py`/`engine/run.py` merge contention with the in-flight operator-aids run. | Changes are **additive** (new flags/functions, different methods); implementation is sequenced **after** operator-aids merges and the branch rebased onto it (header). |
| Auto-port silently widens the bind surface. | `assert_loopback` on every candidate (FR-3.2); scan is loopback-only. |

---

## §11 Open Questions

1. **`?p=` reusable token vs one-time bootstrap.** *Leaning: persistent reusable
   `?p=` (ratified by the author, §7).* The reusable token is simplest and fully
   no-copy-paste, but it lives in the initial URL/history/logs. The stronger
   alternative is a **single-use, short-TTL bootstrap token** that the `?p=` hit
   exchanges for the cookie and then invalidates (Jupyter-style) — still
   zero-copy-paste, with the URL token dead after first load. Recorded for the
   adversarial reviewer to weigh; electing it changes only FR-2's token lifecycle,
   not the rest of the design.
2. **Codex interactive invocation.** Does the pinned `codex` CLI accept an initial
   prompt while entering interactive mode (as `claude "<prompt>"` does), or must
   the prompt be delivered another way (stdin/paste/file)? **Resolution is now
   gated, not open-ended (FR-7.1, §8 P3):** the P3 spike must land one of —
   direct prompt, a confirmed fallback delivery channel, or `--interactive=codex`
   failing closed with an unsupported-codex message. Codex never launches unseeded;
   `--interactive=claude` is unaffected.
3. **Does `run --interactive` imply `--watch`?** *Leaning: no — independent and
   composable* (`run --interactive --watch` for both). The monitor reads run state
   directly and does not require the console. Open if the reviewer/author prefers
   `--interactive` to also bring up the visual console by default.
4. **`status --interactive` run selection when ambiguous.** *Leaning: reuse
   operator-aids' deterministic selection* (`active-run.txt` else lexically-greatest
   `run-*`), with an explicit `--run-id` override if/when operator-aids exposes one.
   Confirm no separate selection semantics are wanted here.

---

*Handoff: this is **Draft v0.1**. The riskiest assumption is §1.3 (correct
operator-vs-in-run judge classification of the monitor), attacked first and
test-validated in P1. Open Questions 1 (token form) and 2 (codex) are the live
ones for review; 3 and 4 carry leanings. Implementation is sequenced after
`operator-aids` merges (header). Next step is `gauntlet run background-start-services`,
which begins with **adversarial review** — not implementation. I ratify; the
pipeline executes.*

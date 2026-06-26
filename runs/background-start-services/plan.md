Based on the approved PRD and the current shape of the codebase, here is the implementation plan.

---

# Implementation Plan: Background service startup & the interactive run monitor

**Branch:** `gauntlet/background-start-services`
**Spec:** `prd.md` (this run's input artifact) — Draft v0.1
**Sequencing precondition:** implementation begins only after `operator-aids`
has merged and this branch is rebased onto it (PRD header). The codebase already
exposes the predecessor primitives this plan builds on:
`engine/operator.driver_liveness(run_root, slug)` (returns
`alive`/`orphaned`/`none`/`indeterminate`), `procident.process_is_alive` +
`read_process_identity`, `web/jobproc.RunProcess` with the FR-6.1a
`reservation_token` handshake, `web/registry.ensure_console`/`ConsoleRecord`,
`web/auth.authenticate`/`SessionStore`, and `engine/judgeproc.ManagedJudge`.

## Orientation: what already exists vs. what each phase adds

The plan is small and additive by design — every change is a new flag, a new
function, a new field, or a new module, never a rewrite of an approved surface.
Grounding the phases in the current code:

- `ManagedJudge.start()` already mints a per-run token, moves off a taken port,
  awaits `/healthz`, and injects `GAUNTLET_RUN_ID`/`GAUNTLET_JUDGE_*` into the
  session env. It does **not** take a `run_dir` and writes **no** on-disk record.
  P1 adds exactly that.
- `_with_judge(man, run_dir, fn)` in `engine/run.py` already receives `run_dir`
  and constructs the `ManagedJudge` — the single thread-point for P1.
- `ConsoleRecord` persists only `token_fingerprint`; `ensure_console` **fails
  closed** (`ConsoleBootError`) on a port held by an unrelated process. P5 adds
  the optional `token` field and bounded auto-port.
- `auth.authenticate(request, sessions)` admits only header + cookie; the
  `?token=` path was deliberately retired in gauntlet-ui P7. P5 reintroduces a
  **narrowed, cookie-bootstrapping** loopback `?p=` source (the §7 relaxation).
- `web/launch.py` and `interactive.py` do **not** exist yet (P5 and P3 create
  them).

## Phase ordering rationale

The PRD §8 orders phases riskiest-assumption-first, and this plan honors that
order without deviation. The load-bearing belief (§1.3) — that a foreground
agent with `GAUNTLET_RUN_ID` set and `GAUNTLET_STEP_ID` unset is classified by
the *unchanged* judge as the operator's own session in **both** directions — is
attacked in **P1**, before any launcher depends on it. Reaping (P2) and the two
monitor entry points (P3, P4) layer onto P1's `judge.json` + classification.
The web-access ergonomics (P5) are independent of P1–P4 and the lowest technical
risk; per the PRD's own resequencing note they are placed last to honor
riskiest-first, and could be pulled forward without any code dependency forcing
the order. Each phase is strictly sequential (FR-10.3): no phase references work
a prior phase has not delivered.

---

## P1 — `judge.json` lifecycle + operator-session env contract + classification proof

**Assumption validated (the load-bearing one, §1.3):** the monitor can *find* a
run's judge on disk, and that judge — running the **unchanged** `policy.yaml` —
classifies a `step_id`-absent caller as the operator (broad auto-allow, no in-run
denials) and a `step_id`-present caller as an in-run agent (push/PR denied),
while still rejecting any caller without the valid per-run token. If this is
wrong in either direction, every later phase is built on sand; P1 proves it with
judge-level decision tests, not env-shape checks.

**Deliverables (FR-5, §6.2, §6.3, FR-10):**

- `ManagedJudge` gains a `run_dir: Path | None` parameter. After `_await_healthy()`
  succeeds in `start()`, it writes `<run_dir>/judge.json` (§6.2 schema:
  `pid`/`pgid`/`proc_identity` of the **judge subprocess** via
  `read_process_identity(self._proc.pid)`, plus `host`/`port`/`url`/`token`/
  `run_id`/`started_at`) atomically with mode `0600`. `stop()` removes it on a
  clean stop. The run dir already gitignores `*`, so it never dirties the
  worktree. The write is **best-effort** (FR-5.2): a failure is logged to stderr
  and the run proceeds — the judge is up in-process regardless.
- Thread `run_dir` from `_with_judge` into the `ManagedJudge(...)` construction in
  `engine/run.py` (the only construction site that has the run dir).
- A `JudgeRecord` dataclass + `read_judge_record(run_dir)` reader and an
  `operator_session_env(record)` builder (in `judgeproc.py`, alongside the
  existing per-run env logic). `operator_session_env` returns exactly
  `{GAUNTLET_RUN_ID, GAUNTLET_JUDGE_URL, GAUNTLET_JUDGE_TOKEN}` and **deliberately
  omits** `GAUNTLET_STEP_ID` (§6.3) — its absence is what marks the operator
  session. This is the contract P3/P4 will consume; it is defined and tested here
  so the consumers inherit a proven primitive.
- The **classification proof** (FR-10): tests that stand up a judge with the
  unchanged `policy.yaml` (reusing the in-process judge harness from
  `tests/unit/test_judge_service.py` / `test_judge_core.py`) and assert every
  cell of the FR-10.1 table under both env combinations, plus FR-10.2 (wrong /
  missing per-run token → rejected, not auto-allowed).

**Test strategy:**

- `test_judgeproc.py` (extend): `judge.json` exists with the recorded fields
  while the judge runs and is absent after a clean `stop()`; its mode is `0600`;
  a write that raises does not abort the run and the judge stays healthy (FR-5.2).
- New env-contract test: `operator_session_env` sets `RUN_ID` + judge `URL`/`TOKEN`
  and **never** `STEP_ID`; the degraded path sets none of them.
- New `test_judge_classification.py` (the P1 gate): the FR-10.1 verdict table
  — `git push`/`gh pr create` **flip** operator-`allow`↔in-run-`deny` purely on
  `step_id` presence; `git status`/`uv run pytest` allow in both; `gh pr merge`
  and `git push --force` deny in both — and FR-10.2 token rejection. All inputs
  chosen to resolve on the policy fast path so assertions are deterministic.

**Exit criteria:** the full FR-5 suite **and** the FR-10.1 table + FR-10.2
assertions pass. P1 is **not** complete on env-shape checks alone (PRD §8). All
existing judge tests pass unchanged. One commit.

**Deferrals:** orphaned-judge reaping → P2; the monitor launcher that *consumes*
`operator_session_env` → P3/P4; all web-access changes → P5.

---

## P2 — Orphaned-judge reaping on `abort` / `finish` / `clean`

**Assumption validated:** a wedged or orphaned judge can be terminated *safely*
— only when identity-verified as ours **and** its owning driver is provably gone
— and a shared per-worktree console is never collateral-damaged by cleanup.
Reuses P1's `judge.json` and the existing `procident` identity gate
(`operator-aids` `recover` uses the same gate).

**Deliverables (FR-6):**

- A `reap_orphaned_judge(run_root, slug)` helper (in `engine/run.py` or a small
  sibling): read `judge.json`; verify the judge's **own** identity
  (`process_is_alive(pid, identity)` + host match via `procident`); read the
  owning-driver liveness through the **single sanctioned primitive**
  `engine/operator.driver_liveness(run_root, slug)` (§6.4). Reap **iff** the
  judge identity verifies **and** the driver is `orphaned` or `none`: signal the
  recorded `pgid` SIGTERM, then SIGKILL after a bounded grace, then remove
  `judge.json`. Fail closed on every other case — absent/mismatched/unverifiable
  identity datum, or driver `alive`/`indeterminate` → **no signal**, file left
  intact, condition noted.
- Wire the helper into `abort`, `finish`, and `clean` in `engine/run.py`.
- These verbs **never** touch `web/registry.ConsoleRecord` / the console process
  — only the per-run judge is a reap target (FR-6.3).

**Test strategy (the FR-6 matrix):**

- **gone-driver/kill:** spawn a dummy process as a stand-in judge, record it in
  `judge.json`, make the driver `none` (no drive lock) or `orphaned`; assert the
  verb terminates the recorded group and removes the file.
- **alive-driver/no-kill** and **indeterminate-driver/no-kill:** assert no signal,
  `judge.json` intact.
- **fail-closed identity:** dead PID, mismatched `proc_identity`, and `null`
  identity each assert **no** signal (no foreign / PID-reused kill).
- **console survival:** a registered live console survives `abort`/`finish`/`clean`
  with its registry entry intact.

**Exit criteria:** all FR-6 tests pass; existing `abort`/`finish`/`clean` and
`test_recover.py` tests unchanged. One commit.

**Deferrals:** none beyond what later phases own; reuses P1 and the existing
`procident`/`driver_liveness` primitives without adding new liveness logic.

---

## P3 — `run --interactive`: detached run + foreground monitor + starter prompt

**Assumption validated:** the detach-the-run / foreground-the-agent handoff works
over the sanctioned `RunProcess` path, the monitor comes up either correctly
wired as the operator (judge present + driver alive) or **honestly degraded** to
a normal prompted session, and the codex feasibility question (OQ-2) is resolved
so no unseeded-codex option ever ships. `claude` is the default and primary path.

**Deliverables (FR-7, FR-9, OQ-2):**

- `src/gauntlet/interactive.py` — the shared monitor launcher
  `launch_monitor(...)`: resolve the run's judge endpoint via
  `read_judge_record` (with a **bounded wait** for `judge.json` to appear) and the
  driver liveness via `driver_liveness(run_root, slug)`. Compose the
  operator-session env (P1's `operator_session_env`) **only when both** the
  record is readable **and** liveness `== "alive"` (FR-7.3); otherwise launch a
  **normal prompted** session with an explicit degraded note and **no** judge env.
  Build the FR-9.1 starter prompt and `exec` the **bare interactive** `claude` /
  `codex` CLI in the foreground — explicitly **not** the one-shot
  `ClaudeCodeAdapter`/`CodexAdapter` (which drive `-p`/`exec` non-interactively).
- `cli.py` `run` gains `--interactive[=claude|codex]` (bare → `claude`; unknown
  value errors **before any launch**, naming the valid choices). On
  `--interactive`, pre-allocate a run-id + single-use reservation token, launch
  the run **detached** via `RunProcess` (the existing FR-6.1a handshake), then
  foreground `launch_monitor` on that run's dir. Composes with `--watch`.
- FR-9.1 starter-prompt composer: states it is supervising `gauntlet run <slug>`,
  names the run dir, directs the agent to monitor / explain parked-failed
  conditions / take operator-directed actions via sanctioned `gauntlet` verbs,
  and **routes it to the `gauntlet-operator` skill/playbook**. It names **no**
  autonomous push/merge action.
- **Codex starter-prompt feasibility spike (OQ-2 / FR-7.1):** determine whether
  the pinned `codex` CLI accepts an initial prompt in interactive mode (analogous
  to `claude "<prompt>"`). Resolve to exactly one of: (a) wire codex the same way;
  (b) deliver the prompt over a spike-confirmed fallback channel
  (stdin / prompt-file); or (c) make `--interactive=codex` **fail closed** before
  any launch with an unsupported-codex message (and `--interactive=claude` is the
  supported path). Codex is **never** launched unseeded. Record the finding in
  `BOOTSTRAP-NOTES.md`.

**Test strategy** (interactive `exec` stubbed throughout):

- detached `RunProcess` started for the pre-allocated run-id with the reservation
  token, and `launch_monitor` invoked with that run's dir (FR-7.2).
- env carries `GAUNTLET_RUN_ID` + judge `URL`/`TOKEN` and **no**
  `GAUNTLET_STEP_ID` when `judge.json` present **and** driver alive; **normal
  prompted** session (none of the judge env) when `judge.json` present **but**
  driver not alive (`none`/`orphaned`), and when `judge.json` is absent after the
  bounded wait (FR-7.3).
- bare `--interactive` selects claude; `--interactive=bogus` exits non-zero naming
  the choices; `--interactive=codex` **either** launches codex with the composed
  prompt delivered **or** exits non-zero with the unsupported-codex message —
  never launching codex without the starter prompt (FR-7.1).
- the composed prompt contains the slug, the run dir, a reference resolving to the
  operator playbook, and names no autonomous push/merge action (FR-9.1).

**Exit criteria:** all FR-7 / FR-9 tests pass; the codex spike is resolved and the
outcome documented in `BOOTSTRAP-NOTES.md`. One commit. Depends on P1
(`judge.json` + `operator_session_env`) and the existing `RunProcess`.

**Deferrals:** the `status --interactive` attach path → P4; all web-access
ergonomics → P5.

---

## P4 — `status --interactive`: attach the monitor to an already-running run

**Assumption validated:** the *same* P3 launcher attaches to a run started without
`--interactive`, selecting its authz purely from driver liveness — the
"I forgot to add it" recovery path. Reuses P3's launcher and operator-aids'
deterministic run-instance selection.

**Deliverables (FR-8, FR-9):**

- `cli.py` `status` gains `--interactive[=claude|codex]`: resolve the run instance
  with the same deterministic selection operator-aids uses (`active-run.txt` else
  lexically-greatest `run-*`); an unknown/absent run errors. It starts **no**
  `RunProcess` — it only invokes `launch_monitor` (P3) for the resolved run dir.
- Judge wiring follows `driver_liveness`: `alive` **and** readable `judge.json` →
  operator-session env (§6.3); any other liveness or unreadable `judge.json` → a
  normal prompted session for diagnosis (the agent can still read `status`/`logs`
  and `resume`).
- `--interactive` is strictly **additive**: the default `status` output and
  operator-aids' `--json` are unchanged when the flag is absent.

**Test strategy:**

- over a fixture run dir, `launch_monitor` is invoked for the resolved run-id and
  **no** `RunProcess` is started (FR-8.1).
- live driver + `judge.json` → operator-session env; a parked / `none`-liveness
  run → a normal session (no judge env) and the command still succeeds (FR-8.2).
- `status <slug>` with no flag does **not** exec an agent; the existing /
  operator-aids `status` and `--json` tests pass unchanged (FR-8.3).

**Exit criteria:** all FR-8 tests pass; existing `status`/`--json` tests
unchanged. One commit. Reuses P3's launcher + operator-aids liveness.

**Deferrals:** the `--run-id` override (OQ-4) is **not** built — it lands only
if/when operator-aids exposes one; recorded as a deferral, not smuggled in here.

---

## P5 — Frictionless access: `?p=` auth + cookie bootstrap, registry token + auto-port, browser-open, `serve --resume`

**Assumption validated:** zero-copy-paste, port-collision-free console access can
be added **without weakening** the rest of the gauntlet-ui auth posture — the
`?p=` token's exposure is bounded to a single first request, and auto-port never
widens the bind beyond loopback. This is the lowest-risk, web-layer-only slice;
it is independent of P1–P4 and is placed last only to honor riskiest-first (it
could move earlier with no code dependency forcing the order).

**Deliverables (FR-1, FR-2, FR-3, FR-4, §6.1, §7):**

- **`auth.py` + `service.py`/`views.py` (FR-2, §7):** `authenticate()` admits a
  valid `?p=<token>` (constant-time compare) as a **third** source after header
  and cookie. The `_make_auth_dependency` `check` gains a `Response` parameter:
  on the first query-authenticated hit with no valid existing cookie it **mints a
  session + CSRF and sets the httpOnly `SameSite=Strict` cookie** exactly as
  `POST /login` does; a **page GET** is then **303-redirected to the same path
  with `p` stripped** (other query params preserved), so the token never lingers
  in the address bar or history. A **pre-existing valid cookie short-circuits**
  the query check (no per-request session minting). All console responses carry
  **`Referrer-Policy: no-referrer`**. A query-authenticated state-changing request
  is treated like a header one for CSRF (not ambient). Loopback bind,
  constant-time compare, session-bound CSRF, same-origin on cookie POSTs, and
  `/login`+`/healthz` as the only unauthenticated routes are **unchanged**.
- **`registry.py` (FR-3, §6.1):** `ConsoleRecord` gains an **optional** `token`
  field (the serve token in clear; loopback-scoped, gitignored). `ensure_console`
  **auto-selects a free loopback port** when the requested one is held by an
  unrelated process: scan the bounded window `[requested, requested+50]`
  inclusive (51 candidates), bind the first free loopback port; if all are taken,
  fall back to a single OS-ephemeral bind (port `0`). Every candidate passes the
  existing loopback guard so the bind surface never widens beyond `127.0.0.1`.
  `ConsoleHandle` surfaces the token on **reuse** too (rebuilt from the record).
  **Migration (normative §6.1):** a legacy tokenless live record is **reused**
  (no new process/port) and surfaces the **`/login`** URL — it is **not**
  rewritten on reuse, and never reclaimed as stale solely for lacking a token.
- **`web/launch.py` (new, FR-1):** `open_authenticated(handle, *, no_browser)`
  builds the loopback `?p=<token>` URL and calls `webbrowser.open` **only** on a
  TTY with `--no-browser` unset and a known token; otherwise it prints the URL.
  It **falls back to `/login`** when the token is unknown/absent (incl. a legacy
  tokenless record) and is **fail-soft** — a `webbrowser.open` failure never
  aborts the caller. One helper so `run --watch` and `serve --resume` behave
  identically.
- **`cli.py` (FR-1, FR-4):** `run` gains `--no-browser` and now calls
  `open_authenticated` under `--watch`; `serve` gains `--resume` and
  `--no-browser`. `serve --resume`: reuse a live console → open browser + return
  (no bind, no block); else boot the console **detached**, await healthz, open
  the browser, return. A non-answering detached boot fails closed naming the log
  path (reuses the existing `ConsoleBootError`). Plain `gauntlet serve` is
  unchanged.

**Test strategy:**

- FR-1: `webbrowser.open` stubbed + `isatty()` forced true → called once with a
  loopback `?p=` URL; `--no-browser`/no-TTY → not called, URL still printed;
  `webbrowser.open` raising → run proceeds, `/login` printed; legacy tokenless
  live record → **reused** (no new process/port), `/login` surfaced, record left
  unrewritten; the existing `run --watch` reuse test passes unchanged.
- FR-2: valid `?p=` → 200 and `Set-Cookie` bootstrap header; cookie-only follow-up
  → 200; invalid `?p=` → 401 / login redirect; pre-existing cookie + `?p=` →
  session count does not grow; page GET to `/?p=<valid>` → 303 to the same path
  **without** `p` (bootstrap cookie on the redirect; redirected request → 200);
  served page links/forms carry **no** `p`/token; `Referrer-Policy: no-referrer`
  present; existing CSRF/same-origin tests pass unchanged.
- FR-3: a dummy listener on the default port → console comes up on a different
  port reflected in registry + URL; the full `[requested, requested+50]` window
  occupied → OS-ephemeral fallback (outside the scan window); a non-loopback
  `--host` still rejected with no port scan; a registered live console on the
  requested port still reused (no new process/port).
- FR-4: live registered console → no new process, browser opened; none → detached
  boot (healthz passes) + browser opened + command returns; non-answering boot →
  non-zero exit naming the log path; plain `serve` tests pass unchanged.

**Exit criteria:** all FR-1/FR-2/FR-3/FR-4 tests pass; the existing
`test_web_auth.py`, `test_web_registry.py`, `test_cli_watch.py`, and serve/CSRF
suites pass unchanged. One commit.

**Deferrals (OQ-1):** P5 implements the **reusable** `?p=` token form ratified by
the author (§7). The stronger **single-use, short-TTL bootstrap token**
alternative (OQ-1) is **not** built; electing it would change only FR-2's token
lifecycle and is recorded as the reviewer-electable alternative, not implemented
ahead of need.

---

## Cross-phase notes

- **No approved-artifact amendment.** `policy.yaml`, `PRD-gauntlet.md`, and every
  approved `plan.md` are untouched (PRD §2.2, CLAUDE.md §8). The
  operator-vs-in-run distinction rides the **existing** `step_id`-keyed policy
  behavior — no new rule.
- **Fail-closed everywhere it matters.** Monitor authz (P3/P4), judge reaping
  (P2), and auto-port (P5) all default to the safe outcome on any doubt: a normal
  prompted session, no signal, or loopback-only.
- **Simplest design per phase.** No speculative abstraction: the env contract is a
  flat dict, reaping is one helper, auto-port is a bounded scan + one ephemeral
  bind, and `?p=` reuses the existing session/CSRF machinery. Anticipated-but-
  unneeded extensions (codex direct-prompt if the spike disfavors it, the
  one-time bootstrap token, a `--run-id` override) are named deferrals above, not
  built ahead of need.

---

## Machine-readable phase list

```gauntlet-phases
- id: P1
  title: judge.json lifecycle + operator-session env + classification proof
  goal: ManagedJudge writes/removes a gitignored 0600 judge.json (FR-5) and the operator-session env contract (§6.3) is defined; the FR-10 classification proof passes against the unchanged policy.yaml — git push / gh pr create flip operator-allow↔in-run-deny on step_id presence, gh pr merge denied in both. Validates the load-bearing §1.3 assumption before any launcher depends on it.
- id: P2
  title: Orphaned-judge reaping on abort/finish/clean
  goal: abort/finish/clean reap an orphaned judge iff its own identity verifies and driver_liveness says the owner is gone (orphaned/none), signalling the recorded pgid and removing judge.json; the shared console is never killed. Validates that a wedged judge can be terminated safely and identity-checked.
- id: P3
  title: run --interactive — detached run + foreground monitor + starter prompt
  goal: run --interactive launches the run detached via RunProcess and foregrounds the shared monitor (interactive.py) wired as the operator when judge.json is present and the driver is alive, else a normal prompted session; includes the codex starter-prompt feasibility spike (FR-7, FR-9, OQ-2). Validates the detach/foreground handoff and honest degradation.
- id: P4
  title: status --interactive — attach the monitor to an existing run
  goal: status --interactive attaches the same P3 monitor to a run started without it, selecting operator-vs-prompted authz from driver liveness, starting no new run; default status/--json output is unchanged (FR-8, FR-9). Validates the attach path reuses the launcher and operator-aids selection.
- id: P5
  title: Frictionless access — ?p= auth, registry token + auto-port, browser-open, serve --resume
  goal: Loopback ?p= auth with cookie bootstrap + p-stripping redirect (FR-2), ConsoleRecord token + bounded auto-port (FR-3, §6.1), open_authenticated + run --watch browser-open (FR-1), and serve --resume (FR-4). Validates zero-copy-paste, port-collision-free console access without weakening the rest of the gauntlet-ui posture; web-layer only, independent of P1–P4.
```
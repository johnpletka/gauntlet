# Implementation Plan: Gauntlet Console — Supervisory Web UI

**Source:** `runs/gauntlet-ui/prd.md` (PRD: Gauntlet Console, Draft v0.3)
**Branch:** `gauntlet/gauntlet-ui` (FR-9.1)
**Status:** Awaiting human ratification. No P1 work begins until approved.
**Relationship to core spec:** Realizes the deferred "TUI/dashboard" item from `PRD-gauntlet.md` §2.2. Does **not** amend that document or `PRD-gauntlet.md`; adds a new, separately-gated surface (`src/gauntlet/web/`) above the existing engine.

---

## 0. Ground rules (apply to every phase; not repeated below)

- **Process (FR-9.2, CLAUDE.md §3):** one phase at a time (FR-10.3). Each phase: implement → `uv run pytest` green (unit; integration locally before handoff) → single commit (`PN: <imperative ≤72 chars>` + body naming the validated assumption and FR refs) → review handoff → triage → fix commits `PN.x: Address review — …` → confirm pass → human gate. No phase begins before the prior phase's gate clears.
- **The central invariant:** the console **never re-implements the engine**. Every control action launches the *same `gauntlet` CLI verb a human would type* (`run`, `resume`, `approve`, `reject`, `abort`) as a subprocess. The read surface only parses on-disk state (`manifest.json` + artifacts). This is the safety guarantee (D6, FR-10.1–10.3) and it is a structural constraint on every phase, not a P-specific deliverable.
- **Exactly one sanctioned engine phase (D7), containing exactly two explicitly approved engine modifications, both landed in P3 and nowhere else:**
    1. the run-dir allocation handshake (`run --run-id`, FR-6.1a) — touches **only** `RunManager.start()` (the `run_id = f"run-{_utc_stamp()}"` mint at `src/gauntlet/engine/run.py:318`) and the `gauntlet run` typer command in `cli.py`;
    2. the repo/worktree-scoped active-run lock (FR-10.5) — touches **only** `RunManager._refuse_if_active_run()` (`src/gauntlet/engine/run.py:260`) and its call site in `RunManager.start()`.
  No other engine file or API may be modified in P3 or any other phase. Everything else is strictly above the orchestrator. Any temptation to touch a third engine seam — or to widen either of these two beyond the named files/APIs — is an **UPSTREAM CONFLICT** (FR-10.4), not a quiet edit.
- **Module layout (fixed across phases):** `src/gauntlet/web/` as a sibling of `src/gauntlet/judge/`. Submodules introduced in the phase that first needs them: `service.py` (app factory), `runner.py` (uvicorn host), `store.py` (read model), `watcher.py`, `supervisor.py` + `jobproc.py`, `notify.py`, `views.py`, `templates/`, `static/`. The `gauntlet serve` typer command is added to `cli.py` in P1 and extended in later phases.
- **Stack / deps (FR-11.3):** FastAPI + uvicorn (already direct). `httpx` and `jinja2` promoted from transitive to **explicit** `pyproject.toml` deps in P1 (the first phase that imports them). HTMX vendored as one static file. No `watchdog`, no `websockets`, no frontend build step. Net new heavy deps: zero (M5).
- **Test taxonomy:** unit/`TestClient` tests run anywhere (CI, no creds), mirroring `tests/.../test_judge_service.py`. Subprocess-lifecycle tests mirror the crash/resume test (`test_resume_crash.py`) — `Popen` + `SIGKILL`. Live end-to-end UI smoke is `@pytest.mark.integration`. The suite only grows; never delete or skip a passing test to make a phase pass.
- **Fail-closed everywhere (CLAUDE.md §2):** parse error, missing artifact, unverifiable PID, absent token → deny/halt, never silent continue.
- **Upstream invalidation (FR-10.4):** if implementation reveals the PRD is wrong (e.g. the manifest does **not** carry a field the read model needs), halt and surface the conflict; do not amend `prd.md`.
- **Reused engine facts the plan commits to** (verified against the tree, so a phase's assumption is grounded, not hoped):
    - `engine/manifest.py` already defines every `StepRecord.status` FR-5 keys on — `pending, running, done, failed, interrupted, parked, halted, skipped` — and run statuses `running, parked, done, aborted, failed`. **v1 adds no manifest fields** (OQ-2 confirmed by reading the model).
    - `Manifest` carries `current_step`, `steps[]`, `commits[]` (`{step_id, phase, sha}`), `totals`, `agent_usage`, `warnings`, and `StepRecord.base_sha` — every field the read model and FR-4.3 phase-diff selection require.
    - `engine/gitops.py` already exposes `range_diff`, `log_range`, `commit_subject`, `commit_message`, `head_sha` — FR-4.3 needs no new git helper.
    - `RunManager.start()` currently mints `run_id = f"run-{_utc_stamp()}"` internally and calls per-slug `_refuse_if_active_run()`; these are the exact two seams P3 modifies (FR-6.1a, FR-10.5).
    - The judge service (`judge/service.py`) is the template for loopback bind + constant-time `hmac.compare_digest` token + `/healthz`; the console diverges only in token *delivery* (cookie, not header) and CSRF (FR-10.4/10.6).

---

## Phase ordering rationale

Risk is retired front-to-back, and the ordering matches PRD §8 (which the human ratifies — the prose plan must not drift from it):

1. **P1** kills the foundational assumption — *the on-disk manifest + artifact layout is sufficient to render full run state with zero engine changes.* If false, the whole "read model above the engine" thesis collapses, so it goes first and cheapest.
2. **P2** validates the *liveness* mechanism (1s mtime poll → edge-triggered event → SSE) with no new dependency, before any process ownership rides on it.
3. **P3** retires the highest-consequence assumptions together: subprocess ownership of a real `gauntlet run`, the run-dir handshake, and **the one sanctioned engine phase's two approved modifications** (run-id handshake + worktree lock). The lock is meaningless to test without supervised launch to create contention, so they land in the same phase — but the lock is still real engine enforcement, not UI.
4. **P4** validates crash survival / re-attach (PID-reuse safety) — only testable once P3 can launch an owned run.
5. **P5** builds the human-value surface (gates, recovery, control verbs) on the now-proven launch + read substrate.
6. **P6** adds notifications (fan-out over the proven P2 watcher).
7. **P7** is ergonomics/polish (search, full-history `run_id` selection, `--watch` console registry, the `/login` cookie + CSRF auth flow) — deliberately last so the durable auth/session story is built once the surface it protects is stable.

Each phase ends green and committed; no phase imports a module a later phase delivers.

---

## P1 — Read-only observer MVP

**Assumption validated:** the existing `manifest.json` + on-disk run layout suffice to render full run state (list + step tree) with **zero engine changes** — the load-bearing premise of the entire design (D2, FR-11.2).

**Deliverables**
- `web/store.py` — `RunStore`, read-only. Discovers slugs under the configured run root (reusing `RunLayout`/`RunManager` dir iteration, never importing the orchestrator drive path), and parses each `manifest.json` via the existing pydantic `Manifest`. Provides: list view rows (slug, status, current step + its status, cost-to-date, age, owned/observed — owned always `false` in P1, no supervisor yet), full-manifest fetch with optional `run_id`, and step detail (which artifacts exist + sizes; nested round dirs for `adversarial_cycle`). All file reads path-contained under repo root (reject `..`).
- `web/service.py` (`create_app(...)`) + `web/runner.py` (uvicorn host) — a FastAPI clone of the judge's posture: bind `127.0.0.1` only (refuse non-loopback), per-serve token via `GAUNTLET_WEB_TOKEN` env override (constant-time compare), `/healthz` unauthenticated. **P1 token delivery is the simple header/`?token=` bootstrap parity with the judge**; the full `/login` cookie + CSRF flow is explicitly **deferred to P7** (named below).
- `gauntlet serve [--host 127.0.0.1] [--port N]` typer command in `cli.py`: resolves config via `RunConfig.load` with `asset_root` fallback `"."`, validates it is inside a git repo (fail-closed otherwise), prints the token + URL on startup (FR-11.1).
- Read endpoints: `GET /api/runs`, `GET /api/runs/{slug}[?run_id=]`, `GET /api/runs/{slug}/steps/{step}[?run_id=]`. `run_id` honored from day one (FR-2.4 is unsatisfiable at the API layer otherwise); unknown id → 404; omitted → latest/active.
- Minimal Jinja run-list + run-detail pages (`views.py`, `templates/`, vendored static CSS) — static render, no live updates yet.
- `pyproject.toml`: promote `httpx`, `jinja2` to explicit deps (FR-11.3).

**Test strategy**
- `TestClient` over a committed fixture run-dir tree (several slugs; at least one with an `adversarial_cycle` step that has nested rounds; one historical + one "latest" run for the same slug).
- Assert: loopback guard rejects a non-loopback host; missing/bad token → 401; `/api/runs` shape matches the §6 contract; `?run_id=` selects the historical run and an unknown id 404s; step tree renders cycle rounds; path-traversal in the **`slug`, `run_id`, and `step` path segments** (the only user-controlled path inputs P1 exposes) is rejected. P1 defines **no** user-selected artifact/log *file* parameter, so file-path traversal tests belong to the phases that introduce such inputs — the log path in P2 and gate `show:`-artifact resolution in P5 — and are specified there, not here (review F-006).

**Exit criteria:** all the above green under `uv run pytest`; `gauntlet serve` boots, prints token+URL, serves the two pages over loopback against the live `runs/` tree; zero engine files modified (the assumption's proof).

**Deferrals (explicit):** no live updates (P2); no supervisor/owned badge (P3, badge is hard-coded `observed`); no `/login` cookie or CSRF (P7); no gate/diff/control/notify.

---

## P2 — Live freshness (poll + SSE)

**Assumption validated:** a single ~1s `mtime` poll of each known `manifest.json` catches **every** state transition (atomic `os.replace` writes mean no torn reads), and SSE pushes them to the browser **with no new dependency** (D4).

**Deliverables**
- `web/watcher.py` — one async task that stats each known `manifest.json` ~1×/s and rescans for new run dirs on the same tick. **File-change detection and semantic-transition identity are separate concerns** (review F-002): a change in the manifest `mtime` (nanosecond `st_mtime_ns`) is used **only** as a cheap gate to decide whether to re-parse the file — it is **not** part of the emitted-event identity. A transition is emitted (edge-triggered, FR-8.1) only when the **semantic identity tuple `(run_id, current_step, current_step_status, run_status)`** changes after re-parse. Consequence: an atomic rewrite that preserves semantic state (e.g. `os.replace` of a byte-identical-state manifest — new `mtime`, same tuple) triggers a re-read but **emits nothing**, so semantic no-op rewrites never produce phantom transitions or duplicate downstream notifications in P6. Remembers the last-seen **semantic tuple** per `run_id`; emits each distinct transition exactly once. Publishes to an in-process async event bus.
- `GET /events` — SSE stream of run-list-level transitions; HTMX-driven live row updates on the run-list and run-detail pages. Auto-reconnect re-reads current state on connect.
- Live log tail: `GET /api/runs/{slug}/steps/{step}/log[/stream]` with `?from=<offset>` byte-offset deltas over `events.jsonl`/`transcript.md`; `/stream` = SSE of appended lines, scoped to *open* viewers only (R5).
- Freshness target ~2s p95 in an open UI (FR-8.3).

**Test strategy**
- Drive a fixture manifest through a scripted sequence of atomic writes; assert exactly **one** edge-triggered event per transition under the **semantic identity tuple** `(run_id, current_step, current_step_status, run_status)` — including a fixture that **parks at two distinct gates** (different `current_step`) and asserts **both** transitions emit and are *not* collapsed (the coarse `(run_id, status)` keying the PRD rejects would swallow the second).
- Assert a **semantic no-op rewrite** (atomic `os.replace` of the manifest that changes only `mtime`, leaving every semantic field identical) emits **nothing** — proving `mtime` is a re-read gate, not part of the event identity (review F-002).
- Assert the SSE endpoint yields those events in order; assert a re-poll of an unchanged (untouched) manifest emits nothing; assert log-tail returns only bytes after `?from=` and that a traversal-laden `step` segment is rejected (the log path is the first user-selected file path; see F-006).

**Exit criteria:** green; an open run-list updates live as a fixture manifest advances, with no manual refresh and no new dependency.

**Deferrals:** notifications still off (the watcher's event bus is built here, but `notify.py` is wired in P6); owned-run log tail (`.serve/…log`) waits for the supervisor (P3).

---

## P3 — Supervised launch + active-run lock (the one sanctioned engine phase)

**Assumption validated:** a run launched as a `Popen` of `gauntlet run` is fully observable, controllable, and reapable — **and can never be double-driven against one worktree**. This phase is the **sole sanctioned engine phase (D7)** and makes exactly the **two approved engine modifications** named in the ground rules (run-id handshake + worktree lock), so it is isolated and heavily tested.

**Deliverables**
- **Engine change 1 — run-dir allocation handshake (FR-6.1a).** Thread an optional `run_id` through `RunManager.start()` and expose it as `gauntlet run <slug> --run-id <run-…>` (equivalently `GAUNTLET_RUN_ID`). When supplied, the engine uses it verbatim instead of minting one, and **errors if a run dir of that id already exists** (single-use). For `resume/approve/abort` the run dir already exists via `active-run.txt`, so no pre-allocation is needed.
- **Engine change 2 — repo/worktree-scoped active-run lock (FR-10.5).** Generalize the existing per-slug `_refuse_if_active_run` into a **worktree-global** advisory lock at `<run_root>/.driving.lock` (gitignored), recording `{nonce, slug, run_id, pid, pgid, started_at, host, proc_create_time}`, where **`nonce` is a unique per-acquisition random token** (e.g. `secrets.token_hex(16)`) generated fresh on each acquire and held in memory by the owner. Acquired **first** by every driving verb (`start`/`resume`/`approve`) via `O_CREAT|O_EXCL` (no TOCTOU), before any run dir / `active-run.txt` / git touch; released on park/done/error/process-exit. A lock held by a **live** pid (verified by `os.kill(pid,0)` **and** matching `proc_create_time` per the ProcessIdentity contract) fail-closes the verb regardless of slug; a dead-pid or PID-reused lock is reclaimed as stale.
  - **Ownership-validated release (review F-004).** Release is **not** an unconditional `unlink`. The holder re-reads `.driving.lock`, and unlinks **only if** the file still contains **its own `nonce`**; if the nonce differs (the holder was already reclaimed as stale and a *new* owner now holds the lock) the release is a **no-op** and is logged. This closes the race where a stale-lock reclaimer acquires a fresh lock and the original holder's late cleanup would otherwise unlink the **new** owner's lock and re-open double-driving. Stale-reclaim itself likewise overwrites the file with the reclaimer's own nonce atomically (`O_CREAT|O_EXCL` on a tmp + `os.replace`, or unlink-then-exclusive-create), so exactly one nonce is authoritative at any instant.
- `web/jobproc.py` (`RunProcess`) — a near-clone of `ManagedJudge`: `subprocess.Popen([python, "-m", "gauntlet", <verb>, slug, *flags], cwd=repo_root, start_new_session=True)`, combined stdout/stderr captured to `run_dir/.serve/…log`, `.stop()` that `killpg`s the whole group (terminate → wait → kill), since a run spawns agent CLIs + a judge as grandchildren.
- `web/supervisor.py` (`JobSupervisor`) — pre-allocates the child's `run_id`, derives `run_dir`, `mkdir -p`'s `run_dir/.serve/` **before** launch, opens the captured log and writes `run_dir/.serve/job.json` (`{pid, pgid, verb, started_at, log_path, proc_identity}`) atomically at launch. `proc_identity` is a structured value per the **ProcessIdentity contract** below (not a raw platform string), so a value captured at launch (P3) is comparable to a value re-read at re-attach (P4) without unit/locale ambiguity. **Bootstrap-log fallback** for the pre-manifest window: if pre-allocation is unavailable, write to `<run_root>/.serve-bootstrap/<slug>-<pid>.log` + provisional `job.json`, migrate into `run_dir/.serve/` once the manifest appears.
- **ProcessIdentity contract (D7 / FR-7.2 PID-reuse safety — review F-001).** A single helper, `read_process_identity(pid) → ProcessIdentity | None`, is the only producer of these values; it is used both at launch (P3 `job.json` / lock record) and at liveness check (P4 re-attach). The value is a structured, platform-tagged, integer-normalized record so comparison is exact and never compares across platforms or units:
    - **Shape:** `{"platform": "linux"|"darwin", "value": <int>, "unit": "boot_ticks"|"epoch_seconds"}`. Serialized verbatim into `job.json` and `.driving.lock`. `proc_create_time` (used loosely elsewhere in this plan) **is** this record.
    - **Linux:** `value` = field 22 (`starttime`) of `/proc/<pid>/stat`, parsed as an integer, `unit = "boot_ticks"`. Precision is exact (integer clock ticks since boot); no normalization beyond `int()`. Parse `/proc/<pid>/stat` by splitting on the **last** `)` first (the comm field may contain spaces/parens) and indexing field 22 from the remainder.
    - **macOS (`darwin`):** `value` = the process start time parsed from `ps -o lstart= -p <pid>` **into integer epoch seconds** (UTC), `unit = "epoch_seconds"`. The subprocess is run with **`LC_ALL=C` / `LANG=C`** in its env so the output is the fixed C-locale form (`"Wed Jun 17 09:04:21 2026"`), parsed with the fixed `strptime` format `"%a %b %d %H:%M:%S %Y"` interpreted in the **local** timezone and converted to epoch seconds. Precision is **1 second** (lstart's granularity); since a process's start time is fixed for its lifetime, 1-second precision is exact for identity, not approximate.
    - **Comparison (replaces the vague "within tolerance"):** two identities are "same process" **iff** `platform`, `unit`, and integer `value` are **all equal** — i.e. **exact equality, tolerance 0**. Both representations are stable integers for a process's lifetime, so no fuzzy window is needed or allowed; a non-zero tolerance would *weaken* PID-reuse safety.
    - **Parsing/acquisition failure → `None` → fail-closed:** any unreadable `/proc` entry, missing/empty/locale-unexpected `ps` output, unparseable timestamp, or **unsupported platform** (anything other than `linux`/`darwin`) yields `None`. A recorded or current `None` is treated as **unverifiable**, which P4 maps to fail-closed (orphan, `resume` offered) — never to a re-attach. Windows/other platforms are therefore explicitly *supported only in fail-closed mode* in v1.
    - **Bootstrap-log fallback** for the pre-manifest window: if pre-allocation is unavailable, write to `<run_root>/.serve-bootstrap/<slug>-<pid>.log` + provisional `job.json`, migrate into `run_dir/.serve/` once the manifest appears.
- `POST /api/runs {slug, pipeline?, no_judge?}` → `gauntlet run <slug> --run-id <pre-allocated> [...]`; `POST /api/runs/{slug}/abort` → `gauntlet abort <slug>`. Owned/observed badge now real (FR-1.4); `--no-judge` exposed only as the existing unsafe flag, defaulted off, with a visible warning (FR-6.5). Owned-run combined log exposed via the P2 log-tail path (FR-3.3).
- UI: while *any* run holds the worktree lock, show the holder as **running (external)** if not console-owned and **disable Launch/Resume/Approve for every slug** with a banner naming the holder (FR-1.4/10.5 — surface only; enforcement is the engine lock).

**Test strategy**
- Launch a trivial fixture pipeline via the supervisor: assert `job.json` written with a well-formed `proc_identity` (`platform`/`unit`/integer `value`) per the ProcessIdentity contract, running→done observed, captured log present under the pre-allocated `run_dir/.serve/` from the first byte, process-group reaped on `.stop()`.
- **ProcessIdentity contract (review F-001):** a platform-gated test of `read_process_identity` for the host (`@pytest.mark.skipif` on `sys.platform` for the linux/darwin branch not running) asserting a real pid yields the right `unit` and a stable integer across two reads; a unit test asserting the macOS parser is locale-pinned (feeds a fixed C-locale `lstart` string → expected epoch seconds, independent of ambient `LANG`); and that an unobtainable/unparseable input and an unsupported `sys.platform` both yield `None`.
- Bootstrap fallback: a child killed **before** the manifest exists leaves a readable bootstrap log and is classified a **failed launch** (no phantom owned run).
- Lock (engine, mirrors the crash/resume subprocess style): a second `resume`/`approve` **and a different-slug `start`** against a live-locked worktree both **fail closed**; concurrent acquisition yields exactly one holder; a stale dead-pid lock **and** a PID-reused lock are both reclaimed; lock released on park/done/error/process-exit.
- **Stale-reclaim races late cleanup (review F-004):** simulate holder A going stale (dead/reused pid), holder B reclaiming and writing its own `nonce`, then A's deferred release running afterward — assert A's release is a **no-op** (nonce mismatch) and **B's lock survives intact**, so the worktree is never left unlocked while B is still driving. Assert the inverse too: A releasing while it still legitimately owns the lock (its nonce present) **does** unlink.

**Exit criteria:** green; a console-launched run drives to completion as an owned subprocess with captured logs; the worktree lock provably blocks cross-slug double-driving; exactly the two approved engine modifications were made — `RunManager.start()` (+ `gauntlet run --run-id` in `cli.py`) per FR-6.1a and `RunManager._refuse_if_active_run()` per FR-10.5 — and no other engine file changed.

**Deferrals:** re-attach after server restart (P4); gate resolution / control verbs beyond `run`+`abort` (P5).

---

## P4 — Re-attach & crash survival

**Assumption validated:** the server holds **no authoritative state** (D2/D3) — a restart re-discovers owned runs from disk, and an orphaned owned run is recoverable on the *same* path as a `kill -9`'d run (`resume`), with PID-reuse safety.

**Deliverables**
- Startup re-discovery: scan run dirs for `.serve/job.json` (FR-7.1).
- **PID-reuse-safe liveness check (FR-7.2):** a PID counts as the original live process only if `os.kill(pid,0)` succeeds **and** `read_process_identity(pid)` returns a value that is **exactly equal** (per the ProcessIdentity contract's `platform`/`unit`/integer-`value` equality — **tolerance 0**) to the `proc_identity` recorded in `job.json`. Outcomes: exact match → re-attach (reopen captured log for tailing); dead PID, or alive-but-mismatch (reuse), or **either** the recorded **or** the freshly-read identity being `None` (unobtainable/unsupported-platform → unverifiable) → **fail closed**: classify **owned, interrupted — resume available** for a non-terminal manifest and remove the stale sidecar.
- Re-attach is the *same* recovery path as `kill -9` (FR-7.3): state from the manifest, recovery via `resume`; the server persists no run state of its own.

**Test strategy (headline test of the design):**
- Launch an owned run, `SIGKILL` the server's process group, start a fresh server, assert re-discovery + correct classification + `resume` recovery to `done` with **exactly one** set of effects. Parametrize kill timing (before manifest, mid-step, between steps).
- Assert a job whose `proc_identity` no longer matches (simulated reuse — same pid, different `value`) **or** is `None` (recorded or freshly-read, incl. unsupported-platform) is classified **fail-closed orphan**, not re-attached.

**Exit criteria:** green; the headline restart-survival test passes (M3); no server-side authoritative state introduced.

**Deferrals:** none new; P5 builds on the now-proven owned-run substrate.

---

## P5 — Gates, recovery intelligence & control actions

**Assumption validated:** the UI can resolve a parked gate's `show:` artifacts, render a deterministic phase diff, and drive approve/reject/resume via **sanctioned verbs** without bypassing any invariant — the human-value core (G2/G3/G5).

**Deliverables**
- `resume_intel(manifest) → {state, recommended_action, rationale, available_controls}` (FR-5.1) — a **pure** classifier keyed on the **existing `StepRecord.status` enum**, using `notes` substrings *only* to split the two `halted` sub-cases (timeout vs budget). The recognized note vocabulary is **normative and grounded in what the engine actually writes** (not builder-invented), so a wording drift is caught by a test rather than silently mis-classifying:
    - **timeout halt** — `notes` contains the case-insensitive substring **`"timeout halt"`** (the engine emits `"timeout halt (FR-3.3): …"` at `orchestrator.py:244` and `"shell timeout halt (FR-3.3): …"` at `steptypes.py:86`, both of which contain it).
    - **budget halt** — `notes` contains the case-insensitive substring **`"budget halt"`** (engine emits `"budget halt (FR-3.3): …"` at `orchestrator.py:324`).
    - **Matching rule:** case-insensitive substring containment; **precedence** when both substrings appear (and the disambiguation when **neither** appears) resolves **fail-closed to a generic halt** — `recommended_action` = "inspect the captured log/diff; raise the relevant guard before resuming" — never a confident timeout-vs-budget guess. This keeps an ambiguous or unrecognized note from producing a misleading single-cause recommendation.
    - This is the **only** place `notes` text is interpreted; every other field comes from the typed status enum. The structured `halt_kind`/`gate_kind` enum that would remove this string match entirely is the named v1→v2 deferral below.
  Implements the full FR-5.2 mapping (gate→Approve/Reject; interrupted→Resume; timeout-halt→"resume re-triggers; raise timeout first" guidance; budget-halt→"raise budget_usd first" guidance; ambiguous/unknown halt→generic "inspect log/diff; raise the relevant guard" guidance; failed→"resume won't help"+log/diff; `rejected:`→terminal). Never offers a meaningless control (FR-5.3).
- `GET /api/runs/{slug}/gate[?run_id=]` — resolves the gate's `show:` list (read from the run's snapshot `pipeline.yaml`), each name resolved first against `run_dir/artifacts/<name>` then the slug-dir artifact root (FR-4.2). Detect gate via `current_step` → step with `status==parked` and `type==human_gate` (FR-4.1).
- `GET /api/runs/{slug}/diff?from=&to=[&run_id=]` — **deterministic phase-diff commit selection (FR-4.3)** over `manifest.commits[]`: group by `PN` base (phase commit + `PN.x` fix rounds), `to`=last in group, `from`=prior phase's last commit or the gated step's `base_sha` for the first committing phase; uses existing `gitops.range_diff`/`log_range`. Explicit "**no committed diff for this gate**" sentinel + artifact-content fallback for the empty case; explicit SHAs override.
- `POST /api/runs/{slug}/approve|reject|resume` → the corresponding `gauntlet <verb>` child (long-lived `RunProcess` for approve/resume, FR-6.2/R6). Reject requires notes.
- UI: gate-review panel (findings/triage/confirm as readable tables; markdown artifacts rendered; phase-diff view), failure/resume panel driven by `resume_intel`, `judge-audit.jsonl` viewer (FR-3.4), upstream-conflict surfacing (FR-4.5). **Destructive-verb confirm step** for abort/reject (FR-10.7) — a UX-safety confirmation token, not a security control.

**Test strategy**
- Fixture run parked at a real gate: assert resolved artifact contents and table shapes; assert a gate `show:` name containing a traversal (`../…`) or resolving outside the run/slug artifact roots is **rejected** (the gate `show:` list is a user-/pipeline-selected file path — the F-006 file-traversal requirement lands here, not in P1).
- Diff: assert it selects phase + `PN.x` fix commits against the pre-phase base, and returns the empty-case sentinel when no phase commit exists.
- Control: assert approve launches `gauntlet approve` (inspect argv) and the run transitions; assert abort/reject require the confirm token.
- `resume_intel`: **table-driven** over one fixture manifest per FR-5.2 row (keyed on the status enum), so a note-wording change fails a test rather than silently mis-classifying (R3). The table includes the two normative `halted` sub-cases (`notes` containing `"timeout halt"` → timeout guidance; `"budget halt"` → budget guidance) **and** the fail-closed cases: a `halted` note containing **both** markers and a `halted` note containing **neither** both classify to the generic "inspect log/diff; raise the relevant guard" recommendation (review F-003).

**Exit criteria:** green; an operator can, in the UI, read a gate's assembled evidence and Approve/Reject, and see the correct recovery offer per failure shape — all via CLI-verb children (M2, M4).

**Deferrals:** notifications (P6); list search/sort/filter polish, `--watch`, and the cookie/CSRF auth flow (P7).

---

## P6 — Notifications

**Assumption validated:** one watcher trigger fans out to macOS/Slack/in-tab, **fail-soft and edge-triggered**, with **zero** ability to affect a run (FR-9, G4).

**Deliverables**
- `web/notify.py` wired to the P2 watcher event bus. Fires on three transition *kinds* — **gate-reached** (parked at a `human_gate`, distinct from a halt), **run-failed**, **run-completed** — de-duplicated **per `(run_id, transition-kind, current_step)`** (FR-9.1) so each distinct gate notifies once.
- Channels (FR-9.2): macOS desktop (`terminal-notifier` if on PATH, else `osascript`), Slack incoming-webhook POST via `httpx`, browser in-tab `Notification` via SSE. Each message carries slug, run_id, new status, current step + note, and a deep link to `/runs/<slug>`.
- **Fail-soft (FR-9.3):** every channel wrapped so an error is logged and swallowed; the notifier lives in the watcher and owns no run state.
- Config (FR-9.4): new optional `web:` block in `config.yaml` + env fallbacks (e.g. `GAUNTLET_SLACK_WEBHOOK`), additive/backward-compatible, per-channel on/off.

**Test strategy**
- Inject transitions with a stub notifier; assert **one** fan-out per `(run_id, transition-kind, current_step)` — including a fixture parking at **two** gates in different steps → **two** gate notifications; assert an unchanged-manifest re-poll → **zero**; assert a *raising* notifier is swallowed (run unaffected); assert Slack call shape via a mocked `httpx` transport.

**Exit criteria:** green; gate/fail/complete fire to configured channels, fail-soft, edge-triggered (M1).

**Deferrals:** background Web Push (service-worker/VAPID) is **v2** (OQ-3/R4) — in-tab only in v1.

---

## P7 — Polish, durable auth & launch ergonomics

**Assumption validated:** the surface is safe and pleasant for daily supervision, full history review, and one-command watching — and the production auth posture (cookie + CSRF) holds without breaking SSE/HTMX.

**Deliverables**
- **`/login` token-exchange + httpOnly session cookie (FR-10.4):** `GET /login` minimal form; `POST /login` constant-time-compares the serve token, sets `Set-Cookie: gauntlet_web=…; HttpOnly; SameSite=Strict; Path=/` (no `Domain`, `Secure` omitted for loopback http), redirects to the originally-requested path. Token **never** in a URL/query/history/SSE-handshake; the startup/`--watch` URL points at `/login` with a one-time POST-prefilled form. `/login` and `/healthz` are the only unauthenticated routes. Non-browser `/api/*` callers may instead send `X-Gauntlet-Token` (judge parity).
- **Session-bound CSRF (FR-10.6):** per-session token (bound/HMAC'd to the cookie), surfaced via `<meta>` + hidden `_csrf` field, validated constant-time on every cookie-authenticated POST **plus** same-origin `Origin`/`Referer`; `X-CSRF-Token` header for HTMX; rotated on login. Header-authenticated (`X-Gauntlet-Token`) API POSTs are CSRF-exempt.
- List **search/sort/filter** (FR-1.2) and the **full-history browser** per slug with `run_id` selection confirmed on **all** per-run endpoints (`/`, `/{slug}`, `/steps`, `/gate`, `/diff`, `/report`) (FR-2.4); `GET /api/runs/{slug}/report` reusing the existing report renderer (FR-2.4/§6).
- **`gauntlet run --watch` + console registry (FR-12.1–12.4):** ensures a console is running — boots one detached (`start_new_session=True`, logs to `<run_root>/.console.log`) if none, else **reuses** per the registry; runs the pipeline in the foreground; the detached console **persists** after the foreground run returns. Console registry `<run_root>/.console.json` (`{pid, pgid, proc_identity, host, port, url, token_fingerprint, started_at, log_path}`, atomic `os.replace`, where `proc_identity` follows the P3 ProcessIdentity contract); discovery reuses iff PID-alive + `proc_identity` **exactly equal** (tolerance 0; `None` → fail-closed, no reuse) + `/healthz` answers; stale entry reclaimed; reused console keeps its own token (`--watch` prints its `/login` URL); unrelated port collision fails closed. `gauntlet serve` stays the first-class durable standalone command (FR-12.3).
- `--no-judge` warning surfaced in the launch UI; empty/error states throughout.

**Test strategy**
- `run --watch` boots a console, records `.console.json`; a **second** `--watch` reuses it (no second process) while a stale registry entry is reclaimed; the run appears observed then drives to a gate.
- `?run_id=` opens a historical run read-only; unknown id 404s.
- `POST /login` sets the cookie and **no token appears in any URL**; a cookie POST without a valid session CSRF token is rejected while an `X-Gauntlet-Token` API POST is accepted.
- `@pytest.mark.integration` end-to-end smoke: launch → gate → approve → done through the UI.

**Exit criteria:** green (unit + the integration smoke locally); production auth flow works end-to-end over loopback; `--watch` and standalone `serve` both satisfy the discovery/lifecycle contract; full history of any slug is reviewable in the UI without touching the filesystem (M6).

---

## Cross-phase deferrals (named, per FR-10.3)

- Background Web Push (service-worker + VAPID): **v2**, not v1 (OQ-3/R4).
- Structured `halt_kind`/`gate_kind` enum on `StepRecord`: tracked future hardening, **not scheduled** — v1 stays with status-enum + table-tested note disambiguation (OQ-2/R3).
- Cross-repo / multi-repo registry: **deferred** (OQ-4); v1 is one repository per `serve` instance.
- The auth surface is intentionally simple (header/`?token=` bootstrap) in P1–P6 and only hardened to cookie+CSRF in **P7**; earlier phases must not pre-build it (it would be smuggling P7 work forward).

---

## Machine-readable phase list

```gauntlet-phases
- id: P1
  title: Read-only observer MVP
  goal: Render full run state (list + step tree) from manifest.json and on-disk artifacts via a loopback FastAPI app with zero engine changes; validates that the existing on-disk layout is a sufficient read model.
- id: P2
  title: Live freshness (poll + SSE)
  goal: A 1s mtime poll emits one edge-triggered event per transition (keyed on the full identity tuple) and SSE pushes it to the browser; validates live updates need no new dependency and never collapse distinct gate transitions.
- id: P3
  title: Supervised launch + active-run lock
  goal: Launch gauntlet run as a reapable Popen child with captured logs and a run-dir handshake, plus the one sanctioned engine phase's two approved modifications (run-id handshake + worktree-scoped active-run lock); validates owned runs are observable and can never be double-driven.
- id: P4
  title: Re-attach & crash survival
  goal: On restart, re-discover owned runs from .serve/job.json with PID-reuse-safe liveness and recover orphans via resume; validates the server holds no authoritative state.
- id: P5
  title: Gates, recovery intelligence & control actions
  goal: "Resolve a parked gate's show: artifacts, render the deterministic phase diff, and drive approve/reject/resume via sanctioned CLI verbs with a pure resume_intel classifier; validates the human-value surface bypasses no invariant."
- id: P6
  title: Notifications
  goal: Fan out gate-reached / run-failed / run-completed to macOS, Slack, and in-tab, fail-soft and edge-triggered per (run_id, kind, current_step); validates one watcher trigger notifies without affecting a run.
- id: P7
  title: Polish, durable auth & launch ergonomics
  goal: Add list search/sort/filter, full-history run_id browsing on all endpoints, run --watch with a console registry, and the /login cookie + session-bound CSRF auth flow; validates daily supervision and the production auth posture.
```

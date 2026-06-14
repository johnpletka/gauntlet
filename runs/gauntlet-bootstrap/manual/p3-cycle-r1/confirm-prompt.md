# Confirm pass — Gauntlet bootstrap, Phase P3, round 1

You produced the findings below against commit `77570f9` (P3). The builder
applied fixes in commit `bb46479` ("P3.1: Address review — close 9 findings").
Check ONLY whether the diff addresses each finding — do not re-review the whole
phase. Scope yourself to the diff. For each finding return a verdict
`resolved | partially_resolved | unresolved | regression_introduced` with a
one-or-two-sentence note. If the diff itself introduces a new defect, report it
in `new_findings` (else empty array). Output only JSON conforming to the schema.

All 9 findings were triaged `legitimate` / `fix_now` and addressed. Key design
choices to evaluate the fixes against:
- F-001: the run-root exclusion was narrowed to ONLY the live bookkeeping
  (run-instance dir + active-run pointer) via `run_bookkeeping_excludes`; real
  artifacts under runs/<slug> are now tracked/detected/committed. The
  reset_to_base `git clean` is intentionally still broad (spares the whole run
  root) so a rewind never wipes the run setup — the re-run regenerates outputs.
- F-006: max_turns is unenforceable on the pinned CLIs (claude 2.1.172 has no
  --max-turns; codex exec has no turn cap — verified), so it is REJECTED at load
  rather than silently ignored; timeout_s + budget_usd are the working halts.

## Your prior findings (round 1)
```json
{"findings":[{"id":"F-001","severity":"blocking","category":"correctness","location":"src/gauntlet/engine/gitops.py:63; src/gauntlet/engine/run.py:331; src/gauntlet/engine/steptypes.py:111; src/gauntlet/engine/orchestrator.py:263","claim":"The run-root exclusion is broader than the ratified live-run bookkeeping boundary, so real run artifacts under `runs/<slug>` are invisible to dirty checks, backups, and commits.","evidence":"`_exclude_pathspec()` excludes both `runs` and `runs/**`; `RunManager` sets `artifact_root=layout.slug_dir`; `agent_task` writes declared outputs to `ctx.artifact_root / output`; resume/backup/commit all pass `exclude=[self.config.run_root]`. A killed step that writes `runs/<slug>/plan.md` or another declared artifact before manifest completion will look clean versus `base_sha`, so the default `interrupted_step=park` policy silently reruns over a real partial effect. This violates the P3 transaction-boundary deliverable that dirty partial edits are detected and never silently rerun.","suggested_fix":"Exclude the exact live run-instance bookkeeping paths (`run_dir`, active pointer, manifest/transcripts) rather than the whole `run_root`, or move declared artifacts outside the excluded subtree. Add a crash test where a killed step leaves a partial declared output under `runs/<slug>`."},{"id":"F-002","severity":"major","category":"correctness","location":"src/gauntlet/engine/run.py:259","claim":"Rollback rewinds the branch but leaves downstream non-commit step records marked done, so a later resume can skip work that `git reset --hard` removed.","evidence":"After resetting to the target SHA, rollback computes `dropped_step_ids` only from dropped commit records and resets only those step records. In a two-phase run, rolling back to P1 leaves P2 implementation/test records as `done` while only the P2 commit step becomes pending; resume then skips the P2 work and reaches a clean-tree commit step. FR-9.9 requires branch and manifest to never disagree.","suggested_fix":"Rewind all manifest step records after the target phase boundary in execution order, not just dropped commit-step IDs. Clear their base SHAs/session IDs as appropriate and set `current_step` to the first rewound step."},{"id":"F-003","severity":"major","category":"correctness","location":"src/gauntlet/engine/run.py:232","claim":"Rollback accepts a branch that is ahead of the manifest, allowing unmanifested commits to be discarded by reset.","evidence":"The guard only rejects when `last_recorded` is not an ancestor of `HEAD`; if a human or tool made an extra commit after the manifest’s last recorded SHA, `last_recorded` is an ancestor and rollback proceeds. The P3 plan says rollback refuses if the current branch has diverged from the manifest’s recorded SHAs, and FR-9.9 requires branch/manifest agreement.","suggested_fix":"Require `HEAD == man.commits[-1].sha` before rollback, unless there is an explicit import/confirm flow that records or preserves the extra commits in the manifest."},{"id":"F-004","severity":"major","category":"correctness","location":"src/gauntlet/engine/orchestrator.py:157; src/gauntlet/engine/orchestrator.py:174; src/gauntlet/engine/orchestrator.py:392","claim":"Step-level `foreach` is not resumable: completed iterations rerun, and parked gates inside foreach cannot be approved.","evidence":"`_run_step_foreach()` calls `_execute()` for every iteration without checking whether that iteration’s record is already `done` or `skipped`; `_execute()` then marks even a done record running again. Separately, `current_step` stores only the step id and `_find_parked_gate()` looks up `record(step_id)` with no iteration, so a parked `human_gate` record like `gate[1]` is absent to `approve`. FR-5.4 makes `foreach` first-class, and FR-8.2 requires safe resume.","suggested_fix":"Make `_run_step_foreach()` skip terminal iteration records, persist the current iteration in the manifest, and allow approve/reject to target a `(step_id, iteration)` gate."},{"id":"F-005","severity":"major","category":"correctness","location":"src/gauntlet/engine/validate.py:47; src/gauntlet/engine/orchestrator.py:143; src/gauntlet/engine/orchestrator.py:191","claim":"`on_fail` targets in other stages pass validation but crash at runtime when the route is taken.","evidence":"Validation checks `route_to` against all step IDs in the whole pipeline. Runtime builds `index` from only the current stage and `_reset_for_retry()` calls `ids.index(route_to)` on only that stage’s IDs. A cross-stage route therefore validates, then raises instead of producing a checkpointed failed/parked run. FR-5.4 requires first-class failure routing; CLAUDE.md §2 requires fail-closed behavior.","suggested_fix":"Either validate `on_fail.route_to` as stage-local or implement explicit cross-stage routing semantics. Add a load-time test for a cross-stage route."},{"id":"F-006","severity":"major","category":"spec-gap","location":"src/gauntlet/engine/pipeline.py:40; src/gauntlet/engine/config.py:39; src/gauntlet/engine/steptypes.py:62; src/gauntlet/engine/steptypes.py:100","claim":"P3 declares `max_turns` and timeout guards but does not enforce them across core step paths.","evidence":"`max_turns` exists on steps/profiles but is never read. Profile `step_timeout_s` is stripped before adapter construction and has no fallback use. `shell` steps run `subprocess.run()` with no timeout at all; only `agent_task.timeout_s` mutates an adapter timeout. FR-3.3 and the P3 plan require per-step `max_turns`/timeout/budget guards that halt at a checkpoint.","suggested_fix":"Apply profile and step timeout fallbacks to every handler, use `subprocess.run(..., timeout=...)` for shell with a `HALTED` result on timeout, and either wire adapter-supported max-turn flags or reject `max_turns` on unsupported adapters at load time."},{"id":"F-007","severity":"major","category":"spec-gap","location":"src/gauntlet/engine/run.py:93","claim":"The entry contract accepts a scaffolded PRD if the marker line is deleted.","evidence":"`new()` writes a fixed stub, but `check_entry_contract()` only rejects when `PRD_STUB_MARKER` is present. A user can remove that one HTML comment while leaving `# PRD: <title>`, empty sections, and scaffold text unchanged, and `run` will proceed. FR-10.1 requires a human-authored PRD; the P3 plan says `run` refuses unless `prd.md` exists and is non-stub.","suggested_fix":"Normalize and compare against the scaffold with and without the marker, reject placeholder headings/text, or require an explicit human-authored metadata marker separate from the scaffold body."},{"id":"F-008","severity":"major","category":"spec-gap","location":"src/gauntlet/engine/steptypes.py:198; src/gauntlet/engine/gitops.py:167","claim":"Commit-message drafting omits required inputs and loses message-agent accounting.","evidence":"The P3 plan says the `message_agent` drafts from diff plus plan section. `_draft_commit_message()` sends only a prompt and `git diff HEAD`; `git diff HEAD` omits untracked files that `commit_all()` will add with `git add -A`, so new-file phases can be drafted from an empty diff. The successful adapter result’s usage/session ID is discarded, so manifest totals omit commit-drafting cost despite §7/FR-3.2 usage requirements.","suggested_fix":"Include the relevant plan excerpt and untracked-file diffs/status in the draft prompt. Accumulate message-agent usage/session IDs into the commit step record and apply the same budget/timeout guards."},{"id":"F-009","severity":"minor","category":"correctness","location":"src/gauntlet/engine/orchestrator.py:222; src/gauntlet/engine/judgeproc.py:112","claim":"The engine-managed judge lifecycle leaks `GAUNTLET_STEP_ID` into the parent process environment.","evidence":"`_execute()` sets `os.environ[\"GAUNTLET_STEP_ID\"]` for judged steps, but `ManagedJudge.stop()` only removes token, URL, mode, and run ID. BOOTSTRAP-NOTES #15 and the P3 contract describe per-run env teardown; the integration test only checks the token variable.","suggested_fix":"Snapshot and restore all Gauntlet judge env vars, including `GAUNTLET_STEP_ID`, in the judge lifecycle. Test that no `GAUNTLET_*` judge variables remain after success and failure."}],"open_questions":[],"summary":"The most serious issue is that the implementation excludes the entire `runs/` tree from engine git operations while also storing real PRD/plan/artifact effects there, which breaks the core crash/resume assumption. Rollback also does not truly rewind the manifest to match the reset branch. Several P3-promised control surfaces exist only partially: foreach resume, on_fail validation, timeout/max_turns guards, entry-contract non-stubness, and message-agent accounting need tightening before this engine should drive later bootstrap phases."}```

## Triage verdicts (ratified by the human)
```json
{
  "phase": "P3",
  "round": 1,
  "handoff_sha": "77570f9",
  "reviewer": "codex exec -s read-only (codex-cli 0.139.0)",
  "verdicts": [
    {
      "finding_id": "F-001",
      "severity": "blocking",
      "verdict": "legitimate",
      "reasoning": "Confirmed: artifact_root=slug_dir (under run_root) and every engine git op excludes the whole `runs` tree, so a declared agent output written under runs/<slug> is invisible to is_dirty_vs(base) — the default park policy could silently re-run over a real partial effect, violating the F-003 transaction-boundary deliverable. The exclusion must be narrowed to the live run-instance bookkeeping (run_dir + active pointer), letting prd.md/plan.md/declared outputs be tracked, detectable work.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-002",
      "severity": "major",
      "verdict": "legitimate",
      "reasoning": "Confirmed: rollback resets only dropped commit-step records to pending; downstream non-commit step records (implement/tests of a rolled-back phase) stay `done`, so resume skips work that `git reset --hard` removed and reaches a clean-tree commit. FR-9.9 requires branch and manifest to agree.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-003",
      "severity": "major",
      "verdict": "legitimate",
      "reasoning": "Confirmed: the divergence guard only rejects when the recorded tip is not an ancestor of HEAD, so a branch that is *ahead* of the manifest (extra unmanifested commits) passes and those commits are silently discarded by reset. The plan says rollback refuses if the branch has diverged from the manifest's recorded SHAs.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-004",
      "severity": "major",
      "verdict": "legitimate",
      "reasoning": "Confirmed for step-level foreach: _run_step_foreach re-executes every iteration without skipping terminal records, and a parked human_gate inside a foreach is unreachable by approve (current_step/_find_parked_gate ignore iteration). Stage-level foreach (what the standard pipeline uses) does skip done iterations and is unaffected, but FR-5.4/FR-8.2 require step-level foreach to resume safely too.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-005",
      "severity": "major",
      "verdict": "legitimate",
      "reasoning": "Confirmed: validation accepts an on_fail.route_to anywhere in the pipeline, but the runtime index/_reset_for_retry are stage-local, so a cross-stage route validates then crashes instead of producing a checkpointed failed/parked run — a fail-open against CLAUDE.md §2. Simplest correct fix: validate route_to as stage-local.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-006",
      "severity": "major",
      "verdict": "legitimate",
      "reasoning": "Confirmed partial enforcement: budget_usd halt and per-invocation adapter timeout are wired/tested, but shell steps have no timeout, profile.step_timeout_s is stripped and never used as a fallback, and max_turns is never read. FR-3.3 asks for per-step max_turns/timeout/budget guards that halt at a checkpoint. Will wire shell timeout + profile-timeout fallback (HALTED on expiry) and pass max_turns to adapters that honor it / reject it at load where they don't.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-007",
      "severity": "major",
      "verdict": "legitimate",
      "reasoning": "The plan scopes the entry contract to a template-marker check, but 'differs from the scaffolded stub' is better served by comparing the whole PRD body against the stub (marker-stripped) so deleting only the marker line still reads as the untouched stub and is refused. Cheap strengthening that honors FR-10.1's non-stub intent.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-008",
      "severity": "major",
      "verdict": "legitimate",
      "reasoning": "Confirmed: _draft_commit_message sends only `git diff HEAD`, which omits untracked files that commit_all then stages — a new-file phase drafts from an empty diff; and the message-agent's usage/session is discarded, so manifest totals understate cost (§7/FR-3.2). Will include untracked-file status in the draft context and accumulate message-agent usage into the commit step record; plan-section inclusion is best-effort when provided.",
      "action": "fix_now"
    },
    {
      "finding_id": "F-009",
      "severity": "minor",
      "verdict": "legitimate",
      "reasoning": "Confirmed: the orchestrator sets GAUNTLET_STEP_ID in os.environ but ManagedJudge.stop() clears only TOKEN/URL/MODE/RUN_ID, leaking STEP_ID into the parent session after a run. Will clear all GAUNTLET_* judge vars (incl. STEP_ID) on stop and assert none remain after success and failure.",
      "action": "fix_now"
    }
  ],
  "summary": "All 9 findings are legitimate and accepted for fix_now; none are bikeshedding, premature optimization, or not_applicable. Two correctness defects are most material: F-001 (the run-root exclusion is broader than the live bookkeeping, so real partial artifact effects can be silently re-run over — undercutting the headline P3 crash/resume guarantee) and F-002/F-003 (rollback does not truly reconcile branch and manifest). The rest tighten genuinely partial P3 control surfaces: step-foreach resume, on_fail validation scope, timeout/max_turns enforcement, entry-contract non-stubness, commit-draft inputs/accounting, and judge-env teardown."
}
```

## The fix diff (`77570f9..bb46479`, i.e. the P3.1 commit)
```diff
diff --git a/.gauntlet/pins.yaml b/.gauntlet/pins.yaml
index 4b0b6b4..f825f22 100644
--- a/.gauntlet/pins.yaml
+++ b/.gauntlet/pins.yaml
@@ -52,6 +52,11 @@ clis:
         Permission-bypass flags observed in --help (--dangerously-skip-permissions,
         --allow-dangerously-skip-permissions, --bare which skips hooks,
         --permission-mode bypassPermissions) are rejected by the config lint.
+      - >-
+        P3: no --max-turns flag on claude 2.1.172 (`claude --help` has no
+        turn/limit option), and `codex exec` has no turn cap either. The engine
+        therefore rejects `max_turns` at pipeline load (review F-006); the
+        working per-step halts are timeout_s and budget_usd (FR-3.3).
   codex:
     version: "codex-cli 0.139.0"
     verified_flags:
diff --git a/src/gauntlet/engine/execution.py b/src/gauntlet/engine/execution.py
index 3ab7ba8..6b9ae6b 100644
--- a/src/gauntlet/engine/execution.py
+++ b/src/gauntlet/engine/execution.py
@@ -64,6 +64,9 @@ class StepContext:
     writer: RedactingWriter
     judge_env: dict[str, str] = field(default_factory=dict)
     artifacts: dict[str, Path] = field(default_factory=dict)
+    # repo-relative paths of the engine's own bookkeeping (review F-001); commit
+    # and dirty checks exclude these but not real run artifacts.
+    excludes: list[str] = field(default_factory=list)
     iteration_item: Any | None = None
     iteration_index: int | None = None
     adapter_factory: AdapterFactory | None = None
@@ -136,3 +139,24 @@ def get_spec(step_type: str) -> StepSpec:
 
 def usage_from_result(result: AgentResult) -> Usage | None:
     return result.usage
+
+
+def run_bookkeeping_excludes(repo_root: Path, run_dir: Path, artifact_root: Path) -> list[str]:
+    """Repo-relative paths of the engine's own live bookkeeping (review F-001).
+
+    These — the run-instance dir (manifest/transcripts/steps/judge-audit) and the
+    active-run pointer — must be invisible to worktree-state checks and commits.
+    Everything *else* under the run root (prd.md, plan.md, declared step outputs)
+    is real work: tracked, detected by the transaction boundary, and committable.
+    Narrowing the exclusion to just this set is the F-001 fix — the prior code
+    excluded the whole run root, hiding real partial effects from the dirty-base
+    check.
+    """
+    excludes: list[str] = []
+    root = repo_root.resolve()
+    for p in (run_dir, artifact_root / "active-run.txt"):
+        try:
+            excludes.append(p.resolve().relative_to(root).as_posix())
+        except ValueError:
+            continue
+    return excludes
diff --git a/src/gauntlet/engine/judgeproc.py b/src/gauntlet/engine/judgeproc.py
index 4a7d86a..1ab5477 100644
--- a/src/gauntlet/engine/judgeproc.py
+++ b/src/gauntlet/engine/judgeproc.py
@@ -21,6 +21,7 @@ from pathlib import Path
 from gauntlet.judge.hook_client import (
     MODE_ENV_VAR,
     RUN_ID_ENV_VAR,
+    STEP_ID_ENV_VAR,
     URL_ENV_VAR,
 )
 from gauntlet.judge.service import TOKEN_ENV_VAR
@@ -28,6 +29,17 @@ from gauntlet.judge.service import TOKEN_ENV_VAR
 DEFAULT_HOST = "127.0.0.1"
 DEFAULT_PORT = 8787
 
+# Every GAUNTLET_* var the run touches — snapshotted at start, restored at stop
+# so nothing (incl. the per-step GAUNTLET_STEP_ID set by the orchestrator) leaks
+# into the parent session (review F-009).
+_MANAGED_ENV_VARS = (
+    TOKEN_ENV_VAR,
+    URL_ENV_VAR,
+    MODE_ENV_VAR,
+    RUN_ID_ENV_VAR,
+    STEP_ID_ENV_VAR,
+)
+
 
 class ManagedJudge:
     def __init__(
@@ -52,6 +64,7 @@ class ManagedJudge:
         self.startup_timeout_s = startup_timeout_s
         self.token = secrets.token_urlsafe(32)
         self._proc: subprocess.Popen | None = None
+        self._env_snapshot: dict[str, str | None] = {}
 
     @property
     def url(self) -> str:
@@ -87,6 +100,8 @@ class ManagedJudge:
             argv += ["--judge-model", self.judge_model]
         self._proc = subprocess.Popen(argv, env=child_env)
         self._await_healthy()
+        # Snapshot prior values of every managed var so stop() restores exactly.
+        self._env_snapshot = {v: os.environ.get(v) for v in _MANAGED_ENV_VARS}
         env = self.env()
         os.environ.update(env)  # the bootstrap session + child agents see it
         return env
@@ -112,8 +127,15 @@ class ManagedJudge:
     def stop(self) -> None:
         if self._proc is None:
             return
-        for var in (TOKEN_ENV_VAR, URL_ENV_VAR, MODE_ENV_VAR, RUN_ID_ENV_VAR):
-            os.environ.pop(var, None)
+        # Restore every managed GAUNTLET_* var to its pre-run value (incl. the
+        # per-step GAUNTLET_STEP_ID set by the orchestrator) — no env leak into
+        # the parent session on success or failure (review F-009).
+        for var, prior in (self._env_snapshot or {v: None for v in _MANAGED_ENV_VARS}).items():
+            if prior is None:
+                os.environ.pop(var, None)
+            else:
+                os.environ[var] = prior
+        self._env_snapshot = {}
         self._proc.terminate()
         try:
             self._proc.wait(timeout=5.0)
diff --git a/src/gauntlet/engine/orchestrator.py b/src/gauntlet/engine/orchestrator.py
index f09d43c..157c865 100644
--- a/src/gauntlet/engine/orchestrator.py
+++ b/src/gauntlet/engine/orchestrator.py
@@ -33,6 +33,7 @@ from gauntlet.engine.execution import (
     StepContext,
     StepResult,
     get_spec,
+    run_bookkeeping_excludes,
 )
 from gauntlet.engine.expr import eval_when, resolve_list
 from gauntlet.engine.manifest import Manifest, StepRecord
@@ -73,6 +74,9 @@ class Orchestrator:
         self.clock = clock
         self.manifest_path = run_dir / "manifest.json"
         self.artifacts: dict[str, Path] = {}
+        # Narrow exclusion: only the engine's own bookkeeping is hidden from
+        # dirty checks / commits — real run artifacts stay visible (review F-001).
+        self.excludes = run_bookkeeping_excludes(repo_root, run_dir, artifact_root)
         self._ignore_run_dir()
         self._seed_artifacts()
 
@@ -174,6 +178,9 @@ class Orchestrator:
     def _run_step_foreach(self, step: Step) -> str:
         items = resolve_list(step.foreach, self._context())
         for idx, item in enumerate(items):
+            rec = self.manifest.record(step.id, str(idx))
+            if rec is not None and rec.status in (M.DONE, M.SKIPPED):
+                continue  # resume: don't re-run a completed iteration (F-004)
             result = self._execute(step, str(idx), item)
             if result.status != DONE:
                 return result.status
@@ -260,8 +267,10 @@ class Orchestrator:
         )
         if not is_agent_write:
             return None
-        exclude = [self.config.run_root]
-        if not gitops.is_dirty_vs(self.repo_root, rec.base_sha, exclude=exclude):
+        # Detect partial work against the narrow bookkeeping exclusion, so a
+        # partial *artifact* under the run root (not just a repo-root file) is
+        # still seen as a mid-edit interruption (review F-001).
+        if not gitops.is_dirty_vs(self.repo_root, rec.base_sha, exclude=self.excludes):
             return None  # clean re-entry: agent never progressed; safe to re-run
         if self.config.interrupted_step == "reset_to_base":
             ts = self.clock().replace(":", "-")
@@ -269,11 +278,13 @@ class Orchestrator:
             # Snapshot the partial work (tracked + untracked) before discarding.
             gitops.backup_dirty_worktree(
                 self.repo_root, backup, f"interrupted {rec.id} partial work",
-                exclude=exclude,
+                exclude=self.excludes,
             )
             gitops.reset_hard(self.repo_root, rec.base_sha)
-            # Spare the run root so the reset never wipes the run pointer,
-            # manifests, or the human-authored prd.md living under it.
+            # `clean` is broader than the dirty check on purpose: it spares the
+            # whole run root so the reset never wipes the run pointer, manifests,
+            # the authored prd.md, or prior declared artifacts — the re-run
+            # regenerates its own outputs over them.
             gitops.clean_untracked(self.repo_root, exclude=[self.config.run_root])
             return None  # tree restored to base; re-run cleanly
         return StepResult(
@@ -352,6 +363,7 @@ class Orchestrator:
             writer=self.writer,
             judge_env=self.judge_env,
             artifacts=dict(self.artifacts),
+            excludes=self.excludes,
             iteration_item=item,
             iteration_index=int(iteration) if iteration is not None else None,
             adapter_factory=self.adapter_factory,
@@ -390,13 +402,16 @@ class Orchestrator:
         rec.ended = self.clock()
 
     def _find_parked_gate(self, step_id: str) -> StepRecord:
-        rec = self.manifest.record(step_id)
-        if rec is None or rec.status != M.PARKED:
-            raise ValueError(
-                f"step {step_id!r} is not parked at a gate "
-                f"(status: {rec.status if rec else 'absent'})"
-            )
-        return rec
+        # Scan across iterations so a gate parked inside a foreach (record
+        # `gate` with iteration `1`) is reachable by approve/reject (F-004).
+        for rec in self.manifest.steps:
+            if rec.id == step_id and rec.status == M.PARKED:
+                return rec
+        existing = self.manifest.record(step_id)
+        raise ValueError(
+            f"step {step_id!r} is not parked at a gate "
+            f"(status: {existing.status if existing else 'absent'})"
+        )
 
     def _head_sha(self) -> str:
         return gitops.head_sha(self.repo_root)
diff --git a/src/gauntlet/engine/run.py b/src/gauntlet/engine/run.py
index 3af06b8..fa9651c 100644
--- a/src/gauntlet/engine/run.py
+++ b/src/gauntlet/engine/run.py
@@ -15,6 +15,7 @@ from pathlib import Path
 
 from gauntlet.engine import gitops, manifest as M
 from gauntlet.engine.config import RunConfig
+from gauntlet.engine.execution import run_bookkeeping_excludes
 from gauntlet.engine.judgeproc import ManagedJudge
 from gauntlet.engine.manifest import Manifest, PipelineRef
 from gauntlet.engine.orchestrator import Orchestrator
@@ -51,6 +52,14 @@ def _utc_stamp() -> str:
     return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
 
 
+def _strip_marker(text: str) -> str:
+    return "\n".join(line for line in text.splitlines() if PRD_STUB_MARKER not in line)
+
+
+def _normalize(text: str) -> str:
+    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())
+
+
 @dataclass
 class RunLayout:
     repo_root: Path
@@ -105,11 +114,20 @@ class RunManager:
                 f"{layout.prd_path} does not exist; `gauntlet new {slug}` scaffolds "
                 "a stub for a human to author (FR-10.1)"
             )
-        if PRD_STUB_MARKER in layout.prd_path.read_text():
+        content = layout.prd_path.read_text()
+        if PRD_STUB_MARKER in content:
             raise EntryContractError(
                 f"{layout.prd_path} is still the scaffolded stub; a human must "
                 "author the PRD before a run can start (FR-10.1)"
             )
+        # Deleting only the marker line leaves the rest of the scaffold intact —
+        # still not a human-authored PRD. Compare the whole body, marker-stripped
+        # and whitespace-normalized, against the stub (review F-007).
+        if _normalize(content) == _normalize(_strip_marker(_PRD_STUB)):
+            raise EntryContractError(
+                f"{layout.prd_path} is the scaffolded stub with only the marker "
+                "removed; a human must author a real PRD before a run (FR-10.1)"
+            )
 
     # ---- run (FR-8.1) -------------------------------------------------------
     def start(
@@ -221,22 +239,25 @@ class RunManager:
         run_dir = layout.active_run_dir()
         man = Manifest.load(run_dir / "manifest.json")
 
-        # Guard 1: clean worktree (the work tree; run bookkeeping is excluded).
-        if not gitops.is_clean(self.repo_root, exclude=[self.config.run_root]):
+        # Guard 1: clean work tree — only the engine's own bookkeeping is
+        # excluded (review F-001), so an uncommitted real artifact still blocks.
+        excludes = run_bookkeeping_excludes(self.repo_root, run_dir, layout.slug_dir)
+        if not gitops.is_clean(self.repo_root, exclude=excludes):
             raise RollbackGuardError(
                 "refusing rollback: worktree is dirty; commit or discard first"
             )
-        # Guard 2: branch agrees with the manifest (no out-of-band rewrite).
+        # Guard 2: branch tip MUST equal the manifest's last recorded commit.
+        # A branch ahead of the manifest (extra unmanifested commits) is a
+        # divergence — reset would silently discard those commits (review F-003).
         if not man.commits:
             raise RollbackGuardError("no recorded commits to roll back to")
         last_recorded = man.commits[-1].sha
         head = gitops.head_sha(self.repo_root)
-        if head != last_recorded and not gitops.is_ancestor(
-            self.repo_root, last_recorded, head
-        ):
+        if head != last_recorded:
             raise RollbackGuardError(
-                "refusing rollback: branch has diverged from the manifest's "
-                f"recorded SHAs (HEAD {head[:10]} vs recorded {last_recorded[:10]})"
+                "refusing rollback: branch has diverged from the manifest "
+                f"(HEAD {head[:10]} != last recorded {last_recorded[:10]}); the "
+                "branch and manifest must agree before a rewind (FR-9.9)"
             )
         # Resolve the target: the last commit whose phase prefix is P<phase>.
         target = self._phase_boundary_sha(man, phase)
@@ -244,10 +265,6 @@ class RunManager:
             raise RollbackGuardError(
                 f"no recorded phase-{phase} commit boundary to roll back to"
             )
-        if not gitops.is_ancestor(self.repo_root, target, head) and target != head:
-            raise RollbackGuardError(
-                f"refusing rollback: target {target[:10]} is not an ancestor of HEAD"
-            )
 
         # Backup ref + manifest snapshot before any rewind (F-010).
         ts = _utc_stamp()
@@ -257,27 +274,42 @@ class RunManager:
         shutil.copy2(run_dir / "manifest.json", run_dir / f"manifest.snapshot-{ts}.json")
 
         gitops.reset_hard(self.repo_root, target)
-        # Rewind the manifest so branch and manifest never disagree about where
-        # the run stands: keep commits up to and including the target, drop the
-        # rest, and reset every step that produced a dropped commit back to
-        # pending (with its base SHA cleared) so a later resume re-does it.
+        self._rewind_manifest(man, run_dir, target)
+        man.write_atomic(run_dir / "manifest.json")
+        return target
+
+    def _rewind_manifest(self, man: Manifest, run_dir: Path, target: str) -> None:
+        """Rewind the manifest to match the reset branch (review F-002).
+
+        Drop commits after the target, and reset to `pending` EVERY step record
+        (any type, any iteration) that executes after the target phase boundary
+        in pipeline order — not just the steps that produced dropped commits.
+        Otherwise a later resume skips work `git reset --hard` removed and the
+        branch and manifest disagree (FR-9.9).
+        """
         keep: list = []
         for commit in man.commits:
             keep.append(commit)
             if commit.sha == target:
                 break
-        kept_step_ids = {c.step_id for c in keep}
-        dropped_step_ids = {c.step_id for c in man.commits} - kept_step_ids
         man.commits = keep
+        target_step = keep[-1].step_id
+
+        pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
+        order = [s.id for s in pipeline.all_steps()]
+        try:
+            cutoff = order.index(target_step)
+        except ValueError:  # pragma: no cover - defensive
+            cutoff = len(order) - 1
+        keep_ids = set(order[: cutoff + 1])
         for rec in man.steps:
-            if rec.id in dropped_step_ids:
+            if rec.id not in keep_ids:
                 rec.status = M.PENDING
                 rec.base_sha = None
+                rec.session_id = None
                 rec.ended = None
         man.status = M.RUN_PARKED
         man.current_step = None
-        man.write_atomic(run_dir / "manifest.json")
-        return target
 
     # ---- internals ----------------------------------------------------------
     def _phase_boundary_sha(self, man: Manifest, phase: int) -> str | None:
diff --git a/src/gauntlet/engine/steptypes.py b/src/gauntlet/engine/steptypes.py
index 40190e9..4f183c5 100644
--- a/src/gauntlet/engine/steptypes.py
+++ b/src/gauntlet/engine/steptypes.py
@@ -21,6 +21,7 @@ from gauntlet.engine.commit_format import header_prefix, validate_commit_message
 from gauntlet.engine.execution import (
     DONE,
     FAILED,
+    HALTED,
     PARKED,
     StepContext,
     StepResult,
@@ -64,13 +65,23 @@ def handle_shell(step: Step, ctx: StepContext) -> StepResult:
     if not template:
         return StepResult(status=FAILED, notes="shell step has no `run:` command")
     command = render_shell_command(template, ctx.config)
-    proc = subprocess.run(
-        command,
-        shell=True,
-        cwd=ctx.repo_root,
-        capture_output=True,
-        text=True,
-    )
+    timeout = step.timeout_s  # per-step guard (FR-3.3); None => unbounded
+    try:
+        proc = subprocess.run(
+            command,
+            shell=True,
+            cwd=ctx.repo_root,
+            capture_output=True,
+            text=True,
+            timeout=timeout,
+        )
+    except subprocess.TimeoutExpired as exc:
+        _write_step_log(ctx, "output.txt", f"$ {command}\n--- TIMEOUT after {timeout}s ---\n")
+        # Halt at a checkpoint rather than letting a stuck command burn on.
+        return StepResult(
+            status=HALTED,
+            notes=f"shell timeout halt (FR-3.3): `{command}` exceeded {timeout}s",
+        )
     _write_step_log(ctx, "output.txt", _proc_log(command, proc))
     if proc.returncode != 0:
         return StepResult(
@@ -97,7 +108,12 @@ def handle_agent_task(step: Step, ctx: StepContext) -> StepResult:
     adapter = ctx.build_adapter(agent_name)
     prompt = _render_prompt(step, ctx)
     schema = _load_schema(step, ctx)
+    # Per-step timeout overrides the profile's step_timeout_s, which overrides
+    # the adapter default (FR-3.3). A timeout raises AgentTimeoutError, which the
+    # orchestrator turns into a HALTED checkpoint.
     timeout = step.timeout_s
+    if timeout is None and agent_name in ctx.config.agents:
+        timeout = ctx.config.profile(agent_name).step_timeout_s
     if timeout is not None and hasattr(adapter, "timeout_s"):
         adapter.timeout_s = timeout
     result = adapter.run(
@@ -151,13 +167,20 @@ def _load_schema(step: Step, ctx: StepContext) -> dict | None:
 # --- commit (FR-9.2/9.7) -----------------------------------------------------
 def handle_commit(step: Step, ctx: StepContext) -> StepResult:
     repo = ctx.repo_root
-    exclude = [ctx.config.run_root]  # never commit / count the run bookkeeping
-    message = _commit_message(step, ctx)
+    # Narrow exclusion (review F-001): commit real artifacts (plan.md, outputs);
+    # keep only the engine's own bookkeeping out of the commit and the checks.
+    exclude = ctx.excludes
+    message, draft_usage, draft_session = _commit_message(step, ctx)
     err = validate_commit_message(message)
     if err is not None:
         # message_agent drafting includes a bounded redraft loop in _draft;
         # a literal/exhausted message that still fails is a hard error.
-        return StepResult(status=FAILED, notes=f"commit message invalid: {err.reason}")
+        return StepResult(
+            status=FAILED,
+            usage=draft_usage,
+            session_id=draft_session,
+            notes=f"commit message invalid: {err.reason}",
+        )
     prefix = header_prefix(message)
 
     # Mid-commit resume reconciliation (review F-003): if a prior attempt
@@ -171,12 +194,16 @@ def handle_commit(step: Step, ctx: StepContext) -> StepResult:
                 status=DONE,
                 commit_sha=existing,
                 commit_phase=prefix,
+                usage=draft_usage,
+                session_id=draft_session,
                 notes="reconciled pre-existing commit after mid-commit interruption",
             )
 
     if gitops.is_clean(repo, exclude=exclude):
         return StepResult(
             status=FAILED,
+            usage=draft_usage,
+            session_id=draft_session,
             notes="commit step found a clean worktree with nothing to commit",
         )
 
@@ -184,53 +211,86 @@ def handle_commit(step: Step, ctx: StepContext) -> StepResult:
     identity = ctx.config.identity(agent_name)
     sha = gitops.commit_all(repo, message, identity=identity, exclude=exclude)
     return StepResult(
-        status=DONE, commit_sha=sha, commit_phase=prefix, notes=f"committed {sha[:10]}"
+        status=DONE, commit_sha=sha, commit_phase=prefix,
+        usage=draft_usage, session_id=draft_session, notes=f"committed {sha[:10]}",
     )
 
 
-def _commit_message(step: Step, ctx: StepContext) -> str:
+def _commit_message(step: Step, ctx: StepContext):
+    """Return ``(message, usage, session_id)``; usage/session are None for a
+    literal message (no model call)."""
     literal = step.get("message")
     if literal:
-        return literal  # human-authored YAML; still format-validated above
+        return literal, None, None  # human-authored YAML; still format-validated
     return _draft_commit_message(step, ctx)
 
 
-def _draft_commit_message(step: Step, ctx: StepContext) -> str:
+def _draft_commit_message(step: Step, ctx: StepContext):
     """Draft a commit message via the message_agent with bounded redraft.
 
-    The agent sees the diff + plan section (data); the engine validates the
-    format and asks for a redraft on violation (FR-9.2). Returns the last draft
-    (valid or not) — :func:`handle_commit` makes the accept/reject decision.
+    The agent sees the change as data — both the tracked diff AND the untracked
+    files `git add -A` will sweep in (review F-008: a new-file phase otherwise
+    drafts from an empty diff) — plus an optional plan section. The engine
+    validates the format and asks for a redraft on violation (FR-9.2). Returns
+    ``(message, usage, session_id)`` so the commit step records the drafter's
+    cost (FR-3.2/§7).
     """
     agent_name = step.get("message_agent")
     if not agent_name:
         raise ValueError("commit step needs either `message:` or `message_agent:`")
     adapter = ctx.build_adapter(agent_name)
-    diff = gitops.diff_head(ctx.repo_root, exclude=[ctx.config.run_root])
+    change = _change_context(ctx)
     base_prompt = (
         (ctx.repo_root / step.get("prompt")).read_text()
         if step.get("prompt")
         else _DEFAULT_COMMIT_PROMPT
     )
     phase_hint = step.get("phase", "")
-    prompt = (
+    plan_section = _plan_section(step, ctx)
+    header = (
         f"{base_prompt}\n\nRequired header phase prefix: {phase_hint or '(infer PN)'}\n"
-        f"\n--- diff (HEAD) ---\n{diff}\n"
+        f"{plan_section}"
     )
+    prompt = f"{header}\n{change}\n"
     max_redrafts = int(step.get("max_redrafts", 2))
     message = ""
-    for attempt in range(1 + max_redrafts):
+    usage = None
+    session_id = None
+    for _attempt in range(1 + max_redrafts):
         result = adapter.run(prompt, cwd=ctx.repo_root)
+        usage = result.usage  # accumulated per call by the orchestrator's totals
+        session_id = result.session_id
         message = result.text.strip()
         if validate_commit_message(message) is None:
-            return message
+            return message, usage, session_id
         prompt = (
-            f"{base_prompt}\n\nYour previous draft was rejected: "
+            f"{header}\n\nYour previous draft was rejected: "
             f"{validate_commit_message(message).reason}. "
-            "Return only the corrected commit message.\n"
-            f"\n--- diff (HEAD) ---\n{diff}\n"
+            f"Return only the corrected commit message.\n{change}\n"
         )
-    return message
+    return message, usage, session_id
+
+
+def _change_context(ctx: StepContext) -> str:
+    """The diff vs HEAD plus the untracked files staging will add (F-008)."""
+    repo = ctx.repo_root
+    diff = gitops.diff_head(repo, exclude=ctx.excludes)
+    status = gitops.status_porcelain(repo, exclude=ctx.excludes)
+    return (
+        f"--- git status (incl. untracked) ---\n{status}\n"
+        f"\n--- diff (tracked, vs HEAD) ---\n{diff}"
+    )
+
+
+def _plan_section(step: Step, ctx: StepContext) -> str:
+    """Optional plan excerpt the message_agent drafts from (FR-9.2)."""
+    ref = step.get("plan_section")
+    if not ref:
+        return ""
+    path = ctx.artifacts.get(ref) or (ctx.artifact_root / ref)
+    if Path(path).exists():
+        return f"\n--- plan section: {ref} ---\n{Path(path).read_text()}\n"
+    return ""
 
 
 _DEFAULT_COMMIT_PROMPT = (
diff --git a/src/gauntlet/engine/validate.py b/src/gauntlet/engine/validate.py
index 5150543..d2f110d 100644
--- a/src/gauntlet/engine/validate.py
+++ b/src/gauntlet/engine/validate.py
@@ -44,14 +44,18 @@ def validate_pipeline(
 ) -> ValidationReport:
     report = ValidationReport()
     available: set[str] = set(seeds)
-    all_ids = {step.id for step in pipeline.all_steps()}
     for stage in pipeline.stages:
+        stage_ids = {step.id for step in stage.steps}
         for step in stage.steps:
             _validate_step(step, config, available, report)
-            if step.on_fail and step.on_fail.route_to not in all_ids:
+            # on_fail routing is stage-local: the runtime jumps within the
+            # current stage's step list, so a cross-stage target would validate
+            # then crash at runtime (review F-005). Reject it at load.
+            if step.on_fail and step.on_fail.route_to not in stage_ids:
                 report.errors.append(
-                    f"step {step.id!r} on_fail routes to unknown step "
-                    f"{step.on_fail.route_to!r}"
+                    f"step {step.id!r} on_fail routes to {step.on_fail.route_to!r}, "
+                    "which is not a step in the same stage (cross-stage routing "
+                    "is unsupported, FR-5.4 / review F-005)"
                 )
             output = step.get("output")
             if output:
@@ -71,6 +75,16 @@ def _validate_step(
         report.errors.append(str(exc))
         return
 
+    # 1b. `max_turns` is unenforceable on the pinned CLIs (claude 2.1.172 has no
+    # --max-turns; codex exec has no turn cap) — reject it rather than silently
+    # ignore a claimed guard (review F-006). timeout_s + budget_usd are the
+    # working per-step halts (FR-3.3).
+    if step.max_turns is not None:
+        report.errors.append(
+            f"step {step.id!r} sets max_turns, but no installed adapter can honor "
+            "it; use timeout_s / budget_usd (FR-3.3 / review F-006)"
+        )
+
     # 2. dangling artifact dataflow (FR-5.3)
     for name in (step.get("inputs", []) or []):
         if name not in available:
@@ -94,6 +108,11 @@ def _validate_step(
             )
             continue
         profile = config.profile(ref)
+        if profile.max_turns is not None:
+            report.errors.append(
+                f"agent profile {ref!r} sets max_turns, unenforceable on the "
+                "pinned CLIs; use step_timeout_s / budget_usd (review F-006)"
+            )
         caps = profile.capabilities()
         if spec.step_requires_repo_write(step) and not caps.repo_write:
             report.errors.append(
diff --git a/tests/integration/test_pipeline_contract.py b/tests/integration/test_pipeline_contract.py
index 9cfae82..0ab896b 100644
--- a/tests/integration/test_pipeline_contract.py
+++ b/tests/integration/test_pipeline_contract.py
@@ -20,6 +20,7 @@ from pathlib import Path
 import pytest
 
 from gauntlet.engine import gitops, manifest as M
+from gauntlet.engine.judgeproc import _MANAGED_ENV_VARS
 from gauntlet.engine.run import RunManager
 from gauntlet.judge.service import TOKEN_ENV_VAR
 
@@ -83,8 +84,10 @@ def test_engine_manages_judge_lifecycle_around_a_run(tmp_path):
     mgr = RunManager(repo)
     status = mgr.start("demo", repo / "pipelines" / "mini.yaml", use_judge=True)
     assert status == M.RUN_DONE
-    # the judge env is torn down after the run (no leakage)
-    assert TOKEN_ENV_VAR not in os.environ
+    # the judge env is torn down after the run (no leakage) — every managed var,
+    # including the per-step GAUNTLET_STEP_ID set by the orchestrator (F-009)
+    for var in _MANAGED_ENV_VARS:
+        assert var not in os.environ, f"{var} leaked into the parent env"
     assert gitops.commit_subject(repo, "HEAD") == "P1: engine-managed judge run"
     assert (repo / "artifact.txt").read_text().strip() == "work"
 
diff --git a/tests/unit/test_judgeproc.py b/tests/unit/test_judgeproc.py
new file mode 100644
index 0000000..70f68b6
--- /dev/null
+++ b/tests/unit/test_judgeproc.py
@@ -0,0 +1,63 @@
+"""Engine-managed judge env teardown (review F-009).
+
+stop() must restore EVERY managed GAUNTLET_* var — including the per-step
+GAUNTLET_STEP_ID the orchestrator sets — to its pre-run value, so nothing leaks
+into the parent session on success or failure.
+"""
+
+from __future__ import annotations
+
+import os
+from pathlib import Path
+
+from gauntlet.engine.judgeproc import _MANAGED_ENV_VARS, ManagedJudge
+from gauntlet.judge.hook_client import STEP_ID_ENV_VAR
+from gauntlet.judge.service import TOKEN_ENV_VAR
+
+
+class _FakeProc:
+    returncode = 0
+
+    def terminate(self):
+        pass
+
+    def wait(self, timeout=None):
+        return 0
+
+    def kill(self):
+        pass
+
+
+def _judge() -> ManagedJudge:
+    mj = ManagedJudge(policy_path=Path("policy.yaml"), audit_path=Path("a.jsonl"), run_id="r")
+    mj._proc = _FakeProc()
+    return mj
+
+
+def test_stop_clears_unset_vars_including_step_id():
+    mj = _judge()
+    mj._env_snapshot = {v: None for v in _MANAGED_ENV_VARS}
+    os.environ[TOKEN_ENV_VAR] = "tok"
+    os.environ[STEP_ID_ENV_VAR] = "implement"  # set by the orchestrator
+    try:
+        mj.stop()
+        assert TOKEN_ENV_VAR not in os.environ
+        assert STEP_ID_ENV_VAR not in os.environ
+        for var in _MANAGED_ENV_VARS:
+            assert var not in os.environ
+    finally:
+        for v in _MANAGED_ENV_VARS:
+            os.environ.pop(v, None)
+
+
+def test_stop_restores_prior_values():
+    mj = _judge()
+    os.environ[TOKEN_ENV_VAR] = "outer-token"
+    mj._env_snapshot = {v: None for v in _MANAGED_ENV_VARS}
+    mj._env_snapshot[TOKEN_ENV_VAR] = "outer-token"  # pre-run value
+    os.environ[TOKEN_ENV_VAR] = "run-token"  # overwritten during the run
+    try:
+        mj.stop()
+        assert os.environ[TOKEN_ENV_VAR] == "outer-token"  # restored, not deleted
+    finally:
+        os.environ.pop(TOKEN_ENV_VAR, None)
diff --git a/tests/unit/test_orchestrator.py b/tests/unit/test_orchestrator.py
index b0c07c1..6f322d7 100644
--- a/tests/unit/test_orchestrator.py
+++ b/tests/unit/test_orchestrator.py
@@ -265,6 +265,82 @@ stages:
     assert "refs/gauntlet/backup/" in refs
 
 
+def test_resume_dirty_artifact_under_runroot_is_detected(fixture_repo):
+    # Review F-001: a partial *declared artifact* under runs/<slug> (not just a
+    # repo-root file) must still be seen as a mid-edit interruption and parked.
+    base = gitops.head_sha(fixture_repo)
+    (fixture_repo / "runs" / "demo").mkdir(parents=True)
+    (fixture_repo / "runs" / "demo" / "plan.md").write_text("half-written plan")
+    man = _seed_running_step(fixture_repo, "author", "agent_task", base)
+    text = """
+name: demo
+version: 1
+stages:
+  - id: s
+    steps:
+      - {id: author, type: agent_task, agent: builder, output: plan.md, prompt_text: go}
+"""
+    adapter = FakeAdapter()
+    orch = _build(fixture_repo, text, adapters={"builder": adapter}, manifest=man,
+                  interrupted="park")
+    assert orch.drive() == M.RUN_PARKED
+    assert orch.manifest.record("author").status == M.INTERRUPTED
+    assert adapter.calls == []  # not re-run over the partial artifact
+
+
+def test_step_foreach_skips_completed_iterations_on_resume(fixture_repo):
+    # Review F-004: a resumed step-level foreach must not re-run done iterations.
+    text = """
+name: demo
+version: 1
+stages:
+  - id: s
+    steps:
+      - {id: work, type: agent_task, agent: builder, foreach: vars.items, prompt_text: go}
+"""
+    adapter = FakeAdapter()
+    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
+                   pipeline=PipelineRef(name="demo", version=1, hash="x"))
+    man.upsert(StepRecord(id="work", type="agent_task", iteration="0", status=M.DONE))
+    orch = _build(fixture_repo, text, adapters={"builder": adapter},
+                  extra_context={"items": ["a", "b", "c"]}, manifest=man)
+    assert orch.drive() == M.RUN_DONE
+    # iteration 0 was already done; only 1 and 2 ran
+    assert len(adapter.calls) == 2
+
+
+def test_gate_inside_foreach_is_approvable(fixture_repo):
+    # Review F-004: a human_gate parked inside a foreach must be reachable.
+    text = """
+name: demo
+version: 1
+stages:
+  - id: s
+    foreach: vars.items
+    steps:
+      - {id: gate, type: human_gate}
+"""
+    orch = _build(fixture_repo, text, extra_context={"items": ["a", "b"]})
+    assert orch.drive() == M.RUN_PARKED
+    # the first iteration's gate is parked; approve targets it across iterations
+    assert orch.approve_gate("gate") in (M.RUN_PARKED, M.RUN_DONE)
+
+
+def test_shell_timeout_halts(fixture_repo):
+    # Review F-006: a shell step exceeding its timeout halts at a checkpoint.
+    text = """
+name: demo
+version: 1
+stages:
+  - id: s
+    steps:
+      - {id: slow, type: shell, run: "sleep 5", timeout_s: 0.3}
+"""
+    orch = _build(fixture_repo, text)
+    assert orch.drive() == M.RUN_PARKED
+    assert orch.manifest.record("slow").status == M.HALTED
+
+
 def test_resume_mid_commit_reconciles_without_double_commit(fixture_repo):
     base = gitops.head_sha(fixture_repo)
     # Simulate: engine recorded base + ran commit, the commit landed, then the
diff --git a/tests/unit/test_pipeline_loader.py b/tests/unit/test_pipeline_loader.py
index 055800a..9f4f158 100644
--- a/tests/unit/test_pipeline_loader.py
+++ b/tests/unit/test_pipeline_loader.py
@@ -152,10 +152,59 @@ stages:
       - {id: tests, type: shell, run: "true", on_fail: {route_to: nowhere, max_retries: 1}}
 """
     pipeline, _ = load_pipeline(_write(tmp_path, text))
-    with pytest.raises(PipelineValidationError, match="unknown step"):
+    with pytest.raises(PipelineValidationError, match="not a step in the same stage"):
         validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))
 
 
+def test_cross_stage_on_fail_route_rejected(tmp_path):
+    # route_to targets a step in a DIFFERENT stage -> rejected at load (F-005),
+    # since runtime routing is stage-local and would otherwise crash.
+    text = """
+name: d
+version: 1
+stages:
+  - id: a
+    steps:
+      - {id: build, type: shell, run: "true"}
+  - id: b
+    steps:
+      - {id: tests, type: shell, run: "true", on_fail: {route_to: build, max_retries: 1}}
+"""
+    pipeline, _ = load_pipeline(_write(tmp_path, text))
+    with pytest.raises(PipelineValidationError, match="not a step in the same stage"):
+        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))
+
+
+def test_max_turns_rejected_at_load(tmp_path):
+    # max_turns is unenforceable on the pinned CLIs -> rejected (F-006).
+    text = """
+name: d
+version: 1
+stages:
+  - id: s
+    steps:
+      - {id: implement, type: agent_task, agent: builder, max_turns: 5}
+"""
+    pipeline, _ = load_pipeline(_write(tmp_path, text))
+    with pytest.raises(PipelineValidationError, match="max_turns"):
+        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))
+
+
+def test_max_turns_on_profile_rejected(tmp_path):
+    text = """
+name: d
+version: 1
+stages:
+  - id: s
+    steps:
+      - {id: implement, type: agent_task, agent: builder}
+"""
+    cfg = {"agents": {"builder": {"adapter": "claude-code", "max_turns": 3}}}
+    pipeline, _ = load_pipeline(_write(tmp_path, text))
+    with pytest.raises(PipelineValidationError, match="max_turns"):
+        validate_pipeline(pipeline, RunConfig.model_validate(cfg))
+
+
 def test_unknown_step_type_rejected(tmp_path):
     text = """
 name: d
diff --git a/tests/unit/test_run_lifecycle.py b/tests/unit/test_run_lifecycle.py
index 249935d..4b2a143 100644
--- a/tests/unit/test_run_lifecycle.py
+++ b/tests/unit/test_run_lifecycle.py
@@ -59,6 +59,20 @@ def test_entry_contract_passes_for_real_prd(fixture_repo):
     mgr.check_entry_contract("demo")  # no raise
 
 
+def test_entry_contract_refuses_marker_only_removed(fixture_repo):
+    # F-007: deleting only the marker line leaves the scaffold body -> refuse.
+    from gauntlet.engine.run import PRD_STUB_MARKER
+
+    mgr = _prepare(fixture_repo)
+    mgr.new("demo")
+    prd = mgr.layout("demo").prd_path
+    stub = prd.read_text()
+    prd.write_text("\n".join(l for l in stub.splitlines() if PRD_STUB_MARKER not in l))
+    assert PRD_STUB_MARKER not in prd.read_text()
+    with pytest.raises(EntryContractError, match="only the marker removed"):
+        mgr.check_entry_contract("demo")
+
+
 LINEAR = """
 name: p
 version: 1
@@ -141,11 +155,31 @@ def test_rollback_to_phase_one_rewinds_branch_and_manifest(fixture_repo):
     assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: phase one"
     man = mgr.status("demo")
     assert [c.phase for c in man.commits] == ["P1"]
+    # F-002: ALL phase-2 step records (not just its commit) are rewound to
+    # pending, so a resume re-does the work git reset removed.
+    assert man.record("impl2").status == M.PENDING
+    assert man.record("c2").status == M.PENDING
+    assert man.record("impl1").status == M.DONE  # phase 1 kept
     # a backup ref preserved the pre-rollback tip
     refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/backup/")
     assert p2_sha in refs or "refs/gauntlet/backup/" in refs
 
 
+def test_rollback_refuses_branch_ahead_of_manifest(fixture_repo):
+    # F-003: an extra unmanifested commit means branch != manifest tip -> refuse.
+    mgr = _prepare(fixture_repo)
+    _author_prd(mgr, "demo")
+    path = _write_pipeline(fixture_repo, LINEAR)
+    mgr.start("demo", path, use_judge=False,
+              adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))
+    (fixture_repo / "extra.py").write_text("out of band\n")
+    git(fixture_repo, "add", "-A")
+    git(fixture_repo, "-c", "user.name=H", "-c", "user.email=h@h.local",
+        "commit", "-qm", "out-of-band commit")
+    with pytest.raises(RollbackGuardError, match="diverged"):
+        mgr.rollback("demo", phase=1)
+
+
 def test_rollback_refuses_dirty_worktree(fixture_repo):
     mgr = _prepare(fixture_repo)
     _author_prd(mgr, "demo")
diff --git a/tests/unit/test_steptypes.py b/tests/unit/test_steptypes.py
index a8b8ac9..15464b3 100644
--- a/tests/unit/test_steptypes.py
+++ b/tests/unit/test_steptypes.py
@@ -85,6 +85,50 @@ stages:
     assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: drafted"
 
 
+class RecordingDrafter:
+    """Captures the draft prompt and reports usage (F-008)."""
+
+    capabilities = FakeAdapter.capabilities
+
+    def __init__(self):
+        self.prompts = []
+
+    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
+        from gauntlet.adapters.base import Usage
+
+        self.prompts.append(prompt)
+        return AgentResult(
+            text="P1: add new file\n\nbody.",
+            usage=Usage(input_tokens=100, output_tokens=10, cost_usd=0.002),
+            session_id="draft-sess",
+            exit_code=0,
+        )
+
+
+def test_commit_draft_sees_untracked_files_and_accounts_usage(fixture_repo):
+    # F-008: a NEW-file phase must not draft from an empty diff, and the
+    # message-agent's usage must land in the manifest totals.
+    (fixture_repo / "brand_new.py").write_text("print('new')\n")  # untracked
+    drafter = RecordingDrafter()
+    text = """
+name: demo
+version: 1
+stages:
+  - id: s
+    steps:
+      - {id: commit, type: commit, message_agent: triage}
+"""
+    cfg = {"agents": {"triage": {"adapter": "api", "model": "h"}}}
+    orch = _orch(fixture_repo, text, config=cfg, adapters={"triage": drafter})
+    assert orch.drive() == M.RUN_DONE
+    # the untracked new file is visible to the drafter (status section)
+    assert "brand_new.py" in drafter.prompts[0]
+    # the drafter's cost is accumulated into the run + step totals
+    assert orch.manifest.totals.cost_usd == 0.002
+    assert orch.manifest.record("commit").usage.cost_usd == 0.002
+    assert orch.manifest.record("commit").session_id == "draft-sess"
+
+
 def test_commit_bad_literal_message_fails(fixture_repo):
     (fixture_repo / "work.py").write_text("code\n")
     text = """
```

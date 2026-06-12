# Confirm pass — Gauntlet bootstrap, Phase P3, round 2

In round 1 you found F-008 `partially_resolved`: "The drafter now sees
untracked-file status, optional plan_section content, and the final draft
result's usage/session are written into the commit step record. However the
redraft loop overwrites usage on each attempt, so usage from rejected draft
calls is still omitted from manifest totals."

The builder applied a round-2 fix in commit `6b389dc` ("P3.2"). Check ONLY
whether this diff resolves that specific F-008 sub-point — do not re-review
anything else. Return a single verdict for F-008
(`resolved | partially_resolved | unresolved | regression_introduced`) with a
one-or-two-sentence note, plus any `new_findings` the diff introduces (else
empty). Output only JSON conforming to the schema.

## The fix diff (`bb46479..6b389dc`, the P3.2 commit)
```diff
diff --git a/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-events.jsonl b/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-events.jsonl
new file mode 100644
index 0000000..2e9a336
--- /dev/null
+++ b/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-events.jsonl
@@ -0,0 +1,40 @@
+{"type":"thread.started","thread_id":"019eb8d1-45d5-7411-8e13-19cdc47c2f99"}
+{"type":"turn.started"}
+{"type":"item.started","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc 'git rev-parse --short HEAD && git status --short && git diff --stat 77570f9..bb46479'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_0","type":"command_execution","command":"/bin/zsh -lc 'git rev-parse --short HEAD && git status --short && git diff --stat 77570f9..bb46479'","aggregated_output":"bb46479\n?? runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-events.jsonl\n?? runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-prompt.md\n?? runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-schema.json\n?? runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-stderr.txt\n .gauntlet/pins.yaml                                |   5 +\n .../manual/p3-cycle-r1/findings.json               |   1 +\n .../manual/p3-cycle-r1/review-events.jsonl         | 112 +++++++++++++++++++\n .../manual/p3-cycle-r1/review-prompt.md            | 123 +++++++++++++++++++++\n .../manual/p3-cycle-r1/review-schema.json          |  41 +++++++\n .../manual/p3-cycle-r1/triage.json                 |  72 ++++++++++++\n src/gauntlet/engine/execution.py                   |  24 ++++\n src/gauntlet/engine/judgeproc.py                   |  26 ++++-\n src/gauntlet/engine/orchestrator.py                |  39 +++++--\n src/gauntlet/engine/run.py                         |  76 +++++++++----\n src/gauntlet/engine/steptypes.py                   | 112 ++++++++++++++-----\n src/gauntlet/engine/validate.py                    |  27 ++++-\n tests/integration/test_pipeline_contract.py        |   7 +-\n tests/unit/test_judgeproc.py                       |  63 +++++++++++\n tests/unit/test_orchestrator.py                    |  76 +++++++++++++\n tests/unit/test_pipeline_loader.py                 |  51 ++++++++-\n tests/unit/test_run_lifecycle.py                   |  34 ++++++\n tests/unit/test_steptypes.py                       |  44 ++++++++\n 18 files changed, 864 insertions(+), 69 deletions(-)\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/execution.py | sed -n '1,220p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.started","item":{"id":"item_2","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/orchestrator.py | sed -n '1,460p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/execution.py | sed -n '1,220p'\"","aggregated_output":"     1\t\"\"\"Step execution contract: context, result, and the step-type registry.\n     2\t\n     3\tStep handlers receive a :class:`StepContext` (everything they may touch) and\n     4\treturn a :class:`StepResult` (what the orchestrator records). Control flow —\n     5\t``on_fail`` routing, retries, parking, budget halts — is the orchestrator's\n     6\tjob, not the handler's; a handler just reports ``done``/``failed``/``parked``.\n     7\t\n     8\tStep types register in :data:`BUILTIN_STEP_TYPES` and via the\n     9\t``gauntlet.step_types`` entry point (FR-5.5); each carries the capability\n    10\tmetadata the load-time validator needs (FR-2.3).\n    11\t\"\"\"\n    12\t\n    13\tfrom __future__ import annotations\n    14\t\n    15\tfrom collections.abc import Callable\n    16\tfrom dataclasses import dataclass, field\n    17\tfrom importlib.metadata import entry_points\n    18\tfrom pathlib import Path\n    19\tfrom typing import Any\n    20\t\n    21\tfrom gauntlet.adapters.base import AgentResult, Usage\n    22\tfrom gauntlet.engine.config import RunConfig\n    23\tfrom gauntlet.engine.manifest import Manifest, StepRecord\n    24\tfrom gauntlet.engine.pipeline import Pipeline, Step\n    25\tfrom gauntlet.logging.redact import RedactingWriter\n    26\t\n    27\tSTEP_TYPE_ENTRY_POINT_GROUP = \"gauntlet.step_types\"\n    28\t\n    29\t# StepResult.status values\n    30\tDONE = \"done\"\n    31\tFAILED = \"failed\"\n    32\tPARKED = \"parked\"\n    33\tHALTED = \"halted\"\n    34\tSKIPPED = \"skipped\"\n    35\tINTERRUPTED = \"interrupted\"  # killed mid-edit; record interrupted, park the run\n    36\t\n    37\t\n    38\t@dataclass\n    39\tclass StepResult:\n    40\t    status: str\n    41\t    session_id: str | None = None\n    42\t    usage: Usage | None = None\n    43\t    commit_sha: str | None = None\n    44\t    commit_phase: str | None = None\n    45\t    notes: str = \"\"\n    46\t    # artifacts this step produced (artifact name -> path), merged into context\n    47\t    artifact_writes: dict[str, Path] = field(default_factory=dict)\n    48\t\n    49\t\n    50\t# An adapter factory lets unit tests inject fakes by agent-profile name without\n    51\t# touching the entry-point registry (the orchestrator stays offline-testable).\n    52\tAdapterFactory = Callable[[str], Any]\n    53\t\n    54\t\n    55\t@dataclass\n    56\tclass StepContext:\n    57\t    repo_root: Path\n    58\t    run_dir: Path\n    59\t    artifact_root: Path  # slug dir: prd.md / plan.md / step outputs live here\n    60\t    config: RunConfig\n    61\t    pipeline: Pipeline\n    62\t    manifest: Manifest\n    63\t    record: StepRecord\n    64\t    writer: RedactingWriter\n    65\t    judge_env: dict[str, str] = field(default_factory=dict)\n    66\t    artifacts: dict[str, Path] = field(default_factory=dict)\n    67\t    # repo-relative paths of the engine's own bookkeeping (review F-001); commit\n    68\t    # and dirty checks exclude these but not real run artifacts.\n    69\t    excludes: list[str] = field(default_factory=list)\n    70\t    iteration_item: Any | None = None\n    71\t    iteration_index: int | None = None\n    72\t    adapter_factory: AdapterFactory | None = None\n    73\t\n    74\t    def build_adapter(self, agent_name: str) -> Any:\n    75\t        \"\"\"Resolve an agent profile to an adapter instance (override in tests).\"\"\"\n    76\t        if self.adapter_factory is not None:\n    77\t            return self.adapter_factory(agent_name)\n    78\t        return self.config.profile(agent_name).build_adapter()\n    79\t\n    80\t    def steps_dir(self) -> Path:\n    81\t        return self.run_dir / \"steps\"\n    82\t\n    83\t\n    84\tHandler = Callable[[Step, StepContext], StepResult]\n    85\t\n    86\t\n    87\t@dataclass(frozen=True)\n    88\tclass StepSpec:\n    89\t    \"\"\"Static metadata + handler for one step type.\"\"\"\n    90\t\n    91\t    type: str\n    92\t    handler: Handler\n    93\t    # FR-2.3 load-time capability checks:\n    94\t    requires_repo_write: bool = False  # bound agent must be repo-write capable\n    95\t    uses_schema: bool = False  # warn if the bound adapter is best-effort JSON\n    96\t    needs_agent: bool = False  # must declare `agent:` (or message_agent)\n    97\t    # F-003: record the step's base SHA before running it.\n    98\t    touches_worktree: bool = False\n    99\t\n   100\t    def step_requires_repo_write(self, step: Step) -> bool:\n   101\t        # agent_task may opt out via `repo_write: false` (e.g. a doc reviewer).\n   102\t        if self.type == \"agent_task\":\n   103\t            return bool(step.get(\"repo_write\", True))\n   104\t        return self.requires_repo_write\n   105\t\n   106\t    def step_touches_worktree(self, step: Step) -> bool:\n   107\t        if self.type == \"agent_task\":\n   108\t            return bool(step.get(\"repo_write\", True))\n   109\t        return self.touches_worktree\n   110\t\n   111\t\n   112\tdef builtin_specs() -> dict[str, StepSpec]:\n   113\t    # Imported lazily to avoid a cycle (steptypes imports this module).\n   114\t    from gauntlet.engine import steptypes\n   115\t\n   116\t    return steptypes.SPECS\n   117\t\n   118\t\n   119\tdef step_specs() -> dict[str, StepSpec]:\n   120\t    \"\"\"All step specs: built-ins plus ``gauntlet.step_types`` entry points.\"\"\"\n   121\t    specs = dict(builtin_specs())\n   122\t    for ep in entry_points(group=STEP_TYPE_ENTRY_POINT_GROUP):\n   123\t        spec = ep.load()\n   124\t        spec_obj = spec() if callable(spec) and not isinstance(spec, StepSpec) else spec\n   125\t        if isinstance(spec_obj, StepSpec):\n   126\t            specs[spec_obj.type] = spec_obj\n   127\t    return specs\n   128\t\n   129\t\n   130\tdef get_spec(step_type: str) -> StepSpec:\n   131\t    specs = step_specs()\n   132\t    try:\n   133\t        return specs[step_type]\n   134\t    except KeyError:\n   135\t        raise KeyError(\n   136\t            f\"unknown step type {step_type!r}; registered: {sorted(specs)}\"\n   137\t        ) from None\n   138\t\n   139\t\n   140\tdef usage_from_result(result: AgentResult) -> Usage | None:\n   141\t    return result.usage\n   142\t\n   143\t\n   144\tdef run_bookkeeping_excludes(repo_root: Path, run_dir: Path, artifact_root: Path) -> list[str]:\n   145\t    \"\"\"Repo-relative paths of the engine's own live bookkeeping (review F-001).\n   146\t\n   147\t    These — the run-instance dir (manifest/transcripts/steps/judge-audit) and the\n   148\t    active-run pointer — must be invisible to worktree-state checks and commits.\n   149\t    Everything *else* under the run root (prd.md, plan.md, declared step outputs)\n   150\t    is real work: tracked, detected by the transaction boundary, and committable.\n   151\t    Narrowing the exclusion to just this set is the F-001 fix — the prior code\n   152\t    excluded the whole run root, hiding real partial effects from the dirty-base\n   153\t    check.\n   154\t    \"\"\"\n   155\t    excludes: list[str] = []\n   156\t    root = repo_root.resolve()\n   157\t    for p in (run_dir, artifact_root / \"active-run.txt\"):\n   158\t        try:\n   159\t            excludes.append(p.resolve().relative_to(root).as_posix())\n   160\t        except ValueError:\n   161\t            continue\n   162\t    return excludes\n","exit_code":0,"status":"completed"}}
+{"type":"item.completed","item":{"id":"item_2","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/orchestrator.py | sed -n '1,460p'\"","aggregated_output":"     1\t\"\"\"The pipeline orchestrator: a resumable state machine (FR-8, FR-10, F-003).\n     2\t\n     3\tDeterminism over cleverness (plan §2): execution is an explicit walk over\n     4\tstages → steps with a write-ahead manifest. Every step is bracketed by a\n     5\tmanifest write *before* (status ``running`` + base SHA for worktree steps) and\n     6\t*after* (terminal status + usage). A ``kill -9`` at any instant therefore\n     7\tleaves a consistent manifest, and :meth:`resume` re-enters at the first\n     8\tnon-``done`` step. Worktree-mutating agent steps killed mid-edit are detected\n     9\tvia the base-SHA transaction boundary and parked (or reset), never re-run over.\n    10\t\n    11\tControl flow lives here, not in handlers: ``when`` skipping, ``foreach``\n    12\tfan-out, ``on_fail`` routing with bounded retries, ``human_gate`` parking,\n    13\tand per-step budget halts (FR-3.3).\n    14\t\"\"\"\n    15\t\n    16\tfrom __future__ import annotations\n    17\t\n    18\timport os\n    19\tfrom datetime import datetime, timezone\n    20\tfrom pathlib import Path\n    21\tfrom typing import Any, Callable\n    22\t\n    23\tfrom gauntlet.adapters.base import AgentTimeoutError\n    24\tfrom gauntlet.engine import gitops, manifest as M\n    25\tfrom gauntlet.engine.config import RunConfig\n    26\tfrom gauntlet.engine.execution import (\n    27\t    DONE,\n    28\t    FAILED,\n    29\t    HALTED,\n    30\t    INTERRUPTED,\n    31\t    PARKED,\n    32\t    SKIPPED,\n    33\t    StepContext,\n    34\t    StepResult,\n    35\t    get_spec,\n    36\t    run_bookkeeping_excludes,\n    37\t)\n    38\tfrom gauntlet.engine.expr import eval_when, resolve_list\n    39\tfrom gauntlet.engine.manifest import Manifest, StepRecord\n    40\tfrom gauntlet.engine.pipeline import Pipeline, Stage, Step\n    41\tfrom gauntlet.logging.redact import RedactingWriter\n    42\t\n    43\t\n    44\tdef _utcnow() -> str:\n    45\t    return datetime.now(timezone.utc).isoformat()\n    46\t\n    47\t\n    48\tclass Orchestrator:\n    49\t    def __init__(\n    50\t        self,\n    51\t        *,\n    52\t        repo_root: Path,\n    53\t        run_dir: Path,\n    54\t        artifact_root: Path,\n    55\t        config: RunConfig,\n    56\t        pipeline: Pipeline,\n    57\t        manifest: Manifest,\n    58\t        writer: RedactingWriter | None = None,\n    59\t        judge_env: dict[str, str] | None = None,\n    60\t        adapter_factory: Callable[[str], Any] | None = None,\n    61\t        extra_context: dict[str, Any] | None = None,\n    62\t        clock: Callable[[], str] = _utcnow,\n    63\t    ) -> None:\n    64\t        self.repo_root = repo_root\n    65\t        self.run_dir = run_dir\n    66\t        self.artifact_root = artifact_root\n    67\t        self.config = config\n    68\t        self.pipeline = pipeline\n    69\t        self.manifest = manifest\n    70\t        self.writer = writer or RedactingWriter()\n    71\t        self.judge_env = judge_env or {}\n    72\t        self.adapter_factory = adapter_factory\n    73\t        self.extra_context = extra_context or {}\n    74\t        self.clock = clock\n    75\t        self.manifest_path = run_dir / \"manifest.json\"\n    76\t        self.artifacts: dict[str, Path] = {}\n    77\t        # Narrow exclusion: only the engine's own bookkeeping is hidden from\n    78\t        # dirty checks / commits — real run artifacts stay visible (review F-001).\n    79\t        self.excludes = run_bookkeeping_excludes(repo_root, run_dir, artifact_root)\n    80\t        self._ignore_run_dir()\n    81\t        self._seed_artifacts()\n    82\t\n    83\t    def _ignore_run_dir(self) -> None:\n    84\t        \"\"\"Keep the engine's own live run-instance dir out of the worktree state.\n    85\t\n    86\t        The manifest/transcripts are written *into* the repo (FR-4.1) and would\n    87\t        otherwise dirty the worktree continuously — destroying the clean-handoff\n    88\t        invariant (CLAUDE.md §1) and the base-SHA transaction boundary (F-003).\n    89\t        A self-ignoring ``.gitignore`` (``*``) makes the live run dir invisible\n    90\t        to ``git status`` / ``git add -A``; finalized, tracked artifacts are the\n    91\t        P4 logger's concern (FR-4.5). prd.md/plan.md live in the parent slug dir\n    92\t        and stay tracked.\n    93\t        \"\"\"\n    94\t        self.run_dir.mkdir(parents=True, exist_ok=True)\n    95\t        gitignore = self.run_dir / \".gitignore\"\n    96\t        if not gitignore.exists():\n    97\t            gitignore.write_text(\"*\\n\")\n    98\t\n    99\t    # ---- public entry points -------------------------------------------------\n   100\t    def drive(self) -> str:\n   101\t        \"\"\"Run pending steps until completion, a gate, a halt, or a failure.\n   102\t\n   103\t        Idempotent and resumable: steps already ``done``/``skipped`` in the\n   104\t        manifest are not re-run. Returns the resulting run status.\n   105\t        \"\"\"\n   106\t        self.manifest.status = M.RUN_RUNNING\n   107\t        self._persist()\n   108\t        for stage in self.pipeline.stages:\n   109\t            status = self._run_stage(stage)\n   110\t            if status != DONE:\n   111\t                return self._set_run_status(status)\n   112\t        return self._set_run_status(DONE)\n   113\t\n   114\t    def approve_gate(self, step_id: str, notes: str | None = None) -> str:\n   115\t        rec = self._find_parked_gate(step_id)\n   116\t        rec.status = M.DONE\n   117\t        rec.ended = self.clock()\n   118\t        rec.notes = f\"approved: {notes}\" if notes else \"approved\"\n   119\t        self._persist()\n   120\t        return self.drive()\n   121\t\n   122\t    def reject_gate(self, step_id: str, notes: str) -> str:\n   123\t        rec = self._find_parked_gate(step_id)\n   124\t        rec.status = M.FAILED\n   125\t        rec.ended = self.clock()\n   126\t        rec.notes = f\"rejected: {notes}\"\n   127\t        self._persist()\n   128\t        return self._set_run_status(FAILED)\n   129\t\n   130\t    # ---- stage / step walk ---------------------------------------------------\n   131\t    def _run_stage(self, stage: Stage) -> str:\n   132\t        if not eval_when(stage.when, self._context()):\n   133\t            for step in stage.steps:\n   134\t                self._mark_skipped(step.id, None)\n   135\t            self._persist()\n   136\t            return DONE\n   137\t        if stage.foreach is None:\n   138\t            return self._run_steps(stage, iteration=None, item=None)\n   139\t        items = resolve_list(stage.foreach, self._context())\n   140\t        for idx, item in enumerate(items):\n   141\t            status = self._run_steps(stage, iteration=str(idx), item=item)\n   142\t            if status != DONE:\n   143\t                return status\n   144\t        return DONE\n   145\t\n   146\t    def _run_steps(self, stage: Stage, *, iteration: str | None, item: Any) -> str:\n   147\t        index = {step.id: i for i, step in enumerate(stage.steps)}\n   148\t        retries: dict[str, int] = {}\n   149\t        ptr = 0\n   150\t        while ptr < len(stage.steps):\n   151\t            step = stage.steps[ptr]\n   152\t            rec = self.manifest.record(step.id, iteration)\n   153\t            if rec is not None and rec.status in (M.DONE, M.SKIPPED):\n   154\t                ptr += 1\n   155\t                continue\n   156\t            if not eval_when(step.when, self._context(item, iteration)):\n   157\t                self._mark_skipped(step.id, iteration)\n   158\t                self._persist()\n   159\t                ptr += 1\n   160\t                continue\n   161\t            if step.foreach is not None and iteration is None:\n   162\t                status = self._run_step_foreach(step)\n   163\t            else:\n   164\t                result = self._execute(step, iteration, item)\n   165\t                status = result.status\n   166\t            if status == DONE:\n   167\t                ptr += 1\n   168\t                continue\n   169\t            if status == FAILED and step.on_fail is not None:\n   170\t                if retries.get(step.id, 0) < step.on_fail.max_retries:\n   171\t                    retries[step.id] = retries.get(step.id, 0) + 1\n   172\t                    self._reset_for_retry(stage, step.on_fail.route_to, iteration)\n   173\t                    ptr = index[step.on_fail.route_to]\n   174\t                    continue\n   175\t            return status  # FAILED, PARKED, HALTED, or INTERRUPTED\n   176\t        return DONE\n   177\t\n   178\t    def _run_step_foreach(self, step: Step) -> str:\n   179\t        items = resolve_list(step.foreach, self._context())\n   180\t        for idx, item in enumerate(items):\n   181\t            rec = self.manifest.record(step.id, str(idx))\n   182\t            if rec is not None and rec.status in (M.DONE, M.SKIPPED):\n   183\t                continue  # resume: don't re-run a completed iteration (F-004)\n   184\t            result = self._execute(step, str(idx), item)\n   185\t            if result.status != DONE:\n   186\t                return result.status\n   187\t        return DONE\n   188\t\n   189\t    def _reset_for_retry(\n   190\t        self, stage: Stage, route_to: str, iteration: str | None\n   191\t    ) -> None:\n   192\t        \"\"\"Mark the route target and everything after it pending so they re-run.\n   193\t\n   194\t        The route target is typically already ``done`` (e.g. routing tests back\n   195\t        to a completed implement); it must be reset too, otherwise the loop skips\n   196\t        it and only the failing step re-runs forever.\n   197\t        \"\"\"\n   198\t        ids = [s.id for s in stage.steps]\n   199\t        start = ids.index(route_to)\n   200\t        for sid in ids[start:]:\n   201\t            rec = self.manifest.record(sid, iteration)\n   202\t            if rec is not None:\n   203\t                rec.status = M.PENDING\n   204\t                rec.ended = None\n   205\t\n   206\t    # ---- single-step execution ----------------------------------------------\n   207\t    def _execute(self, step: Step, iteration: str | None, item: Any) -> StepResult:\n   208\t        spec = get_spec(step.type)\n   209\t        rec = self.manifest.record(step.id, iteration)\n   210\t        resuming = rec is not None and rec.status in (M.RUNNING, M.INTERRUPTED)\n   211\t        if rec is None:\n   212\t            rec = StepRecord(\n   213\t                id=step.id, type=step.type, agent=step.agent, iteration=iteration\n   214\t            )\n   215\t            self.manifest.upsert(rec)\n   216\t\n   217\t        if resuming:\n   218\t            short = self._resume_disposition(step, spec, rec)\n   219\t            if short is not None:\n   220\t                self._finalize(rec, short)\n   221\t                return short\n   222\t\n   223\t        rec.attempts += 1\n   224\t        rec.status = M.RUNNING\n   225\t        rec.started = rec.started or self.clock()\n   226\t        self.manifest.current_step = step.id\n   227\t        if spec.step_touches_worktree(step) and rec.base_sha is None:\n   228\t            rec.base_sha = self._head_sha()\n   229\t        # Per-step judge attribution: child agent hooks read GAUNTLET_STEP_ID\n   230\t        # (only under an active judge run; unit tests leave judge_env empty).\n   231\t        if self.judge_env:\n   232\t            os.environ[\"GAUNTLET_STEP_ID\"] = step.id\n   233\t        self._persist()  # WRITE-AHEAD: before any side effect\n   234\t\n   235\t        ctx = self._make_context(step, rec, iteration, item)\n   236\t        try:\n   237\t            result = spec.handler(step, ctx)\n   238\t        except AgentTimeoutError as exc:\n   239\t            result = StepResult(\n   240\t                status=HALTED,\n   241\t                usage=exc.partial.usage if exc.partial else None,\n   242\t                session_id=exc.partial.session_id if exc.partial else None,\n   243\t                notes=f\"timeout halt (FR-3.3): {exc}\",\n   244\t            )\n   245\t        except Exception as exc:  # fail closed: a handler fault halts the step\n   246\t            result = StepResult(status=FAILED, notes=f\"handler error: {exc}\")\n   247\t\n   248\t        result = self._apply_budget_guard(step, rec, result)\n   249\t        self._finalize(rec, result)\n   250\t        self._persist()  # WRITE-AHEAD: after the side effect, terminal state\n   251\t        return result\n   252\t\n   253\t    def _resume_disposition(\n   254\t        self, step: Step, spec, rec: StepRecord\n   255\t    ) -> StepResult | None:\n   256\t        \"\"\"Decide how to re-enter a step that was interrupted (review F-003).\n   257\t\n   258\t        Returns a terminal StepResult to short-circuit (park), or ``None`` to\n   259\t        proceed with a normal (re-)run. Only repo-writing agent steps that left\n   260\t        a dirty worktree are parked/reset; idempotent steps (shell) and the\n   261\t        commit step (which reconciles from git log) simply re-run.\n   262\t        \"\"\"\n   263\t        if rec.base_sha is None or not spec.step_touches_worktree(step):\n   264\t            return None\n   265\t        is_agent_write = step.type == \"agent_task\" and spec.step_requires_repo_write(\n   266\t            step\n   267\t        )\n   268\t        if not is_agent_write:\n   269\t            return None\n   270\t        # Detect partial work against the narrow bookkeeping exclusion, so a\n   271\t        # partial *artifact* under the run root (not just a repo-root file) is\n   272\t        # still seen as a mid-edit interruption (review F-001).\n   273\t        if not gitops.is_dirty_vs(self.repo_root, rec.base_sha, exclude=self.excludes):\n   274\t            return None  # clean re-entry: agent never progressed; safe to re-run\n   275\t        if self.config.interrupted_step == \"reset_to_base\":\n   276\t            ts = self.clock().replace(\":\", \"-\")\n   277\t            backup = f\"refs/gauntlet/backup/{self.manifest.run_id}/{rec.id}-{ts}\"\n   278\t            # Snapshot the partial work (tracked + untracked) before discarding.\n   279\t            gitops.backup_dirty_worktree(\n   280\t                self.repo_root, backup, f\"interrupted {rec.id} partial work\",\n   281\t                exclude=self.excludes,\n   282\t            )\n   283\t            gitops.reset_hard(self.repo_root, rec.base_sha)\n   284\t            # `clean` is broader than the dirty check on purpose: it spares the\n   285\t            # whole run root so the reset never wipes the run pointer, manifests,\n   286\t            # the authored prd.md, or prior declared artifacts — the re-run\n   287\t            # regenerates its own outputs over them.\n   288\t            gitops.clean_untracked(self.repo_root, exclude=[self.config.run_root])\n   289\t            return None  # tree restored to base; re-run cleanly\n   290\t        return StepResult(\n   291\t            status=INTERRUPTED,\n   292\t            notes=(\n   293\t                \"interrupted mid-edit: worktree dirty vs base SHA \"\n   294\t                f\"{rec.base_sha[:10]}; parked for a human (F-003, \"\n   295\t                \"interrupted_step=park)\"\n   296\t            ),\n   297\t        )\n   298\t\n   299\t    def _apply_budget_guard(\n   300\t        self, step: Step, rec: StepRecord, result: StepResult\n   301\t    ) -> StepResult:\n   302\t        if result.status != DONE or result.usage is None:\n   303\t            return result\n   304\t        budget = step.budget_usd\n   305\t        if budget is None and step.agent and step.agent in self.config.agents:\n   306\t            budget = self.config.profile(step.agent).budget_usd\n   307\t        if budget is None or result.usage.cost_usd is None:\n   308\t            return result\n   309\t        projected = (rec.usage.cost_usd or 0.0) + result.usage.cost_usd\n   310\t        if projected > budget:\n   311\t            return StepResult(\n   312\t                status=HALTED,\n   313\t                usage=result.usage,\n   314\t                session_id=result.session_id,\n   315\t                notes=(\n   316\t                    f\"budget halt (FR-3.3): step cost ${projected:.4f} exceeds \"\n   317\t                    f\"budget ${budget:.4f}; halting at checkpoint\"\n   318\t                ),\n   319\t            )\n   320\t        return result\n   321\t\n   322\t    def _finalize(self, rec: StepRecord, result: StepResult) -> None:\n   323\t        rec.status = {\n   324\t            DONE: M.DONE,\n   325\t            FAILED: M.FAILED,\n   326\t            PARKED: M.PARKED,\n   327\t            HALTED: M.HALTED,\n   328\t            SKIPPED: M.SKIPPED,\n   329\t            INTERRUPTED: M.INTERRUPTED,\n   330\t        }[result.status]\n   331\t        rec.ended = self.clock()\n   332\t        if result.session_id:\n   333\t            rec.session_id = result.session_id\n   334\t        if result.usage is not None:\n   335\t            rec.usage.add(result.usage)\n   336\t            self.manifest.totals.add(result.usage)\n   337\t        if result.notes:\n   338\t            rec.notes = result.notes\n   339\t        if result.commit_sha:\n   340\t            self.manifest.commits.append(\n   341\t                M.CommitRecord(\n   342\t                    step_id=rec.id,\n   343\t                    phase=result.commit_phase or \"\",\n   344\t                    sha=result.commit_sha,\n   345\t                )\n   346\t            )\n   347\t        if result.status == DONE:\n   348\t            for name, path in result.artifact_writes.items():\n   349\t                self.artifacts[name] = path\n   350\t\n   351\t    # ---- helpers -------------------------------------------------------------\n   352\t    def _make_context(\n   353\t        self, step: Step, rec: StepRecord, iteration: str | None, item: Any\n   354\t    ) -> StepContext:\n   355\t        return StepContext(\n   356\t            repo_root=self.repo_root,\n   357\t            run_dir=self.run_dir,\n   358\t            artifact_root=self.artifact_root,\n   359\t            config=self.config,\n   360\t            pipeline=self.pipeline,\n   361\t            manifest=self.manifest,\n   362\t            record=rec,\n   363\t            writer=self.writer,\n   364\t            judge_env=self.judge_env,\n   365\t            artifacts=dict(self.artifacts),\n   366\t            excludes=self.excludes,\n   367\t            iteration_item=item,\n   368\t            iteration_index=int(iteration) if iteration is not None else None,\n   369\t            adapter_factory=self.adapter_factory,\n   370\t        )\n   371\t\n   372\t    def _context(self, item: Any = None, iteration: str | None = None) -> dict[str, Any]:\n   373\t        ctx: dict[str, Any] = {\n   374\t            \"config\": self.config,\n   375\t            \"artifacts\": {name: True for name in self._existing_artifacts()},\n   376\t            \"vars\": self.extra_context,\n   377\t        }\n   378\t        ctx.update(self.extra_context)\n   379\t        if item is not None:\n   380\t            ctx[\"item\"] = item\n   381\t        return ctx\n   382\t\n   383\t    def _seed_artifacts(self) -> None:\n   384\t        for name in (\"prd.md\", \"plan.md\"):\n   385\t            path = self.artifact_root / name\n   386\t            if path.exists():\n   387\t                self.artifacts[name] = path\n   388\t\n   389\t    def _existing_artifacts(self) -> set[str]:\n   390\t        names = set(self.artifacts)\n   391\t        for name in (\"prd.md\", \"plan.md\"):\n   392\t            if (self.artifact_root / name).exists():\n   393\t                names.add(name)\n   394\t        return names\n   395\t\n   396\t    def _mark_skipped(self, step_id: str, iteration: str | None) -> None:\n   397\t        rec = self.manifest.record(step_id, iteration)\n   398\t        if rec is None:\n   399\t            rec = StepRecord(id=step_id, type=\"\", iteration=iteration)\n   400\t            self.manifest.upsert(rec)\n   401\t        rec.status = M.SKIPPED\n   402\t        rec.ended = self.clock()\n   403\t\n   404\t    def _find_parked_gate(self, step_id: str) -> StepRecord:\n   405\t        # Scan across iterations so a gate parked inside a foreach (record\n   406\t        # `gate` with iteration `1`) is reachable by approve/reject (F-004).\n   407\t        for rec in self.manifest.steps:\n   408\t            if rec.id == step_id and rec.status == M.PARKED:\n   409\t                return rec\n   410\t        existing = self.manifest.record(step_id)\n   411\t        raise ValueError(\n   412\t            f\"step {step_id!r} is not parked at a gate \"\n   413\t            f\"(status: {existing.status if existing else 'absent'})\"\n   414\t        )\n   415\t\n   416\t    def _head_sha(self) -> str:\n   417\t        return gitops.head_sha(self.repo_root)\n   418\t\n   419\t    def _set_run_status(self, step_status: str) -> str:\n   420\t        self.manifest.status = {\n   421\t            DONE: M.RUN_DONE,\n   422\t            PARKED: M.RUN_PARKED,\n   423\t            HALTED: M.RUN_PARKED,  # a halt parks the run for a human (FR-3.3)\n   424\t            INTERRUPTED: M.RUN_PARKED,  # mid-edit interruption parks (F-003)\n   425\t            FAILED: M.RUN_FAILED,\n   426\t        }.get(step_status, M.RUN_RUNNING)\n   427\t        if self.manifest.status in (M.RUN_DONE,):\n   428\t            self.manifest.current_step = None\n   429\t        self._persist()\n   430\t        return self.manifest.status\n   431\t\n   432\t    def _persist(self) -> None:\n   433\t        self.manifest.write_atomic(self.manifest_path)\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_3","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/run.py | sed -n '1,360p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_3","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/run.py | sed -n '1,360p'\"","aggregated_output":"     1\t\"\"\"Run lifecycle: new / run / status / approve / reject / resume / abort / rollback.\n     2\t\n     3\tGlue between the CLI and the :class:`Orchestrator`. Owns the on-disk layout\n     4\t(FR-4.1), the entry contract (FR-10.1), branch management (FR-9.1), the\n     5\tengine-managed judge lifecycle (FR-7.1), and guarded rollback (FR-9.9 /\n     6\treview F-010).\n     7\t\"\"\"\n     8\t\n     9\tfrom __future__ import annotations\n    10\t\n    11\timport shutil\n    12\tfrom dataclasses import dataclass\n    13\tfrom datetime import datetime, timezone\n    14\tfrom pathlib import Path\n    15\t\n    16\tfrom gauntlet.engine import gitops, manifest as M\n    17\tfrom gauntlet.engine.config import RunConfig\n    18\tfrom gauntlet.engine.execution import run_bookkeeping_excludes\n    19\tfrom gauntlet.engine.judgeproc import ManagedJudge\n    20\tfrom gauntlet.engine.manifest import Manifest, PipelineRef\n    21\tfrom gauntlet.engine.orchestrator import Orchestrator\n    22\tfrom gauntlet.engine.pipeline import load_pipeline\n    23\tfrom gauntlet.engine.validate import validate_pipeline\n    24\tfrom gauntlet.logging.redact import RedactingWriter\n    25\t\n    26\t# Marker written into a scaffolded PRD; the entry contract refuses to run while\n    27\t# it is still present (FR-10.1 / review OQ-1: existence + non-stub-ness).\n    28\tPRD_STUB_MARKER = \"<!-- GAUNTLET-PRD-STUB: replace this file with a real PRD -->\"\n    29\t\n    30\t_PRD_STUB = f\"\"\"{PRD_STUB_MARKER}\n    31\t# PRD: <title>\n    32\t\n    33\t> Gauntlet does not author PRDs (FR-10.1). Replace this stub with a real,\n    34\t> human-authored PRD, then run `gauntlet run <slug>`. The run refuses to start\n    35\t> while this marker is present.\n    36\t\n    37\t## Problem statement\n    38\t\n    39\t## Requirements\n    40\t\"\"\"\n    41\t\n    42\t\n    43\tclass EntryContractError(RuntimeError):\n    44\t    \"\"\"The entry contract (FR-10.1) is not satisfied.\"\"\"\n    45\t\n    46\t\n    47\tclass RollbackGuardError(RuntimeError):\n    48\t    \"\"\"A rollback guard (review F-010) refused the operation.\"\"\"\n    49\t\n    50\t\n    51\tdef _utc_stamp() -> str:\n    52\t    return datetime.now(timezone.utc).strftime(\"%Y-%m-%dT%H-%M-%S\")\n    53\t\n    54\t\n    55\tdef _strip_marker(text: str) -> str:\n    56\t    return \"\\n\".join(line for line in text.splitlines() if PRD_STUB_MARKER not in line)\n    57\t\n    58\t\n    59\tdef _normalize(text: str) -> str:\n    60\t    return \"\\n\".join(line.strip() for line in text.strip().splitlines() if line.strip())\n    61\t\n    62\t\n    63\t@dataclass\n    64\tclass RunLayout:\n    65\t    repo_root: Path\n    66\t    config: RunConfig\n    67\t    slug: str\n    68\t\n    69\t    @property\n    70\t    def slug_dir(self) -> Path:\n    71\t        return self.repo_root / self.config.run_root / self.slug\n    72\t\n    73\t    @property\n    74\t    def prd_path(self) -> Path:\n    75\t        return self.slug_dir / \"prd.md\"\n    76\t\n    77\t    @property\n    78\t    def active_pointer(self) -> Path:\n    79\t        return self.slug_dir / \"active-run.txt\"\n    80\t\n    81\t    def run_dir(self, name: str) -> Path:\n    82\t        return self.slug_dir / name\n    83\t\n    84\t    def active_run_dir(self) -> Path:\n    85\t        if not self.active_pointer.exists():\n    86\t            raise FileNotFoundError(\n    87\t                f\"no active run for {self.slug!r}; has `gauntlet run` been started?\"\n    88\t            )\n    89\t        return self.slug_dir / self.active_pointer.read_text().strip()\n    90\t\n    91\t\n    92\tclass RunManager:\n    93\t    def __init__(self, repo_root: Path, config: RunConfig | None = None) -> None:\n    94\t        self.repo_root = repo_root\n    95\t        self.config = config or RunConfig.load(repo_root / \".gauntlet/config.yaml\")\n    96\t        self.writer = RedactingWriter()\n    97\t\n    98\t    def layout(self, slug: str) -> RunLayout:\n    99\t        return RunLayout(self.repo_root, self.config, slug)\n   100\t\n   101\t    # ---- new (FR-8.1 scaffold) ----------------------------------------------\n   102\t    def new(self, slug: str) -> Path:\n   103\t        layout = self.layout(slug)\n   104\t        layout.slug_dir.mkdir(parents=True, exist_ok=True)\n   105\t        if not layout.prd_path.exists():\n   106\t            layout.prd_path.write_text(_PRD_STUB)\n   107\t        return layout.prd_path\n   108\t\n   109\t    # ---- entry contract (FR-10.1) -------------------------------------------\n   110\t    def check_entry_contract(self, slug: str) -> None:\n   111\t        layout = self.layout(slug)\n   112\t        if not layout.prd_path.exists():\n   113\t            raise EntryContractError(\n   114\t                f\"{layout.prd_path} does not exist; `gauntlet new {slug}` scaffolds \"\n   115\t                \"a stub for a human to author (FR-10.1)\"\n   116\t            )\n   117\t        content = layout.prd_path.read_text()\n   118\t        if PRD_STUB_MARKER in content:\n   119\t            raise EntryContractError(\n   120\t                f\"{layout.prd_path} is still the scaffolded stub; a human must \"\n   121\t                \"author the PRD before a run can start (FR-10.1)\"\n   122\t            )\n   123\t        # Deleting only the marker line leaves the rest of the scaffold intact —\n   124\t        # still not a human-authored PRD. Compare the whole body, marker-stripped\n   125\t        # and whitespace-normalized, against the stub (review F-007).\n   126\t        if _normalize(content) == _normalize(_strip_marker(_PRD_STUB)):\n   127\t            raise EntryContractError(\n   128\t                f\"{layout.prd_path} is the scaffolded stub with only the marker \"\n   129\t                \"removed; a human must author a real PRD before a run (FR-10.1)\"\n   130\t            )\n   131\t\n   132\t    # ---- run (FR-8.1) -------------------------------------------------------\n   133\t    def start(\n   134\t        self,\n   135\t        slug: str,\n   136\t        pipeline_path: Path,\n   137\t        *,\n   138\t        use_judge: bool = True,\n   139\t        adapter_factory=None,\n   140\t        extra_context: dict | None = None,\n   141\t        clock=None,\n   142\t    ) -> str:\n   143\t        self.check_entry_contract(slug)\n   144\t        layout = self.layout(slug)\n   145\t        pipeline, phash = load_pipeline(pipeline_path)\n   146\t        validate_pipeline(pipeline, self.config)\n   147\t\n   148\t        branch = f\"{self.config.branch_prefix}{slug}\"\n   149\t        gitops.checkout_or_create_branch(self.repo_root, branch, self.config.base_branch)\n   150\t\n   151\t        run_id = f\"run-{_utc_stamp()}\"\n   152\t        run_dir = layout.run_dir(run_id)\n   153\t        run_dir.mkdir(parents=True, exist_ok=True)\n   154\t        # Snapshot the exact pipeline source into the run dir so resume reloads\n   155\t        # precisely what started the run (FR-5.6 reproducibility).\n   156\t        (run_dir / \"pipeline.yaml\").write_text(pipeline_path.read_text())\n   157\t        layout.active_pointer.write_text(run_id)\n   158\t\n   159\t        man = Manifest(\n   160\t            run_id=run_id,\n   161\t            slug=slug,\n   162\t            branch=branch,\n   163\t            base_branch=self.config.base_branch,\n   164\t            pipeline=PipelineRef(name=pipeline.name, version=pipeline.version, hash=phash),\n   165\t            prompt_hashes=self._prompt_hashes(pipeline),\n   166\t        )\n   167\t        return self._drive(\n   168\t            layout, run_dir, pipeline, man,\n   169\t            use_judge=use_judge, adapter_factory=adapter_factory,\n   170\t            extra_context=extra_context, clock=clock,\n   171\t        )\n   172\t\n   173\t    # ---- resume (FR-8.2) ----------------------------------------------------\n   174\t    def resume(self, slug: str, *, use_judge: bool = True, adapter_factory=None,\n   175\t               extra_context: dict | None = None, clock=None) -> str:\n   176\t        layout = self.layout(slug)\n   177\t        run_dir = layout.active_run_dir()\n   178\t        man = Manifest.load(run_dir / \"manifest.json\")\n   179\t        pipeline, phash = load_pipeline(run_dir / \"pipeline.yaml\")\n   180\t        if phash != man.pipeline.hash:\n   181\t            raise RuntimeError(\n   182\t                \"pipeline content hash changed since the run started \"\n   183\t                f\"({man.pipeline.hash} -> {phash}); resume refuses to run a \"\n   184\t                \"different pipeline against an existing manifest (FR-5.6)\"\n   185\t            )\n   186\t        gitops.checkout_or_create_branch(self.repo_root, man.branch, man.base_branch)\n   187\t        return self._drive(\n   188\t            layout, run_dir, pipeline, man,\n   189\t            use_judge=use_judge, adapter_factory=adapter_factory,\n   190\t            extra_context=extra_context, clock=clock,\n   191\t        )\n   192\t\n   193\t    # ---- gates --------------------------------------------------------------\n   194\t    def approve(self, slug: str, gate: str | None = None, notes: str | None = None,\n   195\t                *, use_judge: bool = True, adapter_factory=None) -> str:\n   196\t        layout = self.layout(slug)\n   197\t        run_dir = layout.active_run_dir()\n   198\t        man = Manifest.load(run_dir / \"manifest.json\")\n   199\t        gate = gate or man.current_step\n   200\t        if gate is None:\n   201\t            raise ValueError(\"no gate to approve; run is not parked\")\n   202\t        pipeline, _ = load_pipeline(run_dir / \"pipeline.yaml\")\n   203\t        # Approving a gate drives the rest of the run, so honor use_judge for it.\n   204\t        if use_judge:\n   205\t            return self._with_judge(man, run_dir, lambda env: self._approve_drive(\n   206\t                layout, run_dir, pipeline, man, gate, notes, env, adapter_factory))\n   207\t        orch = self._orchestrator(layout, run_dir, pipeline, man,\n   208\t                                  judge_env={}, adapter_factory=adapter_factory)\n   209\t        return orch.approve_gate(gate, notes)\n   210\t\n   211\t    def reject(self, slug: str, notes: str, gate: str | None = None) -> str:\n   212\t        layout = self.layout(slug)\n   213\t        run_dir = layout.active_run_dir()\n   214\t        man = Manifest.load(run_dir / \"manifest.json\")\n   215\t        gate = gate or man.current_step\n   216\t        if gate is None:\n   217\t            raise ValueError(\"no gate to reject; run is not parked\")\n   218\t        pipeline, _ = load_pipeline(run_dir / \"pipeline.yaml\")\n   219\t        orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env={})\n   220\t        return orch.reject_gate(gate, notes)\n   221\t\n   222\t    # ---- abort --------------------------------------------------------------\n   223\t    def abort(self, slug: str) -> str:\n   224\t        layout = self.layout(slug)\n   225\t        run_dir = layout.active_run_dir()\n   226\t        man = Manifest.load(run_dir / \"manifest.json\")\n   227\t        man.status = M.RUN_ABORTED\n   228\t        man.write_atomic(run_dir / \"manifest.json\")\n   229\t        return man.status\n   230\t\n   231\t    # ---- status -------------------------------------------------------------\n   232\t    def status(self, slug: str) -> Manifest:\n   233\t        layout = self.layout(slug)\n   234\t        return Manifest.load(layout.active_run_dir() / \"manifest.json\")\n   235\t\n   236\t    # ---- rollback (FR-9.9 / review F-010) -----------------------------------\n   237\t    def rollback(self, slug: str, phase: int) -> str:\n   238\t        layout = self.layout(slug)\n   239\t        run_dir = layout.active_run_dir()\n   240\t        man = Manifest.load(run_dir / \"manifest.json\")\n   241\t\n   242\t        # Guard 1: clean work tree — only the engine's own bookkeeping is\n   243\t        # excluded (review F-001), so an uncommitted real artifact still blocks.\n   244\t        excludes = run_bookkeeping_excludes(self.repo_root, run_dir, layout.slug_dir)\n   245\t        if not gitops.is_clean(self.repo_root, exclude=excludes):\n   246\t            raise RollbackGuardError(\n   247\t                \"refusing rollback: worktree is dirty; commit or discard first\"\n   248\t            )\n   249\t        # Guard 2: branch tip MUST equal the manifest's last recorded commit.\n   250\t        # A branch ahead of the manifest (extra unmanifested commits) is a\n   251\t        # divergence — reset would silently discard those commits (review F-003).\n   252\t        if not man.commits:\n   253\t            raise RollbackGuardError(\"no recorded commits to roll back to\")\n   254\t        last_recorded = man.commits[-1].sha\n   255\t        head = gitops.head_sha(self.repo_root)\n   256\t        if head != last_recorded:\n   257\t            raise RollbackGuardError(\n   258\t                \"refusing rollback: branch has diverged from the manifest \"\n   259\t                f\"(HEAD {head[:10]} != last recorded {last_recorded[:10]}); the \"\n   260\t                \"branch and manifest must agree before a rewind (FR-9.9)\"\n   261\t            )\n   262\t        # Resolve the target: the last commit whose phase prefix is P<phase>.\n   263\t        target = self._phase_boundary_sha(man, phase)\n   264\t        if target is None:\n   265\t            raise RollbackGuardError(\n   266\t                f\"no recorded phase-{phase} commit boundary to roll back to\"\n   267\t            )\n   268\t\n   269\t        # Backup ref + manifest snapshot before any rewind (F-010).\n   270\t        ts = _utc_stamp()\n   271\t        gitops.create_ref(\n   272\t            self.repo_root, f\"refs/gauntlet/backup/{man.run_id}/{ts}\", head\n   273\t        )\n   274\t        shutil.copy2(run_dir / \"manifest.json\", run_dir / f\"manifest.snapshot-{ts}.json\")\n   275\t\n   276\t        gitops.reset_hard(self.repo_root, target)\n   277\t        self._rewind_manifest(man, run_dir, target)\n   278\t        man.write_atomic(run_dir / \"manifest.json\")\n   279\t        return target\n   280\t\n   281\t    def _rewind_manifest(self, man: Manifest, run_dir: Path, target: str) -> None:\n   282\t        \"\"\"Rewind the manifest to match the reset branch (review F-002).\n   283\t\n   284\t        Drop commits after the target, and reset to `pending` EVERY step record\n   285\t        (any type, any iteration) that executes after the target phase boundary\n   286\t        in pipeline order — not just the steps that produced dropped commits.\n   287\t        Otherwise a later resume skips work `git reset --hard` removed and the\n   288\t        branch and manifest disagree (FR-9.9).\n   289\t        \"\"\"\n   290\t        keep: list = []\n   291\t        for commit in man.commits:\n   292\t            keep.append(commit)\n   293\t            if commit.sha == target:\n   294\t                break\n   295\t        man.commits = keep\n   296\t        target_step = keep[-1].step_id\n   297\t\n   298\t        pipeline, _ = load_pipeline(run_dir / \"pipeline.yaml\")\n   299\t        order = [s.id for s in pipeline.all_steps()]\n   300\t        try:\n   301\t            cutoff = order.index(target_step)\n   302\t        except ValueError:  # pragma: no cover - defensive\n   303\t            cutoff = len(order) - 1\n   304\t        keep_ids = set(order[: cutoff + 1])\n   305\t        for rec in man.steps:\n   306\t            if rec.id not in keep_ids:\n   307\t                rec.status = M.PENDING\n   308\t                rec.base_sha = None\n   309\t                rec.session_id = None\n   310\t                rec.ended = None\n   311\t        man.status = M.RUN_PARKED\n   312\t        man.current_step = None\n   313\t\n   314\t    # ---- internals ----------------------------------------------------------\n   315\t    def _phase_boundary_sha(self, man: Manifest, phase: int) -> str | None:\n   316\t        prefix = f\"P{phase}\"\n   317\t        match = None\n   318\t        for commit in man.commits:\n   319\t            head = commit.phase.split(\".\")[0]  # P3.1 -> P3\n   320\t            if head == prefix:\n   321\t                match = commit.sha\n   322\t        return match\n   323\t\n   324\t    def _drive(self, layout, run_dir, pipeline, man, *, use_judge, adapter_factory,\n   325\t               extra_context, clock) -> str:\n   326\t        if not use_judge:\n   327\t            orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env={},\n   328\t                                      adapter_factory=adapter_factory,\n   329\t                                      extra_context=extra_context, clock=clock)\n   330\t            return orch.drive()\n   331\t        return self._with_judge(man, run_dir, lambda env: self._orchestrator(\n   332\t            layout, run_dir, pipeline, man, judge_env=env,\n   333\t            adapter_factory=adapter_factory, extra_context=extra_context,\n   334\t            clock=clock).drive())\n   335\t\n   336\t    def _with_judge(self, man, run_dir, fn):\n   337\t        judge_model = None\n   338\t        if \"judge_llm\" in self.config.agents:\n   339\t            judge_model = self.config.agents[\"judge_llm\"].model\n   340\t        judge = ManagedJudge(\n   341\t            policy_path=self.repo_root / \"policy.yaml\",\n   342\t            audit_path=run_dir / \"judge-audit.jsonl\",\n   343\t            run_id=man.run_id,\n   344\t            judge_model=judge_model,\n   345\t        )\n   346\t        env = judge.start()\n   347\t        try:\n   348\t            return fn(env)\n   349\t        finally:\n   350\t            judge.stop()\n   351\t\n   352\t    def _approve_drive(self, layout, run_dir, pipeline, man, gate, notes, env,\n   353\t                       adapter_factory):\n   354\t        orch = self._orchestrator(layout, run_dir, pipeline, man, judge_env=env,\n   355\t                                  adapter_factory=adapter_factory)\n   356\t        return orch.approve_gate(gate, notes)\n   357\t\n   358\t    def _orchestrator(self, layout, run_dir, pipeline, man, *, judge_env,\n   359\t                      adapter_factory=None, extra_context=None, clock=None) -> Orchestrator:\n   360\t        kwargs = dict(\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_4","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/run.py | sed -n '360,460p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_4","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/run.py | sed -n '360,460p'\"","aggregated_output":"   360\t        kwargs = dict(\n   361\t            repo_root=self.repo_root,\n   362\t            run_dir=run_dir,\n   363\t            artifact_root=layout.slug_dir,\n   364\t            config=self.config,\n   365\t            pipeline=pipeline,\n   366\t            manifest=man,\n   367\t            writer=self.writer,\n   368\t            judge_env=judge_env,\n   369\t            adapter_factory=adapter_factory,\n   370\t            extra_context=extra_context or {},\n   371\t        )\n   372\t        if clock is not None:\n   373\t            kwargs[\"clock\"] = clock\n   374\t        return Orchestrator(**kwargs)\n   375\t\n   376\t    def _prompt_hashes(self, pipeline) -> dict[str, str]:\n   377\t        from gauntlet.engine.pipeline import content_hash\n   378\t\n   379\t        hashes: dict[str, str] = {}\n   380\t        for step in pipeline.all_steps():\n   381\t            ref = step.get(\"prompt\")\n   382\t            if ref:\n   383\t                path = self.repo_root / ref\n   384\t                if path.exists():\n   385\t                    hashes[ref] = content_hash(path.read_text())\n   386\t        return hashes\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_5","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/config.py | sed -n '1,220p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_5","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/config.py | sed -n '1,220p'\"","aggregated_output":"     1\t\"\"\"Run configuration: agent profiles, identities, policy (`.gauntlet/config.yaml`).\n     2\t\n     3\tFR-2.1: every step's ``agent:`` references a named profile here, binding an\n     4\tadapter + model + flags. FR-2.2: swapping builder/reviewer is a YAML edit, no\n     5\tcode change. The engine builds the actual adapter instance from a profile via\n     6\tthe entry-point registry (FR-2.4); the banned-flag lint (PRD §8) runs as a side\n     7\teffect of constructing the CLI adapters and is invoked explicitly for ``api``.\n     8\t\"\"\"\n     9\t\n    10\tfrom __future__ import annotations\n    11\t\n    12\timport inspect\n    13\tfrom pathlib import Path\n    14\tfrom typing import Any\n    15\t\n    16\timport yaml\n    17\tfrom pydantic import BaseModel, ConfigDict, Field\n    18\t\n    19\tfrom gauntlet.adapters import get_adapter_class\n    20\tfrom gauntlet.config import lint_flags\n    21\tfrom gauntlet.engine.gitops import Identity\n    22\t\n    23\tDEFAULT_CONFIG_PATH = Path(\".gauntlet/config.yaml\")\n    24\t\n    25\t\n    26\tclass AgentProfile(BaseModel):\n    27\t    \"\"\"One named agent profile (FR-2.1). Adapter-specific fields are passed\n    28\t    through to the adapter constructor; engine-level budget guards (FR-3.3) are\n    29\t    stripped out before construction.\"\"\"\n    30\t\n    31\t    # extra fields are allowed so a plugin adapter can declare its own flags\n    32\t    # without a code change here (FR-2.4); they are filtered to the adapter's\n    33\t    # constructor signature at build time.\n    34\t    model_config = ConfigDict(extra=\"allow\")\n    35\t\n    36\t    adapter: str\n    37\t    model: str | None = None\n    38\t\n    39\t    # --- engine-level guards (FR-3.3); not passed to the adapter ---\n    40\t    max_turns: int | None = None\n    41\t    budget_usd: float | None = None\n    42\t    step_timeout_s: float | None = None\n    43\t\n    44\t    def adapter_class(self) -> type:\n    45\t        return get_adapter_class(self.adapter)\n    46\t\n    47\t    def _adapter_kwargs(self) -> dict[str, Any]:\n    48\t        guard_fields = {\"max_turns\", \"budget_usd\", \"step_timeout_s\"}\n    49\t        data = self.model_dump(exclude_none=True)\n    50\t        data.pop(\"adapter\", None)\n    51\t        for f in guard_fields:\n    52\t            data.pop(f, None)\n    53\t        return data\n    54\t\n    55\t    def build_adapter(self) -> Any:\n    56\t        \"\"\"Construct the adapter, filtering kwargs to its constructor signature.\n    57\t\n    58\t        Unknown profile keys (e.g. an adapter-specific flag the engine has never\n    59\t        heard of) are dropped rather than crashing, keeping FR-2.4 plugins\n    60\t        first-class. Banned flags are still rejected: the CLI adapters lint in\n    61\t        ``__init__``; ``base_flags`` is linted here regardless of adapter.\n    62\t        \"\"\"\n    63\t        cls = self.adapter_class()\n    64\t        kwargs = self._adapter_kwargs()\n    65\t        base_flags = kwargs.get(\"base_flags\")\n    66\t        if isinstance(base_flags, list):\n    67\t            lint_flags(base_flags)\n    68\t        sig = inspect.signature(cls.__init__)\n    69\t        accepted = {\n    70\t            name\n    71\t            for name in sig.parameters\n    72\t            if name not in (\"self\",)\n    73\t        }\n    74\t        # If the constructor takes **kwargs we keep everything; otherwise filter.\n    75\t        takes_var_kw = any(\n    76\t            p.kind is inspect.Parameter.VAR_KEYWORD\n    77\t            for p in sig.parameters.values()\n    78\t        )\n    79\t        if takes_var_kw:\n    80\t            return cls(**kwargs)\n    81\t        return cls(**{k: v for k, v in kwargs.items() if k in accepted})\n    82\t\n    83\t    def capabilities(self) -> Any:\n    84\t        \"\"\"Declared adapter capabilities (FR-2.3 load-time validation).\"\"\"\n    85\t        return self.adapter_class().capabilities\n    86\t\n    87\t\n    88\tclass RunConfig(BaseModel):\n    89\t    \"\"\"Top-level `.gauntlet/config.yaml` (FR-2.1, FR-9.1/9.7, F-003 policy).\"\"\"\n    90\t\n    91\t    model_config = ConfigDict(extra=\"allow\")\n    92\t\n    93\t    base_branch: str = \"main\"\n    94\t    branch_prefix: str = \"gauntlet/\"\n    95\t    # Single run-root for every artifact of a run (BOOTSTRAP-NOTES #2): plan,\n    96\t    # transcripts, and manifests live under run_root/<slug>/. FR-4.1's\n    97\t    # \".gauntlet/runs\" is this same setting; the bootstrap pins it to \"runs\".\n    98\t    run_root: str = \"runs\"\n    99\t    test_command: str = \"uv run pytest\"\n   100\t    agents: dict[str, AgentProfile] = Field(default_factory=dict)\n   101\t    identities: dict[str, Identity] = Field(default_factory=dict)\n   102\t\n   103\t    # Transaction-boundary policy on resume of a dirty interrupted step (F-003).\n   104\t    interrupted_step: str = \"park\"  # park | reset_to_base\n   105\t\n   106\t    # Reviewer-mutation policy is a P4 concern; the field is parsed here so a\n   107\t    # config authored ahead of P4 validates. Default per FR-9.6.\n   108\t    reviewer_mutation: str = \"commit\"\n   109\t\n   110\t    def profile(self, name: str) -> AgentProfile:\n   111\t        try:\n   112\t            return self.agents[name]\n   113\t        except KeyError:\n   114\t            raise KeyError(\n   115\t                f\"no agent profile named {name!r}; known: {sorted(self.agents)}\"\n   116\t            ) from None\n   117\t\n   118\t    def identity(self, agent_name: str) -> Identity:\n   119\t        \"\"\"Commit identity for an agent (FR-9.7); falls back to a generic one.\"\"\"\n   120\t        if agent_name in self.identities:\n   121\t            return self.identities[agent_name]\n   122\t        return Identity(\n   123\t            name=f\"Gauntlet {agent_name}\",\n   124\t            email=f\"{agent_name}@gauntlet.local\",\n   125\t        )\n   126\t\n   127\t    @classmethod\n   128\t    def load(cls, path: Path = DEFAULT_CONFIG_PATH) -> RunConfig:\n   129\t        if not path.exists():\n   130\t            raise FileNotFoundError(\n   131\t                f\"run config not found at {path}; `gauntlet init` scaffolds it (P6)\"\n   132\t            )\n   133\t        data = yaml.safe_load(path.read_text()) or {}\n   134\t        if not isinstance(data, dict):\n   135\t            raise ValueError(f\"{path} must be a YAML mapping, got {type(data).__name__}\")\n   136\t        return cls.model_validate(data)\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_6","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/steptypes.py | sed -n '1,360p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_6","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/steptypes.py | sed -n '1,360p'\"","aggregated_output":"     1\t\"\"\"Built-in step types: agent_task, shell, human_gate, commit (FR-5, FR-9.2).\n     2\t\n     3\tThe ``adversarial_cycle`` step type (the review→triage→fix→confirm primitive)\n     4\tis a P4 deliverable and registers there; P3 ships the four primitives the\n     5\tcrash test and switchover need. Control flow (routing, retries, parking,\n     6\tbudget halts) is the orchestrator's; handlers report status only.\n     7\t\n     8\tTrust model (plan §0 / review F-001): ``shell`` commands come **only** from\n     9\thuman-committed pipeline/config YAML — :func:`render_shell_command` refuses any\n    10\ttemplate token that is not a ``{{config.*}}`` reference, so agent-authored text\n    11\tcan never be substituted into a command line.\n    12\t\"\"\"\n    13\t\n    14\tfrom __future__ import annotations\n    15\t\n    16\timport re\n    17\timport subprocess\n    18\tfrom pathlib import Path\n    19\t\n    20\tfrom gauntlet.engine.commit_format import header_prefix, validate_commit_message\n    21\tfrom gauntlet.engine.execution import (\n    22\t    DONE,\n    23\t    FAILED,\n    24\t    HALTED,\n    25\t    PARKED,\n    26\t    StepContext,\n    27\t    StepResult,\n    28\t    StepSpec,\n    29\t)\n    30\tfrom gauntlet.engine import gitops\n    31\tfrom gauntlet.engine.pipeline import Step\n    32\t\n    33\t_CONFIG_TOKEN_RE = re.compile(r\"\\{\\{\\s*config\\.([a-zA-Z0-9_]+)\\s*\\}\\}\")\n    34\t_ANY_TOKEN_RE = re.compile(r\"\\{\\{.*?\\}\\}\")\n    35\t\n    36\t\n    37\t# --- shell -------------------------------------------------------------------\n    38\tdef render_shell_command(template: str, config) -> str:\n    39\t    \"\"\"Substitute only ``{{config.<key>}}`` tokens; reject anything else.\n    40\t\n    41\t    Refusing non-config tokens is the engine-side enforcement of the trust\n    42\t    model: no agent-authored artifact may be interpolated into a shell command.\n    43\t    \"\"\"\n    44\t    def _sub(m: re.Match[str]) -> str:\n    45\t        key = m.group(1)\n    46\t        value = getattr(config, key, None)\n    47\t        if value is None:\n    48\t            raise ValueError(\n    49\t                f\"shell template references unknown config key {key!r}\"\n    50\t            )\n    51\t        return str(value)\n    52\t\n    53\t    rendered = _CONFIG_TOKEN_RE.sub(_sub, template)\n    54\t    leftover = _ANY_TOKEN_RE.search(rendered)\n    55\t    if leftover:\n    56\t        raise ValueError(\n    57\t            f\"shell command may only reference {{{{config.*}}}}; refusing \"\n    58\t            f\"to substitute {leftover.group(0)!r} (trust model / review F-001)\"\n    59\t        )\n    60\t    return rendered\n    61\t\n    62\t\n    63\tdef handle_shell(step: Step, ctx: StepContext) -> StepResult:\n    64\t    template = step.get(\"run\")\n    65\t    if not template:\n    66\t        return StepResult(status=FAILED, notes=\"shell step has no `run:` command\")\n    67\t    command = render_shell_command(template, ctx.config)\n    68\t    timeout = step.timeout_s  # per-step guard (FR-3.3); None => unbounded\n    69\t    try:\n    70\t        proc = subprocess.run(\n    71\t            command,\n    72\t            shell=True,\n    73\t            cwd=ctx.repo_root,\n    74\t            capture_output=True,\n    75\t            text=True,\n    76\t            timeout=timeout,\n    77\t        )\n    78\t    except subprocess.TimeoutExpired as exc:\n    79\t        _write_step_log(ctx, \"output.txt\", f\"$ {command}\\n--- TIMEOUT after {timeout}s ---\\n\")\n    80\t        # Halt at a checkpoint rather than letting a stuck command burn on.\n    81\t        return StepResult(\n    82\t            status=HALTED,\n    83\t            notes=f\"shell timeout halt (FR-3.3): `{command}` exceeded {timeout}s\",\n    84\t        )\n    85\t    _write_step_log(ctx, \"output.txt\", _proc_log(command, proc))\n    86\t    if proc.returncode != 0:\n    87\t        return StepResult(\n    88\t            status=FAILED,\n    89\t            notes=f\"`{command}` exited {proc.returncode}\",\n    90\t        )\n    91\t    return StepResult(status=DONE, notes=f\"`{command}` exited 0\")\n    92\t\n    93\t\n    94\t# --- human_gate --------------------------------------------------------------\n    95\tdef handle_human_gate(step: Step, ctx: StepContext) -> StepResult:\n    96\t    show = step.get(\"show\", []) or []\n    97\t    return StepResult(\n    98\t        status=PARKED,\n    99\t        notes=f\"awaiting human decision; review: {', '.join(show) or '(nothing listed)'}\",\n   100\t    )\n   101\t\n   102\t\n   103\t# --- agent_task --------------------------------------------------------------\n   104\tdef handle_agent_task(step: Step, ctx: StepContext) -> StepResult:\n   105\t    agent_name = step.agent\n   106\t    if not agent_name:\n   107\t        return StepResult(status=FAILED, notes=\"agent_task step has no `agent:`\")\n   108\t    adapter = ctx.build_adapter(agent_name)\n   109\t    prompt = _render_prompt(step, ctx)\n   110\t    schema = _load_schema(step, ctx)\n   111\t    # Per-step timeout overrides the profile's step_timeout_s, which overrides\n   112\t    # the adapter default (FR-3.3). A timeout raises AgentTimeoutError, which the\n   113\t    # orchestrator turns into a HALTED checkpoint.\n   114\t    timeout = step.timeout_s\n   115\t    if timeout is None and agent_name in ctx.config.agents:\n   116\t        timeout = ctx.config.profile(agent_name).step_timeout_s\n   117\t    if timeout is not None and hasattr(adapter, \"timeout_s\"):\n   118\t        adapter.timeout_s = timeout\n   119\t    result = adapter.run(\n   120\t        prompt,\n   121\t        session=ctx.record.session_id,\n   122\t        schema=schema,\n   123\t        cwd=ctx.repo_root,\n   124\t    )\n   125\t    _write_step_log(ctx, \"prompt.md\", prompt)\n   126\t    artifact_writes: dict[str, Path] = {}\n   127\t    output = step.get(\"output\")\n   128\t    if output:\n   129\t        out_path = ctx.artifact_root / output\n   130\t        ctx.writer.write_text(out_path, result.text)\n   131\t        artifact_writes[output] = out_path\n   132\t    return StepResult(\n   133\t        status=DONE,\n   134\t        session_id=result.session_id,\n   135\t        usage=result.usage,\n   136\t        artifact_writes=artifact_writes,\n   137\t        notes=f\"agent {agent_name!r} completed\",\n   138\t    )\n   139\t\n   140\t\n   141\tdef _render_prompt(step: Step, ctx: StepContext) -> str:\n   142\t    template_ref = step.get(\"prompt\")\n   143\t    if template_ref:\n   144\t        template_path = ctx.repo_root / template_ref\n   145\t        base = template_path.read_text()\n   146\t    else:\n   147\t        base = step.get(\"prompt_text\", \"\") or \"\"\n   148\t    parts = [base]\n   149\t    for name in step.get(\"inputs\", []) or []:\n   150\t        path = ctx.artifacts.get(name) or (ctx.artifact_root / name)\n   151\t        content = Path(path).read_text() if Path(path).exists() else \"\"\n   152\t        parts.append(f\"\\n\\n--- input artifact: {name} ---\\n{content}\")\n   153\t    if ctx.iteration_item is not None:\n   154\t        parts.append(f\"\\n\\n--- foreach item [{ctx.iteration_index}] ---\\n{ctx.iteration_item}\")\n   155\t    return \"\".join(parts)\n   156\t\n   157\t\n   158\tdef _load_schema(step: Step, ctx: StepContext) -> dict | None:\n   159\t    ref = step.get(\"findings_schema\") or step.get(\"schema\")\n   160\t    if not ref:\n   161\t        return None\n   162\t    import json\n   163\t\n   164\t    return json.loads((ctx.repo_root / ref).read_text())\n   165\t\n   166\t\n   167\t# --- commit (FR-9.2/9.7) -----------------------------------------------------\n   168\tdef handle_commit(step: Step, ctx: StepContext) -> StepResult:\n   169\t    repo = ctx.repo_root\n   170\t    # Narrow exclusion (review F-001): commit real artifacts (plan.md, outputs);\n   171\t    # keep only the engine's own bookkeeping out of the commit and the checks.\n   172\t    exclude = ctx.excludes\n   173\t    message, draft_usage, draft_session = _commit_message(step, ctx)\n   174\t    err = validate_commit_message(message)\n   175\t    if err is not None:\n   176\t        # message_agent drafting includes a bounded redraft loop in _draft;\n   177\t        # a literal/exhausted message that still fails is a hard error.\n   178\t        return StepResult(\n   179\t            status=FAILED,\n   180\t            usage=draft_usage,\n   181\t            session_id=draft_session,\n   182\t            notes=f\"commit message invalid: {err.reason}\",\n   183\t        )\n   184\t    prefix = header_prefix(message)\n   185\t\n   186\t    # Mid-commit resume reconciliation (review F-003): if a prior attempt\n   187\t    # already created the commit (HEAD moved off the recorded base) but died\n   188\t    # before recording the SHA, adopt that commit rather than double-committing.\n   189\t    base = ctx.record.base_sha\n   190\t    if base and gitops.head_sha(repo) != base and gitops.is_clean(repo, exclude=exclude):\n   191\t        existing = gitops.head_sha(repo)\n   192\t        if header_prefix(gitops.commit_message(repo, existing)) == prefix:\n   193\t            return StepResult(\n   194\t                status=DONE,\n   195\t                commit_sha=existing,\n   196\t                commit_phase=prefix,\n   197\t                usage=draft_usage,\n   198\t                session_id=draft_session,\n   199\t                notes=\"reconciled pre-existing commit after mid-commit interruption\",\n   200\t            )\n   201\t\n   202\t    if gitops.is_clean(repo, exclude=exclude):\n   203\t        return StepResult(\n   204\t            status=FAILED,\n   205\t            usage=draft_usage,\n   206\t            session_id=draft_session,\n   207\t            notes=\"commit step found a clean worktree with nothing to commit\",\n   208\t        )\n   209\t\n   210\t    agent_name = step.agent or step.get(\"message_agent\") or \"builder\"\n   211\t    identity = ctx.config.identity(agent_name)\n   212\t    sha = gitops.commit_all(repo, message, identity=identity, exclude=exclude)\n   213\t    return StepResult(\n   214\t        status=DONE, commit_sha=sha, commit_phase=prefix,\n   215\t        usage=draft_usage, session_id=draft_session, notes=f\"committed {sha[:10]}\",\n   216\t    )\n   217\t\n   218\t\n   219\tdef _commit_message(step: Step, ctx: StepContext):\n   220\t    \"\"\"Return ``(message, usage, session_id)``; usage/session are None for a\n   221\t    literal message (no model call).\"\"\"\n   222\t    literal = step.get(\"message\")\n   223\t    if literal:\n   224\t        return literal, None, None  # human-authored YAML; still format-validated\n   225\t    return _draft_commit_message(step, ctx)\n   226\t\n   227\t\n   228\tdef _draft_commit_message(step: Step, ctx: StepContext):\n   229\t    \"\"\"Draft a commit message via the message_agent with bounded redraft.\n   230\t\n   231\t    The agent sees the change as data — both the tracked diff AND the untracked\n   232\t    files `git add -A` will sweep in (review F-008: a new-file phase otherwise\n   233\t    drafts from an empty diff) — plus an optional plan section. The engine\n   234\t    validates the format and asks for a redraft on violation (FR-9.2). Returns\n   235\t    ``(message, usage, session_id)`` so the commit step records the drafter's\n   236\t    cost (FR-3.2/§7).\n   237\t    \"\"\"\n   238\t    agent_name = step.get(\"message_agent\")\n   239\t    if not agent_name:\n   240\t        raise ValueError(\"commit step needs either `message:` or `message_agent:`\")\n   241\t    adapter = ctx.build_adapter(agent_name)\n   242\t    change = _change_context(ctx)\n   243\t    base_prompt = (\n   244\t        (ctx.repo_root / step.get(\"prompt\")).read_text()\n   245\t        if step.get(\"prompt\")\n   246\t        else _DEFAULT_COMMIT_PROMPT\n   247\t    )\n   248\t    phase_hint = step.get(\"phase\", \"\")\n   249\t    plan_section = _plan_section(step, ctx)\n   250\t    header = (\n   251\t        f\"{base_prompt}\\n\\nRequired header phase prefix: {phase_hint or '(infer PN)'}\\n\"\n   252\t        f\"{plan_section}\"\n   253\t    )\n   254\t    prompt = f\"{header}\\n{change}\\n\"\n   255\t    max_redrafts = int(step.get(\"max_redrafts\", 2))\n   256\t    message = \"\"\n   257\t    usage = None\n   258\t    session_id = None\n   259\t    for _attempt in range(1 + max_redrafts):\n   260\t        result = adapter.run(prompt, cwd=ctx.repo_root)\n   261\t        usage = result.usage  # accumulated per call by the orchestrator's totals\n   262\t        session_id = result.session_id\n   263\t        message = result.text.strip()\n   264\t        if validate_commit_message(message) is None:\n   265\t            return message, usage, session_id\n   266\t        prompt = (\n   267\t            f\"{header}\\n\\nYour previous draft was rejected: \"\n   268\t            f\"{validate_commit_message(message).reason}. \"\n   269\t            f\"Return only the corrected commit message.\\n{change}\\n\"\n   270\t        )\n   271\t    return message, usage, session_id\n   272\t\n   273\t\n   274\tdef _change_context(ctx: StepContext) -> str:\n   275\t    \"\"\"The diff vs HEAD plus the untracked files staging will add (F-008).\"\"\"\n   276\t    repo = ctx.repo_root\n   277\t    diff = gitops.diff_head(repo, exclude=ctx.excludes)\n   278\t    status = gitops.status_porcelain(repo, exclude=ctx.excludes)\n   279\t    return (\n   280\t        f\"--- git status (incl. untracked) ---\\n{status}\\n\"\n   281\t        f\"\\n--- diff (tracked, vs HEAD) ---\\n{diff}\"\n   282\t    )\n   283\t\n   284\t\n   285\tdef _plan_section(step: Step, ctx: StepContext) -> str:\n   286\t    \"\"\"Optional plan excerpt the message_agent drafts from (FR-9.2).\"\"\"\n   287\t    ref = step.get(\"plan_section\")\n   288\t    if not ref:\n   289\t        return \"\"\n   290\t    path = ctx.artifacts.get(ref) or (ctx.artifact_root / ref)\n   291\t    if Path(path).exists():\n   292\t        return f\"\\n--- plan section: {ref} ---\\n{Path(path).read_text()}\\n\"\n   293\t    return \"\"\n   294\t\n   295\t\n   296\t_DEFAULT_COMMIT_PROMPT = (\n   297\t    \"Draft a git commit message for the staged changes. Line 1: an imperative \"\n   298\t    \"header prefixed with the phase, e.g. 'P3: <summary>', at most 72 chars. \"\n   299\t    \"Then a blank line, then a body explaining what changed and why, the plan \"\n   300\t    \"assumption validated, and relevant FR references.\"\n   301\t)\n   302\t\n   303\t\n   304\t# --- helpers -----------------------------------------------------------------\n   305\tdef _proc_log(command: str, proc: subprocess.CompletedProcess) -> str:\n   306\t    return (\n   307\t        f\"$ {command}\\n--- exit {proc.returncode} ---\\n\"\n   308\t        f\"--- stdout ---\\n{proc.stdout}\\n--- stderr ---\\n{proc.stderr}\\n\"\n   309\t    )\n   310\t\n   311\t\n   312\tdef _write_step_log(ctx: StepContext, name: str, text: str) -> None:\n   313\t    iteration = ctx.record.iteration\n   314\t    leaf = ctx.record.id if iteration is None else f\"{ctx.record.id}.{iteration}\"\n   315\t    ctx.writer.write_text(ctx.steps_dir() / leaf / name, text)\n   316\t\n   317\t\n   318\tSPECS: dict[str, StepSpec] = {\n   319\t    \"agent_task\": StepSpec(\n   320\t        type=\"agent_task\",\n   321\t        handler=handle_agent_task,\n   322\t        needs_agent=True,\n   323\t        # repo_write / touches_worktree are decided per-step (default True)\n   324\t    ),\n   325\t    \"shell\": StepSpec(\n   326\t        type=\"shell\",\n   327\t        handler=handle_shell,\n   328\t        touches_worktree=True,  # a test/build step can mutate the tree\n   329\t    ),\n   330\t    \"human_gate\": StepSpec(\n   331\t        type=\"human_gate\",\n   332\t        handler=handle_human_gate,\n   333\t    ),\n   334\t    \"commit\": StepSpec(\n   335\t        type=\"commit\",\n   336\t        handler=handle_commit,\n   337\t        touches_worktree=True,\n   338\t    ),\n   339\t}\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_7","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/gitops.py | sed -n '1,260p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_7","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/gitops.py | sed -n '1,260p'\"","aggregated_output":"     1\t\"\"\"Thin git wrapper for the engine (FR-9, FR-8 transaction boundary).\n     2\t\n     3\tThe engine executes only human-committed configuration and git operations on\n     4\tits own behalf — it never substitutes agent-authored text into a command line\n     5\t(plan §0 trust model / review F-001). These helpers shell out to ``git`` with\n     6\texplicit, fixed argv; the only model-derived value that reaches git is the\n     7\tcommit *message*, which is passed via a file/`-F`-style stdin path and is\n     8\ttreated as data (format-validated before it is used — see ``commit_format``).\n     9\t\"\"\"\n    10\t\n    11\tfrom __future__ import annotations\n    12\t\n    13\timport subprocess\n    14\tfrom dataclasses import dataclass\n    15\tfrom pathlib import Path\n    16\t\n    17\t\n    18\tclass GitError(RuntimeError):\n    19\t    \"\"\"A git invocation failed. Carries argv + stderr for the manifest/log.\"\"\"\n    20\t\n    21\t    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:\n    22\t        super().__init__(\n    23\t            f\"git {' '.join(argv)} failed (exit {returncode}): {stderr.strip()}\"\n    24\t        )\n    25\t        self.argv = argv\n    26\t        self.returncode = returncode\n    27\t        self.stderr = stderr\n    28\t\n    29\t\n    30\tdef _run(repo: Path, *args: str, stdin: str | None = None) -> str:\n    31\t    argv = [\"git\", \"-C\", str(repo), *args]\n    32\t    proc = subprocess.run(\n    33\t        argv,\n    34\t        input=stdin,\n    35\t        capture_output=True,\n    36\t        text=True,\n    37\t    )\n    38\t    if proc.returncode != 0:\n    39\t        raise GitError(list(args), proc.returncode, proc.stderr)\n    40\t    return proc.stdout\n    41\t\n    42\t\n    43\tdef is_git_repo(repo: Path) -> bool:\n    44\t    try:\n    45\t        _run(repo, \"rev-parse\", \"--git-dir\")\n    46\t        return True\n    47\t    except GitError:\n    48\t        return False\n    49\t\n    50\t\n    51\tdef head_sha(repo: Path) -> str:\n    52\t    return _run(repo, \"rev-parse\", \"HEAD\").strip()\n    53\t\n    54\t\n    55\tdef rev_parse(repo: Path, ref: str) -> str:\n    56\t    return _run(repo, \"rev-parse\", \"--verify\", ref).strip()\n    57\t\n    58\t\n    59\tdef current_branch(repo: Path) -> str:\n    60\t    return _run(repo, \"rev-parse\", \"--abbrev-ref\", \"HEAD\").strip()\n    61\t\n    62\t\n    63\tdef _exclude_pathspec(exclude: list[str] | None) -> list[str]:\n    64\t    \"\"\"Build a git pathspec that limits an operation to everything but ``exclude``.\n    65\t\n    66\t    The engine passes its own run root here: that subtree is bookkeeping\n    67\t    (manifests, run pointer, transcripts), never part of the work tree, so it\n    68\t    must be invisible to status/add — otherwise it reads as perpetual \"dirt\",\n    69\t    pollutes phase commits, and confuses the base-SHA transaction boundary.\n    70\t    \"\"\"\n    71\t    if not exclude:\n    72\t        return []\n    73\t    spec = [\"--\", \".\"]\n    74\t    for e in exclude:\n    75\t        spec.append(f\":(exclude){e}\")\n    76\t        spec.append(f\":(exclude){e}/**\")\n    77\t    return spec\n    78\t\n    79\t\n    80\tdef status_porcelain(repo: Path, *, exclude: list[str] | None = None) -> str:\n    81\t    \"\"\"Porcelain status; empty string means a clean worktree.\"\"\"\n    82\t    return _run(repo, \"status\", \"--porcelain\", *_exclude_pathspec(exclude)).strip()\n    83\t\n    84\t\n    85\tdef is_clean(repo: Path, *, exclude: list[str] | None = None) -> bool:\n    86\t    return status_porcelain(repo, exclude=exclude) == \"\"\n    87\t\n    88\t\n    89\tdef is_dirty_vs(repo: Path, base_sha: str, *, exclude: list[str] | None = None) -> bool:\n    90\t    \"\"\"True if the worktree (tracked + staged + untracked) differs from ``base_sha``.\n    91\t\n    92\t    The engine's transaction boundary (review F-003) records a step's base SHA\n    93\t    before any worktree-touching step. On resume it compares against that base:\n    94\t    a difference means the killed step left partial edits.\n    95\t    \"\"\"\n    96\t    if status_porcelain(repo, exclude=exclude) != \"\":\n    97\t        return True\n    98\t    # No working-tree changes; confirm HEAD still points at the recorded base.\n    99\t    return head_sha(repo) != base_sha\n   100\t\n   101\t\n   102\tdef branch_exists(repo: Path, branch: str) -> bool:\n   103\t    try:\n   104\t        _run(repo, \"rev-parse\", \"--verify\", f\"refs/heads/{branch}\")\n   105\t        return True\n   106\t    except GitError:\n   107\t        return False\n   108\t\n   109\t\n   110\tdef checkout_or_create_branch(repo: Path, branch: str, base: str) -> None:\n   111\t    \"\"\"Check out ``branch``, creating it off ``base`` if it does not exist (FR-9.1).\"\"\"\n   112\t    if branch_exists(repo, branch):\n   113\t        _run(repo, \"checkout\", branch)\n   114\t    else:\n   115\t        _run(repo, \"checkout\", \"-b\", branch, base)\n   116\t\n   117\t\n   118\t@dataclass(frozen=True)\n   119\tclass Identity:\n   120\t    name: str\n   121\t    email: str\n   122\t\n   123\t\n   124\tdef commit_all(\n   125\t    repo: Path,\n   126\t    message: str,\n   127\t    *,\n   128\t    identity: Identity,\n   129\t    allow_empty: bool = False,\n   130\t    exclude: list[str] | None = None,\n   131\t) -> str:\n   132\t    \"\"\"Stage everything and commit with an explicit author/committer identity.\n   133\t\n   134\t    The message is passed on stdin (`-F -`) so no agent-authored text ever\n   135\t    lands on the argv. ``exclude`` (the run root) is kept out of the commit so\n   136\t    phase commits carry the work, not engine bookkeeping. Returns the SHA.\n   137\t    \"\"\"\n   138\t    _run(repo, \"add\", \"-A\", *_exclude_pathspec(exclude))\n   139\t    args = [\n   140\t        \"-c\",\n   141\t        f\"user.name={identity.name}\",\n   142\t        \"-c\",\n   143\t        f\"user.email={identity.email}\",\n   144\t        \"commit\",\n   145\t        \"-F\",\n   146\t        \"-\",\n   147\t    ]\n   148\t    if allow_empty:\n   149\t        args.append(\"--allow-empty\")\n   150\t    _run(repo, *args, stdin=message)\n   151\t    return head_sha(repo)\n   152\t\n   153\t\n   154\tdef commit_subject(repo: Path, sha: str) -> str:\n   155\t    return _run(repo, \"log\", \"-1\", \"--format=%s\", sha).strip()\n   156\t\n   157\t\n   158\tdef commit_message(repo: Path, sha: str) -> str:\n   159\t    return _run(repo, \"log\", \"-1\", \"--format=%B\", sha).rstrip(\"\\n\")\n   160\t\n   161\t\n   162\tdef range_diff(repo: Path, base: str, head: str) -> str:\n   163\t    \"\"\"Diff for the confirm pass / review handoff (`base..head`).\"\"\"\n   164\t    return _run(repo, \"diff\", f\"{base}..{head}\")\n   165\t\n   166\t\n   167\tdef diff_head(repo: Path, *, exclude: list[str] | None = None) -> str:\n   168\t    \"\"\"Working-tree diff vs HEAD (the change a commit step is about to record).\"\"\"\n   169\t    return _run(repo, \"diff\", \"HEAD\", *_exclude_pathspec(exclude))\n   170\t\n   171\t\n   172\tdef is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:\n   173\t    try:\n   174\t        subprocess.run(\n   175\t            [\"git\", \"-C\", str(repo), \"merge-base\", \"--is-ancestor\", ancestor, descendant],\n   176\t            capture_output=True,\n   177\t            text=True,\n   178\t            check=True,\n   179\t        )\n   180\t        return True\n   181\t    except subprocess.CalledProcessError:\n   182\t        return False\n   183\t\n   184\t\n   185\tdef create_ref(repo: Path, ref: str, sha: str) -> None:\n   186\t    \"\"\"Create/update an arbitrary ref (used for rollback backup refs, F-010).\"\"\"\n   187\t    _run(repo, \"update-ref\", ref, sha)\n   188\t\n   189\t\n   190\tdef reset_hard(repo: Path, sha: str) -> None:\n   191\t    _run(repo, \"reset\", \"--hard\", sha)\n   192\t\n   193\t\n   194\tdef clean_untracked(repo: Path, *, exclude: list[str] | None = None) -> None:\n   195\t    \"\"\"Remove untracked files/dirs, preserving *ignored* paths (no ``-x``).\n   196\t\n   197\t    Used after a ``reset_to_base`` rewind so a killed step's untracked partial\n   198\t    files are discarded too (``reset --hard`` alone leaves them). ``exclude``\n   199\t    paths are spared — the engine passes its own run root so a reset never wipes\n   200\t    the run pointer / manifests / authored prd.md living under it.\n   201\t    \"\"\"\n   202\t    args = [\"clean\", \"-fd\"]\n   203\t    for pattern in exclude or []:\n   204\t        args += [\"-e\", pattern]\n   205\t    _run(repo, *args)\n   206\t\n   207\t\n   208\tdef backup_dirty_worktree(\n   209\t    repo: Path, ref: str, message: str, *, exclude: list[str] | None = None\n   210\t) -> str:\n   211\t    \"\"\"Snapshot the full dirty worktree (tracked + untracked) to a backup ref.\n   212\t\n   213\t    Captures partial work that ``reset --hard`` would otherwise destroy\n   214\t    (review F-003 / F-010 safety). ``exclude`` (the run root) is left out so the\n   215\t    snapshot — and the subsequent reset — never touch the run bookkeeping.\n   216\t    Returns the backup commit SHA.\n   217\t    \"\"\"\n   218\t    _run(repo, \"add\", \"-A\", *_exclude_pathspec(exclude))\n   219\t    tree = _run(repo, \"write-tree\").strip()\n   220\t    parent = head_sha(repo)\n   221\t    backup = _run(repo, \"commit-tree\", tree, \"-p\", parent, \"-m\", message).strip()\n   222\t    _run(repo, \"update-ref\", ref, backup)\n   223\t    return backup\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_8","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/pipeline.py | sed -n '1,240p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_8","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/pipeline.py | sed -n '1,240p'\"","aggregated_output":"     1\t\"\"\"Pipeline model + loader (FR-5).\n     2\t\n     3\tA pipeline is a YAML document: ``name``, ``version``, and ordered ``stages``,\n     4\teach with ordered ``steps``. Steps carry first-class ``when:``/``foreach:``/\n     5\t``on_fail:`` attributes and per-step overrides (FR-5.4). Unknown keys are\n     6\tpreserved (``extra=\"allow\"``) so custom step types (FR-5.5) and type-specific\n     7\tfields need no model change. Versioning is ``version:`` + a content hash of the\n     8\texact bytes loaded (FR-5.6), both recorded in the manifest.\n     9\t\"\"\"\n    10\t\n    11\tfrom __future__ import annotations\n    12\t\n    13\timport hashlib\n    14\tfrom pathlib import Path\n    15\tfrom typing import Any\n    16\t\n    17\timport yaml\n    18\tfrom pydantic import BaseModel, ConfigDict, Field\n    19\t\n    20\t\n    21\tclass OnFail(BaseModel):\n    22\t    \"\"\"Failure routing for a step (FR-5.4).\"\"\"\n    23\t\n    24\t    route_to: str\n    25\t    max_retries: int = 0\n    26\t\n    27\t\n    28\tclass Step(BaseModel):\n    29\t    model_config = ConfigDict(extra=\"allow\")\n    30\t\n    31\t    id: str\n    32\t    type: str\n    33\t\n    34\t    # first-class control attributes (FR-5.4)\n    35\t    agent: str | None = None\n    36\t    when: str | None = None\n    37\t    foreach: str | None = None\n    38\t    on_fail: OnFail | None = None\n    39\t\n    40\t    # per-step overrides (FR-5.4) — engine-level budget guards (FR-3.3)\n    41\t    max_turns: int | None = None\n    42\t    budget_usd: float | None = None\n    43\t    timeout_s: float | None = None\n    44\t\n    45\t    def get(self, key: str, default: Any = None) -> Any:\n    46\t        \"\"\"Read a type-specific field — declared or extra.\n    47\t\n    48\t        Deliberately consults only model fields and ``extra``; using ``hasattr``\n    49\t        would collide with pydantic's own methods (``schema``, ``dict``, ``copy``)\n    50\t        and return a bound method for, e.g., a step's ``schema:`` key.\n    51\t        \"\"\"\n    52\t        if key in type(self).model_fields:\n    53\t            return getattr(self, key)\n    54\t        extra = self.__pydantic_extra__ or {}\n    55\t        return extra.get(key, default)\n    56\t\n    57\t\n    58\tclass Stage(BaseModel):\n    59\t    model_config = ConfigDict(extra=\"allow\")\n    60\t\n    61\t    id: str\n    62\t    foreach: str | None = None\n    63\t    when: str | None = None\n    64\t    steps: list[Step]\n    65\t\n    66\t\n    67\tclass Pipeline(BaseModel):\n    68\t    model_config = ConfigDict(extra=\"allow\")\n    69\t\n    70\t    name: str\n    71\t    version: int\n    72\t    stages: list[Stage]\n    73\t\n    74\t    def all_steps(self) -> list[Step]:\n    75\t        return [step for stage in self.stages for step in stage.steps]\n    76\t\n    77\t\n    78\tdef content_hash(text: str) -> str:\n    79\t    \"\"\"Stable content hash of the pipeline source (FR-5.6).\"\"\"\n    80\t    return \"sha256:\" + hashlib.sha256(text.encode(\"utf-8\")).hexdigest()\n    81\t\n    82\t\n    83\tdef load_pipeline(path: Path) -> tuple[Pipeline, str]:\n    84\t    \"\"\"Load and parse a pipeline file; return ``(pipeline, content_hash)``.\n    85\t\n    86\t    Parsing only — semantic load-time validation (dangling artifacts, adapter\n    87\t    capabilities, banned flags) lives in :mod:`gauntlet.engine.validate` so the\n    88\t    model stays free of cross-module imports.\n    89\t    \"\"\"\n    90\t    if not path.exists():\n    91\t        raise FileNotFoundError(f\"pipeline not found at {path}\")\n    92\t    text = path.read_text()\n    93\t    data = yaml.safe_load(text)\n    94\t    if not isinstance(data, dict):\n    95\t        raise ValueError(f\"{path} must be a YAML mapping, got {type(data).__name__}\")\n    96\t    pipeline = Pipeline.model_validate(data)\n    97\t    _assert_unique_ids(pipeline)\n    98\t    return pipeline, content_hash(text)\n    99\t\n   100\t\n   101\tdef _assert_unique_ids(pipeline: Pipeline) -> None:\n   102\t    seen: set[str] = set()\n   103\t    for step in pipeline.all_steps():\n   104\t        if step.id in seen:\n   105\t            raise ValueError(f\"duplicate step id {step.id!r} in pipeline\")\n   106\t        seen.add(step.id)\n   107\t    stage_ids: set[str] = set()\n   108\t    for stage in pipeline.stages:\n   109\t        if stage.id in stage_ids:\n   110\t            raise ValueError(f\"duplicate stage id {stage.id!r} in pipeline\")\n   111\t        stage_ids.add(stage.id)\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_9","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/validate.py | sed -n '1,220p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.started","item":{"id":"item_10","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/manifest.py | sed -n '1,280p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_10","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/manifest.py | sed -n '1,280p'\"","aggregated_output":"     1\t\"\"\"Run manifest: the state-machine checkpoint (§7, FR-8.2, review F-003).\n     2\t\n     3\tThe manifest is written **write-ahead** — before and after every step — and\n     4\tatomically (write-temp + ``os.replace``), so a ``kill -9`` at any instant\n     5\tleaves either the prior or the next consistent state on disk, never a torn\n     6\tfile. The side-effect transaction boundary (review F-003) lives here too: each\n     7\tworktree-touching step records its **base SHA** before running, so resume can\n     8\ttell a clean re-entry from a step that died mid-edit.\n     9\t\"\"\"\n    10\t\n    11\tfrom __future__ import annotations\n    12\t\n    13\timport json\n    14\timport os\n    15\timport tempfile\n    16\tfrom pathlib import Path\n    17\tfrom typing import Any\n    18\t\n    19\tfrom pydantic import BaseModel, Field\n    20\t\n    21\t# --- step lifecycle states ---------------------------------------------------\n    22\tPENDING = \"pending\"\n    23\tRUNNING = \"running\"\n    24\tDONE = \"done\"\n    25\tFAILED = \"failed\"\n    26\tINTERRUPTED = \"interrupted\"  # killed mid-step, dirty worktree (F-003)\n    27\tPARKED = \"parked\"  # human_gate / interrupted-park awaiting a human\n    28\tHALTED = \"halted\"  # budget/timeout guard tripped (FR-3.3)\n    29\tSKIPPED = \"skipped\"  # `when:` false\n    30\t\n    31\t# --- run lifecycle states ----------------------------------------------------\n    32\tRUN_RUNNING = \"running\"\n    33\tRUN_PARKED = \"parked\"\n    34\tRUN_DONE = \"done\"\n    35\tRUN_ABORTED = \"aborted\"\n    36\tRUN_FAILED = \"failed\"\n    37\t\n    38\t\n    39\tclass PipelineRef(BaseModel):\n    40\t    name: str\n    41\t    version: int\n    42\t    hash: str\n    43\t\n    44\t\n    45\tclass UsageTotals(BaseModel):\n    46\t    input_tokens: int = 0\n    47\t    output_tokens: int = 0\n    48\t    cached_input_tokens: int = 0\n    49\t    cost_usd: float | None = None  # None until at least one priced call (§12 Q3)\n    50\t\n    51\t    def add(self, usage: Any | None) -> None:\n    52\t        if usage is None:\n    53\t            return\n    54\t        self.input_tokens += usage.input_tokens or 0\n    55\t        self.output_tokens += usage.output_tokens or 0\n    56\t        self.cached_input_tokens += usage.cached_input_tokens or 0\n    57\t        if usage.cost_usd is not None:\n    58\t            self.cost_usd = (self.cost_usd or 0.0) + usage.cost_usd\n    59\t\n    60\t\n    61\tclass StepRecord(BaseModel):\n    62\t    id: str\n    63\t    type: str\n    64\t    status: str = PENDING\n    65\t    agent: str | None = None\n    66\t    session_id: str | None = None\n    67\t    started: str | None = None\n    68\t    ended: str | None = None\n    69\t    attempts: int = 0\n    70\t    # Transaction boundary (F-003): HEAD before the step touched the worktree.\n    71\t    base_sha: str | None = None\n    72\t    usage: UsageTotals = Field(default_factory=UsageTotals)\n    73\t    notes: str | None = None\n    74\t    # foreach binding key, when this record is one iteration of a fan-out step\n    75\t    iteration: str | None = None\n    76\t\n    77\t\n    78\tclass CommitRecord(BaseModel):\n    79\t    step_id: str\n    80\t    phase: str  # the PN[.x] prefix\n    81\t    sha: str\n    82\t\n    83\t\n    84\tclass Manifest(BaseModel):\n    85\t    \"\"\"The persisted run state (§7).\"\"\"\n    86\t\n    87\t    run_id: str\n    88\t    slug: str\n    89\t    branch: str\n    90\t    base_branch: str\n    91\t    pipeline: PipelineRef\n    92\t    prompt_hashes: dict[str, str] = Field(default_factory=dict)\n    93\t    status: str = RUN_RUNNING\n    94\t    current_step: str | None = None\n    95\t    steps: list[StepRecord] = Field(default_factory=list)\n    96\t    commits: list[CommitRecord] = Field(default_factory=list)\n    97\t    totals: UsageTotals = Field(default_factory=UsageTotals)\n    98\t\n    99\t    # ---- record lookup -------------------------------------------------------\n   100\t    def record(self, step_id: str, iteration: str | None = None) -> StepRecord | None:\n   101\t        for rec in self.steps:\n   102\t            if rec.id == step_id and rec.iteration == iteration:\n   103\t                return rec\n   104\t        return None\n   105\t\n   106\t    def upsert(self, rec: StepRecord) -> StepRecord:\n   107\t        for i, existing in enumerate(self.steps):\n   108\t            if existing.id == rec.id and existing.iteration == rec.iteration:\n   109\t                self.steps[i] = rec\n   110\t                return rec\n   111\t        self.steps.append(rec)\n   112\t        return rec\n   113\t\n   114\t    # ---- atomic persistence (FR-8.2) ----------------------------------------\n   115\t    def write_atomic(self, path: Path) -> None:\n   116\t        \"\"\"Write the manifest atomically: temp file in the same dir + replace.\n   117\t\n   118\t        ``os.replace`` is atomic on POSIX within a filesystem, so a reader (or a\n   119\t        resume after kill) always sees a whole manifest — the prior one until\n   120\t        the instant the new one lands. ``fsync`` before replace so the bytes are\n   121\t        durable, not just in the page cache, before the rename is visible.\n   122\t        \"\"\"\n   123\t        path.parent.mkdir(parents=True, exist_ok=True)\n   124\t        payload = self.model_dump_json(indent=2)\n   125\t        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=\".manifest-\", suffix=\".tmp\")\n   126\t        try:\n   127\t            with os.fdopen(fd, \"w\") as fh:\n   128\t                fh.write(payload)\n   129\t                fh.flush()\n   130\t                os.fsync(fh.fileno())\n   131\t            os.replace(tmp, path)\n   132\t        except BaseException:\n   133\t            # On any failure (including KeyboardInterrupt) leave the prior\n   134\t            # manifest untouched and clean up the temp file.\n   135\t            try:\n   136\t                os.unlink(tmp)\n   137\t            except FileNotFoundError:\n   138\t                pass\n   139\t            raise\n   140\t\n   141\t    @classmethod\n   142\t    def load(cls, path: Path) -> Manifest:\n   143\t        return cls.model_validate_json(path.read_text())\n","exit_code":0,"status":"completed"}}
+{"type":"item.completed","item":{"id":"item_9","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/validate.py | sed -n '1,220p'\"","aggregated_output":"     1\t\"\"\"Load-time pipeline validation (FR-5.3, FR-2.3, PRD §8).\n     2\t\n     3\tRun once when a pipeline is loaded for a run. Three classes of check:\n     4\t\n     5\t* **Dangling artifact dataflow** (FR-5.3): every ``inputs:``/``artifact:``\n     6\t  reference must be a seed artifact (``prd.md``/``plan.md``) or produced by an\n     7\t  earlier step's ``output:``.\n     8\t* **Adapter capabilities** (FR-2.3): a step that writes the repo cannot bind an\n     9\t  adapter that can't (e.g. ``api``); a step that needs a schema *warns* if its\n    10\t  adapter only does best-effort JSON, and *errors* if it does none.\n    11\t* **Banned flags** (§8): constructing each referenced adapter runs the\n    12\t  permission-bypass / hook-disabling lint; a violation aborts the load.\n    13\t\"\"\"\n    14\t\n    15\tfrom __future__ import annotations\n    16\t\n    17\tfrom dataclasses import dataclass, field\n    18\t\n    19\tfrom gauntlet.config import BannedFlagError\n    20\tfrom gauntlet.engine.config import RunConfig\n    21\tfrom gauntlet.engine.execution import get_spec\n    22\tfrom gauntlet.engine.pipeline import Pipeline, Step\n    23\t\n    24\tSEED_ARTIFACTS = frozenset({\"prd.md\", \"plan.md\"})\n    25\t\n    26\t\n    27\tclass PipelineValidationError(ValueError):\n    28\t    def __init__(self, errors: list[str]) -> None:\n    29\t        super().__init__(\"pipeline failed validation:\\n- \" + \"\\n- \".join(errors))\n    30\t        self.errors = errors\n    31\t\n    32\t\n    33\t@dataclass\n    34\tclass ValidationReport:\n    35\t    errors: list[str] = field(default_factory=list)\n    36\t    warnings: list[str] = field(default_factory=list)\n    37\t\n    38\t    def ok(self) -> bool:\n    39\t        return not self.errors\n    40\t\n    41\t\n    42\tdef validate_pipeline(\n    43\t    pipeline: Pipeline, config: RunConfig, *, seeds: frozenset[str] = SEED_ARTIFACTS\n    44\t) -> ValidationReport:\n    45\t    report = ValidationReport()\n    46\t    available: set[str] = set(seeds)\n    47\t    for stage in pipeline.stages:\n    48\t        stage_ids = {step.id for step in stage.steps}\n    49\t        for step in stage.steps:\n    50\t            _validate_step(step, config, available, report)\n    51\t            # on_fail routing is stage-local: the runtime jumps within the\n    52\t            # current stage's step list, so a cross-stage target would validate\n    53\t            # then crash at runtime (review F-005). Reject it at load.\n    54\t            if step.on_fail and step.on_fail.route_to not in stage_ids:\n    55\t                report.errors.append(\n    56\t                    f\"step {step.id!r} on_fail routes to {step.on_fail.route_to!r}, \"\n    57\t                    \"which is not a step in the same stage (cross-stage routing \"\n    58\t                    \"is unsupported, FR-5.4 / review F-005)\"\n    59\t                )\n    60\t            output = step.get(\"output\")\n    61\t            if output:\n    62\t                available.add(output)\n    63\t    if report.errors:\n    64\t        raise PipelineValidationError(report.errors)\n    65\t    return report\n    66\t\n    67\t\n    68\tdef _validate_step(\n    69\t    step: Step, config: RunConfig, available: set[str], report: ValidationReport\n    70\t) -> None:\n    71\t    # 1. step type resolves\n    72\t    try:\n    73\t        spec = get_spec(step.type)\n    74\t    except KeyError as exc:\n    75\t        report.errors.append(str(exc))\n    76\t        return\n    77\t\n    78\t    # 1b. `max_turns` is unenforceable on the pinned CLIs (claude 2.1.172 has no\n    79\t    # --max-turns; codex exec has no turn cap) — reject it rather than silently\n    80\t    # ignore a claimed guard (review F-006). timeout_s + budget_usd are the\n    81\t    # working per-step halts (FR-3.3).\n    82\t    if step.max_turns is not None:\n    83\t        report.errors.append(\n    84\t            f\"step {step.id!r} sets max_turns, but no installed adapter can honor \"\n    85\t            \"it; use timeout_s / budget_usd (FR-3.3 / review F-006)\"\n    86\t        )\n    87\t\n    88\t    # 2. dangling artifact dataflow (FR-5.3)\n    89\t    for name in (step.get(\"inputs\", []) or []):\n    90\t        if name not in available:\n    91\t            report.errors.append(\n    92\t                f\"step {step.id!r} input {name!r} is not a seed artifact and is \"\n    93\t                \"produced by no earlier step (dangling reference, FR-5.3)\"\n    94\t            )\n    95\t    artifact = step.get(\"artifact\")\n    96\t    if artifact and artifact not in available:\n    97\t        report.errors.append(\n    98\t            f\"step {step.id!r} artifact {artifact!r} is not available at this \"\n    99\t            \"point (dangling reference, FR-5.3)\"\n   100\t        )\n   101\t\n   102\t    # 3. agent profile resolution + capabilities (FR-2.3) + banned flags (§8)\n   103\t    agent_refs = _agent_refs(step, spec.needs_agent)\n   104\t    for ref in agent_refs:\n   105\t        if ref not in config.agents:\n   106\t            report.errors.append(\n   107\t                f\"step {step.id!r} references undefined agent profile {ref!r}\"\n   108\t            )\n   109\t            continue\n   110\t        profile = config.profile(ref)\n   111\t        if profile.max_turns is not None:\n   112\t            report.errors.append(\n   113\t                f\"agent profile {ref!r} sets max_turns, unenforceable on the \"\n   114\t                \"pinned CLIs; use step_timeout_s / budget_usd (review F-006)\"\n   115\t            )\n   116\t        caps = profile.capabilities()\n   117\t        if spec.step_requires_repo_write(step) and not caps.repo_write:\n   118\t            report.errors.append(\n   119\t                f\"step {step.id!r} needs repo-write but agent {ref!r} \"\n   120\t                f\"(adapter {profile.adapter!r}) cannot write the repo (FR-2.3)\"\n   121\t            )\n   122\t        if _step_uses_schema(step, spec):\n   123\t            if caps.structured_output == \"none\":\n   124\t                report.errors.append(\n   125\t                    f\"step {step.id!r} needs structured output but agent {ref!r} \"\n   126\t                    \"supports none (FR-2.3)\"\n   127\t                )\n   128\t            elif caps.structured_output == \"best_effort\":\n   129\t                report.warnings.append(\n   130\t                    f\"step {step.id!r} relies on schema output but agent {ref!r} \"\n   131\t                    \"is best-effort JSON only; validate-and-retry applies (FR-2.3)\"\n   132\t                )\n   133\t        try:\n   134\t            profile.build_adapter()\n   135\t        except BannedFlagError as exc:\n   136\t            report.errors.append(\n   137\t                f\"step {step.id!r} agent {ref!r} uses a banned flag: {exc}\"\n   138\t            )\n   139\t        except Exception as exc:  # construction issues surface as warnings\n   140\t            report.warnings.append(\n   141\t                f\"step {step.id!r} agent {ref!r} could not be pre-constructed \"\n   142\t                f\"({exc}); deferred to runtime\"\n   143\t            )\n   144\t\n   145\t\n   146\tdef _agent_refs(step: Step, needs_agent: bool) -> list[str]:\n   147\t    refs: list[str] = []\n   148\t    if step.agent:\n   149\t        refs.append(step.agent)\n   150\t    for key in (\"message_agent\", \"reviewer\", \"triager\", \"fixer\", \"confirmer\"):\n   151\t        ref = step.get(key)\n   152\t        if ref:\n   153\t            refs.append(ref)\n   154\t    return refs\n   155\t\n   156\t\n   157\tdef _step_uses_schema(step: Step, spec) -> bool:\n   158\t    return bool(spec.uses_schema or step.get(\"findings_schema\") or step.get(\"schema\"))\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_11","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/judgeproc.py | sed -n '1,190p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_11","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/engine/judgeproc.py | sed -n '1,190p'\"","aggregated_output":"     1\t\"\"\"Engine-managed judge lifecycle (FR-7.1, plan P3; supersedes BOOTSTRAP-NOTES #12).\n     2\t\n     3\t``gauntlet run`` starts the localhost judge as a subprocess, injects the per-run\n     4\t``GAUNTLET_JUDGE_*`` env so the agent CLIs' PreToolUse hooks gate against it\n     5\t(live session gating, the dogfood deferred from P2), and stops it on exit. The\n     6\tjudge is launched via ``python -m gauntlet judge serve`` so it does not depend\n     7\ton the console script being on PATH.\n     8\t\"\"\"\n     9\t\n    10\tfrom __future__ import annotations\n    11\t\n    12\timport os\n    13\timport secrets\n    14\timport subprocess\n    15\timport sys\n    16\timport time\n    17\timport urllib.error\n    18\timport urllib.request\n    19\tfrom pathlib import Path\n    20\t\n    21\tfrom gauntlet.judge.hook_client import (\n    22\t    MODE_ENV_VAR,\n    23\t    RUN_ID_ENV_VAR,\n    24\t    STEP_ID_ENV_VAR,\n    25\t    URL_ENV_VAR,\n    26\t)\n    27\tfrom gauntlet.judge.service import TOKEN_ENV_VAR\n    28\t\n    29\tDEFAULT_HOST = \"127.0.0.1\"\n    30\tDEFAULT_PORT = 8787\n    31\t\n    32\t# Every GAUNTLET_* var the run touches — snapshotted at start, restored at stop\n    33\t# so nothing (incl. the per-step GAUNTLET_STEP_ID set by the orchestrator) leaks\n    34\t# into the parent session (review F-009).\n    35\t_MANAGED_ENV_VARS = (\n    36\t    TOKEN_ENV_VAR,\n    37\t    URL_ENV_VAR,\n    38\t    MODE_ENV_VAR,\n    39\t    RUN_ID_ENV_VAR,\n    40\t    STEP_ID_ENV_VAR,\n    41\t)\n    42\t\n    43\t\n    44\tclass ManagedJudge:\n    45\t    def __init__(\n    46\t        self,\n    47\t        *,\n    48\t        policy_path: Path,\n    49\t        audit_path: Path,\n    50\t        run_id: str,\n    51\t        judge_model: str | None = None,\n    52\t        host: str = DEFAULT_HOST,\n    53\t        port: int = DEFAULT_PORT,\n    54\t        mode: str = \"unattended\",\n    55\t        startup_timeout_s: float = 15.0,\n    56\t    ) -> None:\n    57\t        self.policy_path = policy_path\n    58\t        self.audit_path = audit_path\n    59\t        self.run_id = run_id\n    60\t        self.judge_model = judge_model\n    61\t        self.host = host\n    62\t        self.port = port\n    63\t        self.mode = mode\n    64\t        self.startup_timeout_s = startup_timeout_s\n    65\t        self.token = secrets.token_urlsafe(32)\n    66\t        self._proc: subprocess.Popen | None = None\n    67\t        self._env_snapshot: dict[str, str | None] = {}\n    68\t\n    69\t    @property\n    70\t    def url(self) -> str:\n    71\t        return f\"http://{self.host}:{self.port}\"\n    72\t\n    73\t    def env(self) -> dict[str, str]:\n    74\t        \"\"\"The per-run judge env to inject for agent subprocesses (FR-7.3).\"\"\"\n    75\t        return {\n    76\t            TOKEN_ENV_VAR: self.token,\n    77\t            URL_ENV_VAR: self.url,\n    78\t            MODE_ENV_VAR: self.mode,\n    79\t            RUN_ID_ENV_VAR: self.run_id,\n    80\t        }\n    81\t\n    82\t    def start(self) -> dict[str, str]:\n    83\t        child_env = {**os.environ, TOKEN_ENV_VAR: self.token}\n    84\t        argv = [\n    85\t            sys.executable,\n    86\t            \"-m\",\n    87\t            \"gauntlet\",\n    88\t            \"judge\",\n    89\t            \"serve\",\n    90\t            \"--policy\",\n    91\t            str(self.policy_path),\n    92\t            \"--audit\",\n    93\t            str(self.audit_path),\n    94\t            \"--host\",\n    95\t            self.host,\n    96\t            \"--port\",\n    97\t            str(self.port),\n    98\t        ]\n    99\t        if self.judge_model:\n   100\t            argv += [\"--judge-model\", self.judge_model]\n   101\t        self._proc = subprocess.Popen(argv, env=child_env)\n   102\t        self._await_healthy()\n   103\t        # Snapshot prior values of every managed var so stop() restores exactly.\n   104\t        self._env_snapshot = {v: os.environ.get(v) for v in _MANAGED_ENV_VARS}\n   105\t        env = self.env()\n   106\t        os.environ.update(env)  # the bootstrap session + child agents see it\n   107\t        return env\n   108\t\n   109\t    def _await_healthy(self) -> None:\n   110\t        deadline = time.monotonic() + self.startup_timeout_s\n   111\t        last: Exception | None = None\n   112\t        while time.monotonic() < deadline:\n   113\t            if self._proc is not None and self._proc.poll() is not None:\n   114\t                raise RuntimeError(\n   115\t                    f\"judge exited during startup (code {self._proc.returncode})\"\n   116\t                )\n   117\t            try:\n   118\t                with urllib.request.urlopen(f\"{self.url}/healthz\", timeout=1.0) as r:\n   119\t                    if r.status == 200:\n   120\t                        return\n   121\t            except (urllib.error.URLError, OSError) as exc:\n   122\t                last = exc\n   123\t                time.sleep(0.1)\n   124\t        self.stop()\n   125\t        raise RuntimeError(f\"judge did not become healthy in time: {last}\")\n   126\t\n   127\t    def stop(self) -> None:\n   128\t        if self._proc is None:\n   129\t            return\n   130\t        # Restore every managed GAUNTLET_* var to its pre-run value (incl. the\n   131\t        # per-step GAUNTLET_STEP_ID set by the orchestrator) — no env leak into\n   132\t        # the parent session on success or failure (review F-009).\n   133\t        for var, prior in (self._env_snapshot or {v: None for v in _MANAGED_ENV_VARS}).items():\n   134\t            if prior is None:\n   135\t                os.environ.pop(var, None)\n   136\t            else:\n   137\t                os.environ[var] = prior\n   138\t        self._env_snapshot = {}\n   139\t        self._proc.terminate()\n   140\t        try:\n   141\t            self._proc.wait(timeout=5.0)\n   142\t        except subprocess.TimeoutExpired:\n   143\t            self._proc.kill()\n   144\t            self._proc.wait(timeout=5.0)\n   145\t        self._proc = None\n   146\t\n   147\t    def __enter__(self) -> ManagedJudge:\n   148\t        self.start()\n   149\t        return self\n   150\t\n   151\t    def __exit__(self, *exc: object) -> None:\n   152\t        self.stop()\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_12","type":"command_execution","command":"/bin/zsh -lc \"rg \\\"exclude=\\\\[self\\\\.config\\\\.run_root\\\\]|exclude=\\\\[ctx\\\\.config\\\\.run_root\\\\]|config\\\\.run_root\\\" src/gauntlet/engine -n\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_12","type":"command_execution","command":"/bin/zsh -lc \"rg \\\"exclude=\\\\[self\\\\.config\\\\.run_root\\\\]|exclude=\\\\[ctx\\\\.config\\\\.run_root\\\\]|config\\\\.run_root\\\" src/gauntlet/engine -n\"","aggregated_output":"src/gauntlet/engine/orchestrator.py:288:            gitops.clean_untracked(self.repo_root, exclude=[self.config.run_root])\nsrc/gauntlet/engine/run.py:71:        return self.repo_root / self.config.run_root / self.slug\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_13","type":"command_execution","command":"/bin/zsh -lc 'rg \"is_clean|is_dirty_vs|backup_dirty_worktree|commit_all|diff_head|status_porcelain\" src/gauntlet/engine -n'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_13","type":"command_execution","command":"/bin/zsh -lc 'rg \"is_clean|is_dirty_vs|backup_dirty_worktree|commit_all|diff_head|status_porcelain\" src/gauntlet/engine -n'","aggregated_output":"src/gauntlet/engine/orchestrator.py:273:        if not gitops.is_dirty_vs(self.repo_root, rec.base_sha, exclude=self.excludes):\nsrc/gauntlet/engine/orchestrator.py:279:            gitops.backup_dirty_worktree(\nsrc/gauntlet/engine/gitops.py:80:def status_porcelain(repo: Path, *, exclude: list[str] | None = None) -> str:\nsrc/gauntlet/engine/gitops.py:85:def is_clean(repo: Path, *, exclude: list[str] | None = None) -> bool:\nsrc/gauntlet/engine/gitops.py:86:    return status_porcelain(repo, exclude=exclude) == \"\"\nsrc/gauntlet/engine/gitops.py:89:def is_dirty_vs(repo: Path, base_sha: str, *, exclude: list[str] | None = None) -> bool:\nsrc/gauntlet/engine/gitops.py:96:    if status_porcelain(repo, exclude=exclude) != \"\":\nsrc/gauntlet/engine/gitops.py:124:def commit_all(\nsrc/gauntlet/engine/gitops.py:167:def diff_head(repo: Path, *, exclude: list[str] | None = None) -> str:\nsrc/gauntlet/engine/gitops.py:208:def backup_dirty_worktree(\nsrc/gauntlet/engine/steptypes.py:190:    if base and gitops.head_sha(repo) != base and gitops.is_clean(repo, exclude=exclude):\nsrc/gauntlet/engine/steptypes.py:202:    if gitops.is_clean(repo, exclude=exclude):\nsrc/gauntlet/engine/steptypes.py:212:    sha = gitops.commit_all(repo, message, identity=identity, exclude=exclude)\nsrc/gauntlet/engine/steptypes.py:277:    diff = gitops.diff_head(repo, exclude=ctx.excludes)\nsrc/gauntlet/engine/steptypes.py:278:    status = gitops.status_porcelain(repo, exclude=ctx.excludes)\nsrc/gauntlet/engine/run.py:245:        if not gitops.is_clean(self.repo_root, exclude=excludes):\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_14","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/adapters/base.py | sed -n '1,170p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_14","type":"command_execution","command":"/bin/zsh -lc \"nl -ba src/gauntlet/adapters/base.py | sed -n '1,170p'\"","aggregated_output":"     1\t\"\"\"Adapter contract: protocol, result model, capabilities, errors.\n     2\t\n     3\t`AgentResult` follows PRD §4.1 exactly: text, structured, session_id, usage,\n     4\traw_events, exit_code.\n     5\t\"\"\"\n     6\t\n     7\tfrom __future__ import annotations\n     8\t\n     9\tfrom pathlib import Path\n    10\tfrom typing import Any, Literal, Protocol, runtime_checkable\n    11\t\n    12\tfrom pydantic import BaseModel, Field\n    13\t\n    14\t\n    15\tclass Usage(BaseModel):\n    16\t    \"\"\"Token/cost accounting for one adapter invocation (FR-3.2).\n    17\t\n    18\t    ``cost_usd`` is ``None`` on the degraded tokens-only path (PRD §12 Q3:\n    19\t    subscription-auth CLIs may not report cost).\n    20\t    \"\"\"\n    21\t\n    22\t    input_tokens: int | None = None\n    23\t    output_tokens: int | None = None\n    24\t    cached_input_tokens: int | None = None\n    25\t    cost_usd: float | None = None\n    26\t\n    27\t\n    28\tclass AgentResult(BaseModel):\n    29\t    \"\"\"Result of one adapter invocation, per PRD §4.1.\"\"\"\n    30\t\n    31\t    text: str\n    32\t    structured: Any | None = None\n    33\t    session_id: str | None = None\n    34\t    usage: Usage | None = None\n    35\t    raw_events: list[dict[str, Any]] = Field(default_factory=list)\n    36\t    exit_code: int\n    37\t\n    38\t\n    39\tclass AdapterCapabilities(BaseModel):\n    40\t    \"\"\"Declared capabilities, checked by pipeline load-time validation (FR-2.3).\"\"\"\n    41\t\n    42\t    repo_write: bool\n    43\t    structured_output: Literal[\"native\", \"best_effort\", \"none\"]\n    44\t    resume: bool\n    45\t\n    46\t\n    47\t@runtime_checkable\n    48\tclass AgentAdapter(Protocol):\n    49\t    \"\"\"Common interface over Claude Code, Codex, and raw-API agents (PRD §4.1).\"\"\"\n    50\t\n    51\t    name: str\n    52\t    capabilities: AdapterCapabilities\n    53\t\n    54\t    def run(\n    55\t        self,\n    56\t        prompt: str,\n    57\t        *,\n    58\t        session: str | None = None,\n    59\t        schema: dict | None = None,\n    60\t        cwd: Path | None = None,\n    61\t        extra_flags: list[str] | None = None,\n    62\t    ) -> AgentResult: ...\n    63\t\n    64\t\n    65\tclass AdapterError(Exception):\n    66\t    \"\"\"Base adapter failure. Carries a partial result for checkpointing.\n    67\t\n    68\t    ``partial`` holds whatever could be salvaged (parsed events, captured\n    69\t    output, exit code) so the engine can persist a checkpointable error\n    70\t    record instead of losing the evidence (FR-3.3).\n    71\t    \"\"\"\n    72\t\n    73\t    def __init__(self, message: str, *, partial: AgentResult | None = None) -> None:\n    74\t        super().__init__(message)\n    75\t        self.partial = partial\n    76\t\n    77\t\n    78\tclass AgentTimeoutError(AdapterError):\n    79\t    \"\"\"The CLI invocation exceeded its hard timeout and was killed (FR-3.3).\"\"\"\n    80\t\n    81\t\n    82\tclass AgentFailedError(AdapterError):\n    83\t    \"\"\"The CLI ran and produced parseable output, but reported failure\n    84\t    (nonzero exit, is_error, turn.failed). Fail closed: a failed call never\n    85\t    surfaces as a normal AgentResult (review P1 F-001).\"\"\"\n    86\t\n    87\t\n    88\tclass MalformedOutputError(AdapterError):\n    89\t    \"\"\"Adapter output could not be parsed (or failed schema validation).\"\"\"\n    90\t\n    91\t\n    92\tclass UnsupportedFeatureError(AdapterError):\n    93\t    \"\"\"The adapter does not support a requested feature (e.g. resume on api).\"\"\"\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_15","type":"command_execution","command":"/bin/zsh -lc 'git diff --name-status 77570f9..bb46479'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_15","type":"command_execution","command":"/bin/zsh -lc 'git diff --name-status 77570f9..bb46479'","aggregated_output":"M\t.gauntlet/pins.yaml\nA\truns/gauntlet-bootstrap/manual/p3-cycle-r1/findings.json\nA\truns/gauntlet-bootstrap/manual/p3-cycle-r1/review-events.jsonl\nA\truns/gauntlet-bootstrap/manual/p3-cycle-r1/review-prompt.md\nA\truns/gauntlet-bootstrap/manual/p3-cycle-r1/review-schema.json\nA\truns/gauntlet-bootstrap/manual/p3-cycle-r1/triage.json\nM\tsrc/gauntlet/engine/execution.py\nM\tsrc/gauntlet/engine/judgeproc.py\nM\tsrc/gauntlet/engine/orchestrator.py\nM\tsrc/gauntlet/engine/run.py\nM\tsrc/gauntlet/engine/steptypes.py\nM\tsrc/gauntlet/engine/validate.py\nM\ttests/integration/test_pipeline_contract.py\nA\ttests/unit/test_judgeproc.py\nM\ttests/unit/test_orchestrator.py\nM\ttests/unit/test_pipeline_loader.py\nM\ttests/unit/test_run_lifecycle.py\nM\ttests/unit/test_steptypes.py\n","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_16","type":"command_execution","command":"/bin/zsh -lc 'git diff --check 77570f9..bb46479'","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_16","type":"command_execution","command":"/bin/zsh -lc 'git diff --check 77570f9..bb46479'","aggregated_output":"","exit_code":0,"status":"completed"}}
+{"type":"item.started","item":{"id":"item_17","type":"command_execution","command":"/bin/zsh -lc \"rg \\\"max_turns|timeout_s|budget_usd|step_timeout_s\\\" -n src tests | sed -n '1,200p'\"","aggregated_output":"","exit_code":null,"status":"in_progress"}}
+{"type":"item.completed","item":{"id":"item_17","type":"command_execution","command":"/bin/zsh -lc \"rg \\\"max_turns|timeout_s|budget_usd|step_timeout_s\\\" -n src tests | sed -n '1,200p'\"","aggregated_output":"tests/integration/test_claude_contract.py:25:    defaults = dict(model=\"haiku\", tools=[], timeout_s=TIMEOUT_S)\ntests/integration/test_claude_contract.py:88:        timeout_s=TIMEOUT_S,\nsrc/gauntlet/engine/orchestrator.py:304:        budget = step.budget_usd\nsrc/gauntlet/engine/orchestrator.py:306:            budget = self.config.profile(step.agent).budget_usd\nsrc/gauntlet/engine/config.py:40:    max_turns: int | None = None\nsrc/gauntlet/engine/config.py:41:    budget_usd: float | None = None\nsrc/gauntlet/engine/config.py:42:    step_timeout_s: float | None = None\nsrc/gauntlet/engine/config.py:48:        guard_fields = {\"max_turns\", \"budget_usd\", \"step_timeout_s\"}\ntests/integration/test_codex_contract.py:31:    adapter = CodexAdapter(sandbox=\"read-only\", timeout_s=TIMEOUT_S)\ntests/integration/test_codex_contract.py:48:    adapter = CodexAdapter(sandbox=\"read-only\", timeout_s=TIMEOUT_S)\ntests/integration/test_codex_contract.py:65:    adapter = CodexAdapter(sandbox=\"read-only\", timeout_s=TIMEOUT_S)\ntests/integration/test_codex_contract.py:110:    adapter = CodexAdapter(sandbox=\"workspace-write\", timeout_s=TIMEOUT_S)\nsrc/gauntlet/engine/steptypes.py:68:    timeout = step.timeout_s  # per-step guard (FR-3.3); None => unbounded\nsrc/gauntlet/engine/steptypes.py:111:    # Per-step timeout overrides the profile's step_timeout_s, which overrides\nsrc/gauntlet/engine/steptypes.py:114:    timeout = step.timeout_s\nsrc/gauntlet/engine/steptypes.py:116:        timeout = ctx.config.profile(agent_name).step_timeout_s\nsrc/gauntlet/engine/steptypes.py:117:    if timeout is not None and hasattr(adapter, \"timeout_s\"):\nsrc/gauntlet/engine/steptypes.py:118:        adapter.timeout_s = timeout\nsrc/gauntlet/adapters/claude_code.py:53:        timeout_s: float = DEFAULT_TIMEOUT_S,\nsrc/gauntlet/adapters/claude_code.py:66:        self.timeout_s = timeout_s\nsrc/gauntlet/adapters/claude_code.py:83:            argv, timeout_s=self.timeout_s, stdin_text=prompt, cwd=cwd\nsrc/gauntlet/adapters/claude_code.py:87:                f\"claude killed after {self.timeout_s}s timeout\",\nsrc/gauntlet/engine/judgeproc.py:55:        startup_timeout_s: float = 15.0,\nsrc/gauntlet/engine/judgeproc.py:64:        self.startup_timeout_s = startup_timeout_s\nsrc/gauntlet/engine/judgeproc.py:110:        deadline = time.monotonic() + self.startup_timeout_s\nsrc/gauntlet/engine/pipeline.py:41:    max_turns: int | None = None\nsrc/gauntlet/engine/pipeline.py:42:    budget_usd: float | None = None\nsrc/gauntlet/engine/pipeline.py:43:    timeout_s: float | None = None\nsrc/gauntlet/engine/validate.py:78:    # 1b. `max_turns` is unenforceable on the pinned CLIs (claude 2.1.172 has no\nsrc/gauntlet/engine/validate.py:80:    # ignore a claimed guard (review F-006). timeout_s + budget_usd are the\nsrc/gauntlet/engine/validate.py:82:    if step.max_turns is not None:\nsrc/gauntlet/engine/validate.py:84:            f\"step {step.id!r} sets max_turns, but no installed adapter can honor \"\nsrc/gauntlet/engine/validate.py:85:            \"it; use timeout_s / budget_usd (FR-3.3 / review F-006)\"\nsrc/gauntlet/engine/validate.py:111:        if profile.max_turns is not None:\nsrc/gauntlet/engine/validate.py:113:                f\"agent profile {ref!r} sets max_turns, unenforceable on the \"\nsrc/gauntlet/engine/validate.py:114:                \"pinned CLIs; use step_timeout_s / budget_usd (review F-006)\"\ntests/integration/test_codex_sandbox.py:73:    adapter = CodexAdapter(sandbox=\"read-only\", timeout_s=TIMEOUT_S)\ntests/integration/test_codex_sandbox.py:94:    adapter = CodexAdapter(sandbox=\"workspace-write\", timeout_s=TIMEOUT_S)\ntests/integration/test_codex_sandbox.py:113:    adapter = CodexAdapter(sandbox=\"workspace-write\", timeout_s=TIMEOUT_S)\nsrc/gauntlet/adapters/api.py:43:        timeout_s: float = DEFAULT_TIMEOUT_S,\nsrc/gauntlet/adapters/api.py:49:        self.timeout_s = timeout_s\nsrc/gauntlet/adapters/api.py:117:            \"timeout\": self.timeout_s,\nsrc/gauntlet/adapters/process.py:35:    timeout_s: float,\nsrc/gauntlet/adapters/process.py:59:        stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout_s)\nsrc/gauntlet/judge/runner.py:42:                timeout_s=JUDGE_LLM_TIMEOUT_S,\ntests/unit/test_pipeline_loader.py:178:def test_max_turns_rejected_at_load(tmp_path):\ntests/unit/test_pipeline_loader.py:179:    # max_turns is unenforceable on the pinned CLIs -> rejected (F-006).\ntests/unit/test_pipeline_loader.py:186:      - {id: implement, type: agent_task, agent: builder, max_turns: 5}\ntests/unit/test_pipeline_loader.py:189:    with pytest.raises(PipelineValidationError, match=\"max_turns\"):\ntests/unit/test_pipeline_loader.py:193:def test_max_turns_on_profile_rejected(tmp_path):\ntests/unit/test_pipeline_loader.py:202:    cfg = {\"agents\": {\"builder\": {\"adapter\": \"claude-code\", \"max_turns\": 3}}}\ntests/unit/test_pipeline_loader.py:204:    with pytest.raises(PipelineValidationError, match=\"max_turns\"):\ntests/unit/test_orchestrator.py:162:      - {id: pricey, type: agent_task, agent: builder, budget_usd: 0.1, prompt_text: go}\ntests/unit/test_orchestrator.py:337:      - {id: slow, type: shell, run: \"sleep 5\", timeout_s: 0.3}\ntests/unit/test_process_timeout.py:11:        [sys.executable, \"-c\", \"print('done')\"], timeout_s=30\ntests/unit/test_process_timeout.py:26:        timeout_s=1.5,\ntests/unit/test_process_timeout.py:48:        timeout_s=1.5,\ntests/unit/test_process_timeout.py:70:        timeout_s=30,\nsrc/gauntlet/adapters/codex.py:46:        timeout_s: float = DEFAULT_TIMEOUT_S,\nsrc/gauntlet/adapters/codex.py:53:        self.timeout_s = timeout_s\nsrc/gauntlet/adapters/codex.py:79:                argv, timeout_s=self.timeout_s, stdin_text=prompt, cwd=cwd\nsrc/gauntlet/adapters/codex.py:86:                f\"codex killed after {self.timeout_s}s timeout\",\ntests/unit/test_claude_adapter.py:147:    event[\"subtype\"] = \"error_max_turns\"\ntests/unit/test_claude_adapter.py:149:    with pytest.raises(AgentFailedError, match=\"error_max_turns\"):\ntests/unit/test_claude_adapter.py:181:    adapter = ClaudeCodeAdapter(timeout_s=5)\ntests/unit/conftest.py:69:        self.timeout_s = 600.0\ntests/unit/test_judge_core.py:129:    assert adapter.timeout_s == JUDGE_LLM_TIMEOUT_S\ntests/unit/test_judge_core.py:135:    worst_case = adapter.timeout_s * (1 + adapter.max_schema_retries)\ntests/unit/test_codex_adapter.py:211:        CodexAdapter(timeout_s=5).run(\"hi\")\n","exit_code":0,"status":"completed"}}
+{"type":"item.completed","item":{"id":"item_18","type":"agent_message","text":"{\"verdicts\":[{\"finding_id\":\"F-001\",\"verdict\":\"resolved\",\"note\":\"The diff now passes a narrow run_bookkeeping_excludes list into dirty checks, backups, rollback cleanliness checks, and commits, excluding only the live run_dir and active-run pointer. The broad reset_to_base git clean exclusion is left only in the intentionally ratified cleanup path.\"},{\"finding_id\":\"F-002\",\"verdict\":\"resolved\",\"note\":\"Rollback now reloads the pipeline order and resets every step record after the target commit step to pending, not just dropped commit-step records. That prevents resume from skipping downstream work removed by git reset.\"},{\"finding_id\":\"F-003\",\"verdict\":\"resolved\",\"note\":\"Rollback now requires HEAD to exactly equal the manifest's last recorded commit before rewinding. A branch ahead of the manifest is rejected instead of silently discarded.\"},{\"finding_id\":\"F-004\",\"verdict\":\"resolved\",\"note\":\"Step-level foreach now skips done/skipped iteration records on resume. Parked gate lookup scans iteration records, so a human_gate parked inside foreach can be approved or rejected by step id.\"},{\"finding_id\":\"F-005\",\"verdict\":\"resolved\",\"note\":\"Validation now treats on_fail.route_to as stage-local, matching the runtime routing model. Cross-stage targets are rejected at load instead of crashing when routed.\"},{\"finding_id\":\"F-006\",\"verdict\":\"resolved\",\"note\":\"max_turns is now rejected at pipeline load for steps and referenced profiles, matching the ratified unsupported-CLI decision. Shell timeout_s now halts with HALTED, and agent_task picks up profile step_timeout_s as a fallback.\"},{\"finding_id\":\"F-007\",\"verdict\":\"resolved\",\"note\":\"The entry contract now compares the marker-stripped, normalized PRD body against the scaffold. Deleting only the marker line is refused.\"},{\"finding_id\":\"F-008\",\"verdict\":\"partially_resolved\",\"note\":\"The drafter now sees untracked-file status, optional plan_section content, and the final draft result's usage/session are written into the commit step record. However the redraft loop overwrites usage on each attempt, so usage from rejected draft calls is still omitted from manifest totals.\"},{\"finding_id\":\"F-009\",\"verdict\":\"resolved\",\"note\":\"ManagedJudge now snapshots and restores the managed judge environment variables, including GAUNTLET_STEP_ID. The added tests cover clearing unset vars and restoring prior values.\"}],\"new_findings\":[],\"summary\":\"The diff resolves 8 of 9 findings. F-008 remains partially resolved because commit-message redraft accounting still drops usage from earlier rejected draft attempts.\"}"}}
+{"type":"turn.completed","usage":{"input_tokens":689664,"cached_input_tokens":616960,"output_tokens":11679,"reasoning_output_tokens":9824}}
diff --git a/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-prompt.md b/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-prompt.md
new file mode 100644
index 0000000..fadeaa4
--- /dev/null
+++ b/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-prompt.md
@@ -0,0 +1,1078 @@
+# Confirm pass — Gauntlet bootstrap, Phase P3, round 1
+
+You produced the findings below against commit `77570f9` (P3). The builder
+applied fixes in commit `bb46479` ("P3.1: Address review — close 9 findings").
+Check ONLY whether the diff addresses each finding — do not re-review the whole
+phase. Scope yourself to the diff. For each finding return a verdict
+`resolved | partially_resolved | unresolved | regression_introduced` with a
+one-or-two-sentence note. If the diff itself introduces a new defect, report it
+in `new_findings` (else empty array). Output only JSON conforming to the schema.
+
+All 9 findings were triaged `legitimate` / `fix_now` and addressed. Key design
+choices to evaluate the fixes against:
+- F-001: the run-root exclusion was narrowed to ONLY the live bookkeeping
+  (run-instance dir + active-run pointer) via `run_bookkeeping_excludes`; real
+  artifacts under runs/<slug> are now tracked/detected/committed. The
+  reset_to_base `git clean` is intentionally still broad (spares the whole run
+  root) so a rewind never wipes the run setup — the re-run regenerates outputs.
+- F-006: max_turns is unenforceable on the pinned CLIs (claude 2.1.172 has no
+  --max-turns; codex exec has no turn cap — verified), so it is REJECTED at load
+  rather than silently ignored; timeout_s + budget_usd are the working halts.
+
+## Your prior findings (round 1)
+```json
+{"findings":[{"id":"F-001","severity":"blocking","category":"correctness","location":"src/gauntlet/engine/gitops.py:63; src/gauntlet/engine/run.py:331; src/gauntlet/engine/steptypes.py:111; src/gauntlet/engine/orchestrator.py:263","claim":"The run-root exclusion is broader than the ratified live-run bookkeeping boundary, so real run artifacts under `runs/<slug>` are invisible to dirty checks, backups, and commits.","evidence":"`_exclude_pathspec()` excludes both `runs` and `runs/**`; `RunManager` sets `artifact_root=layout.slug_dir`; `agent_task` writes declared outputs to `ctx.artifact_root / output`; resume/backup/commit all pass `exclude=[self.config.run_root]`. A killed step that writes `runs/<slug>/plan.md` or another declared artifact before manifest completion will look clean versus `base_sha`, so the default `interrupted_step=park` policy silently reruns over a real partial effect. This violates the P3 transaction-boundary deliverable that dirty partial edits are detected and never silently rerun.","suggested_fix":"Exclude the exact live run-instance bookkeeping paths (`run_dir`, active pointer, manifest/transcripts) rather than the whole `run_root`, or move declared artifacts outside the excluded subtree. Add a crash test where a killed step leaves a partial declared output under `runs/<slug>`."},{"id":"F-002","severity":"major","category":"correctness","location":"src/gauntlet/engine/run.py:259","claim":"Rollback rewinds the branch but leaves downstream non-commit step records marked done, so a later resume can skip work that `git reset --hard` removed.","evidence":"After resetting to the target SHA, rollback computes `dropped_step_ids` only from dropped commit records and resets only those step records. In a two-phase run, rolling back to P1 leaves P2 implementation/test records as `done` while only the P2 commit step becomes pending; resume then skips the P2 work and reaches a clean-tree commit step. FR-9.9 requires branch and manifest to never disagree.","suggested_fix":"Rewind all manifest step records after the target phase boundary in execution order, not just dropped commit-step IDs. Clear their base SHAs/session IDs as appropriate and set `current_step` to the first rewound step."},{"id":"F-003","severity":"major","category":"correctness","location":"src/gauntlet/engine/run.py:232","claim":"Rollback accepts a branch that is ahead of the manifest, allowing unmanifested commits to be discarded by reset.","evidence":"The guard only rejects when `last_recorded` is not an ancestor of `HEAD`; if a human or tool made an extra commit after the manifest’s last recorded SHA, `last_recorded` is an ancestor and rollback proceeds. The P3 plan says rollback refuses if the current branch has diverged from the manifest’s recorded SHAs, and FR-9.9 requires branch/manifest agreement.","suggested_fix":"Require `HEAD == man.commits[-1].sha` before rollback, unless there is an explicit import/confirm flow that records or preserves the extra commits in the manifest."},{"id":"F-004","severity":"major","category":"correctness","location":"src/gauntlet/engine/orchestrator.py:157; src/gauntlet/engine/orchestrator.py:174; src/gauntlet/engine/orchestrator.py:392","claim":"Step-level `foreach` is not resumable: completed iterations rerun, and parked gates inside foreach cannot be approved.","evidence":"`_run_step_foreach()` calls `_execute()` for every iteration without checking whether that iteration’s record is already `done` or `skipped`; `_execute()` then marks even a done record running again. Separately, `current_step` stores only the step id and `_find_parked_gate()` looks up `record(step_id)` with no iteration, so a parked `human_gate` record like `gate[1]` is absent to `approve`. FR-5.4 makes `foreach` first-class, and FR-8.2 requires safe resume.","suggested_fix":"Make `_run_step_foreach()` skip terminal iteration records, persist the current iteration in the manifest, and allow approve/reject to target a `(step_id, iteration)` gate."},{"id":"F-005","severity":"major","category":"correctness","location":"src/gauntlet/engine/validate.py:47; src/gauntlet/engine/orchestrator.py:143; src/gauntlet/engine/orchestrator.py:191","claim":"`on_fail` targets in other stages pass validation but crash at runtime when the route is taken.","evidence":"Validation checks `route_to` against all step IDs in the whole pipeline. Runtime builds `index` from only the current stage and `_reset_for_retry()` calls `ids.index(route_to)` on only that stage’s IDs. A cross-stage route therefore validates, then raises instead of producing a checkpointed failed/parked run. FR-5.4 requires first-class failure routing; CLAUDE.md §2 requires fail-closed behavior.","suggested_fix":"Either validate `on_fail.route_to` as stage-local or implement explicit cross-stage routing semantics. Add a load-time test for a cross-stage route."},{"id":"F-006","severity":"major","category":"spec-gap","location":"src/gauntlet/engine/pipeline.py:40; src/gauntlet/engine/config.py:39; src/gauntlet/engine/steptypes.py:62; src/gauntlet/engine/steptypes.py:100","claim":"P3 declares `max_turns` and timeout guards but does not enforce them across core step paths.","evidence":"`max_turns` exists on steps/profiles but is never read. Profile `step_timeout_s` is stripped before adapter construction and has no fallback use. `shell` steps run `subprocess.run()` with no timeout at all; only `agent_task.timeout_s` mutates an adapter timeout. FR-3.3 and the P3 plan require per-step `max_turns`/timeout/budget guards that halt at a checkpoint.","suggested_fix":"Apply profile and step timeout fallbacks to every handler, use `subprocess.run(..., timeout=...)` for shell with a `HALTED` result on timeout, and either wire adapter-supported max-turn flags or reject `max_turns` on unsupported adapters at load time."},{"id":"F-007","severity":"major","category":"spec-gap","location":"src/gauntlet/engine/run.py:93","claim":"The entry contract accepts a scaffolded PRD if the marker line is deleted.","evidence":"`new()` writes a fixed stub, but `check_entry_contract()` only rejects when `PRD_STUB_MARKER` is present. A user can remove that one HTML comment while leaving `# PRD: <title>`, empty sections, and scaffold text unchanged, and `run` will proceed. FR-10.1 requires a human-authored PRD; the P3 plan says `run` refuses unless `prd.md` exists and is non-stub.","suggested_fix":"Normalize and compare against the scaffold with and without the marker, reject placeholder headings/text, or require an explicit human-authored metadata marker separate from the scaffold body."},{"id":"F-008","severity":"major","category":"spec-gap","location":"src/gauntlet/engine/steptypes.py:198; src/gauntlet/engine/gitops.py:167","claim":"Commit-message drafting omits required inputs and loses message-agent accounting.","evidence":"The P3 plan says the `message_agent` drafts from diff plus plan section. `_draft_commit_message()` sends only a prompt and `git diff HEAD`; `git diff HEAD` omits untracked files that `commit_all()` will add with `git add -A`, so new-file phases can be drafted from an empty diff. The successful adapter result’s usage/session ID is discarded, so manifest totals omit commit-drafting cost despite §7/FR-3.2 usage requirements.","suggested_fix":"Include the relevant plan excerpt and untracked-file diffs/status in the draft prompt. Accumulate message-agent usage/session IDs into the commit step record and apply the same budget/timeout guards."},{"id":"F-009","severity":"minor","category":"correctness","location":"src/gauntlet/engine/orchestrator.py:222; src/gauntlet/engine/judgeproc.py:112","claim":"The engine-managed judge lifecycle leaks `GAUNTLET_STEP_ID` into the parent process environment.","evidence":"`_execute()` sets `os.environ[\"GAUNTLET_STEP_ID\"]` for judged steps, but `ManagedJudge.stop()` only removes token, URL, mode, and run ID. BOOTSTRAP-NOTES #15 and the P3 contract describe per-run env teardown; the integration test only checks the token variable.","suggested_fix":"Snapshot and restore all Gauntlet judge env vars, including `GAUNTLET_STEP_ID`, in the judge lifecycle. Test that no `GAUNTLET_*` judge variables remain after success and failure."}],"open_questions":[],"summary":"The most serious issue is that the implementation excludes the entire `runs/` tree from engine git operations while also storing real PRD/plan/artifact effects there, which breaks the core crash/resume assumption. Rollback also does not truly rewind the manifest to match the reset branch. Several P3-promised control surfaces exist only partially: foreach resume, on_fail validation, timeout/max_turns guards, entry-contract non-stubness, and message-agent accounting need tightening before this engine should drive later bootstrap phases."}```
+
+## Triage verdicts (ratified by the human)
+```json
+{
+  "phase": "P3",
+  "round": 1,
+  "handoff_sha": "77570f9",
+  "reviewer": "codex exec -s read-only (codex-cli 0.139.0)",
+  "verdicts": [
+    {
+      "finding_id": "F-001",
+      "severity": "blocking",
+      "verdict": "legitimate",
+      "reasoning": "Confirmed: artifact_root=slug_dir (under run_root) and every engine git op excludes the whole `runs` tree, so a declared agent output written under runs/<slug> is invisible to is_dirty_vs(base) — the default park policy could silently re-run over a real partial effect, violating the F-003 transaction-boundary deliverable. The exclusion must be narrowed to the live run-instance bookkeeping (run_dir + active pointer), letting prd.md/plan.md/declared outputs be tracked, detectable work.",
+      "action": "fix_now"
+    },
+    {
+      "finding_id": "F-002",
+      "severity": "major",
+      "verdict": "legitimate",
+      "reasoning": "Confirmed: rollback resets only dropped commit-step records to pending; downstream non-commit step records (implement/tests of a rolled-back phase) stay `done`, so resume skips work that `git reset --hard` removed and reaches a clean-tree commit. FR-9.9 requires branch and manifest to agree.",
+      "action": "fix_now"
+    },
+    {
+      "finding_id": "F-003",
+      "severity": "major",
+      "verdict": "legitimate",
+      "reasoning": "Confirmed: the divergence guard only rejects when the recorded tip is not an ancestor of HEAD, so a branch that is *ahead* of the manifest (extra unmanifested commits) passes and those commits are silently discarded by reset. The plan says rollback refuses if the branch has diverged from the manifest's recorded SHAs.",
+      "action": "fix_now"
+    },
+    {
+      "finding_id": "F-004",
+      "severity": "major",
+      "verdict": "legitimate",
+      "reasoning": "Confirmed for step-level foreach: _run_step_foreach re-executes every iteration without skipping terminal records, and a parked human_gate inside a foreach is unreachable by approve (current_step/_find_parked_gate ignore iteration). Stage-level foreach (what the standard pipeline uses) does skip done iterations and is unaffected, but FR-5.4/FR-8.2 require step-level foreach to resume safely too.",
+      "action": "fix_now"
+    },
+    {
+      "finding_id": "F-005",
+      "severity": "major",
+      "verdict": "legitimate",
+      "reasoning": "Confirmed: validation accepts an on_fail.route_to anywhere in the pipeline, but the runtime index/_reset_for_retry are stage-local, so a cross-stage route validates then crashes instead of producing a checkpointed failed/parked run — a fail-open against CLAUDE.md §2. Simplest correct fix: validate route_to as stage-local.",
+      "action": "fix_now"
+    },
+    {
+      "finding_id": "F-006",
+      "severity": "major",
+      "verdict": "legitimate",
+      "reasoning": "Confirmed partial enforcement: budget_usd halt and per-invocation adapter timeout are wired/tested, but shell steps have no timeout, profile.step_timeout_s is stripped and never used as a fallback, and max_turns is never read. FR-3.3 asks for per-step max_turns/timeout/budget guards that halt at a checkpoint. Will wire shell timeout + profile-timeout fallback (HALTED on expiry) and pass max_turns to adapters that honor it / reject it at load where they don't.",
+      "action": "fix_now"
+    },
+    {
+      "finding_id": "F-007",
+      "severity": "major",
+      "verdict": "legitimate",
+      "reasoning": "The plan scopes the entry contract to a template-marker check, but 'differs from the scaffolded stub' is better served by comparing the whole PRD body against the stub (marker-stripped) so deleting only the marker line still reads as the untouched stub and is refused. Cheap strengthening that honors FR-10.1's non-stub intent.",
+      "action": "fix_now"
+    },
+    {
+      "finding_id": "F-008",
+      "severity": "major",
+      "verdict": "legitimate",
+      "reasoning": "Confirmed: _draft_commit_message sends only `git diff HEAD`, which omits untracked files that commit_all then stages — a new-file phase drafts from an empty diff; and the message-agent's usage/session is discarded, so manifest totals understate cost (§7/FR-3.2). Will include untracked-file status in the draft context and accumulate message-agent usage into the commit step record; plan-section inclusion is best-effort when provided.",
+      "action": "fix_now"
+    },
+    {
+      "finding_id": "F-009",
+      "severity": "minor",
+      "verdict": "legitimate",
+      "reasoning": "Confirmed: the orchestrator sets GAUNTLET_STEP_ID in os.environ but ManagedJudge.stop() clears only TOKEN/URL/MODE/RUN_ID, leaking STEP_ID into the parent session after a run. Will clear all GAUNTLET_* judge vars (incl. STEP_ID) on stop and assert none remain after success and failure.",
+      "action": "fix_now"
+    }
+  ],
+  "summary": "All 9 findings are legitimate and accepted for fix_now; none are bikeshedding, premature optimization, or not_applicable. Two correctness defects are most material: F-001 (the run-root exclusion is broader than the live bookkeeping, so real partial artifact effects can be silently re-run over — undercutting the headline P3 crash/resume guarantee) and F-002/F-003 (rollback does not truly reconcile branch and manifest). The rest tighten genuinely partial P3 control surfaces: step-foreach resume, on_fail validation scope, timeout/max_turns enforcement, entry-contract non-stubness, commit-draft inputs/accounting, and judge-env teardown."
+}
+```
+
+## The fix diff (`77570f9..bb46479`, i.e. the P3.1 commit)
+```diff
+diff --git a/.gauntlet/pins.yaml b/.gauntlet/pins.yaml
+index 4b0b6b4..f825f22 100644
+--- a/.gauntlet/pins.yaml
++++ b/.gauntlet/pins.yaml
+@@ -52,6 +52,11 @@ clis:
+         Permission-bypass flags observed in --help (--dangerously-skip-permissions,
+         --allow-dangerously-skip-permissions, --bare which skips hooks,
+         --permission-mode bypassPermissions) are rejected by the config lint.
++      - >-
++        P3: no --max-turns flag on claude 2.1.172 (`claude --help` has no
++        turn/limit option), and `codex exec` has no turn cap either. The engine
++        therefore rejects `max_turns` at pipeline load (review F-006); the
++        working per-step halts are timeout_s and budget_usd (FR-3.3).
+   codex:
+     version: "codex-cli 0.139.0"
+     verified_flags:
+diff --git a/src/gauntlet/engine/execution.py b/src/gauntlet/engine/execution.py
+index 3ab7ba8..6b9ae6b 100644
+--- a/src/gauntlet/engine/execution.py
++++ b/src/gauntlet/engine/execution.py
+@@ -64,6 +64,9 @@ class StepContext:
+     writer: RedactingWriter
+     judge_env: dict[str, str] = field(default_factory=dict)
+     artifacts: dict[str, Path] = field(default_factory=dict)
++    # repo-relative paths of the engine's own bookkeeping (review F-001); commit
++    # and dirty checks exclude these but not real run artifacts.
++    excludes: list[str] = field(default_factory=list)
+     iteration_item: Any | None = None
+     iteration_index: int | None = None
+     adapter_factory: AdapterFactory | None = None
+@@ -136,3 +139,24 @@ def get_spec(step_type: str) -> StepSpec:
+ 
+ def usage_from_result(result: AgentResult) -> Usage | None:
+     return result.usage
++
++
++def run_bookkeeping_excludes(repo_root: Path, run_dir: Path, artifact_root: Path) -> list[str]:
++    """Repo-relative paths of the engine's own live bookkeeping (review F-001).
++
++    These — the run-instance dir (manifest/transcripts/steps/judge-audit) and the
++    active-run pointer — must be invisible to worktree-state checks and commits.
++    Everything *else* under the run root (prd.md, plan.md, declared step outputs)
++    is real work: tracked, detected by the transaction boundary, and committable.
++    Narrowing the exclusion to just this set is the F-001 fix — the prior code
++    excluded the whole run root, hiding real partial effects from the dirty-base
++    check.
++    """
++    excludes: list[str] = []
++    root = repo_root.resolve()
++    for p in (run_dir, artifact_root / "active-run.txt"):
++        try:
++            excludes.append(p.resolve().relative_to(root).as_posix())
++        except ValueError:
++            continue
++    return excludes
+diff --git a/src/gauntlet/engine/judgeproc.py b/src/gauntlet/engine/judgeproc.py
+index 4a7d86a..1ab5477 100644
+--- a/src/gauntlet/engine/judgeproc.py
++++ b/src/gauntlet/engine/judgeproc.py
+@@ -21,6 +21,7 @@ from pathlib import Path
+ from gauntlet.judge.hook_client import (
+     MODE_ENV_VAR,
+     RUN_ID_ENV_VAR,
++    STEP_ID_ENV_VAR,
+     URL_ENV_VAR,
+ )
+ from gauntlet.judge.service import TOKEN_ENV_VAR
+@@ -28,6 +29,17 @@ from gauntlet.judge.service import TOKEN_ENV_VAR
+ DEFAULT_HOST = "127.0.0.1"
+ DEFAULT_PORT = 8787
+ 
++# Every GAUNTLET_* var the run touches — snapshotted at start, restored at stop
++# so nothing (incl. the per-step GAUNTLET_STEP_ID set by the orchestrator) leaks
++# into the parent session (review F-009).
++_MANAGED_ENV_VARS = (
++    TOKEN_ENV_VAR,
++    URL_ENV_VAR,
++    MODE_ENV_VAR,
++    RUN_ID_ENV_VAR,
++    STEP_ID_ENV_VAR,
++)
++
+ 
+ class ManagedJudge:
+     def __init__(
+@@ -52,6 +64,7 @@ class ManagedJudge:
+         self.startup_timeout_s = startup_timeout_s
+         self.token = secrets.token_urlsafe(32)
+         self._proc: subprocess.Popen | None = None
++        self._env_snapshot: dict[str, str | None] = {}
+ 
+     @property
+     def url(self) -> str:
+@@ -87,6 +100,8 @@ class ManagedJudge:
+             argv += ["--judge-model", self.judge_model]
+         self._proc = subprocess.Popen(argv, env=child_env)
+         self._await_healthy()
++        # Snapshot prior values of every managed var so stop() restores exactly.
++        self._env_snapshot = {v: os.environ.get(v) for v in _MANAGED_ENV_VARS}
+         env = self.env()
+         os.environ.update(env)  # the bootstrap session + child agents see it
+         return env
+@@ -112,8 +127,15 @@ class ManagedJudge:
+     def stop(self) -> None:
+         if self._proc is None:
+             return
+-        for var in (TOKEN_ENV_VAR, URL_ENV_VAR, MODE_ENV_VAR, RUN_ID_ENV_VAR):
+-            os.environ.pop(var, None)
++        # Restore every managed GAUNTLET_* var to its pre-run value (incl. the
++        # per-step GAUNTLET_STEP_ID set by the orchestrator) — no env leak into
++        # the parent session on success or failure (review F-009).
++        for var, prior in (self._env_snapshot or {v: None for v in _MANAGED_ENV_VARS}).items():
++            if prior is None:
++                os.environ.pop(var, None)
++            else:
++                os.environ[var] = prior
++        self._env_snapshot = {}
+         self._proc.terminate()
+         try:
+             self._proc.wait(timeout=5.0)
+diff --git a/src/gauntlet/engine/orchestrator.py b/src/gauntlet/engine/orchestrator.py
+index f09d43c..157c865 100644
+--- a/src/gauntlet/engine/orchestrator.py
++++ b/src/gauntlet/engine/orchestrator.py
+@@ -33,6 +33,7 @@ from gauntlet.engine.execution import (
+     StepContext,
+     StepResult,
+     get_spec,
++    run_bookkeeping_excludes,
+ )
+ from gauntlet.engine.expr import eval_when, resolve_list
+ from gauntlet.engine.manifest import Manifest, StepRecord
+@@ -73,6 +74,9 @@ class Orchestrator:
+         self.clock = clock
+         self.manifest_path = run_dir / "manifest.json"
+         self.artifacts: dict[str, Path] = {}
++        # Narrow exclusion: only the engine's own bookkeeping is hidden from
++        # dirty checks / commits — real run artifacts stay visible (review F-001).
++        self.excludes = run_bookkeeping_excludes(repo_root, run_dir, artifact_root)
+         self._ignore_run_dir()
+         self._seed_artifacts()
+ 
+@@ -174,6 +178,9 @@ class Orchestrator:
+     def _run_step_foreach(self, step: Step) -> str:
+         items = resolve_list(step.foreach, self._context())
+         for idx, item in enumerate(items):
++            rec = self.manifest.record(step.id, str(idx))
++            if rec is not None and rec.status in (M.DONE, M.SKIPPED):
++                continue  # resume: don't re-run a completed iteration (F-004)
+             result = self._execute(step, str(idx), item)
+             if result.status != DONE:
+                 return result.status
+@@ -260,8 +267,10 @@ class Orchestrator:
+         )
+         if not is_agent_write:
+             return None
+-        exclude = [self.config.run_root]
+-        if not gitops.is_dirty_vs(self.repo_root, rec.base_sha, exclude=exclude):
++        # Detect partial work against the narrow bookkeeping exclusion, so a
++        # partial *artifact* under the run root (not just a repo-root file) is
++        # still seen as a mid-edit interruption (review F-001).
++        if not gitops.is_dirty_vs(self.repo_root, rec.base_sha, exclude=self.excludes):
+             return None  # clean re-entry: agent never progressed; safe to re-run
+         if self.config.interrupted_step == "reset_to_base":
+             ts = self.clock().replace(":", "-")
+@@ -269,11 +278,13 @@ class Orchestrator:
+             # Snapshot the partial work (tracked + untracked) before discarding.
+             gitops.backup_dirty_worktree(
+                 self.repo_root, backup, f"interrupted {rec.id} partial work",
+-                exclude=exclude,
++                exclude=self.excludes,
+             )
+             gitops.reset_hard(self.repo_root, rec.base_sha)
+-            # Spare the run root so the reset never wipes the run pointer,
+-            # manifests, or the human-authored prd.md living under it.
++            # `clean` is broader than the dirty check on purpose: it spares the
++            # whole run root so the reset never wipes the run pointer, manifests,
++            # the authored prd.md, or prior declared artifacts — the re-run
++            # regenerates its own outputs over them.
+             gitops.clean_untracked(self.repo_root, exclude=[self.config.run_root])
+             return None  # tree restored to base; re-run cleanly
+         return StepResult(
+@@ -352,6 +363,7 @@ class Orchestrator:
+             writer=self.writer,
+             judge_env=self.judge_env,
+             artifacts=dict(self.artifacts),
++            excludes=self.excludes,
+             iteration_item=item,
+             iteration_index=int(iteration) if iteration is not None else None,
+             adapter_factory=self.adapter_factory,
+@@ -390,13 +402,16 @@ class Orchestrator:
+         rec.ended = self.clock()
+ 
+     def _find_parked_gate(self, step_id: str) -> StepRecord:
+-        rec = self.manifest.record(step_id)
+-        if rec is None or rec.status != M.PARKED:
+-            raise ValueError(
+-                f"step {step_id!r} is not parked at a gate "
+-                f"(status: {rec.status if rec else 'absent'})"
+-            )
+-        return rec
++        # Scan across iterations so a gate parked inside a foreach (record
++        # `gate` with iteration `1`) is reachable by approve/reject (F-004).
++        for rec in self.manifest.steps:
++            if rec.id == step_id and rec.status == M.PARKED:
++                return rec
++        existing = self.manifest.record(step_id)
++        raise ValueError(
++            f"step {step_id!r} is not parked at a gate "
++            f"(status: {existing.status if existing else 'absent'})"
++        )
+ 
+     def _head_sha(self) -> str:
+         return gitops.head_sha(self.repo_root)
+diff --git a/src/gauntlet/engine/run.py b/src/gauntlet/engine/run.py
+index 3af06b8..fa9651c 100644
+--- a/src/gauntlet/engine/run.py
++++ b/src/gauntlet/engine/run.py
+@@ -15,6 +15,7 @@ from pathlib import Path
+ 
+ from gauntlet.engine import gitops, manifest as M
+ from gauntlet.engine.config import RunConfig
++from gauntlet.engine.execution import run_bookkeeping_excludes
+ from gauntlet.engine.judgeproc import ManagedJudge
+ from gauntlet.engine.manifest import Manifest, PipelineRef
+ from gauntlet.engine.orchestrator import Orchestrator
+@@ -51,6 +52,14 @@ def _utc_stamp() -> str:
+     return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
+ 
+ 
++def _strip_marker(text: str) -> str:
++    return "\n".join(line for line in text.splitlines() if PRD_STUB_MARKER not in line)
++
++
++def _normalize(text: str) -> str:
++    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())
++
++
+ @dataclass
+ class RunLayout:
+     repo_root: Path
+@@ -105,11 +114,20 @@ class RunManager:
+                 f"{layout.prd_path} does not exist; `gauntlet new {slug}` scaffolds "
+                 "a stub for a human to author (FR-10.1)"
+             )
+-        if PRD_STUB_MARKER in layout.prd_path.read_text():
++        content = layout.prd_path.read_text()
++        if PRD_STUB_MARKER in content:
+             raise EntryContractError(
+                 f"{layout.prd_path} is still the scaffolded stub; a human must "
+                 "author the PRD before a run can start (FR-10.1)"
+             )
++        # Deleting only the marker line leaves the rest of the scaffold intact —
++        # still not a human-authored PRD. Compare the whole body, marker-stripped
++        # and whitespace-normalized, against the stub (review F-007).
++        if _normalize(content) == _normalize(_strip_marker(_PRD_STUB)):
++            raise EntryContractError(
++                f"{layout.prd_path} is the scaffolded stub with only the marker "
++                "removed; a human must author a real PRD before a run (FR-10.1)"
++            )
+ 
+     # ---- run (FR-8.1) -------------------------------------------------------
+     def start(
+@@ -221,22 +239,25 @@ class RunManager:
+         run_dir = layout.active_run_dir()
+         man = Manifest.load(run_dir / "manifest.json")
+ 
+-        # Guard 1: clean worktree (the work tree; run bookkeeping is excluded).
+-        if not gitops.is_clean(self.repo_root, exclude=[self.config.run_root]):
++        # Guard 1: clean work tree — only the engine's own bookkeeping is
++        # excluded (review F-001), so an uncommitted real artifact still blocks.
++        excludes = run_bookkeeping_excludes(self.repo_root, run_dir, layout.slug_dir)
++        if not gitops.is_clean(self.repo_root, exclude=excludes):
+             raise RollbackGuardError(
+                 "refusing rollback: worktree is dirty; commit or discard first"
+             )
+-        # Guard 2: branch agrees with the manifest (no out-of-band rewrite).
++        # Guard 2: branch tip MUST equal the manifest's last recorded commit.
++        # A branch ahead of the manifest (extra unmanifested commits) is a
++        # divergence — reset would silently discard those commits (review F-003).
+         if not man.commits:
+             raise RollbackGuardError("no recorded commits to roll back to")
+         last_recorded = man.commits[-1].sha
+         head = gitops.head_sha(self.repo_root)
+-        if head != last_recorded and not gitops.is_ancestor(
+-            self.repo_root, last_recorded, head
+-        ):
++        if head != last_recorded:
+             raise RollbackGuardError(
+-                "refusing rollback: branch has diverged from the manifest's "
+-                f"recorded SHAs (HEAD {head[:10]} vs recorded {last_recorded[:10]})"
++                "refusing rollback: branch has diverged from the manifest "
++                f"(HEAD {head[:10]} != last recorded {last_recorded[:10]}); the "
++                "branch and manifest must agree before a rewind (FR-9.9)"
+             )
+         # Resolve the target: the last commit whose phase prefix is P<phase>.
+         target = self._phase_boundary_sha(man, phase)
+@@ -244,10 +265,6 @@ class RunManager:
+             raise RollbackGuardError(
+                 f"no recorded phase-{phase} commit boundary to roll back to"
+             )
+-        if not gitops.is_ancestor(self.repo_root, target, head) and target != head:
+-            raise RollbackGuardError(
+-                f"refusing rollback: target {target[:10]} is not an ancestor of HEAD"
+-            )
+ 
+         # Backup ref + manifest snapshot before any rewind (F-010).
+         ts = _utc_stamp()
+@@ -257,27 +274,42 @@ class RunManager:
+         shutil.copy2(run_dir / "manifest.json", run_dir / f"manifest.snapshot-{ts}.json")
+ 
+         gitops.reset_hard(self.repo_root, target)
+-        # Rewind the manifest so branch and manifest never disagree about where
+-        # the run stands: keep commits up to and including the target, drop the
+-        # rest, and reset every step that produced a dropped commit back to
+-        # pending (with its base SHA cleared) so a later resume re-does it.
++        self._rewind_manifest(man, run_dir, target)
++        man.write_atomic(run_dir / "manifest.json")
++        return target
++
++    def _rewind_manifest(self, man: Manifest, run_dir: Path, target: str) -> None:
++        """Rewind the manifest to match the reset branch (review F-002).
++
++        Drop commits after the target, and reset to `pending` EVERY step record
++        (any type, any iteration) that executes after the target phase boundary
++        in pipeline order — not just the steps that produced dropped commits.
++        Otherwise a later resume skips work `git reset --hard` removed and the
++        branch and manifest disagree (FR-9.9).
++        """
+         keep: list = []
+         for commit in man.commits:
+             keep.append(commit)
+             if commit.sha == target:
+                 break
+-        kept_step_ids = {c.step_id for c in keep}
+-        dropped_step_ids = {c.step_id for c in man.commits} - kept_step_ids
+         man.commits = keep
++        target_step = keep[-1].step_id
++
++        pipeline, _ = load_pipeline(run_dir / "pipeline.yaml")
++        order = [s.id for s in pipeline.all_steps()]
++        try:
++            cutoff = order.index(target_step)
++        except ValueError:  # pragma: no cover - defensive
++            cutoff = len(order) - 1
++        keep_ids = set(order[: cutoff + 1])
+         for rec in man.steps:
+-            if rec.id in dropped_step_ids:
++            if rec.id not in keep_ids:
+                 rec.status = M.PENDING
+                 rec.base_sha = None
++                rec.session_id = None
+                 rec.ended = None
+         man.status = M.RUN_PARKED
+         man.current_step = None
+-        man.write_atomic(run_dir / "manifest.json")
+-        return target
+ 
+     # ---- internals ----------------------------------------------------------
+     def _phase_boundary_sha(self, man: Manifest, phase: int) -> str | None:
+diff --git a/src/gauntlet/engine/steptypes.py b/src/gauntlet/engine/steptypes.py
+index 40190e9..4f183c5 100644
+--- a/src/gauntlet/engine/steptypes.py
++++ b/src/gauntlet/engine/steptypes.py
+@@ -21,6 +21,7 @@ from gauntlet.engine.commit_format import header_prefix, validate_commit_message
+ from gauntlet.engine.execution import (
+     DONE,
+     FAILED,
++    HALTED,
+     PARKED,
+     StepContext,
+     StepResult,
+@@ -64,13 +65,23 @@ def handle_shell(step: Step, ctx: StepContext) -> StepResult:
+     if not template:
+         return StepResult(status=FAILED, notes="shell step has no `run:` command")
+     command = render_shell_command(template, ctx.config)
+-    proc = subprocess.run(
+-        command,
+-        shell=True,
+-        cwd=ctx.repo_root,
+-        capture_output=True,
+-        text=True,
+-    )
++    timeout = step.timeout_s  # per-step guard (FR-3.3); None => unbounded
++    try:
++        proc = subprocess.run(
++            command,
++            shell=True,
++            cwd=ctx.repo_root,
++            capture_output=True,
++            text=True,
++            timeout=timeout,
++        )
++    except subprocess.TimeoutExpired as exc:
++        _write_step_log(ctx, "output.txt", f"$ {command}\n--- TIMEOUT after {timeout}s ---\n")
++        # Halt at a checkpoint rather than letting a stuck command burn on.
++        return StepResult(
++            status=HALTED,
++            notes=f"shell timeout halt (FR-3.3): `{command}` exceeded {timeout}s",
++        )
+     _write_step_log(ctx, "output.txt", _proc_log(command, proc))
+     if proc.returncode != 0:
+         return StepResult(
+@@ -97,7 +108,12 @@ def handle_agent_task(step: Step, ctx: StepContext) -> StepResult:
+     adapter = ctx.build_adapter(agent_name)
+     prompt = _render_prompt(step, ctx)
+     schema = _load_schema(step, ctx)
++    # Per-step timeout overrides the profile's step_timeout_s, which overrides
++    # the adapter default (FR-3.3). A timeout raises AgentTimeoutError, which the
++    # orchestrator turns into a HALTED checkpoint.
+     timeout = step.timeout_s
++    if timeout is None and agent_name in ctx.config.agents:
++        timeout = ctx.config.profile(agent_name).step_timeout_s
+     if timeout is not None and hasattr(adapter, "timeout_s"):
+         adapter.timeout_s = timeout
+     result = adapter.run(
+@@ -151,13 +167,20 @@ def _load_schema(step: Step, ctx: StepContext) -> dict | None:
+ # --- commit (FR-9.2/9.7) -----------------------------------------------------
+ def handle_commit(step: Step, ctx: StepContext) -> StepResult:
+     repo = ctx.repo_root
+-    exclude = [ctx.config.run_root]  # never commit / count the run bookkeeping
+-    message = _commit_message(step, ctx)
++    # Narrow exclusion (review F-001): commit real artifacts (plan.md, outputs);
++    # keep only the engine's own bookkeeping out of the commit and the checks.
++    exclude = ctx.excludes
++    message, draft_usage, draft_session = _commit_message(step, ctx)
+     err = validate_commit_message(message)
+     if err is not None:
+         # message_agent drafting includes a bounded redraft loop in _draft;
+         # a literal/exhausted message that still fails is a hard error.
+-        return StepResult(status=FAILED, notes=f"commit message invalid: {err.reason}")
++        return StepResult(
++            status=FAILED,
++            usage=draft_usage,
++            session_id=draft_session,
++            notes=f"commit message invalid: {err.reason}",
++        )
+     prefix = header_prefix(message)
+ 
+     # Mid-commit resume reconciliation (review F-003): if a prior attempt
+@@ -171,12 +194,16 @@ def handle_commit(step: Step, ctx: StepContext) -> StepResult:
+                 status=DONE,
+                 commit_sha=existing,
+                 commit_phase=prefix,
++                usage=draft_usage,
++                session_id=draft_session,
+                 notes="reconciled pre-existing commit after mid-commit interruption",
+             )
+ 
+     if gitops.is_clean(repo, exclude=exclude):
+         return StepResult(
+             status=FAILED,
++            usage=draft_usage,
++            session_id=draft_session,
+             notes="commit step found a clean worktree with nothing to commit",
+         )
+ 
+@@ -184,53 +211,86 @@ def handle_commit(step: Step, ctx: StepContext) -> StepResult:
+     identity = ctx.config.identity(agent_name)
+     sha = gitops.commit_all(repo, message, identity=identity, exclude=exclude)
+     return StepResult(
+-        status=DONE, commit_sha=sha, commit_phase=prefix, notes=f"committed {sha[:10]}"
++        status=DONE, commit_sha=sha, commit_phase=prefix,
++        usage=draft_usage, session_id=draft_session, notes=f"committed {sha[:10]}",
+     )
+ 
+ 
+-def _commit_message(step: Step, ctx: StepContext) -> str:
++def _commit_message(step: Step, ctx: StepContext):
++    """Return ``(message, usage, session_id)``; usage/session are None for a
++    literal message (no model call)."""
+     literal = step.get("message")
+     if literal:
+-        return literal  # human-authored YAML; still format-validated above
++        return literal, None, None  # human-authored YAML; still format-validated
+     return _draft_commit_message(step, ctx)
+ 
+ 
+-def _draft_commit_message(step: Step, ctx: StepContext) -> str:
++def _draft_commit_message(step: Step, ctx: StepContext):
+     """Draft a commit message via the message_agent with bounded redraft.
+ 
+-    The agent sees the diff + plan section (data); the engine validates the
+-    format and asks for a redraft on violation (FR-9.2). Returns the last draft
+-    (valid or not) — :func:`handle_commit` makes the accept/reject decision.
++    The agent sees the change as data — both the tracked diff AND the untracked
++    files `git add -A` will sweep in (review F-008: a new-file phase otherwise
++    drafts from an empty diff) — plus an optional plan section. The engine
++    validates the format and asks for a redraft on violation (FR-9.2). Returns
++    ``(message, usage, session_id)`` so the commit step records the drafter's
++    cost (FR-3.2/§7).
+     """
+     agent_name = step.get("message_agent")
+     if not agent_name:
+         raise ValueError("commit step needs either `message:` or `message_agent:`")
+     adapter = ctx.build_adapter(agent_name)
+-    diff = gitops.diff_head(ctx.repo_root, exclude=[ctx.config.run_root])
++    change = _change_context(ctx)
+     base_prompt = (
+         (ctx.repo_root / step.get("prompt")).read_text()
+         if step.get("prompt")
+         else _DEFAULT_COMMIT_PROMPT
+     )
+     phase_hint = step.get("phase", "")
+-    prompt = (
++    plan_section = _plan_section(step, ctx)
++    header = (
+         f"{base_prompt}\n\nRequired header phase prefix: {phase_hint or '(infer PN)'}\n"
+-        f"\n--- diff (HEAD) ---\n{diff}\n"
++        f"{plan_section}"
+     )
++    prompt = f"{header}\n{change}\n"
+     max_redrafts = int(step.get("max_redrafts", 2))
+     message = ""
+-    for attempt in range(1 + max_redrafts):
++    usage = None
++    session_id = None
++    for _attempt in range(1 + max_redrafts):
+         result = adapter.run(prompt, cwd=ctx.repo_root)
++        usage = result.usage  # accumulated per call by the orchestrator's totals
++        session_id = result.session_id
+         message = result.text.strip()
+         if validate_commit_message(message) is None:
+-            return message
++            return message, usage, session_id
+         prompt = (
+-            f"{base_prompt}\n\nYour previous draft was rejected: "
++            f"{header}\n\nYour previous draft was rejected: "
+             f"{validate_commit_message(message).reason}. "
+-            "Return only the corrected commit message.\n"
+-            f"\n--- diff (HEAD) ---\n{diff}\n"
++            f"Return only the corrected commit message.\n{change}\n"
+         )
+-    return message
++    return message, usage, session_id
++
++
++def _change_context(ctx: StepContext) -> str:
++    """The diff vs HEAD plus the untracked files staging will add (F-008)."""
++    repo = ctx.repo_root
++    diff = gitops.diff_head(repo, exclude=ctx.excludes)
++    status = gitops.status_porcelain(repo, exclude=ctx.excludes)
++    return (
++        f"--- git status (incl. untracked) ---\n{status}\n"
++        f"\n--- diff (tracked, vs HEAD) ---\n{diff}"
++    )
++
++
++def _plan_section(step: Step, ctx: StepContext) -> str:
++    """Optional plan excerpt the message_agent drafts from (FR-9.2)."""
++    ref = step.get("plan_section")
++    if not ref:
++        return ""
++    path = ctx.artifacts.get(ref) or (ctx.artifact_root / ref)
++    if Path(path).exists():
++        return f"\n--- plan section: {ref} ---\n{Path(path).read_text()}\n"
++    return ""
+ 
+ 
+ _DEFAULT_COMMIT_PROMPT = (
+diff --git a/src/gauntlet/engine/validate.py b/src/gauntlet/engine/validate.py
+index 5150543..d2f110d 100644
+--- a/src/gauntlet/engine/validate.py
++++ b/src/gauntlet/engine/validate.py
+@@ -44,14 +44,18 @@ def validate_pipeline(
+ ) -> ValidationReport:
+     report = ValidationReport()
+     available: set[str] = set(seeds)
+-    all_ids = {step.id for step in pipeline.all_steps()}
+     for stage in pipeline.stages:
++        stage_ids = {step.id for step in stage.steps}
+         for step in stage.steps:
+             _validate_step(step, config, available, report)
+-            if step.on_fail and step.on_fail.route_to not in all_ids:
++            # on_fail routing is stage-local: the runtime jumps within the
++            # current stage's step list, so a cross-stage target would validate
++            # then crash at runtime (review F-005). Reject it at load.
++            if step.on_fail and step.on_fail.route_to not in stage_ids:
+                 report.errors.append(
+-                    f"step {step.id!r} on_fail routes to unknown step "
+-                    f"{step.on_fail.route_to!r}"
++                    f"step {step.id!r} on_fail routes to {step.on_fail.route_to!r}, "
++                    "which is not a step in the same stage (cross-stage routing "
++                    "is unsupported, FR-5.4 / review F-005)"
+                 )
+             output = step.get("output")
+             if output:
+@@ -71,6 +75,16 @@ def _validate_step(
+         report.errors.append(str(exc))
+         return
+ 
++    # 1b. `max_turns` is unenforceable on the pinned CLIs (claude 2.1.172 has no
++    # --max-turns; codex exec has no turn cap) — reject it rather than silently
++    # ignore a claimed guard (review F-006). timeout_s + budget_usd are the
++    # working per-step halts (FR-3.3).
++    if step.max_turns is not None:
++        report.errors.append(
++            f"step {step.id!r} sets max_turns, but no installed adapter can honor "
++            "it; use timeout_s / budget_usd (FR-3.3 / review F-006)"
++        )
++
+     # 2. dangling artifact dataflow (FR-5.3)
+     for name in (step.get("inputs", []) or []):
+         if name not in available:
+@@ -94,6 +108,11 @@ def _validate_step(
+             )
+             continue
+         profile = config.profile(ref)
++        if profile.max_turns is not None:
++            report.errors.append(
++                f"agent profile {ref!r} sets max_turns, unenforceable on the "
++                "pinned CLIs; use step_timeout_s / budget_usd (review F-006)"
++            )
+         caps = profile.capabilities()
+         if spec.step_requires_repo_write(step) and not caps.repo_write:
+             report.errors.append(
+diff --git a/tests/integration/test_pipeline_contract.py b/tests/integration/test_pipeline_contract.py
+index 9cfae82..0ab896b 100644
+--- a/tests/integration/test_pipeline_contract.py
++++ b/tests/integration/test_pipeline_contract.py
+@@ -20,6 +20,7 @@ from pathlib import Path
+ import pytest
+ 
+ from gauntlet.engine import gitops, manifest as M
++from gauntlet.engine.judgeproc import _MANAGED_ENV_VARS
+ from gauntlet.engine.run import RunManager
+ from gauntlet.judge.service import TOKEN_ENV_VAR
+ 
+@@ -83,8 +84,10 @@ def test_engine_manages_judge_lifecycle_around_a_run(tmp_path):
+     mgr = RunManager(repo)
+     status = mgr.start("demo", repo / "pipelines" / "mini.yaml", use_judge=True)
+     assert status == M.RUN_DONE
+-    # the judge env is torn down after the run (no leakage)
+-    assert TOKEN_ENV_VAR not in os.environ
++    # the judge env is torn down after the run (no leakage) — every managed var,
++    # including the per-step GAUNTLET_STEP_ID set by the orchestrator (F-009)
++    for var in _MANAGED_ENV_VARS:
++        assert var not in os.environ, f"{var} leaked into the parent env"
+     assert gitops.commit_subject(repo, "HEAD") == "P1: engine-managed judge run"
+     assert (repo / "artifact.txt").read_text().strip() == "work"
+ 
+diff --git a/tests/unit/test_judgeproc.py b/tests/unit/test_judgeproc.py
+new file mode 100644
+index 0000000..70f68b6
+--- /dev/null
++++ b/tests/unit/test_judgeproc.py
+@@ -0,0 +1,63 @@
++"""Engine-managed judge env teardown (review F-009).
++
++stop() must restore EVERY managed GAUNTLET_* var — including the per-step
++GAUNTLET_STEP_ID the orchestrator sets — to its pre-run value, so nothing leaks
++into the parent session on success or failure.
++"""
++
++from __future__ import annotations
++
++import os
++from pathlib import Path
++
++from gauntlet.engine.judgeproc import _MANAGED_ENV_VARS, ManagedJudge
++from gauntlet.judge.hook_client import STEP_ID_ENV_VAR
++from gauntlet.judge.service import TOKEN_ENV_VAR
++
++
++class _FakeProc:
++    returncode = 0
++
++    def terminate(self):
++        pass
++
++    def wait(self, timeout=None):
++        return 0
++
++    def kill(self):
++        pass
++
++
++def _judge() -> ManagedJudge:
++    mj = ManagedJudge(policy_path=Path("policy.yaml"), audit_path=Path("a.jsonl"), run_id="r")
++    mj._proc = _FakeProc()
++    return mj
++
++
++def test_stop_clears_unset_vars_including_step_id():
++    mj = _judge()
++    mj._env_snapshot = {v: None for v in _MANAGED_ENV_VARS}
++    os.environ[TOKEN_ENV_VAR] = "tok"
++    os.environ[STEP_ID_ENV_VAR] = "implement"  # set by the orchestrator
++    try:
++        mj.stop()
++        assert TOKEN_ENV_VAR not in os.environ
++        assert STEP_ID_ENV_VAR not in os.environ
++        for var in _MANAGED_ENV_VARS:
++            assert var not in os.environ
++    finally:
++        for v in _MANAGED_ENV_VARS:
++            os.environ.pop(v, None)
++
++
++def test_stop_restores_prior_values():
++    mj = _judge()
++    os.environ[TOKEN_ENV_VAR] = "outer-token"
++    mj._env_snapshot = {v: None for v in _MANAGED_ENV_VARS}
++    mj._env_snapshot[TOKEN_ENV_VAR] = "outer-token"  # pre-run value
++    os.environ[TOKEN_ENV_VAR] = "run-token"  # overwritten during the run
++    try:
++        mj.stop()
++        assert os.environ[TOKEN_ENV_VAR] == "outer-token"  # restored, not deleted
++    finally:
++        os.environ.pop(TOKEN_ENV_VAR, None)
+diff --git a/tests/unit/test_orchestrator.py b/tests/unit/test_orchestrator.py
+index b0c07c1..6f322d7 100644
+--- a/tests/unit/test_orchestrator.py
++++ b/tests/unit/test_orchestrator.py
+@@ -265,6 +265,82 @@ stages:
+     assert "refs/gauntlet/backup/" in refs
+ 
+ 
++def test_resume_dirty_artifact_under_runroot_is_detected(fixture_repo):
++    # Review F-001: a partial *declared artifact* under runs/<slug> (not just a
++    # repo-root file) must still be seen as a mid-edit interruption and parked.
++    base = gitops.head_sha(fixture_repo)
++    (fixture_repo / "runs" / "demo").mkdir(parents=True)
++    (fixture_repo / "runs" / "demo" / "plan.md").write_text("half-written plan")
++    man = _seed_running_step(fixture_repo, "author", "agent_task", base)
++    text = """
++name: demo
++version: 1
++stages:
++  - id: s
++    steps:
++      - {id: author, type: agent_task, agent: builder, output: plan.md, prompt_text: go}
++"""
++    adapter = FakeAdapter()
++    orch = _build(fixture_repo, text, adapters={"builder": adapter}, manifest=man,
++                  interrupted="park")
++    assert orch.drive() == M.RUN_PARKED
++    assert orch.manifest.record("author").status == M.INTERRUPTED
++    assert adapter.calls == []  # not re-run over the partial artifact
++
++
++def test_step_foreach_skips_completed_iterations_on_resume(fixture_repo):
++    # Review F-004: a resumed step-level foreach must not re-run done iterations.
++    text = """
++name: demo
++version: 1
++stages:
++  - id: s
++    steps:
++      - {id: work, type: agent_task, agent: builder, foreach: vars.items, prompt_text: go}
++"""
++    adapter = FakeAdapter()
++    man = Manifest(run_id="r", slug="demo", branch="b", base_branch="main",
++                   pipeline=PipelineRef(name="demo", version=1, hash="x"))
++    man.upsert(StepRecord(id="work", type="agent_task", iteration="0", status=M.DONE))
++    orch = _build(fixture_repo, text, adapters={"builder": adapter},
++                  extra_context={"items": ["a", "b", "c"]}, manifest=man)
++    assert orch.drive() == M.RUN_DONE
++    # iteration 0 was already done; only 1 and 2 ran
++    assert len(adapter.calls) == 2
++
++
++def test_gate_inside_foreach_is_approvable(fixture_repo):
++    # Review F-004: a human_gate parked inside a foreach must be reachable.
++    text = """
++name: demo
++version: 1
++stages:
++  - id: s
++    foreach: vars.items
++    steps:
++      - {id: gate, type: human_gate}
++"""
++    orch = _build(fixture_repo, text, extra_context={"items": ["a", "b"]})
++    assert orch.drive() == M.RUN_PARKED
++    # the first iteration's gate is parked; approve targets it across iterations
++    assert orch.approve_gate("gate") in (M.RUN_PARKED, M.RUN_DONE)
++
++
++def test_shell_timeout_halts(fixture_repo):
++    # Review F-006: a shell step exceeding its timeout halts at a checkpoint.
++    text = """
++name: demo
++version: 1
++stages:
++  - id: s
++    steps:
++      - {id: slow, type: shell, run: "sleep 5", timeout_s: 0.3}
++"""
++    orch = _build(fixture_repo, text)
++    assert orch.drive() == M.RUN_PARKED
++    assert orch.manifest.record("slow").status == M.HALTED
++
++
+ def test_resume_mid_commit_reconciles_without_double_commit(fixture_repo):
+     base = gitops.head_sha(fixture_repo)
+     # Simulate: engine recorded base + ran commit, the commit landed, then the
+diff --git a/tests/unit/test_pipeline_loader.py b/tests/unit/test_pipeline_loader.py
+index 055800a..9f4f158 100644
+--- a/tests/unit/test_pipeline_loader.py
++++ b/tests/unit/test_pipeline_loader.py
+@@ -152,10 +152,59 @@ stages:
+       - {id: tests, type: shell, run: "true", on_fail: {route_to: nowhere, max_retries: 1}}
+ """
+     pipeline, _ = load_pipeline(_write(tmp_path, text))
+-    with pytest.raises(PipelineValidationError, match="unknown step"):
++    with pytest.raises(PipelineValidationError, match="not a step in the same stage"):
+         validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))
+ 
+ 
++def test_cross_stage_on_fail_route_rejected(tmp_path):
++    # route_to targets a step in a DIFFERENT stage -> rejected at load (F-005),
++    # since runtime routing is stage-local and would otherwise crash.
++    text = """
++name: d
++version: 1
++stages:
++  - id: a
++    steps:
++      - {id: build, type: shell, run: "true"}
++  - id: b
++    steps:
++      - {id: tests, type: shell, run: "true", on_fail: {route_to: build, max_retries: 1}}
++"""
++    pipeline, _ = load_pipeline(_write(tmp_path, text))
++    with pytest.raises(PipelineValidationError, match="not a step in the same stage"):
++        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))
++
++
++def test_max_turns_rejected_at_load(tmp_path):
++    # max_turns is unenforceable on the pinned CLIs -> rejected (F-006).
++    text = """
++name: d
++version: 1
++stages:
++  - id: s
++    steps:
++      - {id: implement, type: agent_task, agent: builder, max_turns: 5}
++"""
++    pipeline, _ = load_pipeline(_write(tmp_path, text))
++    with pytest.raises(PipelineValidationError, match="max_turns"):
++        validate_pipeline(pipeline, RunConfig.model_validate(CONFIG))
++
++
++def test_max_turns_on_profile_rejected(tmp_path):
++    text = """
++name: d
++version: 1
++stages:
++  - id: s
++    steps:
++      - {id: implement, type: agent_task, agent: builder}
++"""
++    cfg = {"agents": {"builder": {"adapter": "claude-code", "max_turns": 3}}}
++    pipeline, _ = load_pipeline(_write(tmp_path, text))
++    with pytest.raises(PipelineValidationError, match="max_turns"):
++        validate_pipeline(pipeline, RunConfig.model_validate(cfg))
++
++
+ def test_unknown_step_type_rejected(tmp_path):
+     text = """
+ name: d
+diff --git a/tests/unit/test_run_lifecycle.py b/tests/unit/test_run_lifecycle.py
+index 249935d..4b2a143 100644
+--- a/tests/unit/test_run_lifecycle.py
++++ b/tests/unit/test_run_lifecycle.py
+@@ -59,6 +59,20 @@ def test_entry_contract_passes_for_real_prd(fixture_repo):
+     mgr.check_entry_contract("demo")  # no raise
+ 
+ 
++def test_entry_contract_refuses_marker_only_removed(fixture_repo):
++    # F-007: deleting only the marker line leaves the scaffold body -> refuse.
++    from gauntlet.engine.run import PRD_STUB_MARKER
++
++    mgr = _prepare(fixture_repo)
++    mgr.new("demo")
++    prd = mgr.layout("demo").prd_path
++    stub = prd.read_text()
++    prd.write_text("\n".join(l for l in stub.splitlines() if PRD_STUB_MARKER not in l))
++    assert PRD_STUB_MARKER not in prd.read_text()
++    with pytest.raises(EntryContractError, match="only the marker removed"):
++        mgr.check_entry_contract("demo")
++
++
+ LINEAR = """
+ name: p
+ version: 1
+@@ -141,11 +155,31 @@ def test_rollback_to_phase_one_rewinds_branch_and_manifest(fixture_repo):
+     assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: phase one"
+     man = mgr.status("demo")
+     assert [c.phase for c in man.commits] == ["P1"]
++    # F-002: ALL phase-2 step records (not just its commit) are rewound to
++    # pending, so a resume re-does the work git reset removed.
++    assert man.record("impl2").status == M.PENDING
++    assert man.record("c2").status == M.PENDING
++    assert man.record("impl1").status == M.DONE  # phase 1 kept
+     # a backup ref preserved the pre-rollback tip
+     refs = gitops._run(fixture_repo, "for-each-ref", "refs/gauntlet/backup/")
+     assert p2_sha in refs or "refs/gauntlet/backup/" in refs
+ 
+ 
++def test_rollback_refuses_branch_ahead_of_manifest(fixture_repo):
++    # F-003: an extra unmanifested commit means branch != manifest tip -> refuse.
++    mgr = _prepare(fixture_repo)
++    _author_prd(mgr, "demo")
++    path = _write_pipeline(fixture_repo, LINEAR)
++    mgr.start("demo", path, use_judge=False,
++              adapter_factory=lambda n: FakeAdapter(writes={"f.py": "x\n"}))
++    (fixture_repo / "extra.py").write_text("out of band\n")
++    git(fixture_repo, "add", "-A")
++    git(fixture_repo, "-c", "user.name=H", "-c", "user.email=h@h.local",
++        "commit", "-qm", "out-of-band commit")
++    with pytest.raises(RollbackGuardError, match="diverged"):
++        mgr.rollback("demo", phase=1)
++
++
+ def test_rollback_refuses_dirty_worktree(fixture_repo):
+     mgr = _prepare(fixture_repo)
+     _author_prd(mgr, "demo")
+diff --git a/tests/unit/test_steptypes.py b/tests/unit/test_steptypes.py
+index a8b8ac9..15464b3 100644
+--- a/tests/unit/test_steptypes.py
++++ b/tests/unit/test_steptypes.py
+@@ -85,6 +85,50 @@ stages:
+     assert gitops.commit_subject(fixture_repo, "HEAD") == "P1: drafted"
+ 
+ 
++class RecordingDrafter:
++    """Captures the draft prompt and reports usage (F-008)."""
++
++    capabilities = FakeAdapter.capabilities
++
++    def __init__(self):
++        self.prompts = []
++
++    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
++        from gauntlet.adapters.base import Usage
++
++        self.prompts.append(prompt)
++        return AgentResult(
++            text="P1: add new file\n\nbody.",
++            usage=Usage(input_tokens=100, output_tokens=10, cost_usd=0.002),
++            session_id="draft-sess",
++            exit_code=0,
++        )
++
++
++def test_commit_draft_sees_untracked_files_and_accounts_usage(fixture_repo):
++    # F-008: a NEW-file phase must not draft from an empty diff, and the
++    # message-agent's usage must land in the manifest totals.
++    (fixture_repo / "brand_new.py").write_text("print('new')\n")  # untracked
++    drafter = RecordingDrafter()
++    text = """
++name: demo
++version: 1
++stages:
++  - id: s
++    steps:
++      - {id: commit, type: commit, message_agent: triage}
++"""
++    cfg = {"agents": {"triage": {"adapter": "api", "model": "h"}}}
++    orch = _orch(fixture_repo, text, config=cfg, adapters={"triage": drafter})
++    assert orch.drive() == M.RUN_DONE
++    # the untracked new file is visible to the drafter (status section)
++    assert "brand_new.py" in drafter.prompts[0]
++    # the drafter's cost is accumulated into the run + step totals
++    assert orch.manifest.totals.cost_usd == 0.002
++    assert orch.manifest.record("commit").usage.cost_usd == 0.002
++    assert orch.manifest.record("commit").session_id == "draft-sess"
++
++
+ def test_commit_bad_literal_message_fails(fixture_repo):
+     (fixture_repo / "work.py").write_text("code\n")
+     text = """
+```
diff --git a/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-schema.json b/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-schema.json
new file mode 100644
index 0000000..5ab1f8d
--- /dev/null
+++ b/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-schema.json
@@ -0,0 +1,35 @@
+{
+  "$schema": "http://json-schema.org/draft-07/schema#",
+  "type": "object",
+  "additionalProperties": false,
+  "required": ["verdicts", "new_findings", "summary"],
+  "properties": {
+    "verdicts": {
+      "type": "array",
+      "items": {
+        "type": "object",
+        "additionalProperties": false,
+        "required": ["finding_id", "verdict", "note"],
+        "properties": {
+          "finding_id": {"type": "string"},
+          "verdict": {"type": "string", "enum": ["resolved", "partially_resolved", "unresolved", "regression_introduced"]},
+          "note": {"type": "string"}
+        }
+      }
+    },
+    "new_findings": {
+      "type": "array",
+      "items": {
+        "type": "object",
+        "additionalProperties": false,
+        "required": ["severity", "claim", "location"],
+        "properties": {
+          "severity": {"type": "string", "enum": ["blocking", "major", "minor", "nit"]},
+          "claim": {"type": "string"},
+          "location": {"type": "string"}
+        }
+      }
+    },
+    "summary": {"type": "string"}
+  }
+}
diff --git a/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-verdict.json b/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-verdict.json
new file mode 100644
index 0000000..821a0d3
--- /dev/null
+++ b/runs/gauntlet-bootstrap/manual/p3-cycle-r1/confirm-verdict.json
@@ -0,0 +1 @@
+{"verdicts":[{"finding_id":"F-001","verdict":"resolved","note":"The diff now passes a narrow run_bookkeeping_excludes list into dirty checks, backups, rollback cleanliness checks, and commits, excluding only the live run_dir and active-run pointer. The broad reset_to_base git clean exclusion is left only in the intentionally ratified cleanup path."},{"finding_id":"F-002","verdict":"resolved","note":"Rollback now reloads the pipeline order and resets every step record after the target commit step to pending, not just dropped commit-step records. That prevents resume from skipping downstream work removed by git reset."},{"finding_id":"F-003","verdict":"resolved","note":"Rollback now requires HEAD to exactly equal the manifest's last recorded commit before rewinding. A branch ahead of the manifest is rejected instead of silently discarded."},{"finding_id":"F-004","verdict":"resolved","note":"Step-level foreach now skips done/skipped iteration records on resume. Parked gate lookup scans iteration records, so a human_gate parked inside foreach can be approved or rejected by step id."},{"finding_id":"F-005","verdict":"resolved","note":"Validation now treats on_fail.route_to as stage-local, matching the runtime routing model. Cross-stage targets are rejected at load instead of crashing when routed."},{"finding_id":"F-006","verdict":"resolved","note":"max_turns is now rejected at pipeline load for steps and referenced profiles, matching the ratified unsupported-CLI decision. Shell timeout_s now halts with HALTED, and agent_task picks up profile step_timeout_s as a fallback."},{"finding_id":"F-007","verdict":"resolved","note":"The entry contract now compares the marker-stripped, normalized PRD body against the scaffold. Deleting only the marker line is refused."},{"finding_id":"F-008","verdict":"partially_resolved","note":"The drafter now sees untracked-file status, optional plan_section content, and the final draft result's usage/session are written into the commit step record. However the redraft loop overwrites usage on each attempt, so usage from rejected draft calls is still omitted from manifest totals."},{"finding_id":"F-009","verdict":"resolved","note":"ManagedJudge now snapshots and restores the managed judge environment variables, including GAUNTLET_STEP_ID. The added tests cover clearing unset vars and restoring prior values."}],"new_findings":[],"summary":"The diff resolves 8 of 9 findings. F-008 remains partially resolved because commit-message redraft accounting still drops usage from earlier rejected draft attempts."}
\ No newline at end of file
diff --git a/src/gauntlet/engine/steptypes.py b/src/gauntlet/engine/steptypes.py
index 4f183c5..6ce8b91 100644
--- a/src/gauntlet/engine/steptypes.py
+++ b/src/gauntlet/engine/steptypes.py
@@ -254,21 +254,47 @@ def _draft_commit_message(step: Step, ctx: StepContext):
     prompt = f"{header}\n{change}\n"
     max_redrafts = int(step.get("max_redrafts", 2))
     message = ""
-    usage = None
+    usage = _UsageAccumulator()  # sum across ALL draft attempts, incl. rejected
     session_id = None
     for _attempt in range(1 + max_redrafts):
         result = adapter.run(prompt, cwd=ctx.repo_root)
-        usage = result.usage  # accumulated per call by the orchestrator's totals
+        usage.add(result.usage)  # a redraft's cost is real spend (F-008 round 2)
         session_id = result.session_id
         message = result.text.strip()
         if validate_commit_message(message) is None:
-            return message, usage, session_id
+            return message, usage.result(), session_id
         prompt = (
             f"{header}\n\nYour previous draft was rejected: "
             f"{validate_commit_message(message).reason}. "
             f"Return only the corrected commit message.\n{change}\n"
         )
-    return message, usage, session_id
+    return message, usage.result(), session_id
+
+
+class _UsageAccumulator:
+    """Sum Usage across redraft attempts so rejected drafts still count (F-008)."""
+
+    def __init__(self) -> None:
+        self._in = 0
+        self._out = 0
+        self._cost: float | None = None
+        self._seen = False
+
+    def add(self, usage) -> None:
+        if usage is None:
+            return
+        self._seen = True
+        self._in += usage.input_tokens or 0
+        self._out += usage.output_tokens or 0
+        if usage.cost_usd is not None:
+            self._cost = (self._cost or 0.0) + usage.cost_usd
+
+    def result(self):
+        from gauntlet.adapters.base import Usage
+
+        if not self._seen:
+            return None
+        return Usage(input_tokens=self._in, output_tokens=self._out, cost_usd=self._cost)
 
 
 def _change_context(ctx: StepContext) -> str:
diff --git a/tests/unit/test_steptypes.py b/tests/unit/test_steptypes.py
index 15464b3..22caf8f 100644
--- a/tests/unit/test_steptypes.py
+++ b/tests/unit/test_steptypes.py
@@ -129,6 +129,49 @@ stages:
     assert orch.manifest.record("commit").session_id == "draft-sess"
 
 
+class RedraftUsageAdapter:
+    """Bad draft (with usage) then a good one (with usage) — usage must sum."""
+
+    capabilities = FakeAdapter.capabilities
+
+    def __init__(self):
+        self.n = 0
+
+    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
+        from gauntlet.adapters.base import Usage
+
+        self.n += 1
+        text = "bad header" if self.n == 1 else "P1: ok\n\nbody."
+        return AgentResult(
+            text=text,
+            usage=Usage(input_tokens=50, output_tokens=5, cost_usd=0.001),
+            session_id="s",
+            exit_code=0,
+        )
+
+
+def test_commit_redraft_usage_is_summed(fixture_repo):
+    # F-008 round 2: a rejected draft attempt's cost is real spend and must be
+    # counted, not overwritten by the accepted attempt.
+    (fixture_repo / "new.py").write_text("x\n")
+    drafter = RedraftUsageAdapter()
+    text = """
+name: demo
+version: 1
+stages:
+  - id: s
+    steps:
+      - {id: commit, type: commit, message_agent: triage, max_redrafts: 2}
+"""
+    cfg = {"agents": {"triage": {"adapter": "api", "model": "h"}}}
+    orch = _orch(fixture_repo, text, config=cfg, adapters={"triage": drafter})
+    assert orch.drive() == M.RUN_DONE
+    assert drafter.n == 2  # one rejected + one accepted
+    # both attempts counted: 2 x 0.001 = 0.002
+    assert abs(orch.manifest.totals.cost_usd - 0.002) < 1e-9
+    assert orch.manifest.record("commit").usage.input_tokens == 100
+
+
 def test_commit_bad_literal_message_fails(fixture_repo):
     (fixture_repo / "work.py").write_text("code\n")
     text = """
```

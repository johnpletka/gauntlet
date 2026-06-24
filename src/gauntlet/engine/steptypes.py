"""Built-in step types: agent_task, shell, human_gate, commit (FR-5, FR-9.2).

The ``adversarial_cycle`` step type (the review→triage→fix→confirm primitive)
is a P4 deliverable and registers there; P3 ships the four primitives the
crash test and switchover need. Control flow (routing, retries, parking,
budget halts) is the orchestrator's; handlers report status only.

Trust model (plan §0 / review F-001): ``shell`` commands come **only** from
human-committed pipeline/config YAML — :func:`render_shell_command` refuses any
template token that is not a ``{{config.*}}`` reference, so agent-authored text
can never be substituted into a command line.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from gauntlet.adapters.base import AdapterError
from gauntlet.engine.commit_format import header_prefix, validate_commit_message
from gauntlet.engine.execution import (
    DONE,
    FAILED,
    HALTED,
    PARKED,
    StepContext,
    StepResult,
    StepSpec,
)
from gauntlet.engine import gitops
from gauntlet.engine.manifest import PARKED_REASON_UPSTREAM_CONFLICT
from gauntlet.engine.pipeline import Step
from gauntlet.logging.transcript import StepLogger

_CONFIG_TOKEN_RE = re.compile(r"\{\{\s*config\.([a-zA-Z0-9_]+)\s*\}\}")
_ANY_TOKEN_RE = re.compile(r"\{\{.*?\}\}")

# Canonical FR-10.4 halt marker. Only a `halt_on:` whose value is *exactly* this
# marker sets the conflict-park discriminator (FR-2.1); a step configured with a
# different `halt_on:` marker parks with `parked_reason` unset. Pipelines use
# this string verbatim (pipelines/standard.yaml `halt_on: "UPSTREAM CONFLICT"`).
UPSTREAM_CONFLICT_MARKER = "UPSTREAM CONFLICT"


# --- shell -------------------------------------------------------------------
def render_shell_command(template: str, config) -> str:
    """Substitute only ``{{config.<key>}}`` tokens; reject anything else.

    Refusing non-config tokens is the engine-side enforcement of the trust
    model: no agent-authored artifact may be interpolated into a shell command.
    """
    def _sub(m: re.Match[str]) -> str:
        key = m.group(1)
        value = getattr(config, key, None)
        if value is None:
            raise ValueError(
                f"shell template references unknown config key {key!r}"
            )
        return str(value)

    rendered = _CONFIG_TOKEN_RE.sub(_sub, template)
    leftover = _ANY_TOKEN_RE.search(rendered)
    if leftover:
        raise ValueError(
            f"shell command may only reference {{{{config.*}}}}; refusing "
            f"to substitute {leftover.group(0)!r} (trust model / review F-001)"
        )
    return rendered


def handle_shell(step: Step, ctx: StepContext) -> StepResult:
    template = step.get("run")
    if not template:
        return StepResult(status=FAILED, notes="shell step has no `run:` command")
    command = render_shell_command(template, ctx.config)
    timeout = step.timeout_s  # per-step guard (FR-3.3); None => unbounded
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=ctx.repo_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _write_step_log(ctx, "output.txt", f"$ {command}\n--- TIMEOUT after {timeout}s ---\n")
        # Halt at a checkpoint rather than letting a stuck command burn on.
        return StepResult(
            status=HALTED,
            notes=f"shell timeout halt (FR-3.3): `{command}` exceeded {timeout}s",
        )
    _write_step_log(ctx, "output.txt", _proc_log(command, proc))
    if proc.returncode != 0:
        return StepResult(
            status=FAILED,
            notes=f"`{command}` exited {proc.returncode}",
        )
    return StepResult(status=DONE, notes=f"`{command}` exited 0")


# --- human_gate --------------------------------------------------------------
def handle_human_gate(step: Step, ctx: StepContext) -> StepResult:
    show = step.get("show", []) or []
    return StepResult(
        status=PARKED,
        notes=f"awaiting human decision; review: {', '.join(show) or '(nothing listed)'}",
    )


# --- agent_task --------------------------------------------------------------
def handle_agent_task(step: Step, ctx: StepContext) -> StepResult:
    agent_name = step.agent
    if not agent_name:
        return StepResult(status=FAILED, notes="agent_task step has no `agent:`")
    adapter = ctx.build_adapter(agent_name)
    prompt = _render_prompt(step, ctx)
    schema = _load_schema(step, ctx)
    # Per-step timeout overrides the profile's step_timeout_s, which overrides
    # the adapter default (FR-3.3). A timeout raises AgentTimeoutError, which the
    # orchestrator turns into a HALTED checkpoint.
    timeout = step.timeout_s
    if timeout is None and agent_name in ctx.config.agents:
        timeout = ctx.config.profile(agent_name).step_timeout_s
    if timeout is not None and hasattr(adapter, "timeout_s"):
        adapter.timeout_s = timeout
    logger = step_logger(ctx)
    logger.log_prompt(prompt)  # before the call: the prompt survives a crash
    try:
        result = adapter.run(
            prompt,
            session=ctx.record.session_id,
            schema=schema,
            cwd=ctx.repo_root,
        )
    except AdapterError as exc:
        # FR-4.2 is lossless for failures too (P4.r1 F-007): persist whatever
        # partial evidence the adapter salvaged before the orchestrator
        # classifies the error.
        if exc.partial is not None:
            logger.log_result(exc.partial, suffix="-failed")
        logger.log_text("failure.txt", str(exc))
        raise
    logger.log_result(result)  # transcript.md + events.jsonl (+ structured)
    usage_by_agent = {agent_name: result.usage} if result.usage else {}

    # Completion-signal contract (BOOTSTRAP-NOTES #32): a headless agent that
    # exits 0 may still have *halted* — surfaced an FR-10.4 upstream conflict
    # instead of doing the work. Exit code alone read that as `done` and the
    # engine marched on to a doomed commit. Opt-in per step: when `halt_on:` is
    # set and its marker is *signalled* (line-leading, per `_marker_signalled`),
    # park for a human (fail closed, never DONE); when `require_signal:` is set
    # and absent, fail closed. Document-authoring tasks must not carry `halt_on:`
    # — their output legitimately quotes such markers as prose (see plan-author
    # in pipelines/standard.yaml); the line-leading match is the second guard.
    signal = _completion_signal(step, result.text)
    if signal is not None:
        status, note, parked_reason = signal
        return StepResult(
            status=status, session_id=result.session_id, usage=result.usage,
            usage_by_agent=usage_by_agent, notes=note,
            parked_reason=parked_reason,
        )

    artifact_writes: dict[str, Path] = {}
    output = step.get("output")
    if output:
        out_path = ctx.artifact_root / output
        ctx.writer.write_text(out_path, result.text)
        artifact_writes[output] = out_path
    return StepResult(
        status=DONE,
        session_id=result.session_id,
        usage=result.usage,
        usage_by_agent=usage_by_agent,
        artifact_writes=artifact_writes,
        notes=f"agent {agent_name!r} completed",
    )


def _completion_signal(step: Step, text: str):
    """Read an agent_task's final output for a halt/completion contract (#32).

    Returns ``None`` to proceed normally, or ``(status, note, parked_reason)`` to
    short-circuit. Both checks are opt-in (absent keys → no contract), so
    existing steps and the document-authoring tasks keep their plain exit-code
    semantics.

    ``parked_reason`` is ``PARKED_REASON_UPSTREAM_CONFLICT`` only when the matched
    ``halt_on`` marker is *exactly* the canonical :data:`UPSTREAM_CONFLICT_MARKER`
    (FR-2.1) — a step parking on a *different* ``halt_on`` marker, or failing on a
    missing ``require_signal``, carries no ``parked_reason``.
    """
    halt_on = step.get("halt_on")
    if halt_on and _marker_signalled(halt_on, text):
        parked_reason = (
            PARKED_REASON_UPSTREAM_CONFLICT
            if halt_on == UPSTREAM_CONFLICT_MARKER
            else None
        )
        return PARKED, (
            f"agent signalled {halt_on!r} (FR-10.4 upstream conflict / halt); "
            "parked for a human instead of marking the step done (#32)"
        ), parked_reason
    require = step.get("require_signal")
    if require and not _marker_signalled(require, text):
        return FAILED, (
            f"agent did not emit the required completion signal {require!r}; "
            "failing closed rather than advancing on a silent non-completion (#32)"
        ), None
    return None


def _marker_signalled(marker: str, text: str) -> bool:
    """True when *marker* appears as a deliberate line-leading signal in *text*.

    The contract (implement-phase.md) tells the agent to emit the marker as a
    *clearly marked block* — i.e. at the start of its own line, optionally behind
    Markdown decoration (``#``/``*``/``>``/`` ` ``/``-``). Matching only there,
    not anywhere in the body, is what keeps a document that merely *discusses*
    the marker in prose from being read as a genuine signal: a plan that quotes
    the FR-10.4 protocol verbatim ("…is an **UPSTREAM CONFLICT** (FR-10.4)…")
    used to false-positive the substring check, park the step, and lose the
    authored ``output:`` (the write happens only on the non-signal path). This
    stays fail-closed on a real signal (a marker on its own line still matches)
    while refusing to invent one from incidental text.
    """
    if not marker:
        return False
    # The marker must OWN its line, not merely begin it (review F-002). A
    # prefix-only match also fired on lines that extend the token into a
    # different word or sentence — "UPSTREAM CONFLICTS: none" (plural) or
    # "UPSTREAM CONFLICT resolved" — parking a step that emitted no genuine
    # signal. After the leading decoration + marker, allow only: a trailing
    # field colon ("MARKER: <reason>", the compact one-line form), or closing
    # Markdown decoration (`*`/`` ` ``/`#`) and whitespace to end-of-line.
    pattern = re.compile(
        rf"^[ \t#*>`\-]*{re.escape(marker)}(?=:|[ \t]*[*`#]*[ \t]*$)",
        re.MULTILINE,
    )
    return pattern.search(text or "") is not None


def _render_prompt(step: Step, ctx: StepContext) -> str:
    template_ref = step.get("prompt")
    if template_ref:
        template_path = ctx.repo_root / ctx.config.asset_root / template_ref
        base = template_path.read_text()
    else:
        base = step.get("prompt_text", "") or ""
    parts = [base]
    for name in step.get("inputs", []) or []:
        path = ctx.artifacts.get(name) or (ctx.artifact_root / name)
        content = Path(path).read_text() if Path(path).exists() else ""
        parts.append(f"\n\n--- input artifact: {name} ---\n{content}")
    if ctx.iteration_item is not None:
        item = ctx.iteration_item
        rendered = item if isinstance(item, str) else json.dumps(item, indent=2)
        parts.append(f"\n\n--- foreach item [{ctx.iteration_index}] ---\n{rendered}")
    return "".join(parts)


def _load_schema(step: Step, ctx: StepContext) -> dict | None:
    ref = step.get("findings_schema") or step.get("schema")
    if not ref:
        return None
    return json.loads((ctx.repo_root / ctx.config.asset_root / ref).read_text())


# --- commit (FR-9.2/9.7) -----------------------------------------------------
def handle_commit(step: Step, ctx: StepContext) -> StepResult:
    repo = ctx.repo_root
    # Narrow exclusion (review F-001): commit real artifacts (plan.md, outputs);
    # keep only the engine's own bookkeeping out of the commit and the checks.
    exclude = ctx.excludes
    message, draft_usage, draft_session, drafter = _commit_message(step, ctx)
    usage_by_agent = {drafter: draft_usage} if draft_usage and drafter else {}
    err = validate_commit_message(message)
    if err is not None:
        # message_agent drafting includes a bounded redraft loop in _draft;
        # a literal/exhausted message that still fails is a hard error.
        return StepResult(
            status=FAILED,
            usage=draft_usage,
            usage_by_agent=usage_by_agent,
            session_id=draft_session,
            notes=f"commit message invalid: {err.reason}",
        )
    prefix = header_prefix(message)

    # Mid-commit resume reconciliation (review F-003): if a prior attempt
    # already created the commit (HEAD moved off the recorded base) but died
    # before recording the SHA, adopt that commit rather than double-committing.
    base = ctx.record.base_sha
    if base and gitops.head_sha(repo) != base and gitops.is_clean(repo, exclude=exclude):
        existing = gitops.head_sha(repo)
        if header_prefix(gitops.commit_message(repo, existing)) == prefix:
            return StepResult(
                status=DONE,
                commit_sha=existing,
                commit_phase=prefix,
                usage=draft_usage,
                usage_by_agent=usage_by_agent,
                session_id=draft_session,
                notes="reconciled pre-existing commit after mid-commit interruption",
            )

    if gitops.is_clean(repo, exclude=exclude):
        return StepResult(
            status=FAILED,
            usage=draft_usage,
            usage_by_agent=usage_by_agent,
            session_id=draft_session,
            notes="commit step found a clean worktree with nothing to commit",
        )

    # Commit AUTHORSHIP is the implementer's, never the message drafter's
    # (FR-9.7, review F-003): a phase commit records the builder's work, so the
    # message_agent (typically `triage`) drafting the text must not bleed into
    # the commit identity — that mislabels implementation work as triage-
    # authored and breaks the builder/triage provenance split. An explicit
    # `agent:` on the commit step overrides; otherwise the builder authors it.
    agent_name = step.agent or "builder"
    identity = ctx.config.identity(agent_name)
    sha = gitops.commit_all(repo, message, identity=identity, exclude=exclude)
    return StepResult(
        status=DONE, commit_sha=sha, commit_phase=prefix,
        usage=draft_usage, usage_by_agent=usage_by_agent,
        session_id=draft_session, notes=f"committed {sha[:10]}",
    )


def _commit_message(step: Step, ctx: StepContext):
    """Return ``(message, usage, session_id, drafter)``; usage/session/drafter
    are None for a literal message (no model call)."""
    literal = step.get("message")
    if literal:
        return literal, None, None, None  # human-authored YAML; still validated
    return _draft_commit_message(step, ctx)


def _draft_commit_message(step: Step, ctx: StepContext):
    """Draft a commit message via the message_agent with bounded redraft.

    The agent sees the change as data — both the tracked diff AND the untracked
    files `git add -A` will sweep in (review F-008: a new-file phase otherwise
    drafts from an empty diff) — plus an optional plan section. The engine
    validates the format and asks for a redraft on violation (FR-9.2). Returns
    ``(message, usage, session_id, drafter)`` so the commit step records the
    drafter's cost (FR-3.2/§7).
    """
    agent_name = step.get("message_agent")
    if not agent_name:
        raise ValueError("commit step needs either `message:` or `message_agent:`")
    adapter = ctx.build_adapter(agent_name)
    change = _change_context(ctx)
    base_prompt = (
        (ctx.repo_root / ctx.config.asset_root / step.get("prompt")).read_text()
        if step.get("prompt")
        else _DEFAULT_COMMIT_PROMPT
    )
    # Phase prefix: an explicit `phase:` wins; otherwise, inside the
    # `foreach: plan.phases` fan-out, the iteration's phase id (P1, P2…) is the
    # required prefix, so each phase commit is labelled from the plan, not
    # left for the drafter to guess (FR-5.1 / FR-9.2).
    phase_hint = step.get("phase") or _iteration_phase(ctx)
    plan_section = _plan_section(step, ctx)
    header = (
        f"{base_prompt}\n\nRequired header phase prefix: {phase_hint or '(infer PN)'}\n"
        f"{plan_section}"
    )
    prompt = f"{header}\n{change}\n"
    max_redrafts = int(step.get("max_redrafts", 2))
    message = ""
    usage = _UsageAccumulator()  # sum across ALL draft attempts, incl. rejected
    session_id = None
    for _attempt in range(1 + max_redrafts):
        result = adapter.run(prompt, cwd=ctx.repo_root)
        usage.add(result.usage)  # a redraft's cost is real spend (F-008 round 2)
        session_id = result.session_id
        message = result.text.strip()
        if validate_commit_message(message) is None:
            return message, usage.result(), session_id, agent_name
        prompt = (
            f"{header}\n\nYour previous draft was rejected: "
            f"{validate_commit_message(message).reason}. "
            f"Return only the corrected commit message.\n{change}\n"
        )
    return message, usage.result(), session_id, agent_name


def _iteration_phase(ctx: StepContext) -> str:
    """The phase id (P1, P2…) of the current foreach item, if it carries one."""
    item = ctx.iteration_item
    if isinstance(item, dict):
        return str(item.get("id", "") or "")
    return ""


class _UsageAccumulator:
    """Sum Usage across calls so rejected drafts / sub-agent calls still count.

    Optionally tracks a per-agent breakdown (FR-3.2): pass ``agent=`` to
    :meth:`add` and the cycle's grand total and its per-profile split fall out
    of one accumulator (F-008 for redraft sums; per-agent for `gauntlet report`).
    """

    def __init__(self) -> None:
        self._in = 0
        self._out = 0
        self._cached = 0
        self._cost: float | None = None
        self._seen = False
        self._by_agent: dict[str, _UsageAccumulator] = {}

    def add(self, usage, *, agent: str | None = None) -> None:
        if usage is None:
            return
        self._seen = True
        self._in += usage.input_tokens or 0
        self._out += usage.output_tokens or 0
        self._cached += usage.cached_input_tokens or 0
        if usage.cost_usd is not None:
            self._cost = (self._cost or 0.0) + usage.cost_usd
        if agent is not None:
            self._by_agent.setdefault(agent, _UsageAccumulator()).add(usage)

    def result(self):
        from gauntlet.adapters.base import Usage

        if not self._seen:
            return None
        return Usage(
            input_tokens=self._in,
            output_tokens=self._out,
            cached_input_tokens=self._cached,
            cost_usd=self._cost,
        )

    def by_agent(self) -> dict:
        """Per-agent-profile Usage (FR-3.2); empty when no agent was tagged."""
        out = {}
        for name, acc in self._by_agent.items():
            r = acc.result()
            if r is not None:
                out[name] = r
        return out


def _change_context(ctx: StepContext) -> str:
    """The diff vs HEAD plus the untracked files staging will add (F-008)."""
    repo = ctx.repo_root
    diff = gitops.diff_head(repo, exclude=ctx.excludes)
    status = gitops.status_porcelain(repo, exclude=ctx.excludes)
    return (
        f"--- git status (incl. untracked) ---\n{status}\n"
        f"\n--- diff (tracked, vs HEAD) ---\n{diff}"
    )


def _plan_section(step: Step, ctx: StepContext) -> str:
    """Optional plan excerpt the message_agent drafts from (FR-9.2)."""
    ref = step.get("plan_section")
    if not ref:
        return ""
    path = ctx.artifacts.get(ref) or (ctx.artifact_root / ref)
    if Path(path).exists():
        return f"\n--- plan section: {ref} ---\n{Path(path).read_text()}\n"
    return ""


_DEFAULT_COMMIT_PROMPT = (
    "Draft a git commit message for the staged changes. Line 1: an imperative "
    "header prefixed with the phase, e.g. 'P3: <summary>', at most 72 chars. "
    "Then a blank line, then a body explaining what changed and why, the plan "
    "assumption validated, and relevant FR references."
)


# --- helpers -----------------------------------------------------------------
def _proc_log(command: str, proc: subprocess.CompletedProcess) -> str:
    return (
        f"$ {command}\n--- exit {proc.returncode} ---\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
    )


def step_log_dir(ctx: StepContext) -> Path:
    iteration = ctx.record.iteration
    leaf = ctx.record.id if iteration is None else f"{ctx.record.id}.{iteration}"
    return ctx.steps_dir() / leaf


def step_logger(ctx: StepContext, *subdir: str) -> StepLogger:
    """FR-4 logger for this step (or a sub-step, e.g. a cycle round's review)."""
    return StepLogger(ctx.writer, step_log_dir(ctx).joinpath(*subdir))


def _write_step_log(ctx: StepContext, name: str, text: str) -> None:
    ctx.writer.write_text(step_log_dir(ctx) / name, text)


SPECS: dict[str, StepSpec] = {
    "agent_task": StepSpec(
        type="agent_task",
        handler=handle_agent_task,
        needs_agent=True,
        # repo_write / touches_worktree are decided per-step (default True)
    ),
    "shell": StepSpec(
        type="shell",
        handler=handle_shell,
        touches_worktree=True,  # a test/build step can mutate the tree
    ),
    "human_gate": StepSpec(
        type="human_gate",
        handler=handle_human_gate,
    ),
    "commit": StepSpec(
        type="commit",
        handler=handle_commit,
        touches_worktree=True,
    ),
}


def _register_builtins() -> None:
    # Imported at the bottom: cycle.py / retro.py use this module's helpers
    # lazily, but registering here keeps adversarial_cycle and retrospective
    # built-ins (PRD §4.1 v1 step set).
    from gauntlet.engine.cycle import SPEC as _CYCLE_SPEC
    from gauntlet.engine.retro import SPEC as _RETRO_SPEC

    for spec in (_CYCLE_SPEC, _RETRO_SPEC):
        SPECS[spec.type] = spec


_register_builtins()

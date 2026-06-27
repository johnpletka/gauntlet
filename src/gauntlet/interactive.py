"""The interactive run monitor launcher (P3, FR-7 / FR-9).

One launcher, two entry points (P3 `run --interactive`, P4 `status
--interactive`): both resolve the run's per-run judge from ``judge.json`` and
the run driver's liveness, compose the **operator-session env** (§6.3), build
the FR-9.1 starter prompt routing the agent to the ``gauntlet-operator``
playbook, and ``exec`` the **bare interactive** ``claude`` / ``codex`` CLI in the
foreground — deliberately **not** the one-shot ``ClaudeCodeAdapter`` /
``CodexAdapter`` (which drive ``-p`` / ``codex exec`` non-interactively).

Fail closed (§7, FR-7.3/FR-8.2): the operator-session env is set **only** when
the run's ``judge.json`` is readable **and** ``engine.operator.driver_liveness``
reports the driver ``alive``. On any doubt — no record, driver not alive, or
``--no-judge`` — the monitor launches as a **normal prompted** session with an
explicit degraded note and **no** judge env, so it never runs ungated while
implying it is gated.

The launch vector is factored into a pure :func:`build_monitor_command` (review
F-002) so the argv/env/cwd/prompt contract is asserted directly — without running
an agent — and reused unchanged by both entry points. The monitor's ``cwd`` is
the **repo root** (the operator's own cwd, where ``gauntlet run`` was typed), so
the sanctioned ``gauntlet`` verbs the operator persona runs resolve
``.gauntlet/config.yaml`` (plan F-002, human-ratified amendment 8855546); the run
dir reaches the agent via the FR-9.1 starter prompt, not as cwd.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from gauntlet.engine.judgeproc import (
    _MANAGED_ENV_VARS,
    JudgeRecord,
    operator_session_env,
    read_judge_record,
)

# The two supported monitor agents. `claude` is the default and primary path;
# `codex` is wired the same way (the OQ-2 spike confirmed `codex [PROMPT]` takes
# an initial prompt in interactive mode, analogous to `claude "<prompt>"` —
# BOOTSTRAP-NOTES #53). Codex is never launched unseeded (FR-7.1).
DEFAULT_MONITOR_AGENT = "claude"
VALID_MONITOR_AGENTS = ("claude", "codex")

# Bounded wait for the run's judge to write `judge.json` after a detached launch
# (FR-7.3). The judge answers healthz and writes the record within its own
# startup window; if it never appears the monitor degrades to a prompted session.
DEFAULT_JUDGE_WAIT_S = 30.0
DEFAULT_POLL_INTERVAL_S = 0.25

# One-shot adapter tokens that must NEVER appear in an interactive monitor argv
# (review F-002): `-p`/`--print` (claude headless), `--output-schema` (codex
# structured), and the `exec` subcommand (codex non-interactive). The monitor is
# a bare interactive session; these would silently turn it into the one-shot
# adapter path and break the foreground hand-off.
_ONE_SHOT_TOKENS = frozenset({"-p", "--print", "--output-schema", "exec"})

# Claude loads a repo's project configuration — its `.claude/settings.json` (the
# judge PreToolUse hook) and its `.claude/skills/` (the `gauntlet-operator` skill
# the FR-9.1 starter prompt routes the monitor to) — only when the project
# setting-source is explicitly selected. This mirrors the claude builder/reviewer
# profiles' `base_flags` in `.gauntlet/config.yaml`, whose `--setting-sources
# project` is what makes claude load the repo hook (pins.yaml: "without it claude
# does not load the repo hook and the agent runs UNGATED"). The same mechanism
# scopes the skill — so the bare interactive monitor must carry the flag too, or
# the `gauntlet-operator` skill is out of scope and the session cannot run it
# (fix/gauntlet-operator-scope). claude-only: codex has no skills/settings-source
# concept and fires no PreToolUse hooks (BOOTSTRAP-NOTES #10).
_CLAUDE_PROJECT_SCOPE_FLAGS = ("--setting-sources", "project")


class MonitorAgentError(ValueError):
    """An unknown ``--interactive`` agent value (FR-7.1) — rejected before launch."""


class MonitorContractError(RuntimeError):
    """An interactive monitor argv carries a one-shot adapter token (review F-002)."""


@dataclass(frozen=True)
class MonitorCommand:
    """The launch vector for the interactive monitor (review F-002).

    The single source of truth for *how* the monitor is invoked, so the contract
    is testable without running an agent and identical across both entry points:

    - ``executable`` / ``argv`` — the bare interactive CLI invocation.
    - ``cwd`` — the **repo root** (the operator's own cwd), so the sanctioned
      ``gauntlet`` verbs the operator runs resolve ``.gauntlet/config.yaml``
      (plan F-002). The run dir reaches the agent via the starter prompt, not cwd.
    - ``env_overlay`` — the operator-session env (§6.3) to layer onto the parent
      environment, or **empty** on the degraded path (no judge env — never partial).
    - ``prompt_delivery`` — how the starter prompt reaches the agent
      (``"positional"`` for both claude and codex per the OQ-2 spike).
    """

    executable: str
    argv: list[str]
    cwd: Path
    env_overlay: dict[str, str]
    prompt_delivery: str


def validate_monitor_agent(agent: str) -> None:
    """Raise :class:`MonitorAgentError` for an unknown agent (FR-7.1).

    Called before any launch so an unknown ``--interactive=<value>`` errors
    naming the valid choices, never half-launching a run."""
    if agent not in VALID_MONITOR_AGENTS:
        raise MonitorAgentError(
            f"unknown monitor agent {agent!r}; valid choices: "
            f"{', '.join(VALID_MONITOR_AGENTS)}"
        )


def assert_interactive_argv(argv: list[str], *, prompt: str) -> None:
    """Guard: an interactive monitor argv must carry no one-shot adapter token.

    Defends the FR-7.2/FR-8 launch contract against a future edit that slips a
    ``-p`` / ``--print`` / ``--output-schema`` / ``exec`` into the vector — which
    would turn the bare interactive session into the non-interactive adapter
    path. The composed ``prompt`` is skipped (it is one argv element, free to
    contain any text); only the flag tokens are checked.
    """
    for tok in argv:
        if tok == prompt:
            continue
        if tok in _ONE_SHOT_TOKENS:
            raise MonitorContractError(
                f"interactive monitor argv must not contain the one-shot adapter "
                f"token {tok!r}; the monitor is a bare interactive CLI session, "
                f"not the {tok!r}-driven one-shot adapter path"
            )


def build_monitor_command(
    agent: str,
    *,
    prompt: str,
    repo_root: Path,
    judge_env: Mapping[str, str],
) -> MonitorCommand:
    """Build the interactive monitor launch vector (review F-002).

    The bare interactive invocation for ``agent`` with the starter ``prompt``
    delivered as the **interactive positional argument** — ``claude "<prompt>"``
    and ``codex "<prompt>"`` both start an interactive session seeded with the
    prompt (the OQ-2 spike confirmed codex ``[PROMPT]`` behaves like claude's;
    BOOTSTRAP-NOTES #53). ``cwd`` is the ``repo_root`` (the operator's own cwd)
    so the sanctioned ``gauntlet`` verbs resolve ``.gauntlet/config.yaml`` (plan
    F-002, human-ratified). ``judge_env`` is the operator-session env (§6.3) on
    the gated path, or empty on the degraded path. The guard rejects any one-shot
    adapter token before the command is returned.
    """
    validate_monitor_agent(agent)
    executable = agent  # "claude" / "codex" — the CLI name is the agent name
    # Bare interactive invocation with the prompt as the positional argument. For
    # claude, prepend `--setting-sources project` so the session loads this repo's
    # `.claude/` config — both the judge hook AND the `gauntlet-operator` skill the
    # starter prompt routes to. Without it the skill is not scoped to the repo and
    # the agent reports it cannot run it (fix/gauntlet-operator-scope). The prompt
    # stays the trailing positional. codex carries no such flag.
    scope_flags = list(_CLAUDE_PROJECT_SCOPE_FLAGS) if agent == "claude" else []
    argv = [executable, *scope_flags, prompt]
    assert_interactive_argv(argv, prompt=prompt)
    return MonitorCommand(
        executable=executable,
        argv=argv,
        cwd=Path(repo_root),
        env_overlay=dict(judge_env),
        prompt_delivery="positional",
    )


def compose_starter_prompt(slug: str, run_dir: Path, *, asset_root: str = ".") -> str:
    """The FR-9.1 monitor starter prompt routing the agent to the operator persona.

    States it is supervising ``gauntlet run <slug>``, names the run dir, directs
    it to monitor / explain parked-failed conditions / take operator-directed
    actions via the sanctioned ``gauntlet`` verbs, and routes it to the
    ``gauntlet-operator`` skill + its playbook (the state→action map). It names
    **no** autonomous push/merge action — the monitor is a supervised assistant,
    not a second pipeline (PRD §2.2).
    """
    # Lazy import: the skill registry pulls jsonschema/yaml; keep the module's
    # import surface light for the common (non-interactive) CLI path.
    from gauntlet.engine import skill

    playbook = skill.OPERATOR_SPEC.playbook_ref(asset_root)
    return (
        f"You are supervising `gauntlet run {slug}` as its interactive operator "
        f"(the `gauntlet-operator` persona). The run's artifacts live under:\n"
        f"  {run_dir}\n\n"
        "Your job:\n"
        f"- Monitor this run: read its state with `gauntlet status {slug} --json` "
        f"and `gauntlet logs {slug}`, and watch for it parking, failing, or "
        "stalling.\n"
        "- Explain parked gates, failed steps, and any outstanding questions in "
        "plain terms, and recommend the next action.\n"
        "- Take only the actions the operator explicitly directs, using the "
        "sanctioned `gauntlet` verbs a human would type (approve / reject / "
        "resume / abort / recover).\n\n"
        "Run every `gauntlet` verb as a SINGLE bare command — no pipes, no "
        "redirects, no `2>&1`, no `| head`/`| tail`. The judge fast-path allows "
        "`gauntlet` only as a standalone command (a chained command is skipped "
        "and escalates to the classifier, which will DENY it). You don't need to "
        "pipe: `gauntlet logs` already tails its output and `gauntlet status "
        "--json` is small. Read long output by paging the command again or with "
        "a follow-up flag, never by piping.\n\n"
        f"Route yourself through the `{skill.OPERATOR_SKILL_NAME}` skill and its "
        f"playbook at `{playbook}` for the run-state → action map and the "
        "gate decisions.\n\n"
        "You are a supervised assistant, not a second pipeline: act only on the "
        "operator's explicit direction, and take no autonomous action on your own "
        "(you open no pull requests and merge nothing without being told)."
    )


def _await_judge_record(
    run_dir: Path,
    *,
    timeout_s: float,
    poll_interval_s: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> JudgeRecord | None:
    """Poll for a readable ``judge.json`` up to ``timeout_s`` (FR-7.3).

    Returns the record the instant it is readable, or ``None`` when the bounded
    wait elapses (the run's judge never came up / wrote its record). A
    ``timeout_s`` of ``0`` reads exactly once."""
    deadline = monotonic() + timeout_s
    while True:
        record = read_judge_record(run_dir)
        if record is not None:
            return record
        if monotonic() >= deadline:
            return None
        sleep(poll_interval_s)


def _default_liveness(run_root: Path, slug: str) -> str:
    from gauntlet.engine import operator

    return operator.driver_liveness(run_root, slug)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr)


def launch_monitor(
    *,
    repo_root: Path,
    run_root: Path,
    slug: str,
    run_dir: Path,
    agent: str = DEFAULT_MONITOR_AGENT,
    use_judge: bool = True,
    asset_root: str = ".",
    judge_wait_s: float = DEFAULT_JUDGE_WAIT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    liveness_fn: Callable[[Path, str], str] | None = None,
    exec_fn: Callable[[str, list[str], Mapping[str, str]], None] = os.execvpe,
    chdir_fn: Callable[[Path], None] = os.chdir,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    echo: Callable[[str], None] = _stderr,
) -> None:
    """Foreground the interactive monitor for a run (FR-7.3 / FR-8.2).

    Fail closed: composes the operator-session env (§6.3) **only when both** the
    run's ``judge.json`` is readable (after a bounded wait) **and** the driver is
    verified ``alive``; otherwise launches a **normal prompted** session with an
    explicit degraded note and **no** judge env. ``exec``s the bare interactive
    CLI in the foreground from the **repo root** (so the operator's ``gauntlet``
    verbs resolve config); the detached run keeps running. ``exec_fn`` replaces
    the process on success and does not return.
    """
    validate_monitor_agent(agent)  # fail before any wait/launch (FR-7.1)

    # The judge env rides ONLY on a readable record + a verified-alive driver
    # (FR-7.3). Under --no-judge we never even look — there is no judge to wire.
    record = (
        _await_judge_record(
            run_dir,
            timeout_s=judge_wait_s,
            poll_interval_s=poll_interval_s,
            monotonic=monotonic,
            sleep=sleep,
        )
        if use_judge
        else None
    )
    liveness = (liveness_fn or _default_liveness)(run_root, slug)
    gated = use_judge and record is not None and liveness == "alive"

    if gated:
        judge_env = operator_session_env(record)
        echo(
            f"monitor: wired to run {slug}'s judge as the operator's own session "
            "(gated, no permission prompts)."
        )
    else:
        judge_env = {}
        if not use_judge:
            reason = "judge disabled (--no-judge)"
        elif record is None:
            reason = "no readable judge.json (the run's judge is not discoverable)"
        else:
            reason = f"the run's driver is not alive (liveness: {liveness})"
        echo(
            f"monitor: DEGRADED — {reason}; launching a normal prompted "
            "session (no judge env, normal permission handling)."
        )

    prompt = compose_starter_prompt(slug, run_dir, asset_root=asset_root)
    command = build_monitor_command(
        agent, prompt=prompt, repo_root=repo_root, judge_env=judge_env
    )
    # Scrub every engine-managed GAUNTLET_* var from the parent env BEFORE layering
    # the overlay (review F-001). The parent process (a `gauntlet run` driver or an
    # operator shell with stale judge vars) may carry GAUNTLET_RUN_ID / JUDGE_URL /
    # JUDGE_TOKEN / STEP_ID / MODE / REPO_ROOT; merging the overlay on top does not
    # remove them. Without scrubbing, the degraded path (empty overlay) would still
    # carry judge env — violating §6.3's "no judge env in degraded mode" — and the
    # gated path would inherit a parent GAUNTLET_STEP_ID, causing the judge to
    # classify the operator's own session as an in-run agent (FR-7.3 / FR-10).
    env = {k: v for k, v in os.environ.items() if k not in _MANAGED_ENV_VARS}
    env.update(command.env_overlay)
    # Run the agent from the repo root (the operator's own cwd) so the sanctioned
    # `gauntlet` verbs it runs resolve `.gauntlet/config.yaml` (plan F-002).
    chdir_fn(command.cwd)
    exec_fn(command.executable, command.argv, env)


__all__ = [
    "DEFAULT_MONITOR_AGENT",
    "VALID_MONITOR_AGENTS",
    "MonitorAgentError",
    "MonitorContractError",
    "MonitorCommand",
    "validate_monitor_agent",
    "assert_interactive_argv",
    "build_monitor_command",
    "compose_starter_prompt",
    "launch_monitor",
]

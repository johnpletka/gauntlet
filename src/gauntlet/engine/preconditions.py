"""Plan-declared environmental preconditions (issue #134, recommendation 7).

A plan's ``gauntlet-phases`` block may declare, at the top level (the whole
plan) and per phase, the *environmental* preconditions the phases depend on:
data files that must be staged, environment variables that must be set, and
provisioning commands that must succeed. In the field a phase parked mid-run
because a data file the plan relied on had never been staged — a fact that was
discoverable before any agent was launched. This module is the deterministic
check that discovers it:

* **at the plan gate** — ``gauntlet approve`` on a ``preflight:
  plan_preconditions`` gate refuses (gate stays parked, nothing stamped) while
  any item is unmet, and ``gauntlet status`` lists the unmet items as an
  advisory;
* **before each implement phase** — a step carrying ``preconditions_from:
  plan`` re-resolves the plan-level items plus its own phase's before the
  handler runs; an unmet item fails the step as a re-runnable precondition
  (no agent invoked, no tokens spent).

The mechanism is deliberately generic — ``path`` / ``env`` / ``command`` are
the only kinds — so an adopter repository's own notion of "the bundle is
staged" is expressed as a path or a command, never as a Gauntlet concept.

Determinism over cleverness (§2): :func:`resolve_preconditions` is a pure
function over an injected environment and an injected command runner, checks
every item (never short-circuits), and reports unmet items in declaration
order. Fail closed: a malformed item is a spec error at parse time, so an
unknown key or shape can never silently pass as "nothing to check".

Secrets: an ``env`` item's VALUE is never read into a message, log or manifest
— only its name and whether it is set/non-empty (the ``doctor`` discipline,
FR-1.4).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The three precondition kinds — the discriminating key of a declared item.
KIND_PATH = "path"
KIND_ENV = "env"
KIND_COMMAND = "command"
KINDS = (KIND_PATH, KIND_ENV, KIND_COMMAND)

# Keys each kind admits. Anything else is an unknown key → fail closed.
_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    KIND_PATH: frozenset({KIND_PATH, "description"}),
    KIND_ENV: frozenset({KIND_ENV, "description"}),
    KIND_COMMAND: frozenset({KIND_COMMAND, "description", "timeout_s"}),
}

# The scope label for items declared at the block's top level.
PLAN_SCOPE = "plan"

# A `command` item with no `timeout_s` still gets a bound: a preflight that can
# hang forever is worse than the mid-run park it exists to prevent.
DEFAULT_COMMAND_TIMEOUT_S = 600

# Pipeline option values (validated at load in engine/validate.py).
#   human_gate `preflight: plan_preconditions` — `gauntlet approve` runs the
#   resolver over every plan item before approving (issue #134).
#   any step `preconditions_from: plan` — the orchestrator re-resolves the
#   plan-level items plus the current foreach phase's before the handler runs.
PREFLIGHT_PLAN_PRECONDITIONS = "plan_preconditions"
PRECONDITIONS_FROM_PLAN = "plan"

# POSIX-portable environment variable name.
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# How much of a failed command's output travels in a note/message. The full
# output is persisted to a file by the command runner; notes carry a tail only.
OUTPUT_TAIL_LINES = 5
OUTPUT_TAIL_WIDTH = 200


class PreconditionSpecError(ValueError):
    """A declared precondition item is malformed (unknown shape or key)."""


@dataclass(frozen=True)
class Precondition:
    """One declared, shape-validated precondition item."""

    kind: str  # path | env | command
    target: str  # the path, the env var NAME, or the shell command
    scope: str  # PLAN_SCOPE or the declaring phase id (e.g. "P2")
    description: str | None = None
    timeout_s: int | None = None  # command only; None → DEFAULT_COMMAND_TIMEOUT_S

    @property
    def effective_timeout_s(self) -> int:
        return self.timeout_s if self.timeout_s is not None else DEFAULT_COMMAND_TIMEOUT_S

    def label(self) -> str:
        """A one-line, secret-free rendering: kind + target + scope (+ description)."""
        target = f"`{self.target}`" if self.kind == KIND_COMMAND else self.target
        text = f"{self.kind} {target} [{self.scope}]"
        if self.description:
            text += f" — {self.description}"
        return text


@dataclass(frozen=True)
class CommandOutcome:
    """What a command runner reports back for one ``command`` item."""

    returncode: int | None  # None when the command did not finish (timeout)
    output_tail: str = ""  # a short, already-truncated tail of stdout+stderr
    timed_out: bool = False
    evidence: str | None = None  # where the full output was persisted, if anywhere


# The injected command executor: (item, cwd, env) -> outcome. Injected so the
# resolver stays pure and unit tests never spawn a shell.
RunCommand = Callable[[Precondition, Path, Mapping[str, str]], CommandOutcome]


@dataclass(frozen=True)
class Unmet:
    """One unmet precondition: the item and, precisely, why."""

    item: Precondition
    reason: str

    def render(self) -> str:
        return f"{self.item.label()} — {self.reason}"


# --- shape validation --------------------------------------------------------
def parse_preconditions(raw: Any, *, scope: str) -> list[Precondition]:
    """Validate a declared ``preconditions:`` list; raise on any bad shape.

    ``scope`` names where the list was declared (:data:`PLAN_SCOPE` or a phase
    id) so every error message and every rendered item says which phase owns
    it. Fail closed (§2): not-a-list, a non-mapping entry, an entry declaring
    zero or several kinds, an unknown key, a wrong-typed value, or a
    ``timeout_s`` on a non-command item all raise :class:`PreconditionSpecError`
    with a message precise enough to fix the plan from.
    """
    where = "plan-level" if scope == PLAN_SCOPE else f"phase {scope}"
    if not isinstance(raw, list):
        raise PreconditionSpecError(
            f"{where} 'preconditions' must be a list of {{path|env|command, "
            f"description?}} items, got {type(raw).__name__}"
        )
    items: list[Precondition] = []
    for i, entry in enumerate(raw):
        label = f"{where} precondition #{i + 1}"
        if not isinstance(entry, dict):
            raise PreconditionSpecError(f"{label} is not a mapping: {entry!r}")
        kinds = [k for k in KINDS if k in entry]
        if len(kinds) != 1:
            raise PreconditionSpecError(
                f"{label} must declare exactly one of 'path', 'env' or "
                f"'command' (found {kinds or 'none'}): {entry!r}"
            )
        kind = kinds[0]
        unknown = sorted(set(entry) - _ALLOWED_KEYS[kind])
        if unknown:
            raise PreconditionSpecError(
                f"{label} ({kind}) has unknown key(s) {unknown}; allowed keys "
                f"for a '{kind}' item are {sorted(_ALLOWED_KEYS[kind])}"
            )
        target = entry[kind]
        if not isinstance(target, str) or not target.strip():
            raise PreconditionSpecError(
                f"{label}: '{kind}' must be a non-empty string, got {target!r}"
            )
        target = target.strip()
        if kind == KIND_ENV and not _ENV_NAME_RE.match(target):
            raise PreconditionSpecError(
                f"{label}: 'env' must be an environment variable NAME "
                f"(letters, digits, underscores), got {target!r}"
            )
        description = entry.get("description")
        if description is not None and (
            not isinstance(description, str) or not description.strip()
        ):
            raise PreconditionSpecError(
                f"{label}: 'description' must be a non-empty string when present"
            )
        timeout = entry.get("timeout_s")
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
        ):
            raise PreconditionSpecError(
                f"{label}: 'timeout_s' must be a positive integer, got {timeout!r}"
            )
        items.append(
            Precondition(
                kind=kind,
                target=target,
                scope=scope,
                description=description.strip() if description else None,
                timeout_s=timeout,
            )
        )
    return items


# --- resolution --------------------------------------------------------------
def resolve_preconditions(
    items: Sequence[Precondition],
    *,
    cwd: Path,
    env: Mapping[str, str],
    run_command: RunCommand | None,
) -> list[Unmet]:
    """Check every item; return the unmet ones in declaration order.

    Pure over its inputs: ``cwd`` anchors relative ``path`` items (the run
    worktree — the tree the phases actually run in) and is the working
    directory handed to ``run_command``; ``env`` answers ``env`` items (a value
    is only ever tested for presence/emptiness, never returned); ``run_command``
    executes ``command`` items. ``run_command=None`` is the READ-ONLY mode
    (``gauntlet status``): command items are not executed and are not reported
    — the caller says so explicitly. Every item is checked (no short-circuit)
    so one refusal lists everything the operator has to fix.
    """
    unmet: list[Unmet] = []
    for item in items:
        if item.kind == KIND_PATH:
            reason = _check_path(item, cwd)
        elif item.kind == KIND_ENV:
            reason = _check_env(item, env)
        elif item.kind == KIND_COMMAND:
            if run_command is None:
                continue
            reason = _check_command(item, cwd, env, run_command)
        else:  # unreachable for parsed items; fail closed for a hand-built one
            reason = f"unknown precondition kind {item.kind!r}"
        if reason is not None:
            unmet.append(Unmet(item, reason))
    return unmet


def _check_path(item: Precondition, cwd: Path) -> str | None:
    path = Path(item.target)
    resolved = path if path.is_absolute() else cwd / path
    if resolved.exists():
        return None
    return f"missing: {resolved}"


def _check_env(item: Precondition, env: Mapping[str, str]) -> str | None:
    # Presence and emptiness only — the value never leaves this frame.
    value = env.get(item.target)
    if value is None:
        return f"environment variable {item.target} is not set"
    if not value.strip():
        return f"environment variable {item.target} is set but empty"
    return None


def _check_command(
    item: Precondition, cwd: Path, env: Mapping[str, str], run_command: RunCommand
) -> str | None:
    try:
        outcome = run_command(item, cwd, env)
    except OSError as exc:  # could not even spawn it — unmet, never a crash
        return f"could not run: {exc}"
    if outcome.timed_out:
        reason = f"timed out after {item.effective_timeout_s}s"
    elif outcome.returncode == 0:
        return None
    else:
        reason = f"exited {outcome.returncode}"
    if outcome.output_tail:
        reason += f"; output tail: {outcome.output_tail}"
    if outcome.evidence:
        reason += f" (full output: {outcome.evidence})"
    return reason


def command_items(items: Sequence[Precondition]) -> list[Precondition]:
    """The ``command`` items of a list (what read-only mode does not run)."""
    return [item for item in items if item.kind == KIND_COMMAND]


# --- the real command runner ---------------------------------------------------
def output_tail(
    text: str, *, lines: int = OUTPUT_TAIL_LINES, width: int = OUTPUT_TAIL_WIDTH
) -> str:
    """The last ``lines`` non-empty lines of ``text``, each clipped to ``width``,
    joined with ``" | "`` so it fits on one note line."""
    kept = [ln.strip() for ln in text.splitlines() if ln.strip()][-lines:]
    clipped = [ln if len(ln) <= width else ln[: width - 1] + "…" for ln in kept]
    return " | ".join(clipped)


def run_shell_command(
    item: Precondition, cwd: Path, env: Mapping[str, str]
) -> tuple[CommandOutcome, str]:
    """Execute one ``command`` item exactly the way a ``shell`` step runs.

    ``shell=True`` in ``cwd`` (the run worktree) with the given environment,
    output captured, bounded by the item's effective timeout. Returns the
    outcome plus the FULL captured log text (``$ cmd`` header, exit, stdout,
    stderr) for the caller to persist — this function never writes.
    """
    command = item.target
    timeout = item.effective_timeout_s
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        partial = _decode(exc.stdout) + _decode(exc.stderr)
        log = f"$ {command}\n--- TIMEOUT after {timeout}s ---\n{partial}"
        return CommandOutcome(None, output_tail(partial), timed_out=True), log
    log = (
        f"$ {command}\n--- exit {proc.returncode} ---\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}\n"
    )
    outcome = CommandOutcome(
        proc.returncode, output_tail(proc.stdout + "\n" + proc.stderr)
    )
    return outcome, log


def _decode(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data)


def command_runner(log_dir: Path, writer) -> RunCommand:
    """A :data:`RunCommand` that persists each command's full output.

    Output lands under ``log_dir`` as ``command-<n>.txt`` (through the run's
    redacting ``writer``, so a command that echoes a secret is redacted on
    disk like every other transcript); the outcome names that file as its
    evidence so a note can point at it instead of inlining the output.
    """
    counter = {"n": 0}

    def _run(item: Precondition, cwd: Path, env: Mapping[str, str]) -> CommandOutcome:
        counter["n"] += 1
        outcome, log = run_shell_command(item, cwd, env)
        path = log_dir / f"command-{counter['n']}.txt"
        try:
            writer.write_text(path, log)
        except OSError:
            return outcome  # evidence is best-effort; the verdict is not
        return CommandOutcome(
            outcome.returncode, outcome.output_tail, outcome.timed_out,
            evidence=str(path),
        )

    return _run


def render_checklist(
    items: Sequence[Precondition], unmet: Sequence[Unmet]
) -> str:
    """A ``preflight.txt``-style record of every checked item and its verdict."""
    failed = {id(u.item): u for u in unmet}
    lines = []
    for item in items:
        hit = failed.get(id(item))
        lines.append(f"[{'UNMET' if hit else 'ok'}] {hit.render() if hit else item.label()}")
    return "\n".join(lines) + "\n"

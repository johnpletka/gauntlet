"""Collector registry + side-effect-free test-id enumeration (FR-3.2).

A *collector* answers one question deterministically: **which testable ids does
this project actually have?** The ``acceptance_gate`` (steptypes.py) cites the
acceptance-map's evidence ids against a collector's enumeration to prove
*citation + existence* — that every clause maps to an id the collector really
enumerates. (Whether a cited test *meaningfully* exercises its clause —
sufficiency — is the spec-coverage review lens's job, not this gate's; G2 is
scoped accordingly.)

v1 registers exactly one collector, ``pytest`` (``pytest --collect-only``). The
registry *namespace* exists so a future collector plugin can widen the enum, but
a ``kind`` with no registered collector is **schema-invalid / load-rejected**,
never a runtime surprise (there is no declare-but-unimplemented path in v1).

Collector-execution threat model + P2-P4 interim posture (plan P2, review F-002).
``pytest --collect-only`` is **not** inert: pytest collection imports
``conftest.py`` and every test module from the branch under review, so it
*executes branch-authored code at import time*. The P5 verifier sandbox is the
isolation backend for branch-code execution and does not exist until P5, so P2
must not run collection wide-open in the interim. Until the P5 migration, every
enumeration runs under a **fail-closed interim mitigation**:

* a **bounded child subprocess** (never an in-process import — that would run the
  branch's ``conftest``/test modules inside the engine process);
* under the run's **active judge ``PreToolUse`` hooks** — the ``GAUNTLET_JUDGE_*``
  env is forwarded to the child, so any tool call the enumeration itself spawns is
  gated by the same judge protecting every other engine-driven command in this run;
* with its **working directory scoped to the run worktree**;
* under a **wall-clock timeout and a process resource limit**.

A non-zero collector exit, a timeout, or an unparseable/empty enumeration
**fails closed** (:class:`CollectorEnumerationError`) — an absent or failed
enumeration is *never* treated as "every cited id exists / all clauses mapped".
The P5 migration moves this enumeration *inside* the verifier sandbox backend
(plan P5, review F-002); the interim judge-hooked subprocess is the compensating
control until it lands.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# POSIX gates the resource-limit preexec (setrlimit lives in the `resource`
# module, absent on Windows). A non-POSIX host still gets the wall-clock bound.
_POSIX = os.name == "posix"

# Interim-posture bounds (plan P2). Deliberately generous — the point is to cap a
# runaway collection (an import that hangs or forks unbounded), not to make a
# healthy collection fail. Both are overridable per acceptance_gate step.
DEFAULT_ENUMERATION_TIMEOUT_S = 120.0
# Address-space cap (best-effort; see _rlimit_preexec). 4 GiB leaves ample room
# for a real pytest collection while still bounding a pathological one.
DEFAULT_ENUMERATION_MEM_BYTES = 4 * 1024**3


class CollectorError(Exception):
    """Base class for collector faults."""


class UnknownCollectorError(CollectorError):
    """A ``kind`` with no registered collector — a config/artifact fault.

    Fail closed: an unregistered kind is rejected structurally (at pipeline load
    and by the acceptance-map schema's closed ``kind`` enum), never run and then
    parked, so an unsupported collector cannot masquerade as supported.
    """


class CollectorEnumerationError(CollectorError):
    """Enumeration failed closed: non-zero exit, timeout, or unparseable output.

    The gate turns this into a fail-closed park — an absent/failed enumeration is
    never read as "all cited ids exist".
    """


def _enumeration_env(judge_env: dict[str, str]) -> dict[str, str]:
    """Child env for the interim posture: inherit the parent env + the run's
    judge ``GAUNTLET_JUDGE_*`` vars, so the enumeration subprocess runs under the
    same active judge hooks as every other engine-driven command (plan P2).

    ``PYTHONDONTWRITEBYTECODE=1`` keeps enumeration **filesystem-side-effect-free**:
    pytest collection imports every test module, which would otherwise write
    ``__pycache__/*.pyc`` into the worktree and dirty the tree the gate runs on —
    breaking the clean-handoff invariant (FR-9.3) before the review cycle. A
    side-effect-free enumeration must not mutate the worktree it inspects.
    """
    return {**os.environ, **(judge_env or {}), "PYTHONDONTWRITEBYTECODE": "1"}


def _rlimit_preexec(mem_bytes: int, cpu_seconds: int) -> Callable[[], None]:
    """Build a POSIX ``preexec_fn`` applying the interim resource bounds.

    Both limits are best-effort: some platforms (notably macOS) do not honor
    ``RLIMIT_AS`` and a too-tight address-space cap can break a legitimate
    interpreter launch, so a failure to set a limit is swallowed rather than
    aborting the (already wall-clock-bounded) enumeration. ``RLIMIT_CPU`` gives a
    hard compute ceiling independent of the wall-clock timeout.
    """
    import resource

    def _apply() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        except (ValueError, OSError):
            pass
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass

    return _apply


def run_bounded_enumeration(
    command: list[str],
    *,
    worktree: Path,
    judge_env: dict[str, str],
    timeout_s: float = DEFAULT_ENUMERATION_TIMEOUT_S,
    mem_bytes: int = DEFAULT_ENUMERATION_MEM_BYTES,
) -> str:
    """Run a side-effect-free enumeration command under the interim posture.

    Returns the command's stdout on a clean (exit 0) run. Raises
    :class:`CollectorEnumerationError` on timeout or non-zero exit — the two
    fail-closed terminals of P2-A6. The child runs with ``cwd`` scoped to the run
    worktree, the run's judge env forwarded, and a wall-clock + resource bound.
    """
    env = _enumeration_env(judge_env)
    preexec = (
        _rlimit_preexec(mem_bytes, math.ceil(timeout_s) + 5) if _POSIX else None
    )
    try:
        proc = subprocess.run(
            list(command),
            cwd=str(worktree),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            preexec_fn=preexec,
        )
    except subprocess.TimeoutExpired:
        raise CollectorEnumerationError(
            f"collector enumeration timed out after {timeout_s}s "
            "(fail closed — an absent enumeration is not 'all mapped')"
        ) from None
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        detail = " / ".join(line.strip() for line in tail) or "(no output)"
        raise CollectorEnumerationError(
            f"collector enumeration exited {proc.returncode} (fail closed): {detail}"
        )
    return proc.stdout


# A pytest ``--collect-only -q`` node id line: a path ending in ``.py`` optionally
# followed by ``::<node path>``. Anchored end-to-end so the trailing summary
# ("3 tests collected in 0.01s"), warning lines, and blanks never parse as ids.
_PYTEST_NODEID_RE = re.compile(r"^\S.*\.py(::\S.*)?$")


def _parse_pytest(stdout: str) -> set[str]:
    """Parse ``pytest --collect-only -q`` stdout into a set of node ids."""
    return {
        line.strip()
        for raw in stdout.splitlines()
        if _PYTEST_NODEID_RE.match(line := raw.strip())
    }


@dataclass(frozen=True)
class Collector:
    """A registered collector: its kind + a side-effect-free listing command.

    ``command`` is engine-owned (checked into this module, never agent-authored),
    so the interim posture never interpolates untrusted text into a command line.
    """

    kind: str
    command: tuple[str, ...]
    parse: Callable[[str], set[str]] = field(repr=False)

    def enumerate(
        self,
        *,
        worktree: Path,
        judge_env: dict[str, str],
        timeout_s: float = DEFAULT_ENUMERATION_TIMEOUT_S,
        mem_bytes: int = DEFAULT_ENUMERATION_MEM_BYTES,
    ) -> set[str]:
        """Enumerate this collector's ids under the interim posture.

        Raises :class:`CollectorEnumerationError` on a failed/timed-out run OR on
        an empty/unparseable enumeration (fail closed — a clean exit that yields
        no parseable ids is not "there are no ids", it is "we could not read them").
        """
        stdout = run_bounded_enumeration(
            list(self.command),
            worktree=worktree,
            judge_env=judge_env,
            timeout_s=timeout_s,
            mem_bytes=mem_bytes,
        )
        ids = self.parse(stdout)
        if not ids:
            raise CollectorEnumerationError(
                f"{self.kind} enumeration produced no parseable ids (fail closed — "
                "an unparseable enumeration is never treated as 'all mapped')"
            )
        return ids


# The v1 collector registry. v1 ships exactly `pytest`; the acceptance-map
# schema's `kind` enum and the pipeline-load check both key off REGISTERED_KINDS,
# so an unregistered kind is caught structurally, never run.
COLLECTORS: dict[str, Collector] = {
    "pytest": Collector(
        kind="pytest",
        # `sys.executable -m pytest` uses the run's own interpreter (which has
        # pytest), not an arbitrary `python` on PATH. `-q` yields flat node ids
        # (one per line); `-p no:cacheprovider` avoids a cache-write side effect.
        command=(sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"),
        parse=_parse_pytest,
    ),
}

# The closed set of registered collector kinds — the single source the schema
# enum, the pipeline-load validator, and the gate all consult.
REGISTERED_KINDS: tuple[str, ...] = tuple(sorted(COLLECTORS))


def is_registered(kind: str) -> bool:
    return kind in COLLECTORS


def get_collector(kind: str) -> Collector:
    try:
        return COLLECTORS[kind]
    except KeyError:
        raise UnknownCollectorError(
            f"unknown collector kind {kind!r}; v1 registers only "
            f"{list(REGISTERED_KINDS)} (widening the enum requires registering "
            "that collector's plugin — no declare-but-unimplemented path)"
        ) from None

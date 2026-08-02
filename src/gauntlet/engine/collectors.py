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

Collector-execution threat model + posture (PR #59 review F3/F4, superseding
the P2-P4 interim posture and the P5 LLM-mediated migration). ``pytest
--collect-only`` is **not** inert: collection imports ``conftest.py`` and every
test module from the branch under review, so it *executes branch-authored code
at import time*. Enumeration therefore runs as a **bounded engine subprocess in
a DISPOSABLE COPY of the worktree** — deterministic (no LLM anywhere in the
evidence path: the P5 design routed stdout through a claude turn asked to echo
it "byte for byte", which could both truncate a large id list into a chronic
false park and fabricate ids into a false pass) — with:

* a **stripped environment** (the verifier's allowlist rebuild — no secrets,
  no judge vars; the child needs no hook because it is not an agent);
* its **working directory scoped to the disposable copy**, so import-time
  filesystem side effects land in a tree that is discarded;
* a **wall-clock timeout and best-effort resource limits** (rlimit CPU/AS).

Residual (documented, PRD v0.4 §7): an engine subprocess is not hook-gated, so
import-time network egress by branch conftest code is bounded only by the strip
+ copy + rlimits — the same subprocess-boundary residual the verifier's forked
children carry.

The enumeration **command** is resolved per project (:func:`resolve_command`,
review F4): an explicit ``collectors.<kind>.command`` config wins; else a
pytest-based ``test_command`` (e.g. ``uv run pytest``) supplies the project's
own test environment; else the engine interpreter (which requires pytest
importable there — gauntlet's own dev layout, not a pipx install).

A non-zero collector exit, a timeout, or an unparseable/empty enumeration
**fails closed** (:class:`CollectorEnumerationError`) — an absent or failed
enumeration is *never* treated as "every cited id exists / all clauses mapped".
"""

from __future__ import annotations

import math
import os
import re
import shlex
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
    """Child env for enumeration: the verifier's STRIPPED allowlist rebuild
    (PR #59 review F3 — previously this merged the engine's whole ``os.environ``
    plus the judge vars, handing every credential-shaped var to branch-authored
    conftest code at import time). No judge vars either: the child is an engine
    subprocess, not an agent — there is no hook to authenticate.

    ``PYTHONDONTWRITEBYTECODE=1`` (set by the rebuild) keeps enumeration
    filesystem-side-effect-lean; import-time writes that do happen land in the
    disposable copy the gate runs it in, never the run worktree. ``judge_env``
    is accepted for call-site compatibility and deliberately unused.
    """
    from gauntlet.engine.verify import build_sandbox_env

    del judge_env  # engine subprocess: stripped env, no hook, no judge vars
    return build_sandbox_env()


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
        command: tuple[str, ...] | None = None,
    ) -> set[str]:
        """Enumerate this collector's ids as a bounded engine subprocess.

        ``command`` overrides the registry default with the project-resolved
        command (:func:`resolve_command`); ``worktree`` should be the disposable
        copy the gate created. Raises :class:`CollectorEnumerationError` on a
        failed/timed-out run OR on an empty/unparseable enumeration (fail
        closed — a clean exit that yields no parseable ids is not "there are no
        ids", it is "we could not read them").
        """
        stdout = run_bounded_enumeration(
            list(command or self.command),
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


# Flags every pytest-shaped enumeration appends: flat one-per-line node ids,
# no cache-write side effect.
_PYTEST_COLLECT_FLAGS: tuple[str, ...] = (
    "--collect-only", "-q", "-p", "no:cacheprovider",
)


def resolve_command(collector: Collector, config) -> tuple[str, ...]:
    """The enumeration command for THIS project (PR #59 review F4).

    Resolution order, all engine/operator-owned (never agent-authored):

    1. An explicit ``collectors.<kind>.command`` in the run config — the
       operator's word, taken verbatim (string → shlex-split).
    2. For ``pytest``: derive from the project's ``test_command`` when it is
       pytest-shaped (``uv run pytest`` → ``uv run pytest --collect-only …``),
       so enumeration runs in the SAME environment the tests do. The previous
       hard-coded ``sys.executable -m pytest`` used the *engine's* interpreter:
       pytest is not a runtime dependency of gauntlet, so every pipx/uv-tool
       install — every adopter repo — parked the gate with ``No module named
       pytest``.
    3. The registry default (engine interpreter) — correct for gauntlet's own
       dev layout, where the engine runs from the repo venv.
    """
    entries = getattr(config, "collectors", None) or {}
    entry = entries.get(collector.kind) if isinstance(entries, dict) else None
    override = getattr(entry, "command", None) if entry is not None else None
    if override:
        parts = shlex.split(override) if isinstance(override, str) else list(override)
        if parts:
            return tuple(parts)
    if collector.kind == "pytest":
        test_command = getattr(config, "test_command", None) or ""
        if re.search(r"\bpytest\b", test_command):
            return tuple(shlex.split(test_command)) + _PYTEST_COLLECT_FLAGS
    return collector.command


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

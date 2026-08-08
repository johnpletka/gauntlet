"""Is the run worktree actually writable by the agents that will drive it? (P7f)

`proposals/P7d-gate-blocker.md` §5 Option 4. The engine could not tell "the agent
chose not to write" from "the agent was refused", and that blindness is what let
a whole dogfood run fail while every durable record said the writes were
allowed.

**Why the engine cannot see the refusal on its own (§2.4).** Gauntlet's judge is
a **PreToolUse** hook: it adjudicates *before* the CLI applies its own permission
rules. So `judge-audit.jsonl` faithfully records `Write … allow` for a write that
the CLI then refused and that never happened. The only engine-visible symptom was
the fail-closed "the fixer made no changes in round 1 despite 7 accepted
finding(s)", which reads as a model failure. Every probe here therefore takes its
verdict from the **filesystem after the turn**, never from the adapter's output
and never from the hook.

**Why the probe must name each mechanism (§2.3).** The guard is not uniform.
Measured under `.git/`: `Write` and `Edit` refused unconditionally, `printf >
file` refused, but `cat > file <<EOF` and `tee file` permitted. A probe that
asks a model to "write a file here" therefore inherits precisely the
nondeterminism it exists to detect — it can pass by choosing `tee` while the
real task later fails on `Write`, or fail merely because it chose a blocked
form. A stochastic probe for a stochastic failure proves nothing in either
direction. So each mechanism gets its own turn, named, with the tools restricted
so the model has no choice left to make.

**Why it never generalizes between adapters (§2.5 probe 2).** `codex` is not
subject to this guard and `claude` is. A check that probed whichever adapter was
convenient would report the tree healthy while every `claude` builder and
verifier step failed silently — which is exactly the shape of the dogfood
failure, where the codex reviewer worked throughout and only the builder and
fixer were blocked.

**Scope: real CLI adapters only.** ``probes_writability`` is declared by the
adapter class. The `api` adapter has no repo access at all, so there is nothing
to probe; an in-process test double is not subject to a real CLI's permission
guard, so probing it would measure nothing. This is a statement about what the
probe *means*, not a convenience — the question is "will THIS CLI's permission
layer refuse writes at THIS path?", and only a real CLI can answer it.

Stated plainly, as §5 does: this is a **detector, not a fix**. It converts a
silent, model-dependent failure into `worktree_unavailable`-class evidence.
Moving the root out of `.git/` (P7e) is the correction.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# One probe per write mechanism the CLI adapters actually use. The names are
# stable identifiers: they appear in the park reason, in `doctor` output and in
# tests, so a future mechanism is an addition here rather than a reworded string.
WRITE_TOOL = "write_tool"
EDIT_TOOL = "edit_tool"
SHELL_REDIRECT = "shell_redirect"
MECHANISMS = (WRITE_TOOL, EDIT_TOOL, SHELL_REDIRECT)

# The scratch directory the probe writes in, inside the run worktree. A PREFIX,
# not a fixed name (post-review F-002): the directory is created exclusively,
# with a random suffix, and an existing entry is never reused. A fixed name
# cannot be trusted here — `mkdir(exist_ok=True)` accepts an existing symlink to
# a directory, and this probe's very first act is an ENGINE-side write
# (`probe_edit.txt`'s seed), so a planted `.gauntlet-writability-probe ->
# /somewhere/else` would put engine bytes outside the repository before any
# agent runs, and the `rmtree(ignore_errors=True)` cleanup would leave both the
# link and the escaped file behind. The same reuse would delete a pre-existing
# real directory of that name.
#
# Removed on every exit path regardless: the central invariant (CLAUDE.md §1) is
# that the tree is clean at every handoff, and the preflight runs before the
# first agent step.
PROBE_DIRNAME = ".gauntlet-writability-probe"

_SEED = "SEED\n"
_EXPECT = "OK"

# Each prompt names ONE mechanism and forbids the others, and the allowed-tools
# list is narrowed to match, so a refusal cannot be routed around by the model
# picking a different form. That is the whole design (§2.3).
_PROMPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    WRITE_TOOL: (
        "Use the Write tool to create a file named probe.txt in the current "
        f"directory whose entire content is exactly {_EXPECT}. "
        "Do not use Bash. Do not use Edit. Do nothing else.",
        ("Write",),
    ),
    EDIT_TOOL: (
        "Use the Edit tool to replace the word SEED with the word "
        f"{_EXPECT} in the file probe_edit.txt in the current directory. "
        "Do not use Bash. Do not use Write. Do nothing else.",
        ("Edit", "Read"),
    ),
    SHELL_REDIRECT: (
        "Use the Bash tool to run exactly this command in the current "
        f"directory:  printf {_EXPECT} > probe_sh.txt  "
        "Do not use the Write tool. Do not use the Edit tool. Do nothing else.",
        ("Bash",),
    ),
}

_TARGETS = {
    WRITE_TOOL: "probe.txt",
    EDIT_TOOL: "probe_edit.txt",
    SHELL_REDIRECT: "probe_sh.txt",
}


@dataclass(frozen=True)
class MechanismOutcome:
    """What happened when one named write mechanism was exercised.

    ``refused`` and ``error`` are deliberately distinct. A refusal means the
    adapter RAN and the bytes did not land — that is the P7d defect, and it
    parks. An error means the adapter could not run at all (missing CLI, no
    credentials, a timeout); that is a different problem with a different
    remedy, and conflating the two would send an operator to fix the wrong
    thing.
    """

    mechanism: str
    landed: bool
    error: str | None = None

    @property
    def refused(self) -> bool:
        return not self.landed and self.error is None


@dataclass(frozen=True)
class AdapterWritability:
    """One adapter's verdict at one path."""

    adapter: str
    work_root: Path
    outcomes: tuple[MechanismOutcome, ...]

    @property
    def refused(self) -> tuple[str, ...]:
        return tuple(o.mechanism for o in self.outcomes if o.refused)

    @property
    def errored(self) -> tuple[str, ...]:
        return tuple(o.mechanism for o in self.outcomes if o.error is not None)

    @property
    def ok(self) -> bool:
        return not self.refused and not self.errored

    def summary(self) -> str:
        if self.ok:
            return f"{self.adapter}: all {len(self.outcomes)} write mechanisms OK"
        parts = []
        if self.refused:
            parts.append(f"REFUSED {', '.join(self.refused)}")
        if self.errored:
            parts.append(f"could not run {', '.join(self.errored)}")
        return f"{self.adapter}: " + "; ".join(parts)


def should_probe(adapter: Any) -> bool:
    """Whether this adapter's permission behaviour is a real thing to measure.

    Opt-IN, and fail-closed in the direction that matters: an adapter that does
    not declare the flag is not probed, because a probe of something that is not
    a real CLI produces a verdict about nothing — and a *false* refusal would
    park a healthy run, which is worse than the detector being silent.
    """
    return bool(getattr(adapter, "probes_writability", False))


def probe(adapter: Any, work_root: Path, *, adapter_name: str) -> AdapterWritability:
    """Exercise every mechanism in ``work_root`` and report what landed.

    Never raises for a refusal — a refusal is a RESULT, and the caller decides
    whether it parks (start-time preflight) or is reported (`doctor`). An
    adapter that blows up is captured per-mechanism as an ``error`` for the same
    reason, and so does a scratch directory that cannot be created safely: that
    is "could not run", not "refused", and it reaches the operator through the
    same fail-closed park rather than through an exception from a function
    whose whole contract is that it reports instead of raising.
    """
    outcomes: list[MechanismOutcome] = []
    try:
        probe_dir = _scratch_dir(work_root)
    except OSError as exc:
        detail = f"{type(exc).__name__}: {exc}"
        return AdapterWritability(
            adapter=adapter_name,
            work_root=work_root,
            outcomes=tuple(
                MechanismOutcome(m, landed=False, error=detail) for m in MECHANISMS
            ),
        )
    try:
        for mechanism in MECHANISMS:
            outcomes.append(_one(adapter, probe_dir, mechanism))
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    return AdapterWritability(
        adapter=adapter_name, work_root=work_root, outcomes=tuple(outcomes)
    )


def _scratch_dir(work_root: Path) -> Path:
    """Create the probe's scratch directory exclusively, inside ``work_root``.

    ``mkdtemp`` is the exclusivity: it creates with ``O_EXCL`` semantics under a
    name nothing else holds, so there is no pre-existing entry to reuse, no
    symlink to follow, and no foreign directory for the cleanup ``rmtree`` to
    delete. The containment assertion after it is the belt to that braces — the
    resolved directory must still be under the resolved ``work_root``, which is
    what makes "the engine writes only inside the run's tree" a checked property
    here rather than an assumption about the name.

    Raises :class:`OSError` (including a :class:`ValueError` for an escaped
    path, normalized) so :func:`probe` can report it as a per-mechanism error.
    """
    path = Path(tempfile.mkdtemp(prefix=f"{PROBE_DIRNAME}-", dir=work_root))
    try:
        path.resolve().relative_to(work_root.resolve())
    except (ValueError, OSError, RuntimeError) as exc:
        shutil.rmtree(path, ignore_errors=True)
        raise OSError(
            f"the writability probe's scratch directory {path} resolves outside "
            f"the run worktree {work_root}; refusing to write there"
        ) from exc
    return path


def _one(adapter: Any, probe_dir: Path, mechanism: str) -> MechanismOutcome:
    prompt, tools = _PROMPTS[mechanism]
    target = probe_dir / _TARGETS[mechanism]
    if mechanism == EDIT_TOOL:
        # Edit needs something to edit. Seeded by the ENGINE, not the agent, so
        # this measures the edit and not a write that preceded it.
        target.write_text(_SEED)
    flags = getattr(adapter, "writability_flags", lambda _t: [])(tools)
    try:
        adapter.run(prompt, cwd=probe_dir, extra_flags=flags or None)
    except Exception as exc:  # adapter-level failure, not a refusal
        return MechanismOutcome(mechanism, landed=False, error=f"{type(exc).__name__}: {exc}")
    # THE VERDICT: the filesystem after the turn. Not the adapter's text (a
    # model will cheerfully report success for a write it was refused — both
    # dogfood rounds did) and not the judge's PreToolUse verdict (§2.4).
    try:
        landed = target.is_file() and target.read_text().strip() == _EXPECT
    except OSError:
        landed = False
    return MechanismOutcome(mechanism, landed=landed)


def park_reason(reports: list[AdapterWritability]) -> str | None:
    """The operator-facing refusal, or ``None`` when every adapter is clean.

    Names the adapter, the mechanisms and the path, because those are the three
    facts that distinguish this from a model failure — which is what it looked
    like for an entire dogfood run.
    """
    bad = [r for r in reports if not r.ok]
    if not bad:
        return None
    lines = [
        "the run worktree is not writable by every agent that will drive it.",
        f"  Tree probed: {bad[0].work_root}",
    ]
    lines += [f"  {r.summary()}" for r in bad]
    if any(r.refused for r in bad):
        lines += [
            "",
            "A REFUSED mechanism means the adapter ran and the bytes did not "
            "land. Nothing in the manifest, journal or judge audit would have "
            "recorded that: the judge is a PreToolUse hook, so it adjudicates "
            "before the CLI applies its own permission rules and logs `allow` "
            "for writes that never happen. The run would have failed later as "
            "an apparent model failure (`the fixer made no changes ...`).",
            "  The known cause is a `.git` segment in the path; "
            "`gauntlet migrate-worktree <slug>` relocates a run whose tree is "
            "at the pre-P7e location.",
        ]
    return "\n".join(lines)


__all__ = [
    "EDIT_TOOL",
    "MECHANISMS",
    "PROBE_DIRNAME",
    "SHELL_REDIRECT",
    "WRITE_TOOL",
    "AdapterWritability",
    "MechanismOutcome",
    "park_reason",
    "probe",
    "should_probe",
]

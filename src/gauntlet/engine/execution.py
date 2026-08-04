"""Step execution contract: context, result, and the step-type registry.

Step handlers receive a :class:`StepContext` (everything they may touch) and
return a :class:`StepResult` (what the orchestrator records). Control flow —
``on_fail`` routing, retries, parking, budget halts — is the orchestrator's
job, not the handler's; a handler just reports ``done``/``failed``/``parked``.

Step types register in :data:`BUILTIN_STEP_TYPES` and via the
``gauntlet.step_types`` entry point (FR-5.5); each carries the capability
metadata the load-time validator needs (FR-2.3).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from gauntlet.adapters.base import AgentResult, Usage
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, RevalidationRecord, StepRecord
from gauntlet.engine.pipeline import Pipeline, Step
from gauntlet.logging.redact import RedactingWriter

STEP_TYPE_ENTRY_POINT_GROUP = "gauntlet.step_types"

# StepResult.status values
DONE = "done"
FAILED = "failed"
PARKED = "parked"
HALTED = "halted"
SKIPPED = "skipped"
INTERRUPTED = "interrupted"  # killed mid-edit; record interrupted, park the run


@dataclass
class StepResult:
    status: str
    session_id: str | None = None
    usage: Usage | None = None
    commit_sha: str | None = None
    commit_phase: str | None = None
    # A multi-commit step (adversarial_cycle: fix rounds + reviewer-attributed
    # mutation commits) reports every (phase-prefix, sha) it created, in order.
    commits: list[tuple[str, str]] = field(default_factory=list)
    notes: str = ""
    # Conflict-park discriminator (FR-2.1): set to
    # ``PARKED_REASON_UPSTREAM_CONFLICT`` ONLY when this step halted on an
    # UPSTREAM CONFLICT signal; ``None`` for every other outcome (including a
    # park on a *different* ``halt_on`` marker, a human_gate, or a budget halt).
    # ``_finalize`` copies this onto the record so the field stays current-state.
    parked_reason: str | None = None
    # Terminal halt discriminator (harness-efficiency FR-7.2), DISJOINT from
    # ``parked_reason``. P2 sets only ``manifest.HALT_REASON_TIMEOUT`` on the
    # suspend-cap timeout halt (FR-5.2); P3 completes the enum. ``_finalize``
    # copies it onto the record so it stays current-state. ``None`` for every
    # non-terminal or non-timeout outcome.
    halt_reason: str | None = None
    # Usage-limit park stamps (harness-efficiency FR-3.2), carried onto the
    # StepRecord by ``_finalize`` when a transient failure parks the step.
    # ``retry_after_s`` is the provider's structured retry hint (``None`` when
    # none); ``quota_reset_at`` is its derived absolute UTC reset time (``None``
    # when unreported). Both ``None`` for every non-usage-limit outcome.
    retry_after_s: int | None = None
    quota_reset_at: str | None = None
    # Engine-computed backoff deadline in seconds for a ``provider_unavailable``
    # park (plan §5.2, P5): the NEXT dependency-retry delay after the persisted
    # budget was exhausted. DISTINCT from ``retry_after_s`` (which is reserved
    # for a provider's STRUCTURED hint, never synthesized): ``_finalize``
    # derives ``quota_reset_at`` from it when neither an explicit reset time nor
    # a structured hint exists, so every dependency park carries the concrete
    # deadline the P4.1 F-006 no-progress exemption requires. ``None`` for every
    # other outcome.
    backoff_s: int | None = None
    # Which cycle sub-step produced the preserved ``session_id`` on a usage-limit
    # park (harness-efficiency FR-3.3): e.g. ``"r1-review"``, ``"r1-fix"``,
    # ``"r2-triage"``. Carried onto the StepRecord by ``_finalize`` so a resume
    # continues the preserved session only in the matching sub-step and never
    # misroutes a fixer/triager session into the round-1 reviewer. ``None`` for
    # every non-usage-limit outcome (current-state, like ``parked_reason``).
    parked_substep: str | None = None
    # Failure-kind discriminator (current-state, like ``parked_reason``): set to a
    # ``manifest.FAILURE_KIND_*`` value ONLY when this step failed a re-runnable
    # PRECONDITION guard (no adapter invoked, no cost — e.g. the FR-9.3 round-1
    # clean-handoff guard). ``None`` for every other outcome. ``_finalize`` copies
    # it onto the record so ``_is_terminal_failure`` can let a plain ``resume``
    # re-run such a step once the operator fixes the precondition.
    failure_kind: str | None = None
    # Content-hash pair for an ``artifact_invalid`` park / its resume-revalidate
    # (harness-efficiency FR-2.2/§6). Set ONLY on an ``artifact_invalid`` park
    # (``hash_at_park`` populated) and on the plain-resume revalidation that
    # resolves it (``hash_at_resume``/``changed_while_parked``/``passed_on_resume``
    # filled). ``None`` for every other outcome; ``_finalize`` copies it onto the
    # record as current-state so the audit trail lands on the resolving step.
    revalidation: RevalidationRecord | None = None
    # artifacts this step produced (artifact name -> path), merged into context
    artifact_writes: dict[str, Path] = field(default_factory=dict)
    # Per-agent-profile usage breakdown for this step (FR-3.2). Single-agent
    # steps report one entry; an adversarial_cycle splits across its roles.
    # The orchestrator merges this into the manifest's per-profile totals.
    usage_by_agent: dict[str, Usage] = field(default_factory=dict)
    # Structured outcome counts persisted onto the StepRecord for `--trend`
    # (FR-6.6): rounds, finding/verdict/confirm tallies an adversarial_cycle
    # emits so trend metrics come from the manifest (P7 test strategy).
    metrics: dict[str, Any] = field(default_factory=dict)


# An adapter factory lets unit tests inject fakes by agent-profile name without
# touching the entry-point registry (the orchestrator stays offline-testable).
AdapterFactory = Callable[[str], Any]


@dataclass(frozen=True)
class RunPaths:
    """The three roots a run acts on, resolved once and named separately (P7a).

    ``repo_root`` has meant three things at once since the bootstrap — *the git
    repository*, *the tree the agent edits*, and *where run artifacts live* —
    and every site that conflated them is a site P7c would have to find again
    (spike §9 catalogues 25 of them). This type is where that policy lives, so
    the split is expressed once rather than re-derived per call site:

    * ``work_root`` — **the tree the agents edit and the engine commits in.**
      Everything that mutates or observes a working tree takes this: status /
      clean / checkout / commit, the agent's cwd, the shell step's cwd, the
      verifier's disposable-copy parent, the judge's path boundary.
    * ``repo_root`` — **the git repository**, for everything that is a property
      of the repository rather than of a tree: ref existence and resolution,
      the object database, branch graph queries, snapshot refs (which E1 proved
      are shared across every worktree).
    * ``state_root`` — the run-instance dir: the P6 journal (authoritative),
      the manifest projection, transcripts, heartbeat and recovery intent.

    **Today ``work_root`` IS ``repo_root``**, so this commit changes no
    behaviour; :meth:`same_tree` is the invariant that says so. P7c constructs a
    ``RunPaths`` whose ``work_root`` is a dedicated worktree, and nothing else
    has to move.

    ``state_outside_worktree`` is deliberately a DECLARATION and not derived
    from comparing the paths: deriving it would auto-silence exactly the
    fail-closed contract :class:`StateDirNotContained` exists to enforce.
    """

    repo_root: Path
    work_root: Path
    state_root: Path
    artifact_root: Path
    state_outside_worktree: bool = False

    @classmethod
    def same_tree(
        cls,
        repo_root: Path,
        state_root: Path,
        artifact_root: Path,
        *,
        state_outside_worktree: bool = False,
    ) -> "RunPaths":
        """The pre-P7 layout: the agent edits the operator's own checkout."""
        return cls(
            repo_root=repo_root,
            work_root=repo_root,
            state_root=state_root,
            artifact_root=artifact_root,
            state_outside_worktree=state_outside_worktree,
        )

    @property
    def dedicated_worktree(self) -> bool:
        """True once ``work_root`` is a tree distinct from the operator's."""
        return self.work_root != self.repo_root

    def git_common_dir(self) -> Path:
        """The shared git dir, identical from every worktree (spike E1/E8)."""
        from gauntlet.engine import gitops

        return gitops.git_common_dir(self.work_root)


@dataclass
class StepContext:
    repo_root: Path
    run_dir: Path
    artifact_root: Path  # slug dir: prd.md / plan.md / step outputs live here
    config: RunConfig
    pipeline: Pipeline
    manifest: Manifest
    record: StepRecord
    writer: RedactingWriter
    judge_env: dict[str, str] = field(default_factory=dict)
    artifacts: dict[str, Path] = field(default_factory=dict)
    # repo-relative paths of the engine's own bookkeeping (review F-001); commit
    # and dirty checks exclude these but not real run artifacts.
    excludes: list[str] = field(default_factory=list)
    # Declares that `run_dir`/`artifact_root` legitimately live OUTSIDE the tree
    # the engine commits in (a `gauntlet review` run's out-of-repo state dir),
    # so the bookkeeping builders return empty by design instead of failing
    # closed. False for every `gauntlet run` — see `StateDirNotContained`.
    state_outside_worktree: bool = False
    # INDEPENDENT of the above (review F-002): declares that the GOVERNED
    # artifacts (prd.md / plan.md) legitimately live outside the work tree, so
    # R9's approved-artifact evidence is empty by design. A review run has none;
    # a `gauntlet run` always does. Sharing one flag with the state declaration
    # would let an external state dir silently switch off governance.
    artifacts_outside_worktree: bool = False
    # The tree this step edits and commits in (P7a). Defaults to `repo_root`
    # via __post_init__, which is the pre-P7 same-tree layout; P7c points it at
    # the run's dedicated worktree. Every worktree-mutating or -observing call
    # in a handler takes THIS, never `repo_root` — see RunPaths.
    work_root: Path | None = None
    iteration_item: Any | None = None
    iteration_index: int | None = None
    adapter_factory: AdapterFactory | None = None
    # Write-ahead persist hook (FR-4.1): the orchestrator wires this to its own
    # atomic manifest+RUN.md flush so a long-running handler (the adversarial
    # cycle) can checkpoint sub-step progress to disk mid-step, surviving a
    # kill/park between sub-steps. ``None`` for a standalone handler invocation
    # (no orchestrator); such callers fall back to writing the manifest directly.
    persist: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        # `work_root=None` means "the same tree as the repo" — the pre-P7
        # layout and every existing construction site, including the tests
        # that build a StepContext directly. Normalising here (rather than
        # defaulting each reader) keeps `ctx.work_root` non-optional for
        # handlers, so a handler can never silently fall back to `repo_root`
        # after P7c repoints it.
        if self.work_root is None:
            object.__setattr__(self, "work_root", self.repo_root)

    @property
    def paths(self) -> "RunPaths":
        """This step's roots as the single :class:`RunPaths` policy object."""
        return RunPaths(
            repo_root=self.repo_root,
            work_root=self.work_root or self.repo_root,
            state_root=self.run_dir,
            artifact_root=self.artifact_root,
            state_outside_worktree=self.state_outside_worktree,
        )

    def build_adapter(self, agent_name: str, *, effort: str | None = None) -> Any:
        """Resolve an agent profile to an adapter instance (override in tests).

        ``effort`` (FR-6.1) is a step-level canonical-effort override that wins
        over the profile's own ``effort``; ``None`` uses the profile's value. An
        injected ``adapter_factory`` (test double) ignores it — effort mapping is
        exercised against real profiles/adapters, not fakes."""
        if self.adapter_factory is not None:
            return self.adapter_factory(agent_name)
        return self.config.profile(agent_name).build_adapter(effort=effort)

    def steps_dir(self) -> Path:
        return self.run_dir / "steps"


Handler = Callable[[Step, StepContext], StepResult]


@dataclass(frozen=True)
class StepSpec:
    """Static metadata + handler for one step type."""

    type: str
    handler: Handler
    # FR-2.3 load-time capability checks:
    requires_repo_write: bool = False  # bound agent must be repo-write capable
    uses_schema: bool = False  # warn if the bound adapter is best-effort JSON
    needs_agent: bool = False  # must declare `agent:` (or message_agent)
    # F-003: record the step's base SHA before running it.
    touches_worktree: bool = False

    def step_requires_repo_write(self, step: Step) -> bool:
        # agent_task may opt out via `repo_write: false` (e.g. a doc reviewer).
        if self.type == "agent_task":
            return bool(step.get("repo_write", True))
        return self.requires_repo_write

    def step_touches_worktree(self, step: Step) -> bool:
        if self.type == "agent_task":
            return bool(step.get("repo_write", True))
        return self.touches_worktree


def builtin_specs() -> dict[str, StepSpec]:
    # Imported lazily to avoid a cycle (steptypes imports this module).
    from gauntlet.engine import steptypes

    return steptypes.SPECS


def step_specs() -> dict[str, StepSpec]:
    """All step specs: built-ins plus ``gauntlet.step_types`` entry points."""
    specs = dict(builtin_specs())
    for ep in entry_points(group=STEP_TYPE_ENTRY_POINT_GROUP):
        spec = ep.load()
        spec_obj = spec() if callable(spec) and not isinstance(spec, StepSpec) else spec
        if isinstance(spec_obj, StepSpec):
            specs[spec_obj.type] = spec_obj
    return specs


def get_spec(step_type: str) -> StepSpec:
    specs = step_specs()
    try:
        return specs[step_type]
    except KeyError:
        raise KeyError(
            f"unknown step type {step_type!r}; registered: {sorted(specs)}"
        ) from None


def usage_from_result(result: AgentResult) -> Usage | None:
    return result.usage


class StateDirNotContained(ValueError):
    """A run-instance dir declared in-tree does not resolve inside the work tree.

    The bookkeeping builders below all answer the same question — "what is this
    path, relative to the tree the engine commits in?" — and every one of them
    used to swallow the failure (``except ValueError: pass``) and return a
    quietly shorter list. That silence is load-bearing in the wrong direction
    (P7 spike §9.3): an empty ``engine_bookkeeping_candidates`` makes
    :func:`gitops.advance_is_engine_bookkeeping` classify *nothing* as
    bookkeeping, so every resume of an interrupted step re-parks (the #62/#65
    regression), and an empty ``run_bookkeeping_paths`` makes
    ``_commit_manifest_checkpoint`` a silent no-op, dropping the FR-2.2 audit
    trail with no diagnostic anywhere.

    Being outside the tree is nevertheless a LEGITIMATE, designed state for one
    caller: a ``gauntlet review`` run keeps its state out-of-repo under
    ``review.state_dir``/XDG on purpose, mints no branch, and commits no
    bookkeeping — so an empty result is exactly right there. The distinction
    the old code could not express is *declared* versus *accidental*, which is
    what the ``state_outside_worktree`` flag now carries: callers that expect
    containment fail closed and loudly, and the one caller that expects an
    external state dir says so at the call site.
    """


def _tree_rel(
    path: Path, root: Path, *, field: str, state_outside_worktree: bool
) -> str | None:
    """``path`` relative to ``root``, or ``None`` when legitimately outside.

    Raises :class:`StateDirNotContained` when the caller declared containment
    and the path is not contained — the fail-closed half of the contract
    described on that class.
    """
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        if state_outside_worktree:
            return None
        raise StateDirNotContained(
            f"{field} {str(path)!r} does not resolve inside the work tree "
            f"{str(root)!r}. The engine commits its bookkeeping in that tree, "
            "so an uncontained path would silently disable the bookkeeping "
            "allowlist (re-parking every interrupted resume) and the manifest "
            "checkpoint commit. If the state dir is deliberately out-of-tree "
            "(a `gauntlet review` run), pass state_outside_worktree=True."
        ) from None


def run_bookkeeping_excludes(
    repo_root: Path,
    run_dir: Path,
    artifact_root: Path,
    *,
    state_outside_worktree: bool = False,
) -> list[str]:
    """Repo-relative paths of the engine's own live bookkeeping (review F-001).

    The run-instance dir (manifest/transcripts/steps/judge-audit) must be
    invisible to worktree-state checks and commits. Everything *else* under the
    run root (prd.md, plan.md, declared step outputs) is real work: tracked,
    detected by the transaction boundary, and committable. Narrowing to just
    this set is the F-001 fix — the prior code excluded the whole run root,
    hiding real partial effects from the dirty-base check.

    The active-run pointer is deliberately NOT listed here: it is ignored via a
    ``.gitignore`` rule (``runs/*/active-run.txt``, shipped by ``init``), so git
    already keeps it out of status and ``add``. Naming a gitignored path in an
    ``:(exclude)`` pathspec makes ``git add`` ERROR ("paths are ignored ... use
    -f"), which broke the commit step (BOOTSTRAP-NOTES #33). Letting gitignore
    own it is both correct and avoids the pathspec collision.

    ``PR.md`` (FR-9.8) IS listed — for EVERY slug under the run root, as a
    ``<run_root>/*/PR.md`` exclude glob (PR #75 review): it is engine-drafted at
    final-gate pass but must never be auto-committed or pushed — opening the PR
    stays a human action (PRD §2.2). A completed sibling run's PR.md therefore
    legitimately sits uncommitted while the next run drives; scoping the exclude
    to only the current slug made that pending file read as dirt to the next
    run's clean-handoff checks and let commit steps sweep it into a machine
    commit. The glob keeps every slug's PR.md out of the engine's commits and
    clean/rollback/handoff checks while leaving each plainly visible for the
    human to ``git add`` and commit deliberately (not gitignored, no #33 clash).
    """
    excludes: list[str] = []
    root = repo_root.resolve()
    run_rel = _tree_rel(
        run_dir, root, field="run dir",
        state_outside_worktree=state_outside_worktree,
    )
    if run_rel is not None:
        excludes.append(run_rel)
    art = artifact_root.resolve()
    if art != root:
        try:
            run_root_rel = art.parent.relative_to(root).as_posix()
            excludes.append(f"{run_root_rel}/*/PR.md")
            return excludes
        except ValueError:
            # The artifact root's PARENT (the run root) is outside the tree.
            # Not independently declarable: an external state dir is also an
            # external artifact root, so `state_outside_worktree` already
            # covers the only legitimate case and the run-dir leg above has
            # already failed closed for every other one.
            pass
    # Fallback (artifact_root at/outside the repo root — no sibling slugs can
    # exist): the original single-file exclude.
    try:
        excludes.append((art / "PR.md").relative_to(root).as_posix())
    except ValueError:
        pass
    return excludes


def human_owned_excludes(exclude: list[str] | None) -> list[str]:
    """The human-owned entries of a ``run_bookkeeping_excludes()`` list.

    That list emits exactly two kinds of entries: the engine's own
    run-instance dir, and human-owned ``PR.md`` paths/globs (FR-9.8). Rewind
    code must tell them apart: engine bookkeeping survives a rewind via the
    bookkeeping-preserving mechanisms, but a human-owned file is invisible to
    the dirty checks *by policy* while ``reset --hard`` is not policy-scoped —
    an uncommitted edit to a tracked ``PR.md`` would be silently destroyed,
    unbacked-up (PR #77 review). Every rewind site passes these patterns as
    the ``protected`` set of its durable recovery snapshot
    (``git_snapshot.create_snapshot``), and the executor restores them from
    the snapshot after the rewind (``git_snapshot.restore_protected``).
    """
    return [
        e for e in (exclude or []) if e.rsplit("/", 1)[-1] == "PR.md"
    ]


def engine_bookkeeping_candidates(
    repo_root: Path, run_dir: Path, *, state_outside_worktree: bool = False
) -> list[str]:
    """Every path an engine bookkeeping COMMIT may touch (PR #76 review F-001).

    The exact allowlist — existence-independent, because a commit classifier
    walks history, where a path may appear that no longer exists on disk. This
    is deliberately NARROWER than ``run_bookkeeping_excludes``: the dirty-check
    exclusions also hide human-owned files (every slug's ``PR.md``) that the
    engine must never commit, so classifying a commit as engine bookkeeping by
    the *exclusion* list would let an engine-shaped commit touching ``PR.md``
    pass — and rollback would then hard-reset a human-owned change away.

    An empty result here is consequential, not neutral — see
    :class:`StateDirNotContained` — so an uncontained ``run_dir`` raises unless
    the caller declares ``state_outside_worktree``.
    """
    root = repo_root.resolve()
    paths: list[str] = []
    for name in ("manifest.json", "RUN.md"):
        rel = _tree_rel(
            run_dir / name, root, field="bookkeeping path",
            state_outside_worktree=state_outside_worktree,
        )
        if rel is not None:
            paths.append(rel)
    return paths


def governed_artifact_paths(
    repo_root: Path,
    artifact_root: Path,
    *,
    artifacts_outside_worktree: bool = False,
) -> list[str]:
    """Repo-relative paths of the run's governed artifacts (R9 / FR-10.4).

    The PRD and plan are ratified through their review loops and human gates,
    and hand-editing + committing them is a SANCTIONED operator workflow.
    Recovery observation flags any commit-range change to these paths so a
    rewind that would discard an operator's edit is surfaced LOUDLY (planner
    evidence promoted to manifest warnings) with the state preserved in the
    recovery snapshot — never refused (post-P3 review F-004, per operator
    direction). Existence-independent: a classifier walks history, where the
    path may predate or outlive the file on disk. Approval-state routing landed
    with the P5 taxonomy (plan §5.1): pre-approval defects park back into the
    artifact's own author/fix loop at their sites, post-approval edits stay on
    this loud governance path.

    Same declared-containment contract as the bookkeeping builders
    (:class:`StateDirNotContained`), and here the silent-empty consequence is a
    SAFETY one rather than a liveness one: with no governed paths, an
    approved-artifact edit inside a rewind range stops being flagged at all, so
    R9's "never silently adopt" degrades to "never notice". A review run has no
    governed prd/plan and correctly gets an empty list — declared, not inferred.

    The declaration is DELIBERATELY SEPARATE from the bookkeeping builders'
    ``state_outside_worktree`` (review F-002). Those are independent facts: the
    ratified P7 layout (spike §4.4) keeps the run-instance state dir out of the
    run worktree while the governed prd.md/plan.md remain fully in scope for R9.
    Sharing one flag would mean that declaring an external state dir silently
    switched OFF governed-artifact validation — the exact silent suppression
    :class:`StateDirNotContained` exists to prevent, reintroduced one layer up.
    """
    root = repo_root.resolve()
    art = artifact_root.resolve()
    paths: list[str] = []
    for name in ("prd.md", "plan.md"):
        rel = _tree_rel(
            art / name, root, field="governed artifact",
            state_outside_worktree=artifacts_outside_worktree,
        )
        if rel is not None:
            paths.append(rel)
    return paths


def run_bookkeeping_paths(
    repo_root: Path, run_dir: Path, *, state_outside_worktree: bool = False
) -> list[str]:
    """Repo-relative paths of the two on-disk run-bookkeeping files.

    The manifest is authoritative; RUN.md is its derived index. Returns only
    the files that currently exist, repo-root-relative and POSIX-formatted —
    the exact set every engine bookkeeping commit (and the bookkeeping-
    preserving rewinds) force-stages. Shared by the orchestrator's checkpoint
    commits and the cycle's fix-rerun rewind so the two can never disagree on
    what counts as bookkeeping.
    """
    return [
        rel
        for rel in engine_bookkeeping_candidates(
            repo_root, run_dir, state_outside_worktree=state_outside_worktree
        )
        if (repo_root.resolve() / rel).exists()
    ]

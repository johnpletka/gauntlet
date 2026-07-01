"""Lightweight `gauntlet review` run lifecycle (resolve-and-stop; P2).

This module owns the zero-footprint, in-place review lifecycle that the
`gauntlet review` command drives: the entry contract, in-place target-branch
adoption, the out-of-repo state dir, four-source intent resolution with
precedence + the FR-2.4 in-repo `--intent` exclusion, provenance tagging +
pre-run ratification + the manifest provenance record, and base resolution with
the merge-base / empty-diff guard.

It composes the existing engine primitives (``gitops``, the ``Manifest``, the
redaction list) rather than subclassing the heavyweight ``RunManager.start`` —
none of the heavyweight branch-minting, PRD entry contract, or gate stages
apply. In P2 the lifecycle **resolves and validates, then stops at the pre-cycle
boundary**: it persists ``intent.md`` + a manifest carrying the §6 intent block
out of repo and returns, leaving the `adversarial_cycle` wiring to P3.

Every failure fails closed (CLAUDE.md §2): a missing/unfetchable intent, an
empty diff, a dirty tree, a degenerate base, or an unratified non-independent
intent halts before any state is committed — never a silent degrade.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from gauntlet.engine import gitops
from gauntlet.engine import manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.identity import OperatorIdentityError, resolve_operator_identity
from gauntlet.engine.manifest import (
    IntentRecord,
    Manifest,
    PipelineRef,
    RatificationRecord,
)
from gauntlet.trackers import (
    IssueTrackerError,
    get_tracker,
    render_intent,
)

# The lightweight review runs a single `adversarial_cycle` from `review.yaml`.
# That pipeline asset (and the cycle execution that binds it) is a P3
# deliverable, so P2 persists a placeholder ref: the review run is established
# and its intent is resolved, but no pipeline is bound and nothing is driven.
# P3 replaces this when it wires the cycle. `hash=""` marks it explicitly
# unbound (never a real content hash).
_PIPELINE_PENDING = PipelineRef(name="review", version=1, hash="")

# Legal `--rounds` range: default 1 (FR-3.3), max 10 is a deterministic runaway
# guard (plan P2). Validated at parse time, before any lifecycle work.
ROUNDS_MIN = 1
ROUNDS_MAX = 10


class ReviewError(RuntimeError):
    """Base for every `gauntlet review` failure."""


class ReviewUsageError(ReviewError):
    """A usage-class error (bad flag combination / value). Maps to CLI exit 2.

    Raised before any run-lifecycle work or agent spawn, so the offending
    invocation never touches git or the state dir.
    """


class ReviewFailClosed(ReviewError):
    """A fail-closed halt with operator guidance. Maps to CLI exit 1.

    Every entry-contract, intent, base, or ratification failure raises this (or
    a subclass): the run stops before persisting state or spawning any agent.
    """


class RatificationDeclined(ReviewFailClosed):
    """The operator declined to ratify a non-independent intent (FR-2.5)."""


# ---------------------------------------------------------------------------
# Pure helpers (state-path derivation) — unit-testable without a filesystem run
# ---------------------------------------------------------------------------

# Every character NOT in the review-slug charset (§6 "Review state path").
_SLUG_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_DASH_RUN_RE = re.compile(r"-{2,}")


def sanitize_slug(name: str) -> str:
    """Map a target name to the review-slug charset ``[A-Za-z0-9._-]`` (§6).

    Every run of out-of-charset characters collapses to a single ``-``, adjacent
    dashes collapse, and leading/trailing dashes are trimmed — so ``feature/x``
    maps to ``feature-x`` (§6's stated transform). This is the pure char
    transform; the collision-proofing suffix is applied by :func:`review_slug`.
    """
    s = _SLUG_UNSAFE_RE.sub("-", name)
    s = _SLUG_DASH_RUN_RE.sub("-", s).strip("-")
    return s or "review"


def review_slug(target: str) -> str:
    """The on-disk ``<slug>`` for a review target (§6 "Review state path").

    Returns :func:`sanitize_slug` when the target is already slug-safe. When
    sanitization was **lossy** (the target carried out-of-charset characters), a
    short hash of the *unsanitized* target is appended, so two distinct targets
    that sanitize to the same string can never silently share one state dir
    (§6's collision-disambiguation rule). A slug-safe name (``main``, ``pr-123``)
    is returned unchanged; only distinct raw names that needed transforming
    carry the disambiguating suffix, so they never collide with a clean name.
    """
    base = sanitize_slug(target)
    if base == target:
        return base
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def normalize_repo_key(raw: str) -> str:
    """Normalize a remote URL (or repo path) for the ``<repo-id>`` hash (§6).

    Strips a URL scheme, any ``user[:pass]@`` credentials, rewrites an scp-like
    ``host:path`` to ``host/path``, drops a trailing ``.git`` and trailing
    slashes, and lowercases — so every equivalent spelling of one repo's origin
    (``https://``, ``git@…:``, ``ssh://…``, with/without ``.git``) hashes to the
    same id, independent of the local checkout location.
    """
    u = raw.strip()
    u = re.sub(r"^[A-Za-z][A-Za-z0-9+.\-]*://", "", u)  # scheme://
    if "@" in u:  # user[:pass]@ credentials/userinfo
        u = u[u.index("@") + 1:]
    u = u.replace(":", "/", 1)  # scp-like host:path -> host/path
    u = u.rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    return u.lower().strip("/")


def derive_repo_id(repo_root: Path) -> str:
    """The deterministic ``<repo-id>`` for a repo (§6): 12 hex of a SHA-256.

    Hashes the normalized ``origin`` remote URL when one exists (stable across
    checkouts); otherwise the normalized repo toplevel path (defined for
    local-only repos).
    """
    url = gitops.remote_url(repo_root, "origin")
    if url:
        key = normalize_repo_key(url)
    else:
        try:
            top = gitops.show_toplevel(repo_root)
        except gitops.GitError:
            top = str(repo_root.resolve())
        key = normalize_repo_key(top)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def _xdg_state_home(environ: Mapping[str, str]) -> Path:
    """``${XDG_STATE_HOME:-~/.local/state}`` from the environment (§6).

    Per the XDG Base Directory spec a relative ``XDG_STATE_HOME`` is invalid and
    MUST be ignored: honoring one (e.g. ``XDG_STATE_HOME=.state``) would resolve
    the default review state dir against the caller's CWD — landing it inside the
    repo and dirtying ``git status``, which breaks the zero-footprint invariant
    (FR-8.1). A relative value therefore falls back to ``~/.local/state`` exactly
    as if unset.
    """
    xdg = environ.get("XDG_STATE_HOME")
    if xdg and Path(xdg).is_absolute():
        return Path(xdg)
    home = environ.get("HOME") or str(Path.home())
    return Path(home) / ".local" / "state"


def _normabs(p: Path) -> Path:
    return Path(os.path.normpath(str(p if p.is_absolute() else p.resolve())))


def _within(child: Path, ancestor: Path) -> bool:
    try:
        _normabs(child).relative_to(_normabs(ancestor))
        return True
    except ValueError:
        return False


def resolve_state_dir(
    repo_root: Path,
    config: RunConfig,
    *,
    repo_id: str,
    slug: str,
    environ: Mapping[str, str],
) -> Path:
    """Resolve the review run's state dir (§6 "Review state path", FR-8.1/8.3).

    Default (``review.state_dir`` unset): the out-of-repo XDG location
    ``${XDG_STATE_HOME:-~/.local/state}/gauntlet/reviews/<repo-id>/<slug>/`` — no
    bytes under the repo tree at all. An override replaces the *root* only; the
    ``<repo-id>/<slug>`` layout under it is unchanged. When the override resolves
    **inside** the repo it must be covered by a gitignore rule (verified via
    ``git check-ignore``) so the only legal in-repo state is ignored state,
    preserving the zero-Git-status-footprint invariant; an out-of-repo override
    is unconstrained.
    """
    override = config.review.state_dir
    if override:
        root = Path(override)
        if not root.is_absolute():
            root = repo_root / root
        state_dir = root / repo_id / slug
        if _within(state_dir, repo_root):
            rel = _normabs(state_dir).relative_to(_normabs(repo_root)).as_posix()
            if not gitops.path_is_ignored(repo_root, rel):
                raise ReviewFailClosed(
                    f"review.state_dir resolves inside the repo at {rel!r} but is "
                    "not covered by a .gitignore rule; an in-repo review state dir "
                    "must be gitignored so the review leaves zero git-status "
                    "footprint (FR-8.3). Add it to .gitignore, or use an "
                    "out-of-repo path / the default (unset)."
                )
        return state_dir
    default = (
        _xdg_state_home(environ)
        / "gauntlet"
        / "reviews"
        / repo_id
        / slug
    )
    # The default path must be strictly out-of-repo — unlike an override, it is
    # never gitignore-guarded, so any byte it writes under the repo would dirty
    # git status (FR-8.1). This can only happen if HOME/XDG_STATE_HOME itself
    # points inside the repo; fail closed rather than write review state into the
    # tree under review.
    if _within(default, repo_root):
        raise ReviewFailClosed(
            f"the default review state dir {default!s} resolves inside the repo, "
            "which would dirty git status and break the zero-footprint invariant "
            "(FR-8.1). HOME/XDG_STATE_HOME points inside the repo; set "
            "XDG_STATE_HOME to an absolute path outside the repo, or set an "
            "out-of-repo review.state_dir."
        )
    return default


# ---------------------------------------------------------------------------
# Inputs + injectable hooks
# ---------------------------------------------------------------------------


@dataclass
class ReviewInputs:
    """The parsed `gauntlet review` invocation (FR-1.1)."""

    branch: str | None = None
    pr: str | None = None
    issue: str | None = None
    intent_path: str | None = None
    message: str | None = None
    intent_provenance: str | None = None
    approved_intent: bool = False
    base: str | None = None
    code_only: bool = False
    rounds: int = ROUNDS_MIN
    test: bool | None = None  # None => baseline-tests off (default); True/False explicit


def _default_editor(path: Path, environ: Mapping[str, str]) -> None:
    editor = environ.get("VISUAL") or environ.get("EDITOR") or "vi"
    subprocess.run([*editor.split(), str(path)], check=True)


@dataclass
class Hooks:
    """Injectable side-effect seams so the whole lifecycle is offline-testable.

    Defaults are the real implementations; the unit suite overrides ``isatty`` /
    ``edit_statement`` / ``confirm_statement`` / ``environ`` / ``now`` /
    ``tracker_transport`` to drive the non-interactive paths without a TTY,
    editor, network, or wall clock.
    """

    isatty: Callable[[], bool] = field(default=lambda: False)
    # Edit the given statement text and return the (possibly edited) text.
    edit_statement: Callable[[str, Path], str] | None = None
    # Present the resolved statement and return True to confirm, False to decline.
    confirm_statement: Callable[[str], bool] = field(default=lambda text: False)
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    now: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc)
    )
    tracker_transport: object | None = None


# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------


@dataclass
class ReviewResolution:
    """The resolved review run at the pre-cycle boundary (P2 output).

    Carries everything P3 needs to wire and execute the cycle: the target branch
    (HEAD), the resolved base ref, the concrete ``merge_base`` SHA to inject as
    ``review_base`` (three-dot scope, FR-5.2), the out-of-repo state dir, the
    ``intent.md`` path (``None`` for ``--code-only``), the persisted intent
    record, and the validated ``rounds`` / ``code_only`` / ``run_tests`` knobs.
    """

    slug: str
    target_branch: str
    base_ref: str
    merge_base: str
    state_dir: Path
    manifest_path: Path
    intent_path: Path | None
    intent_record: IntentRecord
    excludes: list[str]
    rounds: int
    code_only: bool
    run_tests: bool


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


class ReviewLifecycle:
    """Resolve a `gauntlet review` invocation up to the pre-cycle boundary."""

    def __init__(
        self,
        repo_root: Path,
        config: RunConfig | None = None,
        *,
        hooks: Hooks | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.config = config or RunConfig.load(repo_root / ".gauntlet/config.yaml")
        self.hooks = hooks or Hooks()

    # ---- top-level -----------------------------------------------------------

    def locate(self, inputs: ReviewInputs) -> tuple[str, str, Path]:
        """Resolve the target branch, slug, and state dir — with NO side effects.

        Runs input validation and target-name resolution (both read-only: no
        checkout, no clean-tree check, no manifest write), then derives the
        deterministic state dir. The CLI calls this BEFORE :meth:`resolve` so it
        can detect an existing (parked/running) review run and route a resume to it
        rather than clobbering it with a fresh resolution (FR-9.1/FR-8.4)."""
        self._validate_inputs(inputs)
        target = self._resolve_target(inputs)
        slug = review_slug(target)
        repo_id = derive_repo_id(self.repo_root)
        state_dir = resolve_state_dir(
            self.repo_root, self.config,
            repo_id=repo_id, slug=slug, environ=self.hooks.environ,
        )
        return target, slug, state_dir

    def resolve(self, inputs: ReviewInputs) -> ReviewResolution:
        """Run the full P2 resolve-and-stop path; raise on any fail-closed halt."""
        self._validate_inputs(inputs)

        source = self._intent_source(inputs)
        excludes = self._intent_excludes(inputs)

        # 1) Target branch (name only) — resolved before any base/diff work.
        target = self._resolve_target(inputs)

        # 2) Entry contract: clean worktree BEFORE any checkout (FR-9.2), so
        #    adoption never discards uncommitted work. The in-repo --intent file
        #    is excluded (FR-2.4) so it cannot trip the contract.
        if not gitops.is_clean(self.repo_root, exclude=excludes or None):
            raise ReviewFailClosed(
                "the worktree has uncommitted changes; a review reviews the "
                "target branch's committed state, so commit or stash first "
                "(FR-9.2). A --intent file inside the repo is exempt."
            )

        # 3) Refuse if another non-terminal run already owns the target branch.
        self._refuse_competing_run(target)

        # 4) Adopt the target branch in place (no gauntlet/<slug> minted).
        self._adopt_target(target)

        # 5) Out-of-repo (default) state dir.
        slug = review_slug(target)
        repo_id = derive_repo_id(self.repo_root)
        state_dir = resolve_state_dir(
            self.repo_root, self.config,
            repo_id=repo_id, slug=slug, environ=self.hooks.environ,
        )

        # 6) Base resolution + merge-base / empty-diff guard (HEAD == target tip).
        base_ref, merge_base = self._resolve_base(inputs, target)

        # 7) Intent: resolve, ratify (non-independent), snapshot to intent.md.
        state_dir.mkdir(parents=True, exist_ok=True)
        intent_record, intent_path = self._resolve_intent(
            inputs, source=source, state_dir=state_dir,
        )

        # 8) Persist the manifest (intent block) and stop at the pre-cycle boundary.
        manifest_path = self._persist_manifest(
            slug=slug, target=target, base_ref=base_ref,
            intent_record=intent_record, state_dir=state_dir,
        )

        return ReviewResolution(
            slug=slug,
            target_branch=target,
            base_ref=base_ref,
            merge_base=merge_base,
            state_dir=state_dir,
            manifest_path=manifest_path,
            intent_path=intent_path,
            intent_record=intent_record,
            excludes=excludes,
            rounds=inputs.rounds,
            code_only=inputs.code_only,
            run_tests=bool(inputs.test),
        )

    # ---- input validation (usage class; no git) ------------------------------

    def _validate_inputs(self, inputs: ReviewInputs) -> None:
        if inputs.pr is not None and inputs.branch is not None:
            raise ReviewUsageError(
                "--pr and a positional <branch> are mutually exclusive: --pr "
                "reviews a PR's head branch, a positional reviews a local branch "
                "(FR-1.2)"
            )
        if not (ROUNDS_MIN <= inputs.rounds <= ROUNDS_MAX):
            raise ReviewUsageError(
                f"--rounds must be an integer in [{ROUNDS_MIN}, {ROUNDS_MAX}]; "
                f"got {inputs.rounds} (default {ROUNDS_MIN}, FR-3.3)"
            )
        if inputs.test and not (self.config.test_command or "").strip():
            raise ReviewUsageError(
                "--test requires config.test_command to be set (the baseline-test "
                "step runs it); set test_command or drop --test (FR-1.1)"
            )
        if inputs.intent_provenance is not None:
            if inputs.issue is not None:
                raise ReviewUsageError(
                    "--intent-provenance cannot be combined with --issue: a "
                    "tracker ticket is always 'tracker' provenance (FR-1.1)"
                )
            if inputs.intent_provenance not in M.MANUAL_PROVENANCES:
                raise ReviewUsageError(
                    f"--intent-provenance must be one of "
                    f"{sorted(M.MANUAL_PROVENANCES)}; got "
                    f"{inputs.intent_provenance!r} (FR-2.1a)"
                )
        if inputs.code_only and (
            inputs.issue is not None
            or inputs.intent_path is not None
            or inputs.message is not None
            or inputs.intent_provenance is not None
            or inputs.approved_intent
        ):
            raise ReviewUsageError(
                "--code-only runs a diff-only review with no intent; it cannot be "
                "combined with --issue/--intent/-m/--intent-provenance/"
                "--approved-intent (FR-2.3)"
            )
        # --pr is parsed here but its checkout/auto-derive path is P4/P5.
        if inputs.pr is not None:
            raise ReviewUsageError(
                "--pr (GitHub PR mode) is not implemented yet; it lands in a later "
                "phase. Review a local branch instead (positional <branch> or the "
                "current branch)."
            )

    def _intent_source(self, inputs: ReviewInputs) -> str:
        """The single resolved intent source, by FR-2.1 precedence."""
        if inputs.code_only:
            return M.INTENT_SOURCE_CODE_ONLY
        if inputs.issue is not None:
            return M.INTENT_SOURCE_ISSUE
        if inputs.intent_path is not None:
            return M.INTENT_SOURCE_INTENT_FILE
        if inputs.message is not None:
            return M.INTENT_SOURCE_MESSAGE
        # No explicit intent: the $EDITOR template is the last resort, and it can
        # only be completed interactively. In a non-interactive context (CI, a
        # headless `gauntlet review fix`) launching $EDITOR would block forever
        # with no way to close it, wedging the run. Fail closed here — before any
        # git/lifecycle work — rather than spawn a `vi` that never returns
        # (FR-2.3 fail-closed; CLAUDE.md §2 "fail closed").
        if not self.hooks.isatty():
            raise ReviewFailClosed(
                "no intent source was given and no TTY is attached, so the "
                "$EDITOR intent template cannot be opened. Pass --issue/--intent/"
                "-m to supply a problem statement, or --code-only for a diff-only "
                "review (FR-2.3)."
            )
        return M.INTENT_SOURCE_EDITOR

    def _intent_excludes(self, inputs: ReviewInputs) -> list[str]:
        """Repo-relative exclude for an in-repo, **untracked** ``--intent`` file.

        A ``--intent`` path resolving inside the repo is added to the run's
        worktree-exclude set so it cannot trip the clean-tree entry contract or
        be swept into a fix commit — but ONLY when it is genuinely untracked
        user-owned dirt (FR-2.4/G3). The file is left present and untracked.

        A tracked in-repo path (clean, staged, or modified) is NEVER exempted:
        excluding it would let ``--intent src/foo.py`` mask uncommitted changes
        to a tracked file and start the review from an unclean worktree, breaking
        the requirement that the target branch's committed state be clean before
        review (FR-9.2). A dirty tracked path is left in the clean checks so the
        entry contract fails closed on it; a clean tracked path has nothing to
        exclude. A path outside the repo needs no exclusion either way.
        """
        if not inputs.intent_path:
            return []
        p = Path(inputs.intent_path)
        if not p.is_absolute():
            p = self.repo_root / p
        if not _within(p, self.repo_root):
            return []
        rel = _normabs(p).relative_to(_normabs(self.repo_root)).as_posix()
        if not gitops.path_is_untracked(self.repo_root, rel):
            return []
        return [rel]

    # ---- target-branch resolution + adoption ---------------------------------

    def _resolve_target(self, inputs: ReviewInputs) -> str:
        repo = self.repo_root
        if inputs.branch is None:
            current = gitops.current_branch(repo)
            if current == "HEAD":
                raise ReviewFailClosed(
                    "HEAD is detached; a review needs a named branch to land any "
                    "REVIEW.x fix commits on. Check out a branch, or pass a "
                    "positional <branch> (FR-5.3)."
                )
            return current

        arg = inputs.branch
        is_branch = gitops.branch_exists(repo, arg)
        if is_branch and gitops.tag_exists(repo, arg):
            raise ReviewFailClosed(
                f"{arg!r} is ambiguous — it names both a branch and a tag; "
                "rename or delete one so the review target is unambiguous."
            )
        if is_branch:
            return arg
        # Not a local branch: distinguish a remote-only / other-namespace ref
        # from a genuinely absent one, but fail closed either way.
        if gitops.ref_is_valid_commit(repo, arg):
            raise ReviewFailClosed(
                f"{arg!r} is not a local branch (it resolves as a tag or "
                "remote-only ref). Branch mode operates on local branches only; "
                "check out a local branch of that name first, or pass one that "
                "exists locally."
            )
        raise ReviewFailClosed(
            f"no local branch named {arg!r}; pass an existing local branch or "
            "omit it to review the current branch."
        )

    def _adopt_target(self, target: str) -> None:
        """Put HEAD on ``target`` in place; fail closed if it cannot (FR-1.3)."""
        if gitops.current_branch(self.repo_root) == target:
            return
        try:
            gitops.checkout_branch(self.repo_root, target)
        except gitops.GitError as exc:
            raise ReviewFailClosed(
                f"could not check out target branch {target!r}: {exc}"
            ) from exc
        if gitops.current_branch(self.repo_root) != target:
            raise ReviewFailClosed(
                f"checking out {target!r} did not leave HEAD on that branch; "
                "refusing to run a review from an unexpected HEAD (FR-5.3)."
            )

    def _refuse_competing_run(self, target: str) -> None:
        """Fail closed if a non-terminal heavyweight run owns ``target`` (FR-9.3).

        Read-only: scans ``run_root`` for active runs and refuses if any
        non-terminal run's branch equals the target — launching a review's fix
        agents against a worktree another run is driving would break the
        clean-handoff invariant. A missing run_root (fresh repo) means no
        competing runs, and reading it writes nothing under the repo.
        """
        run_root = self.repo_root / self.config.run_root
        if not run_root.is_dir():
            return
        terminal = M.RUN_DONE, M.RUN_ABORTED, M.RUN_FAILED
        for slug_dir in sorted(run_root.iterdir()):
            if not slug_dir.is_dir():
                continue
            pointer = slug_dir / "active-run.txt"
            if not pointer.is_file():
                continue
            try:
                run_id = pointer.read_text().strip()
                man = Manifest.load(slug_dir / run_id / "manifest.json")
            except (OSError, ValueError):
                continue
            if man.status not in terminal and man.branch == target:
                raise ReviewFailClosed(
                    f"run {man.run_id!r} ({man.status}) already owns branch "
                    f"{target!r}; refusing to launch a review against a worktree "
                    "another run is driving (FR-9.3). Finish/abort it first."
                )

    # ---- base resolution + empty-diff guard (FR-5) ---------------------------

    def _resolve_base(self, inputs: ReviewInputs, target: str) -> tuple[str, str]:
        repo = self.repo_root
        base_ref = self._resolve_base_ref(inputs)
        if not gitops.ref_is_valid_commit(repo, base_ref):
            raise ReviewFailClosed(
                f"the resolved review base {base_ref!r} is not a valid ref; pass "
                "an existing base with --base."
            )
        mb = gitops.merge_base(repo, base_ref, "HEAD")
        if mb is None:
            raise ReviewFailClosed(
                f"the review base {base_ref!r} shares no history with the target "
                f"branch {target!r} (unrelated histories); pass a --base that "
                "shares a merge-base with HEAD."
            )
        if gitops.diff_range_empty(repo, mb, "HEAD"):
            raise ReviewFailClosed(
                "base resolves to the branch under review or has no changes to "
                f"review; nothing to diff — pass `--base <ref>` (base {base_ref!r})."
            )
        return base_ref, mb

    def _resolve_base_ref(self, inputs: ReviewInputs) -> str:
        """FR-5.1 order: --base > concrete config.base_branch > origin/HEAD.

        The ``base_branch: current`` sentinel is never a review base (it would
        resolve to the branch under review), so it falls through to the remote
        default branch.
        """
        if inputs.base:
            return inputs.base
        configured = (self.config.base_branch or "").strip()
        if configured and configured.lower() not in ("current", "@current"):
            return configured
        default = gitops.remote_default_branch(self.repo_root, "origin")
        if default is None:
            raise ReviewFailClosed(
                "cannot resolve a review base: config.base_branch is 'current' "
                "(never a review base) and origin/HEAD is unset. Set origin's "
                "default (`git remote set-head origin -a`), set a concrete "
                "base_branch, or pass --base <ref> (FR-5.1)."
            )
        return default

    # ---- intent resolution + ratification (FR-2) -----------------------------

    def _resolve_intent(
        self, inputs: ReviewInputs, *, source: str, state_dir: Path
    ) -> tuple[IntentRecord, Path | None]:
        if source == M.INTENT_SOURCE_CODE_ONLY:
            return (
                IntentRecord(
                    source=source,
                    provenance=M.PROVENANCE_NONE,
                    independent=False,
                ),
                None,
            )

        if source == M.INTENT_SOURCE_ISSUE:
            issue = self._fetch_issue(inputs.issue)
            provenance = M.PROVENANCE_TRACKER
            body = render_intent(
                issue,
                provenance=provenance,
                independent=True,
                source=source,
                provider=self.config.issue_tracker.provider,
            )
            record = IntentRecord(source=source, provenance=provenance, independent=True)
            intent_path = self._write_intent(state_dir, body)
            return record, intent_path

        # Manual sources: --intent file, -m text, or the $EDITOR template.
        text = self._manual_statement(inputs, source, state_dir)
        provenance = inputs.intent_provenance or M.PROVENANCE_AUTHOR_SESSION_SUMMARY
        independent = provenance == M.PROVENANCE_TRACKER
        ratification, text = self._ratify(
            text, independent=independent, approved_intent=inputs.approved_intent
        )
        body = render_intent(
            text, provenance=provenance, independent=independent, source=source
        )
        record = IntentRecord(
            source=source,
            provenance=provenance,
            independent=independent,
            ratification=ratification,
        )
        intent_path = self._write_intent(state_dir, body)
        return record, intent_path

    def _fetch_issue(self, ref: str):
        tracker_cfg = self.config.issue_tracker
        if tracker_cfg is None or not tracker_cfg.enabled:
            raise ReviewFailClosed(
                "--issue requires a configured issue_tracker; none is set (or "
                "provider: none). Configure one, or supply --intent/-m instead "
                "(FR-6.5)."
            )
        try:
            tracker = get_tracker(
                tracker_cfg,
                env=self.hooks.environ,
                transport=self.hooks.tracker_transport,
            )
        except KeyError as exc:  # unregistered provider (config normally catches)
            raise ReviewFailClosed(
                f"issue tracker provider {tracker_cfg.provider!r} is not "
                f"registered: {exc}"
            ) from exc
        try:
            parsed = tracker.parse_ref(ref)
        except ValueError as exc:
            raise ReviewFailClosed(
                f"--issue {ref!r} is not a valid reference for provider "
                f"{tracker_cfg.provider!r}: {exc}"
            ) from exc
        try:
            return tracker.fetch(parsed)
        except IssueTrackerError as exc:
            # --issue is the sole, highest-precedence source; a failure NEVER
            # falls back to a lower one (FR-2.1). Fail closed with the typed error.
            raise ReviewFailClosed(
                f"could not resolve --issue {ref!r} from the tracker "
                f"({type(exc).__name__}): {exc}"
            ) from exc

    def _manual_statement(
        self, inputs: ReviewInputs, source: str, state_dir: Path
    ) -> str:
        if source == M.INTENT_SOURCE_INTENT_FILE:
            p = Path(inputs.intent_path)
            if not p.is_absolute():
                p = self.repo_root / p
            try:
                text = p.read_text()
            except OSError as exc:
                raise ReviewFailClosed(
                    f"could not read --intent file {inputs.intent_path!r}: {exc}"
                ) from exc
            if not text.strip():
                raise ReviewFailClosed(
                    f"--intent file {inputs.intent_path!r} is empty; supply a "
                    "problem statement or use --code-only (FR-2.3)."
                )
            return text
        if source == M.INTENT_SOURCE_MESSAGE:
            if not (inputs.message or "").strip():
                raise ReviewFailClosed(
                    "-m was given an empty problem statement; supply text or use "
                    "--code-only (FR-2.3)."
                )
            return inputs.message
        # $EDITOR template — the temp file lives under the out-of-repo state dir,
        # never in the repo (FR-2.4).
        return self._editor_statement(state_dir)

    def _editor_statement(self, state_dir: Path) -> str:
        template = (
            "# Describe the problem this change is meant to fix.\n"
            "# Lines starting with '#' are ignored. Save and close when done.\n"
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp = state_dir / "intent-editor.md"
        tmp.write_text(template)
        try:
            _default_editor(tmp, self.hooks.environ)
            raw = tmp.read_text()
        except (OSError, subprocess.SubprocessError) as exc:
            # A missing editor binary (OSError) or a non-zero editor exit /
            # timeout (SubprocessError, incl. CalledProcessError) is a
            # fail-closed halt, not an uncaught crash — the review never
            # proceeds on an unconfirmed intent.
            raise ReviewFailClosed(
                f"could not run $EDITOR for the intent template: {exc}"
            ) from exc
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        text = "\n".join(
            ln for ln in raw.splitlines() if not ln.lstrip().startswith("#")
        )
        if not text.strip():
            raise ReviewFailClosed(
                "no problem statement was entered in $EDITOR; supply one, use "
                "--intent/-m, or --code-only (FR-2.3)."
            )
        return text

    def _ratify(
        self, text: str, *, independent: bool, approved_intent: bool
    ) -> tuple[RatificationRecord | None, str]:
        """Pre-run ratification of a non-independent intent (FR-2.5).

        Independent (``tracker``) intent needs none. Non-independent intent must
        be ratified before the cycle: interactive TTY → render + optional edit +
        explicit confirm; non-interactive → require ``--approved-intent`` else
        fail closed. Returns ``(record, possibly-edited-text)``.
        """
        if independent:
            return None, text
        try:
            user = resolve_operator_identity(self.repo_root, self.hooks.environ)
        except OperatorIdentityError as exc:
            raise ReviewFailClosed(
                f"cannot record intent ratification: {exc}"
            ) from exc
        timestamp = self.hooks.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.hooks.isatty():
            if self.hooks.edit_statement is not None:
                text = self.hooks.edit_statement(text, self.repo_root)
            if not self.hooks.confirm_statement(text):
                raise RatificationDeclined(
                    "the resolved problem statement was not confirmed; the review "
                    "did not start (FR-2.5). Re-run and approve it, or edit it "
                    "first."
                )
            method = M.RATIFICATION_INTERACTIVE
        else:
            if not approved_intent:
                raise ReviewFailClosed(
                    "this intent is non-independent (author/session-derived) and "
                    "must be ratified by a human before the review runs (FR-2.5). "
                    "No TTY is attached, so pass --approved-intent to assert it "
                    "was ratified out of band, or run interactively."
                )
            method = M.RATIFICATION_APPROVED_FLAG
        return RatificationRecord(method=method, user=user, timestamp=timestamp), text

    def _write_intent(self, state_dir: Path, body: str) -> Path:
        path = state_dir / "intent.md"
        path.write_text(body)
        return path

    # ---- persistence ---------------------------------------------------------

    def _persist_manifest(
        self,
        *,
        slug: str,
        target: str,
        base_ref: str,
        intent_record: IntentRecord,
        state_dir: Path,
    ) -> Path:
        """Write the review run's manifest (with the §6 intent block) out of repo.

        This is the write-ahead checkpoint at the pre-cycle boundary: run_id is
        the stable slug (so a resume resolves the same dir, FR-8.4), branch is the
        in-place target, base_branch is the resolved review base. The pipeline
        ref is the P3-pending placeholder — no cycle is bound or driven here.
        """
        man = Manifest(
            run_id=slug,
            slug=slug,
            branch=target,
            base_branch=base_ref,
            pipeline=_PIPELINE_PENDING,
            intent=intent_record,
        )
        path = state_dir / "manifest.json"
        man.write_atomic(path)
        return path


# ---------------------------------------------------------------------------
# P3 — cycle execution + the FR-3.4 terminal-severity summary
# ---------------------------------------------------------------------------

# Severity ordering for the residual-risk / declined summary (FR-3.4).
_SEVERITY_RANK = {"blocking": 0, "major": 1, "minor": 2, "nit": 3}
_LEGITIMATE = "legitimate"
_CONFIRM_RESOLVED = "resolved"

# The review pipeline asset is a single `adversarial_cycle` step (§6 review.yaml).
_REVIEW_PIPELINE = "pipelines/review.yaml"
# Terminal run statuses — a run in one of these is not resumable.
_TERMINAL_STATUSES = (M.RUN_DONE, M.RUN_ABORTED, M.RUN_FAILED)


@dataclass(frozen=True)
class SummaryFinding:
    """One finding surfaced in a review run's terminal summary (FR-3.4)."""

    id: str
    severity: str
    location: str
    claim: str
    triage_verdict: str | None
    triage_reasoning: str
    confirm_verdict: str | None  # last confirm verdict; None => never confirmed
    round: int


@dataclass(frozen=True)
class ReviewSummary:
    """The FR-3.4 terminal partition of a completed review's findings.

    ``residual_risk`` — legitimate, non-blocking findings whose last confirm
    verdict is not ``resolved`` (they complete the run but surface to the human on
    the branch/PR, never silently dropped). ``declined`` — findings triaged
    not-legitimate, carried with their triage reasoning. A legitimate *blocking*
    finding never reaches this partition: it parks the run (FR-3.2), it does not
    complete.
    """

    residual_risk: list[SummaryFinding]
    declined: list[SummaryFinding]


@dataclass(frozen=True)
class RoundRecord:
    """One cycle round's persisted findings / triage / confirm records.

    The summary is a pure function of these — the review run adds no new finding
    store (FR-3.4 residual-risk contract). ``findings`` carries the raw
    ``findings.json`` entries, ``triage`` the ``triage.json`` verdicts, ``confirm``
    the ``confirm.json`` verdicts, for a single round index.
    """

    round: int
    findings: list[dict]
    triage: list[dict]
    confirm: list[dict]


@dataclass
class ReviewOutcome:
    """The result of driving a review cycle to a terminal state (P3)."""

    status: str  # a manifest RUN_* status
    parked: bool
    commits: list[tuple[str, str]]  # (phase-prefix, sha) for each REVIEW.x
    summary: ReviewSummary
    state_dir: Path
    cycle_notes: str


def summarize_cycle(rounds: list[RoundRecord]) -> ReviewSummary:
    """Partition a cycle's findings into residual-risk / declined (FR-3.4).

    A pure, deterministic function of the per-round records (no I/O, no clock):

    - **Merge across rounds + dedup by id.** A finding id is unique within a round
      but may recur across rounds; the merged entry takes the record from the
      **highest round index** in which the id appears (latest triage and confirm
      verdicts win). No id appears twice.
    - **Partition.** *Residual risk* = ids whose final triage verdict is
      ``legitimate`` and whose last confirm verdict is not ``resolved`` (i.e.
      ``unresolved`` / ``partially_resolved`` / ``regression_introduced`` — or
      absent, meaning never confirmed resolved), restricted to non-blocking
      severities. *Declined* = ids whose final triage verdict is not
      ``legitimate``, carrying their triage reasoning. A finding with no triage
      record at all is neither (an anomaly, not surfaced).
    - **Deterministic order.** Within each partition, sort by severity rank
      (blocking > major > minor > nit), then ascending merged round index, then id
      lexicographically — never dict/iteration order.
    """
    merged: dict[str, dict] = {}
    for rec in sorted(rounds, key=lambda r: r.round):
        for f in rec.findings:
            fid = f.get("id")
            if fid is None:
                continue
            m = merged.setdefault(fid, {"id": fid})
            m["severity"] = f.get("severity", "")
            m["location"] = f.get("location", "")
            m["claim"] = f.get("claim", "")
            m["round"] = rec.round
        for v in rec.triage:
            fid = v.get("finding_id")
            if fid is None:
                continue
            m = merged.setdefault(fid, {"id": fid})
            m["triage_verdict"] = v.get("verdict")
            m["triage_reasoning"] = v.get("reasoning", "")
            m["round"] = rec.round
        for c in rec.confirm:
            fid = c.get("finding_id")
            if fid is None:
                continue
            m = merged.setdefault(fid, {"id": fid})
            m["confirm_verdict"] = c.get("verdict")
            m["round"] = rec.round

    residual: list[SummaryFinding] = []
    declined: list[SummaryFinding] = []
    for m in merged.values():
        verdict = m.get("triage_verdict")
        sf = SummaryFinding(
            id=m["id"],
            severity=m.get("severity", ""),
            location=m.get("location", ""),
            claim=m.get("claim", ""),
            triage_verdict=verdict,
            triage_reasoning=m.get("triage_reasoning", ""),
            confirm_verdict=m.get("confirm_verdict"),
            round=m.get("round", 0),
        )
        if verdict == _LEGITIMATE:
            # A legitimate blocking finding parks the run (FR-3.2) and never
            # reaches a completion summary; exclude it defensively. Everything
            # else legitimate but not confirmed-resolved is residual risk.
            if sf.severity != "blocking" and sf.confirm_verdict != _CONFIRM_RESOLVED:
                residual.append(sf)
        elif verdict is not None:
            declined.append(sf)

    def _key(sf: SummaryFinding) -> tuple[int, int, str]:
        return (_SEVERITY_RANK.get(sf.severity, 99), sf.round, sf.id)

    residual.sort(key=_key)
    declined.sort(key=_key)
    return ReviewSummary(residual_risk=residual, declined=declined)


def _collect_round_records(state_dir: Path) -> list[RoundRecord]:
    """Assemble the cycle's persisted records into round records for the summary.

    Reads the terminal authoritative artifacts the cycle already persists —
    ``artifacts/{findings,triage,confirm}.json`` under the run dir (latest round
    wins). This is the highest-round record for every id present at completion,
    which is exactly what :func:`summarize_cycle`'s cross-round merge selects; the
    default ``max_rounds: 1`` review has a single round, so this is the whole
    story. Missing/torn artifacts read as empty (fail soft for the summary: an
    absent record simply surfaces nothing, never a crash)."""
    artifacts = state_dir / "artifacts"

    def _load(name: str, key: str) -> list[dict]:
        path = artifacts / name
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return []
        value = data.get(key)
        return value if isinstance(value, list) else []

    findings = _load("findings.json", "findings")
    triage = _load("triage.json", "verdicts")
    confirm = _load("confirm.json", "verdicts")
    if not (findings or triage or confirm):
        return []
    return [RoundRecord(round=1, findings=findings, triage=triage, confirm=confirm)]


def _find_cycle_step(raw: dict) -> dict:
    """The single ``adversarial_cycle`` step dict in a parsed ``review.yaml``.

    Raises ``ReviewError`` if the asset is malformed (no cycle step) — fail closed
    rather than drive a review that reviews nothing."""
    for stage in raw.get("stages", []):
        for step in stage.get("steps", []):
            if step.get("type") == "adversarial_cycle":
                return step
    raise ReviewError(
        f"{_REVIEW_PIPELINE} has no adversarial_cycle step; the review pipeline "
        "asset is malformed"
    )


def _prepend_baseline_tests(raw: dict, config: RunConfig) -> None:
    """Inject the optional baseline-tests shell step ahead of the cycle (FR-1.1).

    ``--test`` opts a review into a pre-review test baseline; the CLI has already
    verified ``config.test_command`` is set (else a usage error), so the step runs
    a concrete command. A failing baseline fails the run closed before the cycle
    (the shell step's non-zero exit is a step failure), giving the correctness
    reviewer a known-green (or known-red) starting point."""
    command = (config.test_command or "").strip()
    stage = raw["stages"][0]
    stage["steps"].insert(
        0,
        {
            "id": "baseline-tests",
            "type": "shell",
            "run": command,
            "timeout_s": 1800,
        },
    )


def _bind_review_pipeline(
    repo_root: Path, config: RunConfig, resolution: ReviewResolution
):
    """Load ``review.yaml``, inject the runtime fields, and snapshot it.

    The review command injects what the cycle cannot know statically:
    ``review_base`` (the merge-base SHA, so the two-dot ``range_diff`` yields the
    FR-5.2 three-dot scope), ``max_rounds`` (from ``--rounds``), and — unless
    ``--code-only`` — the resolved ``intent_path`` + provenance threaded into the
    reviewer/triager prompts (FR-2.2). The **modified** pipeline (fields injected,
    optional baseline step prepended) is snapshotted to ``state_dir/pipeline.yaml``
    and its content hash recorded, so a ``resume`` reloads exactly what drove the
    run (FR-5.6 / FR-9.1) — the injected concrete SHA and out-of-repo intent path
    are stable across a resume. Returns ``(pipeline, content_hash)``."""
    import yaml

    from gauntlet.engine.pipeline import Pipeline, content_hash

    pipeline_path = repo_root / config.asset_root / _REVIEW_PIPELINE
    if not pipeline_path.is_file():
        raise ReviewFailClosed(
            f"review pipeline asset {_REVIEW_PIPELINE!r} is missing under "
            f"asset_root {config.asset_root!r}; run `gauntlet init` to scaffold "
            "the review pipeline + prompt, or add it before running a review."
        )
    raw = yaml.safe_load(pipeline_path.read_text())
    if not isinstance(raw, dict):
        raise ReviewError(f"{_REVIEW_PIPELINE} is not a YAML mapping")

    if resolution.run_tests:
        _prepend_baseline_tests(raw, config)

    step = _find_cycle_step(raw)
    step["review_base"] = resolution.merge_base
    step["max_rounds"] = resolution.rounds
    if resolution.intent_path is not None:
        step["intent_path"] = str(resolution.intent_path)
        step["intent_provenance"] = resolution.intent_record.provenance
        step["intent_independent"] = resolution.intent_record.independent

    snapshot = yaml.safe_dump(raw, sort_keys=False)
    phash = content_hash(snapshot)
    (resolution.state_dir / "pipeline.yaml").write_text(snapshot)
    return Pipeline.model_validate(raw), phash


def _judge_model(config: RunConfig) -> str | None:
    profile = config.agents.get("judge_llm")
    return profile.model if profile is not None else None


def _with_review_judge(config, repo_root: Path, state_dir: Path, man: Manifest, fn):
    """Run ``fn(judge_env)`` under a run-scoped judge (FR-7.1).

    Mirrors the heavyweight run's judge wiring: a review run carries a ``RUN_ID``
    and ``step_id`` so the existing FR-9.8 rule keeps denying in-step
    ``git push`` / ``gh pr create`` — fixes land locally, the operator pushes.
    The judge enforces the run's own ``policy.yaml`` and audits to the (out-of-repo)
    state dir. Started before the drive and stopped after, always."""
    from gauntlet.engine.judgeproc import ManagedJudge

    judge = ManagedJudge(
        policy_path=repo_root / config.asset_root / "policy.yaml",
        audit_path=state_dir / "judge-audit.jsonl",
        run_id=man.run_id,
        judge_model=_judge_model(config),
        repo_root=repo_root,
        run_dir=state_dir,
    )
    env = judge.start()
    try:
        return fn(env)
    finally:
        judge.stop()


def _build_review_orchestrator(
    repo_root: Path,
    config: RunConfig,
    resolution: ReviewResolution,
    pipeline,
    man: Manifest,
    *,
    judge_env: dict,
    adapter_factory,
    writer,
    clock,
    response_action,
):
    from gauntlet.engine.orchestrator import Orchestrator

    kwargs = dict(
        repo_root=repo_root,
        run_dir=resolution.state_dir,
        artifact_root=resolution.state_dir,
        config=config,
        pipeline=pipeline,
        manifest=man,
        writer=writer,
        judge_env=judge_env,
        adapter_factory=adapter_factory,
        response_action=response_action,
    )
    if clock is not None:
        kwargs["clock"] = clock
    orch = Orchestrator(**kwargs)
    # FR-2.4: an in-repo, untracked --intent file is excluded from the cycle's
    # clean-tree / clean-handoff checks and from every REVIEW.x fix commit, so it
    # is neither swept into a commit nor allowed to trip the handoff guard. The
    # engine's own bookkeeping excludes (run dir, PR.md) are already resolved by
    # the Orchestrator; append the review's intent exclude to that same set.
    for rel in resolution.excludes:
        if rel not in orch.excludes:
            orch.excludes.append(rel)
    return orch


def _outcome(state_dir: Path, man: Manifest, status: str) -> ReviewOutcome:
    return ReviewOutcome(
        status=status,
        parked=(status == M.RUN_PARKED),
        commits=[(c.phase, c.sha) for c in man.commits],
        summary=summarize_cycle(_collect_round_records(state_dir)),
        state_dir=state_dir,
        cycle_notes=_cycle_notes(man),
    )


def _cycle_notes(man: Manifest) -> str:
    rec = man.record("review-cycle")
    return (rec.notes or "").strip() if rec is not None else ""


def drive_review(
    repo_root: Path,
    config: RunConfig,
    resolution: ReviewResolution,
    *,
    adapter_factory: Callable[[str], object] | None = None,
    use_judge: bool = True,
    writer: object | None = None,
    clock: Callable[[], str] | None = None,
) -> ReviewOutcome:
    """Execute the review `adversarial_cycle` for a resolved run (P3).

    Binds ``review.yaml`` with the injected ``review_base`` / ``max_rounds`` /
    intent (FR-2.2/FR-5.2), rebinds the manifest's pipeline ref, then drives the
    single-stage, zero-gate pipeline to a terminal state. The cycle's fail-closed
    behavior is preserved unchanged (FR-3.2): an unresolved legitimate blocking
    finding parks; anything else completes. The returned :class:`ReviewOutcome`
    carries the FR-3.4 residual-risk / declined partition and the REVIEW.x
    commits."""
    from gauntlet.logging.redact import RedactingWriter

    state_dir = resolution.state_dir
    pipeline, phash = _bind_review_pipeline(repo_root, config, resolution)

    man = Manifest.load(resolution.manifest_path)
    man.pipeline = PipelineRef(
        name=pipeline.name, version=pipeline.version, hash=phash
    )
    man.write_atomic(resolution.manifest_path)

    writer = writer or RedactingWriter()

    def _drive(judge_env: dict) -> str:
        orch = _build_review_orchestrator(
            repo_root, config, resolution, pipeline, man,
            judge_env=judge_env, adapter_factory=adapter_factory,
            writer=writer, clock=clock, response_action=None,
        )
        return orch.drive()

    if use_judge:
        status = _with_review_judge(config, repo_root, state_dir, man, _drive)
    else:
        status = _drive({})
    return _outcome(state_dir, man, status)


def load_review_run(state_dir: Path) -> Manifest | None:
    """The bound, non-terminal review run at ``state_dir``, if one exists.

    A review run is *resumable* once its cycle has been wired (``pipeline.yaml``
    snapshotted, FR-9.1) and it is not in a terminal state. Returns the manifest
    then, else ``None`` — so the CLI can refuse to clobber a parked run and route
    a ``--response`` to it instead."""
    manifest_path = state_dir / "manifest.json"
    if not (manifest_path.is_file() and (state_dir / "pipeline.yaml").is_file()):
        return None
    try:
        man = Manifest.load(manifest_path)
    except (OSError, ValueError):
        return None
    if man.pipeline.hash == "" or man.status in _TERMINAL_STATUSES:
        return None
    return man


def resume_review(
    repo_root: Path,
    config: RunConfig,
    state_dir: Path,
    *,
    response: str | None = None,
    adapter_factory: Callable[[str], object] | None = None,
    use_judge: bool = True,
    writer: object | None = None,
    clock: Callable[[], str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> ReviewOutcome:
    """Resume a parked/failed review run, optionally injecting a `--response` (FR-3.2).

    Reloads the snapshotted pipeline (refusing a content-hash drift, FR-5.6),
    re-adopts the target branch, and re-drives the cycle. A ``--response`` on a
    parked cycle escalation (or a failed cycle) is appended as an authoritative
    human decision and injected into the reviewer/triager on the re-drive (the
    same FR-10.4 mechanism the heavyweight run uses); a response-less resume just
    re-drives from the last checkpoint."""
    from gauntlet.engine.pipeline import load_pipeline
    from gauntlet.logging.redact import RedactingWriter

    environ = environ if environ is not None else dict(os.environ)
    manifest_path = state_dir / "manifest.json"
    man = Manifest.load(manifest_path)
    pipeline, phash = load_pipeline(state_dir / "pipeline.yaml")
    if phash != man.pipeline.hash:
        raise ReviewFailClosed(
            "the snapshotted review pipeline changed since the run started "
            f"({man.pipeline.hash} -> {phash}); resume refuses to run a different "
            "pipeline against an existing manifest (FR-5.6)."
        )

    # Re-adopt the target branch in place so HEAD is where REVIEW.x commits land.
    if gitops.current_branch(repo_root) != man.branch:
        if not gitops.branch_exists(repo_root, man.branch):
            raise ReviewFailClosed(
                f"resume: review target branch {man.branch!r} is missing; "
                "restore it before resuming."
            )
        try:
            gitops.checkout_branch(repo_root, man.branch)
        except gitops.GitError as exc:
            raise ReviewFailClosed(
                f"resume: could not check out target branch {man.branch!r}: {exc}"
            ) from exc

    action = _plan_review_response(repo_root, man, response, environ)

    writer = writer or RedactingWriter()
    resolution = _resolution_from_manifest(man, state_dir)

    def _drive(judge_env: dict) -> str:
        orch = _build_review_orchestrator(
            repo_root, config, resolution, pipeline, man,
            judge_env=judge_env, adapter_factory=adapter_factory,
            writer=writer, clock=clock, response_action=action,
        )
        return orch.drive()

    if use_judge:
        status = _with_review_judge(config, repo_root, state_dir, man, _drive)
    else:
        status = _drive({})
    return _outcome(state_dir, man, status)


def _resolution_from_manifest(man: Manifest, state_dir: Path) -> ReviewResolution:
    """A minimal :class:`ReviewResolution` for a resume drive.

    Only the fields :func:`_build_review_orchestrator` reads are needed — the
    state dir and the intent exclude. The excludes are not persisted in the
    manifest (they are worktree-local), so a resumed run does not re-derive the
    in-repo ``--intent`` exclude; a review whose intent lived inside the repo is
    an uncommon case, and the intent file is untracked either way. The rest of the
    fields carry manifest-recorded values for completeness."""
    return ReviewResolution(
        slug=man.slug,
        target_branch=man.branch,
        base_ref=man.base_branch,
        merge_base="",  # already baked into the snapshotted pipeline's review_base
        state_dir=state_dir,
        manifest_path=state_dir / "manifest.json",
        intent_path=None,
        intent_record=man.intent
        or IntentRecord(source="unknown", provenance=M.PROVENANCE_NONE, independent=False),
        excludes=[],
        rounds=ROUNDS_MIN,
        code_only=man.intent is not None and man.intent.source == M.INTENT_SOURCE_CODE_ONLY,
        run_tests=False,
    )


def _plan_review_response(
    repo_root: Path,
    man: Manifest,
    response: str | None,
    environ: Mapping[str, str],
):
    """Plan the `--response` transition for a review resume (fail closed).

    Recovery FIRST (a still-``pending`` entry from a crashed transition is
    reused, never re-appended, FR-7.1); then a response-less re-drive
    (``kind='none'``); then a new ``--response`` append onto the run's stuck
    respondable cycle step. Operator identity is resolved LAST so a fail-closed
    identity error leaves the manifest untouched."""
    from gauntlet.engine.orchestrator import ResponseAction

    for rec in man.steps:
        if rec.human_responses and rec.human_responses[-1].state == M.RESPONSE_PENDING:
            latest = rec.human_responses[-1]
            if response is not None and response != latest.response_text:
                raise ReviewFailClosed(
                    f"a pending response ({latest.response_id}) is awaiting "
                    f"processing; re-run resume to finish it before supplying a "
                    "new one."
                )
            return ResponseAction(kind="recover", step_id=rec.id, iteration=rec.iteration)

    if response is None:
        # A cycle-escalation park is response-resolvable (FR-3.2/FR-10.4): a plain
        # re-drive would only re-surface it. Require a decision, like the
        # heavyweight resume does — never a silent re-park loop.
        for rec in man.steps:
            if (
                rec.status == M.PARKED
                and rec.parked_reason in M.RESPONSE_RESOLVABLE_PARK_REASONS
            ):
                raise ReviewFailClosed(
                    f"review step {rec.id!r} parked on a cycle escalation its own "
                    'loop cannot resolve; resume it with --response "<decision>". '
                    "Re-running without a decision would only re-surface it."
                )
        return ResponseAction(kind="none")

    if man.status not in (M.RUN_PARKED, M.RUN_FAILED):
        raise ReviewFailClosed(
            f"review run is {man.status}, neither parked nor failed; cannot "
            "resume with --response."
        )
    stuck = None
    for rec in man.steps:
        if rec.status == M.PARKED:
            stuck = rec
            break
    if stuck is None:
        for rec in reversed(man.steps):
            if rec.status == M.FAILED:
                stuck = rec
                break
    if stuck is None:
        raise ReviewFailClosed(
            "review run has no parked or failed step to resume with --response."
        )
    if stuck.type not in M.RESPONDABLE_STEP_TYPES:
        raise ReviewFailClosed(
            f"step {stuck.id!r} is a {stuck.type}; --response only applies to "
            f"{' / '.join(sorted(M.RESPONDABLE_STEP_TYPES))} steps."
        )
    try:
        user = resolve_operator_identity(repo_root, environ)
    except OperatorIdentityError as exc:
        raise ReviewFailClosed(f"cannot record the response: {exc}") from exc
    return ResponseAction(
        kind="append", step_id=stuck.id, iteration=stuck.iteration,
        text=response, user=user,
    )

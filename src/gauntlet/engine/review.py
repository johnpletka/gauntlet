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
    """``${XDG_STATE_HOME:-~/.local/state}`` from the environment (§6)."""
    xdg = environ.get("XDG_STATE_HOME")
    if xdg:
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
    return (
        _xdg_state_home(environ)
        / "gauntlet"
        / "reviews"
        / repo_id
        / slug
    )


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
        return M.INTENT_SOURCE_EDITOR

    def _intent_excludes(self, inputs: ReviewInputs) -> list[str]:
        """Repo-relative exclude for an in-repo ``--intent`` file (FR-2.4).

        A ``--intent`` path resolving inside the repo is added to the run's
        worktree-exclude set so it cannot trip the clean-tree entry contract or
        be swept into a fix commit; the file is left present and untracked. A
        path outside the repo needs no exclusion.
        """
        if not inputs.intent_path:
            return []
        p = Path(inputs.intent_path)
        if not p.is_absolute():
            p = self.repo_root / p
        if not _within(p, self.repo_root):
            return []
        rel = _normabs(p).relative_to(_normabs(self.repo_root)).as_posix()
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
        except OSError as exc:
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

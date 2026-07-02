"""GitHub PR mode for `gauntlet review` (FR-4, P5).

`gauntlet review --pr <N|url>` pulls a GitHub PR down and reviews it exactly as
a local-branch review of its head branch (FR-4.1). This module owns the two
PR-specific concerns the branch flow does not have:

- **The PR checkout contract (FR-4.5)** — a *non-destructive* checkout. `gh pr
  checkout` will fast-forward an existing local branch, but we never want to
  silently rewrite a user's diverged local branch. So before letting `gh` touch
  anything, we fetch the PR head into a scratch ref (``refs/gauntlet/pr/<N>``),
  compute whether the existing local branch (if any) fast-forwards toward it, and
  **fail closed on divergence with the local branch untouched**. Only on a clean
  fast-forward (or when no local branch of that name exists) do we run
  ``gh pr checkout`` — then verify HEAD landed on a named branch (never detached).
- **PR metadata resolution (FR-4.1)** — the ``gh pr view`` JSON the review needs:
  head/base refs, cross-repository flag, title/body/url, head-repo owner.

The `gh`/`git fetch` reads are Gauntlet's own process calls (not hooked-agent
calls), so they are not routed through the PreToolUse judge (FR-7.2); the
deterministic FR-7.4 preflight — run by the lifecycle *before* any of these —
is the gate. The seam (`PrClient`) is injectable so the whole contract is
exercised offline against a real git fixture without a live `gh` or network.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from gauntlet.engine import gitops

# The scratch ref the PR head is fetched into for the divergence check (FR-4.5
# step 2). It lives outside refs/heads so it never shadows a real branch and is
# always safe to delete afterwards.
PR_TEMP_REF = "refs/gauntlet/pr/{n}"

# GitHub exposes every PR's head commit under refs/pull/<N>/head on the *base*
# repo — same-repo and fork PRs alike — so one fetch spec resolves both.
_PR_HEAD_REFSPEC = "pull/{n}/head:{ref}"

# A `--pr` value: a bare number, or a github.com PR URL. The URL form captures
# owner/repo so the caller can confirm the URL targets the *current* repo before
# reducing it to a bare number (a bare number can only ever mean this repo).
_PR_NUM_RE = re.compile(r"^[0-9]+$")
_PR_URL_RE = re.compile(
    r"github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<num>[0-9]+)",
    re.IGNORECASE,
)


class PrModeError(RuntimeError):
    """A PR-mode failure the lifecycle surfaces as a fail-closed halt.

    Kept distinct from ``ReviewFailClosed`` so this module has no import cycle
    with ``engine.review``; the lifecycle catches it and re-raises the
    review-level fail-closed error with the same message.
    """


@dataclass(frozen=True)
class PrMetadata:
    """The `gh pr view` fields the review needs (FR-4.1)."""

    number: str
    head_ref: str            # headRefName
    base_ref: str            # baseRefName (e.g. "main")
    is_cross_repository: bool
    title: str
    body: str
    url: str
    head_owner: str | None   # headRepositoryOwner login, for the fork branch name


@dataclass(frozen=True)
class PrCheckout:
    """The result of the FR-4.5 checkout: metadata + the landed local branch."""

    metadata: PrMetadata
    local_branch: str


@dataclass(frozen=True)
class PrRef:
    """A parsed ``--pr`` value (usage-class): the number plus URL owner/repo.

    ``owner``/``repo`` are the case-folded slug halves of a github.com PR URL, so
    the lifecycle can confirm the URL names the *current* repo before applying its
    number to repo-scoped commands (FR-1.1/FR-4.1). Both are ``None`` for a bare
    number — that unambiguously means the current repo, so there is nothing to
    confirm.
    """

    number: str
    owner: str | None = None
    repo: str | None = None


def parse_pr_ref(raw: str) -> PrRef:
    """The :class:`PrRef` for a bare number or a github.com PR URL (usage-class).

    Raises :class:`ValueError` (a usage-class failure the CLI maps to exit 2)
    when ``raw`` is neither a positive integer nor a recognizable PR URL — never
    a fail-closed run halt, since no network happened.
    """
    s = (raw or "").strip()
    if _PR_NUM_RE.match(s):
        return PrRef(number=s)
    m = _PR_URL_RE.search(s)
    if m:
        return PrRef(
            number=m.group("num"),
            owner=m.group("owner").lower(),
            repo=m.group("repo").lower(),
        )
    raise ValueError(
        f"--pr must be a PR number or a github.com/<owner>/<repo>/pull/<N> URL; "
        f"got {raw!r}"
    )


def parse_pr_number(raw: str) -> str:
    """The PR number from a bare number or a github.com PR URL (usage-class).

    Thin wrapper over :func:`parse_pr_ref` for callers that only key on the
    number (state-dir slug, usage validation); raises :class:`ValueError` on a
    malformed value exactly as :func:`parse_pr_ref` does.
    """
    return parse_pr_ref(raw).number


class PrClient(Protocol):
    """The injectable seam over `gh` / `git fetch` for PR mode (FR-4.1/4.5)."""

    def view(self, pr: str) -> PrMetadata:
        """Resolve PR metadata (`gh pr view --json …`)."""
        ...

    def fetch_head(self, pr: str, into_ref: str) -> None:
        """Fetch the PR head into ``into_ref`` (`git fetch origin pull/<N>/head:…`).

        Must NOT touch any local branch — it only writes the scratch ref, so the
        divergence check can compare against the user's untouched local branch.
        """
        ...

    def checkout(self, pr: str) -> None:
        """Check the PR out locally (`gh pr checkout <N>`), leaving HEAD on a
        named branch."""
        ...


class GhPrClient:
    """The real `gh` / `git` implementation of :class:`PrClient` (FR-4.1/4.5).

    Every call is a read (`gh pr view`, `git fetch`, `gh pr checkout`); none
    mutate a remote. They are blessed on the deterministic fast path by the
    ratified ``pr_read_commands@v1`` policy rule the FR-7.4 preflight verifies
    before this client is ever constructed.
    """

    def __init__(self, repo_root: Path, *, runner=subprocess.run) -> None:
        self.repo_root = repo_root
        self._runner = runner

    def _gh(self, *args: str) -> str:
        proc = self._runner(
            ["gh", *args],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise PrModeError(
                f"`gh {' '.join(args)}` failed (exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )
        return proc.stdout

    def view(self, pr: str) -> PrMetadata:
        fields = "headRefName,baseRefName,isCrossRepository,title,body,url,headRepositoryOwner"
        out = self._gh("pr", "view", pr, "--json", fields)
        try:
            data = json.loads(out)
        except ValueError as exc:
            raise PrModeError(f"`gh pr view {pr}` returned non-JSON output") from exc
        owner = data.get("headRepositoryOwner") or {}
        return PrMetadata(
            number=pr,
            head_ref=data.get("headRefName") or "",
            base_ref=data.get("baseRefName") or "",
            is_cross_repository=bool(data.get("isCrossRepository")),
            title=data.get("title") or "",
            body=data.get("body") or "",
            url=data.get("url") or "",
            head_owner=(owner.get("login") if isinstance(owner, dict) else None),
        )

    def fetch_head(self, pr: str, into_ref: str) -> None:
        # `git fetch` into a scratch ref; touches no local branch (FR-4.5 step 2).
        proc = self._runner(
            ["git", "-C", str(self.repo_root), "fetch", "origin",
             _PR_HEAD_REFSPEC.format(n=pr, ref=into_ref)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise PrModeError(
                f"`git fetch` of PR #{pr} head failed (exit {proc.returncode}): "
                f"{(proc.stderr or '').strip()}"
            )

    def checkout(self, pr: str) -> None:
        self._gh("pr", "checkout", pr)


def candidate_branch(md: PrMetadata, pr_number: str) -> str:
    """The local branch name ``gh pr checkout`` will land the PR on (FR-4.5).

    The head ref for a same-repo PR; ``<owner>/<head-ref>`` for a fork (`gh`'s
    fork-disambiguated name). Exposed so the lifecycle can learn the target
    branch from ``gh pr view`` metadata *before* the checkout — e.g. to refuse a
    competing run that already owns it (FR-9.3) before the worktree is touched.
    Raises :class:`PrModeError` when no head branch name resolves — the same
    fail-closed condition :func:`resolve_pr_checkout` enforces.
    """
    if md.is_cross_repository and md.head_owner:
        candidate = f"{md.head_owner}/{md.head_ref}"
    else:
        candidate = md.head_ref
    if not candidate:
        raise PrModeError(
            f"PR #{pr_number} resolved no head branch name from `gh pr view`; "
            "cannot check it out"
        )
    return candidate


def resolve_pr_checkout(
    repo_root: Path, pr_number: str, client: PrClient, *, md: PrMetadata | None = None
) -> PrCheckout:
    """Run the FR-4.5 PR checkout contract; fail closed on divergence/detach.

    Order (so implementations behave identically and never clobber local work):

    1. ``gh pr view`` → metadata; compute the candidate local branch name (the
       head ref for a same-repo PR, ``<owner>/<head-ref>`` for a fork — `gh`'s
       fork-disambiguated name).
    2. Record the candidate local branch's current SHA **before any ref update**
       (``None`` if it does not exist).
    3. Fetch the PR head into the scratch ref (touches no local branch).
    4. If the local branch exists and does **not** fast-forward toward the
       fetched head, fail closed — the local branch is left exactly as it was
       (:class:`PrModeError`).
    5. Otherwise run ``gh pr checkout`` and confirm HEAD is on a named branch
       (never detached), else fail closed.

    The scratch ref is always deleted (``finally``), including on every
    fail-closed path.

    ``md`` may be supplied by a caller that already ran ``gh pr view`` (e.g. to
    compute the candidate branch for a pre-checkout competing-run refusal), so
    the view is not repeated; when ``None`` it is fetched here.
    """
    md = md or client.view(pr_number)
    candidate = candidate_branch(md, pr_number)

    temp_ref = PR_TEMP_REF.format(n=pr_number)
    existing_sha = (
        gitops.rev_parse(repo_root, f"refs/heads/{candidate}")
        if gitops.branch_exists(repo_root, candidate)
        else None
    )
    try:
        client.fetch_head(pr_number, temp_ref)
        fetched_sha = gitops.rev_parse(repo_root, temp_ref)
        if existing_sha is not None and not gitops.is_ancestor(
            repo_root, existing_sha, fetched_sha
        ):
            # FR-4.5 step 3: a non-fast-forward update would rewrite the user's
            # diverged local branch. Refuse BEFORE touching it — the recorded
            # SHA is left unchanged.
            raise PrModeError(
                f"local branch {candidate!r} has diverged from PR #{pr_number}'s "
                f"head (updating it would not be a fast-forward); refusing to "
                "rewrite it. Reconcile it (or delete the local branch) and re-run."
            )
        client.checkout(pr_number)
    finally:
        gitops.delete_ref(repo_root, temp_ref)

    branch = gitops.current_branch(repo_root)
    if branch == "HEAD":
        # FR-4.5 step 5 / FR-5.3: a detached HEAD has no branch to land REVIEW.x
        # commits on.
        raise PrModeError(
            f"checking out PR #{pr_number} left HEAD detached; a review needs a "
            "named branch to land any REVIEW.x fix commits on"
        )
    return PrCheckout(metadata=md, local_branch=branch)

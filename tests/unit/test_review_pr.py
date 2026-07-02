"""`gauntlet review --pr` PR mode (FR-4 / FR-7.4 preflight), P5.

Exercised OFFLINE against a real git fixture with an injected fake `PrClient`
(no live `gh`, no network) and a monkeypatched tracker, so the whole PR checkout
contract + linked-ticket auto-derive + fail-closed paths run without creds:

- FR-7.4 preflight: an un-ratified `pr_read_commands@v1` fails closed with the
  EXACT message and issues NO gh/git-fetch command (the fake records calls);
  branch mode is unaffected by a rule-less policy.
- FR-4.5 checkout contract: fresh checkout lands on a named branch; a diverged
  existing local branch fails closed with its SHA unchanged; a detached result
  fails closed.
- FR-4.1/4.2: base = the PR base ref, three-dot; review proceeds as a
  local-branch review of the head branch.
- FR-4.3: linked-ticket auto-derive (first ref wins; extra refs recorded as
  ignored secondary); explicit `--issue` overrides; no ref + no explicit intent
  + no `--code-only` fails closed (never the PR body as intent).
- FR-4.4: a fork PR is flagged so the summary notes push-back is manual.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from gauntlet.engine import gitops
from gauntlet.engine import manifest as M
from gauntlet.engine import review as review_mod
from gauntlet.engine import reviewpr
from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import Manifest, PipelineRef
from gauntlet.engine.review import (
    Hooks,
    ReviewFailClosed,
    ReviewInputs,
    ReviewLifecycle,
    ReviewUsageError,
)
from gauntlet.trackers.base import Issue, IssueRef

from conftest import git

RATIFIED_POLICY = textwrap.dedent(
    """\
    version: 1
    allow:
      - name: pr-read-commands
        id: pr_read_commands
        version: v1
        ratified: true
        applies_to_tools: [Bash]
        command_patterns:
          - '^\\s*gh\\s+pr\\s+(view|checkout)\\b'
          - '^\\s*git\\s+fetch\\b'
    """
)
RULELESS_POLICY = textwrap.dedent(
    """\
    version: 1
    allow:
      - name: git-readonly
        applies_to_tools: [Bash]
        command_patterns:
          - '^\\s*git\\s+status\\b'
    """
)

_EXACT_ABSENT_MSG = (
    "P4 (PR mode) requires policy rule 'pr_read_commands@v1' to be ratified in "
    "policy.yaml; it is absent. Ratify it through the policy-change process "
    "(Open Question 11.4) before using --pr."
)


def _config(*, tracker: bool = True) -> RunConfig:
    data: dict = {"base_branch": "main", "run_root": "runs", "asset_root": "."}
    if tracker:
        data["issue_tracker"] = {"provider": "linear", "api_key_env": "LINEAR_API_KEY"}
    return RunConfig.model_validate(data)


def _hooks(repo: Path, tmp_path: Path, client) -> Hooks:
    return Hooks(
        isatty=lambda: False,
        environ={"XDG_STATE_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "home")},
        pr_client=client,
    )


def _write_policy(repo: Path, body: str) -> None:
    """Write + commit policy.yaml on main so it is present (and the tree clean)
    after any later branch checkout — the preflight reads repo_root/policy.yaml."""
    (repo / "policy.yaml").write_text(body)
    git(repo, "add", "policy.yaml")
    git(repo, "commit", "-qm", "add policy")


def _pr_head_branch(repo: Path, name: str, *, files: dict[str, str]) -> str:
    """Create `name` off main with `files`, return its head SHA; end back on main."""
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-q", "-b", name)
    for rel, content in files.items():
        (repo / rel).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"{name} changes")
    sha = git(repo, "rev-parse", "HEAD").strip()
    git(repo, "checkout", "-q", "main")
    return sha


class FakePrClient:
    """Offline `PrClient`: canned metadata + a real-git fetch/checkout on the fixture."""

    def __init__(self, repo: Path, md: reviewpr.PrMetadata, head_sha: str,
                 *, local_branch: str, detach: bool = False) -> None:
        self.repo = repo
        self.md = md
        self.head_sha = head_sha
        self.local_branch = local_branch
        self.detach = detach
        self.calls: list[tuple] = []

    def view(self, pr: str) -> reviewpr.PrMetadata:
        self.calls.append(("view", pr))
        return self.md

    def fetch_head(self, pr: str, into_ref: str) -> None:
        self.calls.append(("fetch", pr, into_ref))
        git(self.repo, "update-ref", into_ref, self.head_sha)

    def checkout(self, pr: str) -> None:
        self.calls.append(("checkout", pr))
        if self.detach:
            git(self.repo, "checkout", "-q", "--detach", self.head_sha)
        else:
            git(self.repo, "checkout", "-q", "-B", self.local_branch, self.head_sha)


class FakeTracker:
    name = "linear"

    def __init__(self, *, refs=None, issue=None, fetch_error=None) -> None:
        self._refs = refs or []
        self._issue = issue
        self._fetch_error = fetch_error

    def parse_ref(self, raw: str) -> IssueRef:
        return IssueRef(provider="linear", raw=raw, key=raw.strip().upper())

    def extract_refs(self, text: str) -> list[IssueRef]:
        return list(self._refs)

    def fetch(self, ref: IssueRef) -> Issue:
        if self._fetch_error is not None:
            raise self._fetch_error
        return self._issue

    def verify_auth(self) -> None:
        return None


def _issue(key: str = "ENG-1234") -> Issue:
    return Issue(
        identifier=key, title="Widget crashes on empty input",
        description="Clicking Save with an empty widget name throws NPE.",
        url=f"https://linear.app/acme/issue/{key}", state="In Progress",
    )


def _md(number: str = "7", *, body: str, head="feature", base="main",
        fork=False, owner=None) -> reviewpr.PrMetadata:
    return reviewpr.PrMetadata(
        number=number, head_ref=head, base_ref=base, is_cross_repository=fork,
        title="Fix widget crash", body=body, url=f"https://github.com/acme/w/pull/{number}",
        head_owner=owner,
    )


def _plant_review_run(repo, tmp_path, *, slug, branch, status=M.RUN_RUNNING):
    """Write a lightweight review run's manifest under the out-of-repo review
    state root, so a competing-run scan (FR-9.3, F-002) can discover it. Mirrors
    the XDG layout `_hooks` configures (`tmp_path/xdg`)."""
    repo_id = review_mod.derive_repo_id(repo)
    d = tmp_path / "xdg" / "gauntlet" / "reviews" / repo_id / slug
    d.mkdir(parents=True, exist_ok=True)
    man = Manifest(
        run_id=slug, slug=slug, branch=branch, base_branch="main",
        pipeline=PipelineRef(name="review", version=1, hash=""), status=status,
    )
    man.write_atomic(d / "manifest.json")


def _lifecycle(repo, tmp_path, client, *, tracker=None, cfg=None) -> ReviewLifecycle:
    lc = ReviewLifecycle(repo, cfg or _config(), hooks=_hooks(repo, tmp_path, client))
    if tracker is not None:
        # _get_tracker() calls the module-level get_tracker; return the fake so
        # extract_refs/fetch run offline (no LINEAR_API_KEY, no network).
        lc._fake_tracker = tracker
    return lc


@pytest.fixture(autouse=True)
def _patch_get_tracker(monkeypatch):
    """Route review._get_tracker() to a per-lifecycle fake when one is attached."""
    real = review_mod.get_tracker

    def fake_get_tracker(config, *, env=None, transport=None):  # noqa: ARG001
        raise AssertionError("real get_tracker should not be called in PR unit tests")

    monkeypatch.setattr(review_mod, "get_tracker", fake_get_tracker)
    # Patch the bound _get_tracker to consult the attached fake first.
    orig = ReviewLifecycle._get_tracker

    def patched(self):
        fake = getattr(self, "_fake_tracker", None)
        if fake is not None:
            return fake
        return None if (self.config.issue_tracker is None or not self.config.issue_tracker.enabled) else orig(self)

    monkeypatch.setattr(ReviewLifecycle, "_get_tracker", patched)
    yield


# --- FR-7.4 preflight ---------------------------------------------------------

def test_pr_preflight_fails_closed_when_rule_absent(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RULELESS_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print(1)\n"})
    client = FakePrClient(fixture_repo, _md(body="Fixes ENG-1234"), head, local_branch="feature")
    lc = _lifecycle(fixture_repo, tmp_path, client, tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()))
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="7"))
    assert str(exc.value) == _EXACT_ABSENT_MSG
    # No gh/git-fetch command issued, no checkout, HEAD unmoved.
    assert client.calls == []
    assert gitops.current_branch(fixture_repo) == "main"


def test_branch_mode_unaffected_by_missing_rule(fixture_repo, tmp_path):
    # Branch mode issues none of the gated reads, so a rule-less policy is fine.
    _write_policy(fixture_repo, RULELESS_POLICY)
    git(fixture_repo, "checkout", "-q", "-b", "fix")
    (fixture_repo / "fix.py").write_text("print(1)\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "the fix")
    lc = _lifecycle(fixture_repo, tmp_path, client=None)
    res = lc.resolve(ReviewInputs(branch="fix", code_only=True))
    assert res.target_branch == "fix"
    assert res.pr_number is None


# --- FR-4.1/4.2/4.3 happy path ------------------------------------------------

def test_pr_same_repo_auto_derives_intent_from_linked_ticket(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")  # fresh: no pre-existing local branch
    md = _md(body="This PR. Fixes ENG-1234.")
    client = FakePrClient(fixture_repo, md, head, local_branch="feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    res = lc.resolve(ReviewInputs(pr="7"))

    assert res.target_branch == "feature"
    assert gitops.current_branch(fixture_repo) == "feature"
    assert res.base_ref == "main"          # PR base ref (origin/main absent → local main)
    assert res.merge_base                   # three-dot merge-base resolved
    assert res.slug == "pr-7"
    assert res.pr_number == "7"
    assert res.pr_chosen_ref == "ENG-1234"
    assert res.pr_ignored_refs == []
    assert res.pr_is_fork is False
    # intent.md carries the ticket body AND the PR body as secondary context.
    intent = res.intent_path.read_text()
    assert "Clicking Save with an empty widget name" in intent
    assert "PR context (secondary" in intent
    assert res.intent_record.provenance == "tracker"
    assert res.intent_record.independent is True
    # Zero repo footprint: intent lives out-of-repo; only the fix commit exists.
    assert gitops.is_clean(fixture_repo)
    assert str(tmp_path / "xdg") in str(res.intent_path)


def test_pr_multi_ref_records_ignored_secondary(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    md = _md(body="Fixes ENG-1234 and ENG-5678")
    client = FakePrClient(fixture_repo, md, head, local_branch="feature")
    refs = [IssueRef("linear", "ENG-1234", "ENG-1234"), IssueRef("linear", "ENG-5678", "ENG-5678")]
    lc = _lifecycle(fixture_repo, tmp_path, client, tracker=FakeTracker(refs=refs, issue=_issue()))
    res = lc.resolve(ReviewInputs(pr="7"))
    assert res.pr_chosen_ref == "ENG-1234"          # first in textual order
    assert res.pr_ignored_refs == ["ENG-5678"]      # listed as ignored secondary


def test_pr_no_ref_and_no_explicit_intent_fails_closed(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    md = _md(body="No ticket linked here.")
    client = FakePrClient(fixture_repo, md, head, local_branch="feature")
    lc = _lifecycle(fixture_repo, tmp_path, client, tracker=FakeTracker(refs=[]))
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="7"))
    assert "links no linear ticket" in str(exc.value)
    assert "PR body is not used as the intent" in str(exc.value)


def test_pr_code_only_skips_intent_but_checks_out(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    client = FakePrClient(fixture_repo, _md(body="no ticket"), head, local_branch="feature")
    lc = _lifecycle(fixture_repo, tmp_path, client, tracker=FakeTracker(refs=[]))
    res = lc.resolve(ReviewInputs(pr="7", code_only=True))
    assert res.intent_path is None
    assert res.intent_record.provenance == "none"
    assert res.target_branch == "feature"


def test_pr_explicit_issue_overrides_body_autoderive(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    md = _md(body="Fixes ENG-9999")   # body ref would be ENG-9999
    client = FakePrClient(fixture_repo, md, head, local_branch="feature")
    # extract_refs is never consulted when --issue is explicit; fetch returns the
    # explicit ticket (parse_ref upper-cases the raw --issue value).
    lc = _lifecycle(fixture_repo, tmp_path, client, tracker=FakeTracker(issue=_issue("ENG-1234")))
    res = lc.resolve(ReviewInputs(pr="7", issue="ENG-1234"))
    assert res.pr_chosen_ref is None                 # not an auto-derive
    assert res.intent_record.provenance == "tracker"
    assert "PR context (secondary" in res.intent_path.read_text()


# --- FR-4.4 fork --------------------------------------------------------------

def test_pr_fork_flagged_for_manual_push(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    md = _md(body="Fixes ENG-1234", fork=True, owner="contributor")
    client = FakePrClient(fixture_repo, md, head, local_branch="contributor/feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    res = lc.resolve(ReviewInputs(pr="7"))
    assert res.pr_is_fork is True
    assert res.target_branch == "contributor/feature"


# --- FR-4.5 checkout contract -------------------------------------------------

def test_pr_diverged_local_branch_fails_closed_sha_unchanged(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    # The PR head.
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('pr')\n"})
    # Now make the local `feature` DIVERGE: reset it to its own separate commit
    # that is not an ancestor of the PR head.
    git(fixture_repo, "checkout", "-q", "feature")
    (fixture_repo / "local.py").write_text("print('local only')\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "local divergent work")
    diverged_sha = git(fixture_repo, "rev-parse", "HEAD").strip()
    git(fixture_repo, "checkout", "-q", "main")

    client = FakePrClient(fixture_repo, _md(body="Fixes ENG-1234"), head, local_branch="feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="7"))
    assert "diverged" in str(exc.value)
    # The local branch's SHA is left exactly as it was — no destructive checkout.
    assert git(fixture_repo, "rev-parse", "refs/heads/feature").strip() == diverged_sha
    # checkout() was never called (we refused before touching the branch).
    assert ("checkout", "7") not in client.calls
    # The scratch ref was cleaned up.
    assert not gitops.ref_is_valid_commit(fixture_repo, "refs/gauntlet/pr/7")


def test_pr_fast_forward_existing_local_branch_succeeds(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    # Local `feature` sits at an ANCESTOR of the PR head (fast-forwardable).
    git(fixture_repo, "checkout", "-q", "-b", "feature")
    (fixture_repo / "a.py").write_text("print('a')\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "ancestor")
    # PR head extends feature with another commit.
    (fixture_repo / "b.py").write_text("print('b')\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "pr head")
    head = git(fixture_repo, "rev-parse", "HEAD").strip()
    git(fixture_repo, "reset", "-q", "--hard", "HEAD~1")   # local feature = ancestor
    git(fixture_repo, "checkout", "-q", "main")

    client = FakePrClient(fixture_repo, _md(body="Fixes ENG-1234"), head, local_branch="feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    res = lc.resolve(ReviewInputs(pr="7"))
    assert res.target_branch == "feature"
    assert ("checkout", "7") in client.calls


def test_pr_detached_result_fails_closed(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    client = FakePrClient(fixture_repo, _md(body="Fixes ENG-1234"), head,
                          local_branch="feature", detach=True)
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="7"))
    assert "detached" in str(exc.value)


# --- usage-class validation ---------------------------------------------------

def test_bad_pr_value_is_usage_error(fixture_repo, tmp_path):
    lc = _lifecycle(fixture_repo, tmp_path, client=None)
    with pytest.raises(ReviewUsageError):
        lc.resolve(ReviewInputs(pr="not-a-pr"))


def test_pr_url_parses_to_number(fixture_repo, tmp_path):
    _write_policy(fixture_repo, RATIFIED_POLICY)
    # A URL is only accepted when its owner/repo matches this repo's origin (see
    # test_pr_url_mismatched_repo_fails_closed); the scp-form origin normalizes
    # to acme/w, matching the URL.
    git(fixture_repo, "remote", "add", "origin", "git@github.com:acme/w.git")
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    client = FakePrClient(fixture_repo, _md(number="42", body="Fixes ENG-1234"),
                          head, local_branch="feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    res = lc.resolve(ReviewInputs(pr="https://github.com/acme/w/pull/42"))
    assert res.slug == "pr-42"
    assert res.pr_number == "42"


def test_pr_url_mismatched_repo_fails_closed(fixture_repo, tmp_path):
    # A URL for a DIFFERENT repo must not have its number applied to this repo's
    # PR N — refuse before any gh/git command runs (F-001, FR-4.1).
    _write_policy(fixture_repo, RATIFIED_POLICY)
    git(fixture_repo, "remote", "add", "origin", "git@github.com:acme/w.git")
    client = FakePrClient(fixture_repo, _md(number="42", body="Fixes ENG-1234"),
                          "deadbeef", local_branch="feature")
    lc = _lifecycle(fixture_repo, tmp_path, client, tracker=FakeTracker())
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="https://github.com/other/repo/pull/42"))
    assert "other/repo" in str(exc.value)
    assert "acme/w" in str(exc.value)
    # No gh/git-fetch command issued and HEAD unmoved (refused before any work).
    assert client.calls == []
    assert gitops.current_branch(fixture_repo) == "main"


def test_pr_url_without_origin_fails_closed(fixture_repo, tmp_path):
    # A URL cannot be confirmed to target this repo when there is no origin remote
    # (the default fixture has none); fail closed rather than guess (F-001).
    _write_policy(fixture_repo, RATIFIED_POLICY)
    client = FakePrClient(fixture_repo, _md(number="42", body="Fixes ENG-1234"),
                          "deadbeef", local_branch="feature")
    lc = _lifecycle(fixture_repo, tmp_path, client, tracker=FakeTracker())
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="https://github.com/acme/w/pull/42"))
    assert "origin" in str(exc.value)
    assert client.calls == []


def test_pr_bare_number_needs_no_origin(fixture_repo, tmp_path):
    # A bare number is unambiguously this repo, so it is accepted with no origin
    # remote and no URL-repo check (F-001).
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    client = FakePrClient(fixture_repo, _md(number="7", body="Fixes ENG-1234"),
                          head, local_branch="feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    res = lc.resolve(ReviewInputs(pr="7"))
    assert res.pr_number == "7"


# --- FR-9.3 competing-run refusal across the review state root (F-002) --------

def test_pr_refuses_when_branch_mode_review_owns_head(fixture_repo, tmp_path):
    # A branch-mode review already driving `feature` must block a --pr run whose
    # head resolves to `feature`, even though it keys on a different slug (`pr-7`
    # vs `feature`) invisible to the run_root scan (F-002).
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    _plant_review_run(fixture_repo, tmp_path, slug="feature", branch="feature")
    client = FakePrClient(fixture_repo, _md(body="Fixes ENG-1234"), head, local_branch="feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="7"))
    assert "already owns branch 'feature'" in str(exc.value)
    # Refused BEFORE checkout: HEAD unmoved, no `gh pr checkout` issued.
    assert gitops.current_branch(fixture_repo) == "main"
    assert ("checkout", "7") not in client.calls


def test_pr_refuses_when_another_pr_owns_same_head(fixture_repo, tmp_path):
    # Two PRs sharing a head branch: the second is refused against the first (F-002).
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    _plant_review_run(fixture_repo, tmp_path, slug="pr-5", branch="feature")
    client = FakePrClient(fixture_repo, _md(number="7", body="Fixes ENG-1234"),
                          head, local_branch="feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="7"))
    assert "pr-5" in str(exc.value)
    assert ("checkout", "7") not in client.calls


def test_pr_allows_when_competing_review_is_terminal(fixture_repo, tmp_path):
    # A terminal review run owning the same head branch does not block (F-002).
    _write_policy(fixture_repo, RATIFIED_POLICY)
    head = _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    git(fixture_repo, "branch", "-q", "-D", "feature")
    _plant_review_run(fixture_repo, tmp_path, slug="pr-5", branch="feature", status=M.RUN_DONE)
    client = FakePrClient(fixture_repo, _md(number="7", body="Fixes ENG-1234"),
                          head, local_branch="feature")
    lc = _lifecycle(
        fixture_repo, tmp_path, client,
        tracker=FakeTracker(refs=[IssueRef("linear", "ENG-1234", "ENG-1234")], issue=_issue()),
    )
    res = lc.resolve(ReviewInputs(pr="7"))
    assert res.target_branch == "feature"


def test_branch_review_refuses_when_pr_review_owns_head(fixture_repo, tmp_path):
    # Symmetric direction: an active --pr review of `feature` blocks a later
    # branch-mode review of the same branch (F-002).
    _write_policy(fixture_repo, RATIFIED_POLICY)
    _pr_head_branch(fixture_repo, "feature", files={"fix.py": "print('x')\n"})
    _plant_review_run(fixture_repo, tmp_path, slug="pr-5", branch="feature")
    lc = _lifecycle(fixture_repo, tmp_path, client=None)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="feature", code_only=True))
    assert "pr-5" in str(exc.value)

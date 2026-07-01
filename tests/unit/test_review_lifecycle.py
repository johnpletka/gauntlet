"""P2 — `gauntlet review` resolve-and-stop lifecycle + CLI entrypoint.

Covers the pre-cycle boundary the review command resolves and validates before
the `adversarial_cycle` (a P3 deliverable): the FR-1.1 flag surface + usage
errors, four-source intent resolution with precedence + provenance/ratification
(FR-2), the FR-2.4 in-repo `--intent` exclusion, target-branch adoption
(FR-1.3/FR-5.3), base resolution + the merge-base/empty-diff guard (FR-5), and
the out-of-repo zero-footprint state dir (FR-8, §6 "Review state path").

Every path is offline: the tracker runs against an ``httpx.MockTransport`` and
the interactive/editor/clock side effects are injected through ``Hooks``. No
cycle runs (that is P3), so all assertions stop at the resolution boundary.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from gauntlet.engine import gitops
from gauntlet.engine import manifest as M
from gauntlet.engine.config import IssueTrackerConfig, RunConfig
from gauntlet.engine.manifest import Manifest
from gauntlet.engine.review import (
    Hooks,
    RatificationDeclined,
    ReviewFailClosed,
    ReviewInputs,
    ReviewLifecycle,
    ReviewUsageError,
    derive_repo_id,
    normalize_repo_key,
    resolve_state_dir,
    review_slug,
    sanitize_slug,
)

from conftest import git


FIXED_NOW = datetime(2026, 7, 1, 14, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Repo / config / hooks helpers
# ---------------------------------------------------------------------------


def _make_branch(repo: Path, name: str, filename: str, content: str) -> None:
    """Create ``name`` off the current HEAD with one commit adding ``filename``.

    Leaves the originally-checked-out branch checked out again, so the caller's
    HEAD is unchanged — the new branch simply carries a diff versus the base.
    """
    start = gitops.current_branch(repo)
    git(repo, "checkout", "-q", "-b", name)
    (repo / filename).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"work on {name}")
    git(repo, "checkout", "-q", start)


def _empty_branch(repo: Path, name: str) -> None:
    """Create ``name`` at HEAD with no extra commit (diff-empty vs base)."""
    start = gitops.current_branch(repo)
    git(repo, "checkout", "-q", "-b", name)
    git(repo, "checkout", "-q", start)


def _config(**over) -> RunConfig:
    data: dict = {"base_branch": "main", "run_root": "runs"}
    data.update(over)
    return RunConfig(**data)


def _hooks(tmp_path: Path, **over) -> Hooks:
    """Non-interactive hooks with an XDG state home under tmp and a fixed clock."""
    environ = {
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "home"),
        "GAUNTLET_USER_EMAIL": "john.pletka@gmail.com",
    }
    environ.update(over.pop("environ_extra", {}))
    defaults = dict(
        isatty=lambda: False,
        environ=environ,
        now=lambda: FIXED_NOW,
    )
    defaults.update(over)
    return Hooks(**defaults)


def _lifecycle(repo: Path, tmp_path: Path, *, config: RunConfig | None = None, **hk):
    return ReviewLifecycle(repo, config or _config(), hooks=_hooks(tmp_path, **hk))


# ---- tracker transport ------------------------------------------------------


def _issue_transport(payload: dict, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


ISSUE_OK = {
    "data": {
        "issue": {
            "identifier": "ENG-1234",
            "title": "Widget crashes on click",
            "description": "Clicking the widget throws a null-pointer.",
            "url": "https://linear.app/acme/issue/ENG-1234",
            "state": {"name": "In Progress"},
        }
    }
}
ISSUE_NOT_FOUND = {"data": {"issue": None}}


def _tracker_config() -> RunConfig:
    return _config(issue_tracker=IssueTrackerConfig(provider="linear"))


# ===========================================================================
# Pure helpers — state-path derivation (no filesystem run)
# ===========================================================================


def test_sanitize_slug_maps_to_charset():
    assert sanitize_slug("feature/x") == "feature-x"
    assert sanitize_slug("a//b__c") == "a-b__c"  # '_' is in-charset, '/' is not
    assert sanitize_slug("--weird//") == "weird"
    assert sanitize_slug("////") == "review"  # empty after transform => fallback


def test_review_slug_leaves_clean_names_untouched():
    assert review_slug("main") == "main"
    assert review_slug("pr-123") == "pr-123"
    assert review_slug("fix.thing_1") == "fix.thing_1"


def test_review_slug_disambiguates_lossy_collisions():
    a = review_slug("feature/x")
    b = review_slug("feature\\x")  # different raw, same sanitized base "feature-x"
    assert a.startswith("feature-x-") and b.startswith("feature-x-")
    assert a != b  # collision-proofed by the unsanitized-name hash


def test_normalize_repo_key_equates_url_spellings():
    keys = {
        normalize_repo_key("https://github.com/acme/repo.git"),
        normalize_repo_key("git@github.com:acme/repo.git"),
        normalize_repo_key("ssh://git@github.com/acme/repo"),
        normalize_repo_key("https://user:token@github.com/acme/Repo.git/"),
    }
    assert keys == {"github.com/acme/repo"}


def test_derive_repo_id_stable_and_origin_keyed(fixture_repo: Path):
    # No origin => toplevel-path keyed, but stable across calls.
    first = derive_repo_id(fixture_repo)
    assert first == derive_repo_id(fixture_repo)
    assert len(first) == 12 and all(c in "0123456789abcdef" for c in first)
    # With an origin, the id is keyed on the normalized remote URL, independent
    # of the checkout location.
    git(fixture_repo, "remote", "add", "origin", "git@github.com:acme/widget.git")
    rid = derive_repo_id(fixture_repo)
    assert rid == hashlib.sha256(b"github.com/acme/widget").hexdigest()[:12]


def test_resolve_state_dir_default_is_xdg(fixture_repo: Path, tmp_path: Path):
    environ = {"XDG_STATE_HOME": str(tmp_path / "xdg")}
    p = resolve_state_dir(
        fixture_repo, _config(), repo_id="deadbeef0000", slug="main", environ=environ
    )
    assert p == tmp_path / "xdg" / "gauntlet" / "reviews" / "deadbeef0000" / "main"


def test_resolve_state_dir_ignores_relative_xdg_state_home(
    fixture_repo: Path, tmp_path: Path
):
    # A relative XDG_STATE_HOME is invalid per the XDG spec and must be ignored:
    # honoring `.state` would land the default state dir inside the repo (against
    # CWD) and dirty git status. It falls back to ~/.local/state under HOME.
    environ = {"XDG_STATE_HOME": ".state", "HOME": str(tmp_path / "home")}
    p = resolve_state_dir(
        fixture_repo, _config(), repo_id="rid", slug="main", environ=environ
    )
    assert p == (
        tmp_path / "home" / ".local" / "state"
        / "gauntlet" / "reviews" / "rid" / "main"
    )


def test_resolve_state_dir_default_inside_repo_fails_closed(fixture_repo: Path):
    # If HOME/XDG_STATE_HOME resolves inside the repo, the un-gitignored default
    # path would write review bytes into the tree under review — fail closed.
    environ = {"XDG_STATE_HOME": str(fixture_repo / "sneaky-state")}
    with pytest.raises(ReviewFailClosed) as exc:
        resolve_state_dir(
            fixture_repo, _config(), repo_id="rid", slug="main", environ=environ
        )
    assert "zero-footprint" in str(exc.value)


def test_resolve_state_dir_inrepo_override_requires_gitignore(fixture_repo: Path):
    cfg = _config(review={"state_dir": ".gauntlet/reviews"})
    # Not gitignored => fail closed at resolution.
    with pytest.raises(ReviewFailClosed) as exc:
        resolve_state_dir(fixture_repo, cfg, repo_id="rid", slug="main", environ={})
    assert "gitignore" in str(exc.value).lower()
    # Once ignored, the same override resolves fine under the repo.
    (fixture_repo / ".gitignore").write_text(".gauntlet/reviews/\n")
    p = resolve_state_dir(fixture_repo, cfg, repo_id="rid", slug="main", environ={})
    assert p == fixture_repo / ".gauntlet/reviews" / "rid" / "main"


def test_resolve_state_dir_outofrepo_override_unconstrained(
    fixture_repo: Path, tmp_path: Path
):
    out = tmp_path / "elsewhere"
    cfg = _config(review={"state_dir": str(out)})
    p = resolve_state_dir(fixture_repo, cfg, repo_id="rid", slug="s", environ={})
    assert p == out / "rid" / "s"


# ===========================================================================
# Input validation (usage class; before any git work)
# ===========================================================================


def test_pr_and_positional_are_mutually_exclusive(fixture_repo, tmp_path):
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewUsageError):
        lc.resolve(ReviewInputs(branch="foo", pr="123"))


@pytest.mark.parametrize("bad", [0, -1, 11, 100])
def test_rounds_out_of_range_is_usage_error(fixture_repo, tmp_path, bad):
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewUsageError) as exc:
        lc.resolve(ReviewInputs(rounds=bad, code_only=True))
    assert "[1, 10]" in str(exc.value)


def test_intent_provenance_with_issue_is_usage_error(fixture_repo, tmp_path):
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewUsageError):
        lc.resolve(ReviewInputs(issue="ENG-1", intent_provenance="tracker-session"))


def test_bad_intent_provenance_value_is_usage_error(fixture_repo, tmp_path):
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewUsageError):
        lc.resolve(ReviewInputs(message="x", intent_provenance="bogus"))


def test_code_only_with_intent_source_is_usage_error(fixture_repo, tmp_path):
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewUsageError):
        lc.resolve(ReviewInputs(code_only=True, message="x"))


def test_test_flag_requires_test_command(fixture_repo, tmp_path):
    cfg = _config(test_command="")
    lc = _lifecycle(fixture_repo, tmp_path, config=cfg)
    with pytest.raises(ReviewUsageError) as exc:
        lc.resolve(ReviewInputs(code_only=True, test=True))
    assert "test_command" in str(exc.value)


def test_pr_mode_gated_by_fr74_preflight_when_rule_unratified(fixture_repo, tmp_path):
    # P5 implements --pr: it is no longer a P2 "not implemented" stub. It now
    # enters PR mode, whose FIRST action is the deterministic FR-7.4 preflight —
    # a pure policy read, before any gh/git fetch. This repo has no ratified
    # pr_read_commands@v1 (no policy.yaml at all), so the run fails closed with
    # the EXACT FR-7.4 message (a fail-closed halt, not a usage error). The full
    # PR-mode happy/edge paths live in test_review_pr.py.
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(pr="123"))
    assert str(exc.value) == (
        "P4 (PR mode) requires policy rule 'pr_read_commands@v1' to be ratified "
        "in policy.yaml; it is absent. Ratify it through the policy-change "
        "process (Open Question 11.4) before using --pr."
    )


# ===========================================================================
# Intent resolution + provenance + ratification (FR-2)
# ===========================================================================


def test_message_intent_defaults_provenance_and_ratifies_with_flag(
    fixture_repo, tmp_path
):
    _make_branch(fixture_repo, "fix", "fix.py", "print('fixed')\n")
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(
        ReviewInputs(branch="fix", message="The widget crashes.", approved_intent=True)
    )
    rec = res.intent_record
    assert rec.source == M.INTENT_SOURCE_MESSAGE
    assert rec.provenance == M.PROVENANCE_AUTHOR_SESSION_SUMMARY
    assert rec.independent is False
    assert rec.ratification is not None
    assert rec.ratification.method == M.RATIFICATION_APPROVED_FLAG
    assert rec.ratification.user == "john.pletka@gmail.com"
    assert rec.ratification.timestamp == "2026-07-01T14:00:00Z"
    # intent.md is written out of repo and carries the statement verbatim.
    assert res.intent_path is not None and res.intent_path.exists()
    body = res.intent_path.read_text()
    assert "The widget crashes." in body
    assert "author-session-summary · non-independent" in body


def test_message_intent_non_interactive_without_approval_fails_closed(
    fixture_repo, tmp_path
):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)  # isatty False, no --approved-intent
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="fix", message="hi"))
    assert "--approved-intent" in str(exc.value)


def test_tracker_session_provenance_still_requires_ratification(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed):
        lc.resolve(
            ReviewInputs(
                branch="fix", message="hi", intent_provenance="tracker-session"
            )
        )


def test_manual_tracker_provenance_is_independent_no_ratification(
    fixture_repo, tmp_path
):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)  # non-interactive, no --approved-intent
    res = lc.resolve(
        ReviewInputs(branch="fix", message="hi", intent_provenance="tracker")
    )
    assert res.intent_record.independent is True
    assert res.intent_record.ratification is None


def test_interactive_ratification_confirm_edits_and_records(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    edited = "Edited problem statement."
    lc = _lifecycle(
        fixture_repo,
        tmp_path,
        isatty=lambda: True,
        edit_statement=lambda text, root: edited,
        confirm_statement=lambda text: True,
    )
    res = lc.resolve(ReviewInputs(branch="fix", message="original"))
    assert res.intent_record.ratification.method == M.RATIFICATION_INTERACTIVE
    assert edited in res.intent_path.read_text()


def test_interactive_ratification_declined_aborts(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(
        fixture_repo,
        tmp_path,
        isatty=lambda: True,
        edit_statement=lambda text, root: text,
        confirm_statement=lambda text: False,
    )
    with pytest.raises(RatificationDeclined):
        lc.resolve(ReviewInputs(branch="fix", message="original"))


def test_intent_file_inside_repo_is_excluded_and_left_untracked(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    bug = fixture_repo / "bug.md"
    bug.write_text("# Bug\nThe thing is broken.\n")
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(
        ReviewInputs(branch="fix", intent_path="bug.md", approved_intent=True)
    )
    assert res.intent_record.source == M.INTENT_SOURCE_INTENT_FILE
    assert "bug.md" in res.excludes
    # The in-repo intent file did not trip the entry contract and is left present
    # and untracked; it is snapshotted into the out-of-repo intent.md.
    assert bug.exists()
    porcelain = git(fixture_repo, "status", "--porcelain")
    assert "?? bug.md" in porcelain
    assert "The thing is broken." in res.intent_path.read_text()


def test_inrepo_intent_pointing_at_dirty_tracked_file_fails_closed(
    fixture_repo, tmp_path
):
    # --intent must not silently exempt a TRACKED file from the clean check: a
    # dirty tracked file it points at still trips the entry contract, so the
    # review never starts from an unclean worktree (F-003 / FR-9.2).
    _make_branch(fixture_repo, "fix", "fix.py", "orig\n")
    git(fixture_repo, "checkout", "-q", "fix")
    (fixture_repo / "fix.py").write_text("uncommitted edit\n")  # tracked + dirty
    lc = _lifecycle(fixture_repo, tmp_path)
    # The dirty tracked path is NOT exempted (only untracked in-repo files are).
    assert lc._intent_excludes(ReviewInputs(intent_path="fix.py")) == []
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(intent_path="fix.py", approved_intent=True))
    assert "uncommitted" in str(exc.value)


def test_inrepo_intent_pointing_at_clean_tracked_file_proceeds(fixture_repo, tmp_path):
    # A tracked-and-clean in-repo --intent file has no dirt to mask: it is simply
    # not excluded, and the review resolves normally (no over-rejection).
    _make_branch(fixture_repo, "fix", "notes.md", "The widget crashes.\n")
    git(fixture_repo, "checkout", "-q", "fix")
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(ReviewInputs(intent_path="notes.md", approved_intent=True))
    assert res.excludes == []  # clean tracked file => nothing to exclude
    assert "The widget crashes." in res.intent_path.read_text()


def test_empty_intent_file_fails_closed(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    (fixture_repo / "bug.md").write_text("   \n")
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed):
        lc.resolve(
            ReviewInputs(branch="fix", intent_path="bug.md", approved_intent=True)
        )


def test_editor_source_yields_intent(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    # A fake $EDITOR: a script that appends a problem statement to its file arg.
    editor = tmp_path / "fake-editor.sh"
    editor.write_text('#!/bin/sh\necho "Editor-entered problem." >> "$1"\n')
    editor.chmod(0o755)
    # The $EDITOR source is only reachable interactively (a TTY must be present);
    # the same interactive session then confirms the resolved statement.
    lc = _lifecycle(
        fixture_repo,
        tmp_path,
        isatty=lambda: True,
        confirm_statement=lambda text: True,
        environ_extra={"EDITOR": f"sh {editor}"},
    )
    res = lc.resolve(ReviewInputs(branch="fix"))
    assert res.intent_record.source == M.INTENT_SOURCE_EDITOR
    assert "Editor-entered problem." in res.intent_path.read_text()
    # The editor temp file lives under the state dir, never in the repo.
    assert git(fixture_repo, "status", "--porcelain").strip() == ""


def test_editor_source_empty_fails_closed(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    # `true` leaves the template untouched => stripped empty => fail closed.
    lc = _lifecycle(
        fixture_repo, tmp_path, isatty=lambda: True, environ_extra={"EDITOR": "true"}
    )
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="fix", approved_intent=True))
    assert "no problem statement" in str(exc.value)


def test_editor_source_non_interactive_fails_closed_before_launch(
    fixture_repo, tmp_path
):
    # No explicit intent + no --code-only + no TTY: the review must fail closed
    # BEFORE any lifecycle work, never launching $EDITOR (which would wedge a
    # headless run on a `vi` that never returns) — F-001 / FR-2.3.
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    # A "bomb" editor: if it were ever launched the run would hang/crash. It must
    # not run, so a plain `false` (non-zero exit) proves the launch never happens
    # — the fail-closed halt is raised regardless of what $EDITOR would do.
    lc = _lifecycle(
        fixture_repo, tmp_path, isatty=lambda: False, environ_extra={"EDITOR": "false"}
    )
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="fix"))
    assert "no TTY" in str(exc.value)
    # Fail-closed before lifecycle work: no state dir, no editor temp file, repo
    # untouched.
    assert git(fixture_repo, "status", "--porcelain").strip() == ""


def test_editor_nonzero_exit_maps_to_fail_closed(fixture_repo, tmp_path):
    # Interactive, but $EDITOR exits non-zero: a subprocess failure must map to a
    # fail-closed halt with operator guidance, not an uncaught CalledProcessError
    # (F-001 "map editor failures into ReviewFailClosed").
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(
        fixture_repo, tmp_path, isatty=lambda: True, environ_extra={"EDITOR": "false"}
    )
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="fix", approved_intent=True))
    assert "$EDITOR" in str(exc.value)


def test_code_only_writes_no_intent(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(ReviewInputs(branch="fix", code_only=True))
    assert res.intent_path is None
    assert res.intent_record.source == M.INTENT_SOURCE_CODE_ONLY
    assert res.intent_record.provenance == M.PROVENANCE_NONE
    assert res.intent_record.ratification is None
    assert not (res.state_dir / "intent.md").exists()


# ===========================================================================
# --issue (tracker) intent + fail-closed, never-fall-back (FR-2.1/FR-6)
# ===========================================================================


def test_issue_intent_is_tracker_provenance_unattended(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(
        fixture_repo,
        tmp_path,
        config=_tracker_config(),
        environ_extra={"LINEAR_API_KEY": "lin_test"},
        tracker_transport=_issue_transport(ISSUE_OK),
    )
    res = lc.resolve(ReviewInputs(branch="fix", issue="ENG-1234"))
    rec = res.intent_record
    assert rec.source == M.INTENT_SOURCE_ISSUE
    assert rec.provenance == M.PROVENANCE_TRACKER
    assert rec.independent is True
    assert rec.ratification is None  # independent => no ratification, unattended
    body = res.intent_path.read_text()
    assert "Clicking the widget throws a null-pointer." in body
    assert "source: linear ENG-1234" in body


def test_issue_with_no_tracker_configured_fails_closed(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)  # no issue_tracker block
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="fix", issue="ENG-1"))
    assert "issue_tracker" in str(exc.value)


def test_issue_missing_api_key_fails_closed(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(
        fixture_repo,
        tmp_path,
        config=_tracker_config(),  # no LINEAR_API_KEY in environ
        tracker_transport=_issue_transport(ISSUE_OK),
    )
    with pytest.raises(ReviewFailClosed):
        lc.resolve(ReviewInputs(branch="fix", issue="ENG-1234"))


def test_issue_not_found_never_falls_back_to_message(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(
        fixture_repo,
        tmp_path,
        config=_tracker_config(),
        environ_extra={"LINEAR_API_KEY": "lin_test"},
        tracker_transport=_issue_transport(ISSUE_NOT_FOUND),
    )
    # --issue is the sole, highest-precedence source: an unresolvable ref is
    # terminal and must NEVER silently use the lower-precedence -m.
    with pytest.raises(ReviewFailClosed):
        lc.resolve(ReviewInputs(branch="fix", issue="ENG-9999", message="fallback"))


def test_issue_unavailable_5xx_fails_closed(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(
        fixture_repo,
        tmp_path,
        config=_tracker_config(),
        environ_extra={"LINEAR_API_KEY": "lin_test"},
        tracker_transport=_issue_transport({"errors": []}, status=503),
    )
    with pytest.raises(ReviewFailClosed):
        lc.resolve(ReviewInputs(branch="fix", issue="ENG-1234"))


# ===========================================================================
# Target-branch resolution + adoption (FR-1.3 / FR-5.3)
# ===========================================================================


def test_positional_branch_is_adopted_before_base_computation(fixture_repo, tmp_path):
    # main has README; make a diverged fix branch. The empty-diff guard would
    # fire if base were computed against main-while-on-main; adopting `fix` first
    # means the diff/merge-base is computed against fix's tip.
    _make_branch(fixture_repo, "fix", "fix.py", "print('x')\n")
    assert gitops.current_branch(fixture_repo) == "main"
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(ReviewInputs(branch="fix", code_only=True))
    assert res.target_branch == "fix"
    assert gitops.current_branch(fixture_repo) == "fix"  # adopted in place
    # merge-base is main's tip (a concrete SHA to inject as review_base).
    main_sha = git(fixture_repo, "rev-parse", "main").strip()
    assert res.merge_base == main_sha


def test_omitted_positional_reviews_current_branch(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    git(fixture_repo, "checkout", "-q", "fix")
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(ReviewInputs(code_only=True))
    assert res.target_branch == "fix"


def test_nonexistent_branch_fails_closed(fixture_repo, tmp_path):
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="ghost", code_only=True))
    assert "no local branch" in str(exc.value)


def test_ambiguous_branch_and_tag_fails_closed(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "dup", "fix.py", "x\n")
    git(fixture_repo, "tag", "dup")  # same name as a branch
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="dup", code_only=True))
    assert "ambiguous" in str(exc.value)


def test_tag_only_ref_fails_closed_as_non_local_branch(fixture_repo, tmp_path):
    git(fixture_repo, "tag", "v1")  # a valid ref that is not a local branch
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="v1", code_only=True))
    assert "not a local branch" in str(exc.value)


def test_detached_head_no_positional_fails_closed(fixture_repo, tmp_path):
    git(fixture_repo, "checkout", "-q", "--detach")
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(code_only=True))
    assert "detached" in str(exc.value)


# ===========================================================================
# Base resolution + empty-diff / unrelated-history guard (FR-5)
# ===========================================================================


def test_empty_diff_guard_fires_with_message(fixture_repo, tmp_path):
    # A branch identical to base: merge-base == its tip, three-dot diff empty.
    _empty_branch(fixture_repo, "nochange")
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="nochange", code_only=True))
    assert "nothing to diff" in str(exc.value)


def test_unrelated_history_fails_closed(fixture_repo, tmp_path):
    # An orphan branch shares no merge-base with main.
    git(fixture_repo, "checkout", "-q", "--orphan", "orphan")
    (fixture_repo / "other.txt").write_text("unrelated\n")
    git(fixture_repo, "add", "-A")
    git(fixture_repo, "commit", "-qm", "orphan root")
    git(fixture_repo, "checkout", "-q", "main")
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="orphan", base="main", code_only=True))
    assert "no history" in str(exc.value) or "unrelated" in str(exc.value)


def test_explicit_base_overrides_and_resolves(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(ReviewInputs(branch="fix", base="main", code_only=True))
    assert res.base_ref == "main"
    assert res.merge_base  # concrete SHA


def test_invalid_base_fails_closed(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed):
        lc.resolve(ReviewInputs(branch="fix", base="does-not-exist", code_only=True))


def test_base_branch_current_resolves_to_origin_head(fixture_repo, tmp_path):
    # Set up an origin whose default branch is main, and a diverged fix branch.
    bare = tmp_path / "origin.git"
    git(fixture_repo, "clone", "-q", "--bare", str(fixture_repo), str(bare))
    git(fixture_repo, "remote", "add", "origin", str(bare))
    git(fixture_repo, "fetch", "-q", "origin")
    git(fixture_repo, "remote", "set-head", "origin", "main")
    _make_branch(fixture_repo, "fix", "fix.py", "print('x')\n")
    lc = _lifecycle(fixture_repo, tmp_path, config=_config(base_branch="current"))
    res = lc.resolve(ReviewInputs(branch="fix", code_only=True))
    assert res.base_ref == "origin/main"  # the 'current' sentinel is never a base
    assert res.merge_base


# ===========================================================================
# Entry contract, dirty tree, zero-footprint state dir (FR-8/FR-9)
# ===========================================================================


def test_dirty_worktree_fails_entry_contract(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    git(fixture_repo, "checkout", "-q", "fix")
    (fixture_repo / "fix.py").write_text("dirty edit\n")  # uncommitted change
    lc = _lifecycle(fixture_repo, tmp_path)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(code_only=True))
    assert "uncommitted" in str(exc.value)


def test_default_state_dir_leaves_zero_repo_footprint(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(
        ReviewInputs(branch="fix", message="a problem", approved_intent=True)
    )
    # State lives out of repo under the XDG dir; the repo is untouched.
    assert str(res.state_dir).startswith(str(tmp_path / "xdg"))
    assert (res.state_dir / "intent.md").exists()
    assert (res.state_dir / "manifest.json").exists()
    assert git(fixture_repo, "status", "--porcelain").strip() == ""
    # No review bytes anywhere under the repo tree (tracked, ignored, untracked).
    repo_files = {
        p.name for p in fixture_repo.rglob("*") if ".git" not in p.parts and p.is_file()
    }
    assert "intent.md" not in repo_files
    assert "manifest.json" not in repo_files


def test_inrepo_gitignored_state_dir_stays_out_of_git_status(fixture_repo, tmp_path):
    # Commit the ignore rule on main BEFORE branching so the adopted `fix`
    # branch inherits it (resolution checks the target branch's tree).
    (fixture_repo / ".gitignore").write_text(".gauntlet/reviews/\n")
    git(fixture_repo, "add", ".gitignore")
    git(fixture_repo, "commit", "-qm", "ignore review state")
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    cfg = _config(review={"state_dir": ".gauntlet/reviews"})
    lc = _lifecycle(fixture_repo, tmp_path, config=cfg)
    res = lc.resolve(ReviewInputs(branch="fix", message="p", approved_intent=True))
    # State written in-repo but under an ignored path => git status stays clean.
    assert (res.state_dir / "intent.md").exists()
    assert str(res.state_dir).startswith(str(fixture_repo))
    assert git(fixture_repo, "status", "--porcelain").strip() == ""


def test_inrepo_non_ignored_state_dir_fails_closed(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    cfg = _config(review={"state_dir": "review-state"})  # not gitignored
    lc = _lifecycle(fixture_repo, tmp_path, config=cfg)
    with pytest.raises(ReviewFailClosed) as exc:
        lc.resolve(ReviewInputs(branch="fix", message="p", approved_intent=True))
    assert "gitignore" in str(exc.value).lower()


def test_manifest_persisted_with_intent_block_and_resumable(fixture_repo, tmp_path):
    _make_branch(fixture_repo, "fix", "fix.py", "x\n")
    lc = _lifecycle(fixture_repo, tmp_path)
    res = lc.resolve(ReviewInputs(branch="fix", message="p", approved_intent=True))
    man = Manifest.load(res.manifest_path)
    assert man.slug == res.slug
    assert man.branch == "fix"
    assert man.base_branch == res.base_ref
    assert man.intent is not None
    assert man.intent.source == M.INTENT_SOURCE_MESSAGE
    assert man.intent.provenance == M.PROVENANCE_AUTHOR_SESSION_SUMMARY
    assert man.intent.ratification.method == M.RATIFICATION_APPROVED_FLAG
    # Stable slug => a resume resolves the identical state dir (FR-8.4).
    res2 = lc.resolve(ReviewInputs(branch="fix", message="p", approved_intent=True))
    assert res2.state_dir == res.state_dir

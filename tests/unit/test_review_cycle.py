"""P3 — `gauntlet review` cycle execution + the FR-3.4 terminal-severity summary.

Covers wiring the P2 review lifecycle through to *executing* the
`adversarial_cycle` in `code_review` mode (PRD "Lightweight Issue Workflow" §8
P3): a clean fix completes with zero footprint; an unresolved blocking finding
parks and is resumable with `--response`; an unresolved legitimate non-blocking
finding completes and is recorded as residual risk (a not-legitimate one as
declined). The injected three-dot `review_base` and the intent + provenance
reaching the reviewer / triager prompts are asserted directly, and the pure
`summarize_cycle` merge/ordering contract is exercised with a constructed
multi-round fixture.

Every path is offline: agents are scripted `SeqAdapter`s injected via the
review driver's `adapter_factory`, the judge is off, and the review state dir
lives out-of-repo under a tmp XDG home.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from gauntlet.adapters.base import AgentResult, Usage
from gauntlet.engine import gitops
from gauntlet.engine import manifest as M
from gauntlet.engine.config import RunConfig
from gauntlet.engine.cycle import DATA_BEGIN
from gauntlet.engine.review import (
    Hooks,
    ReviewInputs,
    ReviewLifecycle,
    RoundRecord,
    drive_review,
    load_review_run,
    resume_review,
    summarize_cycle,
    _bind_review_pipeline,
)

from conftest import FakeAdapter, git

REPO = Path(__file__).resolve().parents[2]


# --- scripted fakes (mirrors test_cycle.py) ---------------------------------
class SeqAdapter:
    """Returns scripted responses in order; records prompts for assertions."""

    capabilities = FakeAdapter.capabilities

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.timeout_s = 600.0

    def run(self, prompt, *, session=None, schema=None, cwd=None, extra_flags=None):
        self.calls.append({"prompt": prompt, "schema": schema})
        if not self.responses:
            raise AssertionError("SeqAdapter exhausted; unexpected extra call")
        r = self.responses.pop(0)
        if callable(r):
            r = r(cwd)
        return AgentResult(
            text=json.dumps(r), structured=r,
            usage=Usage(input_tokens=10, output_tokens=5), exit_code=0,
        )


def F(fid, severity="major", claim=None, location="feature1.py:1"):
    return {
        "id": fid, "severity": severity, "category": "correctness",
        "location": location, "claim": claim or f"defect {fid}",
        "evidence": "seen in code", "suggested_fix": None,
    }


def REVIEW(*findings, summary="reviewed"):
    return {"findings": list(findings), "open_questions": [], "summary": summary}


def V(fid, verdict="legitimate", action="fix_now", confidence="high", **kw):
    return {"finding_id": fid, "verdict": verdict, "reasoning": "1-3 sentences.",
            "action": action, "confidence": confidence,
            "target_artifact": None, **kw}


def CV(fid, verdict="resolved"):
    return {"finding_id": fid, "verdict": verdict, "notes": "checked the diff"}


def CONFIRM(*verdicts, new=()):
    return {"verdicts": list(verdicts), "new_findings": list(new), "summary": ""}


def writer(rel, content, result):
    """A SeqAdapter callable: write a file under cwd, then return ``result``."""
    def _run(cwd):
        target = Path(cwd) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return result
    return _run


# --- config / repo harness --------------------------------------------------
REVIEW_CONFIG = {
    "base_branch": "main",
    "run_root": "runs",
    "test_command": "true",
    "agents": {
        "reviewer": {"adapter": "codex"},
        "triage": {"adapter": "api", "model": "h"},
        "builder": {"adapter": "claude-code"},
        "escalation": {"adapter": "api", "model": "strong"},
    },
    "identities": {
        "reviewer": {"name": "Gauntlet Reviewer (codex)", "email": "reviewer@gauntlet.local"},
        "builder": {"name": "Gauntlet Builder (claude)", "email": "builder@gauntlet.local"},
    },
}


def _config(**over) -> RunConfig:
    data = dict(REVIEW_CONFIG)
    data.update(over)
    return RunConfig.model_validate(data)


def _hooks(tmp_path: Path, **over) -> Hooks:
    environ = {
        "XDG_STATE_HOME": str(tmp_path / "xdg"),
        "HOME": str(tmp_path / "home"),
        "GAUNTLET_USER_EMAIL": "john.pletka@gmail.com",
    }
    defaults = dict(isatty=lambda: False, environ=environ)
    defaults.update(over)
    return Hooks(**defaults)


@pytest.fixture
def review_repo(fixture_repo):
    """Fixture repo carrying the real assets + a 2-commit feature branch.

    The assets (schemas, prompts, review.yaml) are committed on ``main``; the
    ``feature`` branch adds ``feature1.py`` then ``feature2.py`` in two commits so
    ``feature`` diverges from ``main`` by more than one commit — which lets the
    three-dot ``review_base`` assertion distinguish merge-base scope from the
    default ``HEAD^`` two-dot scope."""
    repo = fixture_repo
    shutil.copytree(REPO / "schemas", repo / "schemas")
    shutil.copytree(REPO / "prompts", repo / "prompts")
    (repo / "pipelines").mkdir()
    shutil.copy(REPO / "pipelines" / "review.yaml", repo / "pipelines" / "review.yaml")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "assets")

    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature1.py").write_text("def one():\n    return 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature commit 1")
    (repo / "feature2.py").write_text("def two():\n    return 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "feature commit 2")
    git(repo, "checkout", "-q", "main")
    return repo


def run_review(repo, tmp_path, adapters, *, config=None, **inputs_over):
    """Resolve + drive a review on the ``feature`` branch, offline, judge off."""
    cfg = config or _config()
    lifecycle = ReviewLifecycle(repo, cfg, hooks=_hooks(tmp_path))
    defaults = dict(branch="feature", message="Widget must not crash on click",
                    approved_intent=True)
    defaults.update(inputs_over)
    resolution = lifecycle.resolve(ReviewInputs(**defaults))
    outcome = drive_review(
        repo, cfg, resolution,
        adapter_factory=lambda n: adapters[n], use_judge=False,
    )
    return outcome, resolution


def _no_repo_review_artifacts(repo: Path) -> bool:
    """No review-generated artifact lands anywhere in the repo tree.

    ``intent.md`` is unique to a review run; the round artifacts (findings/triage/
    confirm.json) share names with the pre-committed *schema* files under
    ``schemas/``, so those are checked only outside ``schemas/``. The stronger
    tracked/untracked guarantee is the caller's ``git status`` clean assertion."""
    if list(repo.rglob("intent.md")):
        return False
    for name in ("findings.json", "triage.json", "confirm.json"):
        if any(p for p in repo.rglob(name) if "schemas" not in p.parts):
            return False
    return True


# ===========================================================================
# Scenario 1 — a clean/correct fix completes with zero footprint (FR-3.4)
# ===========================================================================
def test_clean_fix_completes_with_zero_footprint(review_repo, tmp_path):
    adapters = {
        "reviewer": SeqAdapter(REVIEW()),  # no findings → converge round 1
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
        "escalation": SeqAdapter(),
    }
    outcome, resolution = run_review(review_repo, tmp_path, adapters)

    assert outcome.status == M.RUN_DONE
    assert not outcome.parked
    assert outcome.commits == []  # nothing accepted → no REVIEW.x commit
    assert outcome.summary.residual_risk == []
    assert outcome.summary.declined == []
    # Zero footprint (FR-8.1 / §9): clean tree, no review artifact anywhere in repo.
    assert gitops.is_clean(review_repo)
    assert _no_repo_review_artifacts(review_repo)
    # State lives out-of-repo under the tmp XDG home.
    assert str(resolution.state_dir).startswith(str(tmp_path / "xdg"))
    assert (resolution.state_dir / "intent.md").is_file()


# ===========================================================================
# Scenario 2 — an unresolved blocking finding PARKS (FR-3.2), resumable
# ===========================================================================
def test_blocking_finding_parks_and_is_resumable(review_repo, tmp_path):
    reviewer = SeqAdapter(
        REVIEW(F("F-001", "blocking")),
        CONFIRM(CV("F-001", "unresolved")),
    )
    adapters = {
        "reviewer": reviewer,
        "triage": SeqAdapter(V("F-001")),          # blocking → escalates
        "escalation": SeqAdapter(V("F-001")),      # legitimate + fix_now
        "builder": SeqAdapter(writer("feature1.py", "def one():\n    return 11\n", {})),
    }
    outcome, resolution = run_review(review_repo, tmp_path, adapters)

    assert outcome.status == M.RUN_PARKED
    assert outcome.parked
    # The fix commit landed but the run parked rather than silently passing.
    assert [p for p, _ in outcome.commits] == ["REVIEW.1"]
    assert gitops.is_clean(review_repo)

    # Resumable: a parked review is a bound, non-terminal run at its state dir.
    existing = load_review_run(resolution.state_dir)
    assert existing is not None and existing.status == M.RUN_PARKED

    # A resume with a --response re-drives the parked cycle (offline, judge off).
    reviewer2 = SeqAdapter(REVIEW())  # human decision resolved it → converge
    adapters2 = {
        "reviewer": reviewer2,
        "triage": SeqAdapter(),
        "escalation": SeqAdapter(),
        "builder": SeqAdapter(),
    }
    resumed = resume_review(
        review_repo, _config(), resolution.state_dir,
        response="F-001 is acceptable; the fix is correct.",
        adapter_factory=lambda n: adapters2[n], use_judge=False,
        environ={"GAUNTLET_USER_EMAIL": "john.pletka@gmail.com"},
    )
    assert resumed.status == M.RUN_DONE


def test_resume_reapplies_in_repo_intent_exclude(review_repo, tmp_path):
    # FR-2.4: an in-repo, *untracked* --intent file must stay excluded across a
    # resume — it must neither trip the resumed round's clean-handoff guard nor
    # be swept into a REVIEW.x fix commit. The exclude is worktree-local, so a
    # resume can only re-apply it if the fresh run persisted it in the manifest.
    intent_file = review_repo / "bug.md"  # inside the repo, left untracked
    intent_file.write_text("The widget crashes on click; fix it.\n")

    reviewer = SeqAdapter(
        REVIEW(F("F-001", "blocking")),
        CONFIRM(CV("F-001", "unresolved")),
    )
    adapters = {
        "reviewer": reviewer,
        "triage": SeqAdapter(V("F-001")),
        "escalation": SeqAdapter(V("F-001")),
        "builder": SeqAdapter(writer("feature1.py", "def one():\n    return 11\n", {})),
    }
    outcome, resolution = run_review(
        review_repo, tmp_path, adapters, message=None, intent_path="bug.md",
    )
    assert outcome.status == M.RUN_PARKED
    # The exclude was derived and PERSISTED on the manifest's intent record so a
    # resume can rehydrate it.
    man = M.Manifest.load(resolution.manifest_path)
    assert man.intent is not None and man.intent.repo_exclude == "bug.md"
    # The fresh run left bug.md present and untracked (never swept into REVIEW.1).
    assert intent_file.is_file()
    assert "bug.md" in git(review_repo, "status", "--porcelain")

    # Resume with a decision. The untracked bug.md is still in the tree; without
    # the rehydrated exclude the resumed round-1 clean-handoff guard would fail on
    # it (FR-9.3). With it, the review converges cleanly.
    reviewer2 = SeqAdapter(REVIEW())  # human decision resolved it → converge
    adapters2 = {
        "reviewer": reviewer2, "triage": SeqAdapter(),
        "escalation": SeqAdapter(), "builder": SeqAdapter(),
    }
    resumed = resume_review(
        review_repo, _config(), resolution.state_dir,
        response="F-001 is acceptable; the fix is correct.",
        adapter_factory=lambda n: adapters2[n], use_judge=False,
        environ={"GAUNTLET_USER_EMAIL": "john.pletka@gmail.com"},
    )
    assert resumed.status == M.RUN_DONE
    # bug.md is still untracked and appears in no commit on any branch.
    assert intent_file.is_file()
    assert "bug.md" in git(review_repo, "status", "--porcelain")
    committed = git(review_repo, "log", "--all", "--name-only", "--pretty=format:")
    assert "bug.md" not in committed


def test_response_less_resume_of_parked_review_refuses(review_repo, tmp_path):
    reviewer = SeqAdapter(
        REVIEW(F("F-001", "blocking")), CONFIRM(CV("F-001", "unresolved")),
    )
    adapters = {
        "reviewer": reviewer,
        "triage": SeqAdapter(V("F-001")),
        "escalation": SeqAdapter(V("F-001")),
        "builder": SeqAdapter(writer("feature1.py", "def one():\n    return 11\n", {})),
    }
    _outcome, resolution = run_review(review_repo, tmp_path, adapters)

    from gauntlet.engine.review import ReviewFailClosed

    # A cycle-escalation park is response-resolvable: a plain resume must refuse
    # rather than silently re-park (fail closed).
    with pytest.raises(ReviewFailClosed, match="resume it with --response"):
        resume_review(review_repo, _config(), resolution.state_dir, use_judge=False)


# ===========================================================================
# Scenario 3 — legitimate non-blocking = residual risk; declined recorded
# ===========================================================================
def test_major_residual_risk_and_declined_findings(review_repo, tmp_path):
    reviewer = SeqAdapter(
        REVIEW(F("F-001", "major"), F("F-002", "minor")),
        CONFIRM(CV("F-001", "unresolved")),  # fix did not resolve the major one
    )
    adapters = {
        "reviewer": reviewer,
        "triage": SeqAdapter(
            V("F-001", verdict="legitimate", action="fix_now"),
            V("F-002", verdict="bikeshedding", action="reject"),
        ),
        "escalation": SeqAdapter(),
        "builder": SeqAdapter(writer("feature1.py", "def one():\n    return 111\n", {})),
    }
    outcome, _resolution = run_review(review_repo, tmp_path, adapters)

    # Non-blocking open finding → the run COMPLETES (does not park).
    assert outcome.status == M.RUN_DONE
    assert not outcome.parked

    residual = outcome.summary.residual_risk
    assert [f.id for f in residual] == ["F-001"]
    r = residual[0]
    assert r.severity == "major" and r.location == "feature1.py:1"
    assert r.claim == "defect F-001"
    assert r.confirm_verdict == "unresolved"  # last confirm verdict recorded

    declined = outcome.summary.declined
    assert [f.id for f in declined] == ["F-002"]
    assert declined[0].triage_verdict == "bikeshedding"
    assert declined[0].triage_reasoning  # carries the triage reasoning


# ===========================================================================
# Multi-round residual risk: an earlier round's non-blocking finding survives a
# later blocking-resolution round in the terminal summary (FR-3.4)
# ===========================================================================
def test_earlier_round_residual_survives_later_blocking_resolution(review_repo, tmp_path):
    # Round 1 surfaces a legitimate *major* (non-blocking, stays open) alongside a
    # legitimate *blocking* finding (forces another round). Round 2 resolves only
    # the blocker and converges. The terminal residual-risk summary must still
    # carry the round-1 major — reading only the latest (round-2) artifacts would
    # silently drop it, since round 2 never saw the major.
    reviewer = SeqAdapter(
        REVIEW(F("F-maj", "major"), F("F-blk", "blocking")),  # round 1 review
        CONFIRM(CV("F-maj", "unresolved"), CV("F-blk", "unresolved")),  # round 1 confirm
        REVIEW(F("F-blk", "blocking")),                        # round 2 regression review
        CONFIRM(CV("F-blk", "resolved")),                      # round 2 confirm: blocker fixed
    )
    adapters = {
        "reviewer": reviewer,
        # F-maj triaged (no escalation, major); F-blk base triage in each round
        # (both escalate because blocking).
        "triage": SeqAdapter(V("F-maj"), V("F-blk"), V("F-blk")),
        "escalation": SeqAdapter(V("F-blk"), V("F-blk")),
        "builder": SeqAdapter(
            writer("feature1.py", "def one():\n    return 11\n", {}),   # round 1 fix
            writer("feature1.py", "def one():\n    return 111\n", {}),  # round 2 fix
        ),
    }
    outcome, _resolution = run_review(review_repo, tmp_path, adapters, rounds=2)

    # The blocker resolved in round 2 → the run COMPLETES (does not park).
    assert outcome.status == M.RUN_DONE
    assert not outcome.parked
    assert [p for p, _ in outcome.commits] == ["REVIEW.1", "REVIEW.2"]

    # The round-1 major is still surfaced as residual risk with its round-1
    # confirm verdict; the round-2-resolved blocker is not (resolved + blocking).
    residual = outcome.summary.residual_risk
    assert [f.id for f in residual] == ["F-maj"]
    assert residual[0].severity == "major"
    assert residual[0].confirm_verdict == "unresolved"
    assert "F-blk" not in {f.id for f in residual}
    assert outcome.summary.declined == []


# ===========================================================================
# Injected three-dot review_base + intent/provenance in the prompts (FR-2.2/5.2)
# ===========================================================================
def test_review_base_is_three_dot_and_intent_reaches_prompts(review_repo, tmp_path):
    reviewer = SeqAdapter(REVIEW())  # converge immediately; we inspect the prompt
    adapters = {
        "reviewer": reviewer,
        "triage": SeqAdapter(),
        "builder": SeqAdapter(),
        "escalation": SeqAdapter(),
    }
    outcome, resolution = run_review(review_repo, tmp_path, adapters)
    assert outcome.status == M.RUN_DONE

    review_prompt = reviewer.calls[0]["prompt"]
    # Three-dot scope: the injected merge-base SHA is the diff base, and the diff
    # spans BOTH feature commits (a two-dot HEAD^ base would omit feature1.py).
    assert resolution.merge_base[:10] in review_prompt
    assert "feature1.py" in review_prompt and "feature2.py" in review_prompt
    # Intent + provenance reach the reviewer, wrapped as untrusted data (§7).
    assert "originating problem statement (intent)" in review_prompt
    assert "author-session-summary (non-independent)" in review_prompt
    assert "Widget must not crash on click" in review_prompt
    assert DATA_BEGIN in review_prompt


def test_intent_reaches_triager_wrapped_as_data(review_repo, tmp_path):
    triage = SeqAdapter(V("F-001", action="reject", verdict="bikeshedding"))
    adapters = {
        "reviewer": SeqAdapter(REVIEW(F("F-001", "minor"))),
        "triage": triage,
        "builder": SeqAdapter(),
        "escalation": SeqAdapter(),
    }
    run_review(review_repo, tmp_path, adapters)
    triage_prompt = triage.calls[0]["prompt"]
    assert "originating problem statement (intent)" in triage_prompt
    assert "Widget must not crash on click" in triage_prompt
    assert DATA_BEGIN in triage_prompt  # wrapped as data in the triager path


# ===========================================================================
# --code-only: a diff-only review runs with no intent in the prompt
# ===========================================================================
def test_code_only_review_omits_intent(review_repo, tmp_path):
    reviewer = SeqAdapter(REVIEW())
    adapters = {
        "reviewer": reviewer, "triage": SeqAdapter(),
        "builder": SeqAdapter(), "escalation": SeqAdapter(),
    }
    cfg = _config()
    lifecycle = ReviewLifecycle(review_repo, cfg, hooks=_hooks(tmp_path))
    resolution = lifecycle.resolve(ReviewInputs(branch="feature", code_only=True))
    outcome = drive_review(
        review_repo, cfg, resolution,
        adapter_factory=lambda n: adapters[n], use_judge=False,
    )
    assert outcome.status == M.RUN_DONE
    assert resolution.intent_path is None
    assert "originating problem statement (intent)" not in reviewer.calls[0]["prompt"]
    assert not (resolution.state_dir / "intent.md").exists()


# ===========================================================================
# --rounds / --test wiring (FR-1.1 / FR-3.3)
# ===========================================================================
def test_rounds_and_test_are_injected_into_the_bound_pipeline(review_repo, tmp_path):
    cfg = _config()
    lifecycle = ReviewLifecycle(review_repo, cfg, hooks=_hooks(tmp_path))
    resolution = lifecycle.resolve(
        ReviewInputs(branch="feature", message="fix", approved_intent=True,
                     rounds=3, test=True)
    )
    pipeline, _phash = _bind_review_pipeline(review_repo, cfg, resolution)
    steps = pipeline.stages[0].steps
    # --test prepends a baseline-tests shell step (FR-1.1).
    assert steps[0].id == "baseline-tests" and steps[0].type == "shell"
    assert steps[0].get("run") == "true"
    # The cycle step carries the injected rounds + three-dot base + intent.
    cycle = steps[1]
    assert cycle.get("max_rounds") == 3
    assert cycle.get("review_base") == resolution.merge_base
    assert cycle.get("intent_provenance") == "author-session-summary"
    assert cycle.get("intent_independent") is False
    # The modified pipeline is snapshotted for resume (FR-9.1).
    assert (resolution.state_dir / "pipeline.yaml").is_file()


def test_default_rounds_is_one(review_repo, tmp_path):
    cfg = _config()
    lifecycle = ReviewLifecycle(review_repo, cfg, hooks=_hooks(tmp_path))
    resolution = lifecycle.resolve(
        ReviewInputs(branch="feature", message="fix", approved_intent=True)
    )
    pipeline, _ = _bind_review_pipeline(review_repo, cfg, resolution)
    cycle = pipeline.stages[0].steps[0]
    assert cycle.id == "review-cycle" and cycle.get("max_rounds") == 1


# ===========================================================================
# Pure summary contract — merge across rounds, dedup, deterministic order (3b)
# ===========================================================================
def _rr(rnd, findings, triage, confirm):
    return RoundRecord(round=rnd, findings=findings, triage=triage, confirm=confirm)


def test_summarize_cycle_partitions_and_orders():
    rounds = [
        _rr(
            1,
            [F("F-001", "major"), F("F-002", "minor"), F("F-003", "nit")],
            [V("F-001", verdict="legitimate", action="fix_now"),
             V("F-002", verdict="bikeshedding", action="reject"),
             V("F-003", verdict="legitimate", action="fix_now")],
            [CV("F-001", "unresolved"), CV("F-003", "resolved")],
        ),
    ]
    summary = summarize_cycle(rounds)
    # residual = legitimate + non-blocking + not-resolved. F-003 resolved → out.
    assert [f.id for f in summary.residual_risk] == ["F-001"]
    assert summary.residual_risk[0].confirm_verdict == "unresolved"
    assert [f.id for f in summary.declined] == ["F-002"]


def test_summarize_cycle_merges_recurring_id_across_rounds():
    # F-001 recurs in rounds 1 and 2; the highest round's triage + confirm win,
    # and it appears exactly once.
    rounds = [
        _rr(1, [F("F-001", "major")],
            [V("F-001", verdict="legitimate", action="fix_now")],
            [CV("F-001", "unresolved")]),
        _rr(2, [F("F-001", "major")],
            [V("F-001", verdict="legitimate", action="fix_now")],
            [CV("F-001", "resolved")]),  # resolved in the later round
    ]
    summary = summarize_cycle(rounds)
    # Latest round's confirm verdict (resolved) wins → no residual risk, no dup.
    assert summary.residual_risk == []
    assert summary.declined == []


def test_summarize_cycle_deterministic_ordering():
    # A mix of severities + rounds; assert severity-rank → round → id ordering.
    rounds = [
        _rr(1,
            [F("Z-nit", "nit"), F("A-major", "major"), F("M-minor", "minor")],
            [V("Z-nit", verdict="legitimate", action="fix_now"),
             V("A-major", verdict="legitimate", action="fix_now"),
             V("M-minor", verdict="legitimate", action="fix_now")],
            []),  # no confirm → none resolved → all residual
        _rr(2,
            [F("B-major", "major")],
            [V("B-major", verdict="legitimate", action="fix_now")],
            []),
    ]
    summary = summarize_cycle(rounds)
    # major (round 1 A, round 2 B) then minor then nit; within major, round asc.
    assert [f.id for f in summary.residual_risk] == [
        "A-major", "B-major", "M-minor", "Z-nit"
    ]


def test_summarize_cycle_excludes_blocking_from_residual():
    # A legitimate blocking finding parks the run and never reaches a completion
    # summary; summarize_cycle defensively excludes it from residual risk.
    rounds = [
        _rr(1, [F("F-001", "blocking")],
            [V("F-001", verdict="legitimate", action="fix_now")],
            [CV("F-001", "unresolved")]),
    ]
    summary = summarize_cycle(rounds)
    assert summary.residual_risk == [] and summary.declined == []

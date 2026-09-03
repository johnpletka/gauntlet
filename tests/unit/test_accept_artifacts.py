"""`gauntlet resume --accept-artifacts` — first-class artifact ratification (#134).

A structured alternative to `--response "<prose>"` for an FR-10.4 park: the
operator states that the governed artifacts as they stand on the authoring
surface ARE the approved artifacts. The engine hashes them, records the
digests, and both disposition gates short-circuit the pending entry to
proceed_in_place with zero model calls.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from gauntlet.engine import manifest as M
from gauntlet.engine import ratification as RT
from gauntlet.engine.manifest import HumanResponse, Manifest, PipelineRef, StepRecord

from test_cycle import CONFIRM, CV, F, REVIEW, SeqAdapter, V, writer
from test_cycle_resume_response import _drive_disposition_cycle_to_park
from test_resume_disposition import DispositionAdapter, _disposition
from test_resume_response import _build_repo, _clock, _drive_to_conflict, _run_dir


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- the ratification helper --------------------------------------------------
def test_plan_ratification_hashes_governed_artifacts_and_flags_drift(tmp_path):
    (tmp_path / "prd.md").write_text("# PRD\n")
    (tmp_path / "plan.md").write_text("# plan\n")
    plan = RT.plan_ratification(tmp_path, known={"prd.md": "0" * 64})
    by = {d.name: d for d in plan.digests}
    assert by["prd.md"].sha256 == _sha(tmp_path / "prd.md")
    assert by["prd.md"].drifted and not by["plan.md"].drifted
    assert plan.text.startswith(RT.ACCEPT_ARTIFACTS_TEXT_PREFIX)
    assert f"prd.md sha256={by['prd.md'].sha256}" in plan.text
    assert [d.name for d in plan.drifted] == ["prd.md"]


def test_plan_ratification_refuses_to_approve_nothing(tmp_path):
    with pytest.raises(ValueError, match="refuses to approve nothing"):
        RT.plan_ratification(tmp_path)


def test_manifest_round_trips_kind_and_ratified_artifacts():
    man = Manifest(
        run_id="r", slug="demo", branch="b", base_branch="main",
        pipeline=PipelineRef(name="p", version=1, hash="h"),
        steps=[StepRecord(id="s", type="agent_task", status=M.PARKED, human_responses=[
            HumanResponse(response_id="s-resp-1", response_text="x", timestamp="t",
                          user="u", response_attempt=1, state=M.RESPONSE_PENDING),
        ])],
    )
    # pre-#134 entries load as `text`; nothing ratified by default
    assert man.record("s").human_responses[0].kind == M.RESPONSE_KIND_TEXT
    assert man.ratified_artifacts == []
    loaded = Manifest.model_validate_json(man.model_dump_json())
    assert loaded.record("s").human_responses[0].kind == "text"
    with pytest.raises(ValueError):
        HumanResponse(response_id="x", response_text="x", timestamp="t", user="u",
                      response_attempt=1, state=M.RESPONSE_PENDING, kind="bogus")


# --- agent_task: routed (cheap disposition agent) and unrouted -----------------
def test_accept_artifacts_completes_agent_task_without_classification(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    slug_dir = mgr.layout("demo").slug_dir
    prd_sha = _sha(slug_dir / "prd.md")
    builder = DispositionAdapter(_disposition("proceed_in_place"), write=True)
    status = mgr.resume(
        "demo", accept_artifacts=True, use_judge=False,
        adapter_factory=lambda n: builder, clock=_clock(),
    )
    assert status == M.RUN_DONE
    man = mgr.status("demo")
    rec = man.record("implement")
    assert rec.status == M.DONE and rec.parked_reason is None
    entry = rec.human_responses[-1]
    assert entry.kind == M.RESPONSE_KIND_ACCEPT_ARTIFACTS
    assert entry.state == M.RESPONSE_CONSUMED
    assert entry.response_text.startswith(RT.ACCEPT_ARTIFACTS_TEXT_PREFIX)
    assert f"prd.md sha256={prd_sha}" in entry.response_text
    assert [(r.name, r.sha256, r.response_id) for r in man.ratified_artifacts] == [
        ("prd.md", prd_sha, entry.response_id)
    ]
    # The builder's prompt carries the ratification hint, not bare prose.
    assert any("structured artifact ratification" in p for p in builder.prompts)
    # No drift: the ratified bytes are exactly what the run branch committed.
    assert not any("artifact ratification" in w for w in man.warnings)


def test_planner_baseline_falls_back_to_committed_bytes(tmp_path, monkeypatch):
    """With no prior ratification, the run's last-known approved digest is the
    bytes committed on the run branch; ratifying different bytes plans a
    drifted digest (recorded loudly by the orchestrator, never refused).
    Planner-level: the governance publish on a real resume also reads the
    committed bytes, so a full-drive fake would overwrite the authoring copy."""
    from gauntlet.engine import gitops

    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    calls = []

    def fake(repo_, sha, rel):
        calls.append((str(sha), rel))
        return b"# the bytes the branch committed\n" if rel.endswith("prd.md") else None

    monkeypatch.setattr(gitops, "file_bytes_at_commit", fake)
    man = mgr.status("demo")
    action = mgr._plan_response_action(
        man, None, None, accept_artifacts=True, layout=mgr.layout("demo")
    )
    assert action.kind == "append" and action.response_kind == M.RESPONSE_KIND_ACCEPT_ARTIFACTS
    assert (man.branch, f"{mgr.config.run_root}/demo/prd.md") in calls
    (digest,) = action.ratified
    assert digest.name == "prd.md"
    assert digest.prior_sha256 == hashlib.sha256(b"# the bytes the branch committed\n").hexdigest()
    assert digest.drifted
    assert digest.sha256 == _sha(mgr.layout("demo").slug_dir / "prd.md")
    note = RT.drift_note(digest, "implement-resp-1")
    assert note.startswith("artifact ratification implement-resp-1: prd.md ratified at")
    assert "differs" in note and digest.prior_sha256 in note


def test_accept_artifacts_without_a_known_baseline_is_not_drift(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    builder = DispositionAdapter(_disposition("proceed_in_place"), write=True)
    assert mgr.resume("demo", accept_artifacts=True, use_judge=False,
                      adapter_factory=lambda n: builder, clock=_clock()) == M.RUN_DONE
    assert not any(w.startswith("artifact ratification") for w in mgr.status("demo").warnings)


def test_accept_artifacts_is_refused_unless_parked_for_response(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    # exclusive with --response
    with pytest.raises(ValueError, match="mutually exclusive"):
        mgr.resume("demo", accept_artifacts=True, response="also prose",
                   use_judge=False, clock=_clock())
    # a completed run is not parked for a decision
    builder = DispositionAdapter(_disposition("proceed_in_place"), write=True)
    assert mgr.resume("demo", accept_artifacts=True, use_judge=False,
                      adapter_factory=lambda n: builder, clock=_clock()) == M.RUN_DONE
    with pytest.raises(ValueError, match="applies only to a run parked awaiting"):
        mgr.resume("demo", accept_artifacts=True, use_judge=False, clock=_clock())


def test_second_ratification_compares_against_the_first(tmp_path):
    repo, mgr = _build_repo(tmp_path / "repo")
    _drive_to_conflict(repo, mgr)
    slug_dir = mgr.layout("demo").slug_dir
    # First ratification re-parks via the builder's own `new_conflict`.
    reparker = DispositionAdapter(_disposition("new_conflict"))
    assert mgr.resume("demo", accept_artifacts=True, use_judge=False,
                      adapter_factory=lambda n: reparker, clock=_clock()) == M.RUN_PARKED
    first = mgr.status("demo").ratified_artifacts[-1].sha256
    # Edit, ratify again: drift is measured against the FIRST ratification.
    (slug_dir / "prd.md").write_text("# PRD v2\n")
    builder = DispositionAdapter(
        _disposition("proceed_in_place", responses=("implement-resp-1", "implement-resp-2")),
        write=True,
    )
    assert mgr.resume("demo", accept_artifacts=True, use_judge=False,
                      adapter_factory=lambda n: builder, clock=_clock()) == M.RUN_DONE
    man = mgr.status("demo")
    assert [r.sha256 for r in man.ratified_artifacts] == [first, _sha(slug_dir / "prd.md")]
    drift = [w for w in man.warnings if w.startswith("artifact ratification")]
    assert len(drift) == 1 and f"approved digest sha256={first}" in drift[0]


# --- adversarial_cycle: the routed gate is bypassed ----------------------------------
def test_cycle_accept_artifacts_skips_the_disposition_agent(tmp_path):
    repo, mgr = _drive_disposition_cycle_to_park(tmp_path)
    mechanic = SeqAdapter()  # exhausts (raises) if ever invoked
    finding_id = "1-reviewer-spec-coverage:F-001"
    resume_reviewer = SeqAdapter(REVIEW(F(finding_id)), CONFIRM(CV(finding_id, "resolved")))
    builder = SeqAdapter(writer("src.py", "fixed\n", {"done": True}))
    resume = {
        "reviewer": resume_reviewer,
        # The same (root, target) pair as the park: settled by the ratification (#106).
        "triage": SeqAdapter(V(finding_id, action="fix_now", target_artifact="plan.md")),
        "builder": builder,
        "mechanic": mechanic,
    }
    status = mgr.resume(
        "demo", accept_artifacts=True, use_judge=False,
        adapter_factory=lambda n: resume[n],
    )
    assert status == M.RUN_DONE
    rec = mgr.status("demo").record("cycle")
    assert rec.status == M.DONE and rec.parked_reason is None
    assert mechanic.calls == []  # zero model calls for the classification
    assert resume_reviewer.calls, "the roles re-drive against the ratified artifacts"
    assert rec.human_responses[-1].kind == M.RESPONSE_KIND_ACCEPT_ARTIFACTS
    triage = json.loads(
        (mgr.layout("demo").active_run_dir() / "artifacts" / "triage.json").read_text()
    )
    assert triage["verdicts"][0]["target_artifact"] is None
    assert "consumed the prior FR-10.4 target" in triage["verdicts"][0]["reasoning"]
    assert mgr.status("demo").ratified_artifacts


# --- the CLI flag --------------------------------------------------------------------
def test_cli_accept_artifacts_exclusive_with_response(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import gauntlet.cli as cli

    repo, mgr = _build_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)
    r = CliRunner().invoke(
        cli.app, ["resume", "demo", "--accept-artifacts", "--response", "x"]
    )
    assert r.exit_code == 2
    assert "mutually exclusive" in r.output

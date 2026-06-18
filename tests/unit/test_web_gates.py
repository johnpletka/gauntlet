"""Gates, recovery intelligence & control actions (P5, FR-4/FR-5/FR-10.7).

Three layers, all offline:
- ``resume_intel`` is table-driven over fixture manifests, keyed on the existing
  ``StepRecord.status`` enum, so a note-wording drift fails a test rather than
  silently mis-classifying (FR-5.1, R3) — incl. the three adversarial_cycle
  escalation sub-kinds and the fail-closed halt cases.
- the gate / escalation / diff resolution is exercised over fixture run dirs (a
  real git repo for the phase-diff selection), incl. containment of a
  traversal-laden ``show:`` name (review F-006).
- the control surface (approve/reject/resume + destructive-verb confirm) is
  driven over a ``TestClient`` with a fake supervisor so argv/guards are asserted
  without spawning.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gauntlet.engine.config import RunConfig
from gauntlet.engine.manifest import (
    RUN_DONE,
    RUN_FAILED,
    RUN_PARKED,
    CommitRecord,
    Manifest,
    PipelineRef,
    StepRecord,
)
from gauntlet.web import intel as I
from gauntlet.web.gate import GateResolver, NoPendingGate, handoff_prompt
from gauntlet.web.intel import resume_intel
from gauntlet.web.service import TOKEN_HEADER, create_app
from gauntlet.web.store import RunStore, UnsafePath

from conftest import git

TOKEN = "p5-test-token"


# --------------------------------------------------------------------------- #
# manifest / fixture builders
# --------------------------------------------------------------------------- #
def _man(slug, run_id, *, status, steps, current_step=None, commits=None) -> Manifest:
    return Manifest(
        run_id=run_id,
        slug=slug,
        branch=f"gauntlet/{slug}",
        base_branch="main",
        pipeline=PipelineRef(name="standard", version=1, hash="sha256:x"),
        status=status,
        current_step=current_step,
        steps=steps,
        commits=commits or [],
    )


def _step(id_, type_, status, *, notes=None, base_sha=None):
    return StepRecord(id=id_, type=type_, status=status, notes=notes, base_sha=base_sha)


def _write_run(
    repo: Path, slug: str, run_id: str, man: Manifest, *, active=True
) -> Path:
    run_dir = repo / "runs" / slug / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    man.write_atomic(run_dir / "manifest.json")
    if active:
        (repo / "runs" / slug / "active-run.txt").write_text(run_id)
    return run_dir


def _store(repo: Path) -> RunStore:
    return RunStore(repo, RunConfig())


# --------------------------------------------------------------------------- #
# resume_intel — table-driven (FR-5.1/5.2/5.3)
# --------------------------------------------------------------------------- #
GATE = _man("s", "r", status=RUN_PARKED, current_step="g",
            steps=[_step("g", "human_gate", "parked", notes="awaiting human")])

GATE_CONFLICT = _man("s", "r", status=RUN_PARKED, current_step="g",
                     steps=[_step("g", "human_gate", "parked",
                                  notes="UPSTREAM CONFLICT: plan disagrees")])

ESC_UPSTREAM = _man("s", "r", status=RUN_PARKED, current_step="c", steps=[
    _step("c", "adversarial_cycle", "parked",
          notes="escalation: finding(s) whose fix lands in an upstream artifact "
                "(FR-10.4 upstream invalidation): F-003")])

ESC_OPEN = _man("s", "r", status=RUN_PARKED, current_step="c", steps=[
    _step("c", "adversarial_cycle", "parked",
          notes="escalation (FR-10.5): max_rounds=3 exhausted with open "
                "finding(s): F-007; a human must resolve")])

ESC_F009 = _man("s", "r", status=RUN_PARKED, current_step="c", steps=[
    _step("c", "adversarial_cycle", "parked",
          notes="escalation (review F-009): blocking-severity or low-confidence "
                "verdicts need a human (no escalation_agent resolution): F-011")])

INTERRUPTED = _man("s", "r", status=RUN_PARKED, current_step="b", steps=[
    _step("b", "agent_task", "interrupted", notes="killed mid-edit")])

HALT_TIMEOUT = _man("s", "r", status=RUN_PARKED, current_step="b", steps=[
    _step("b", "agent_task", "halted", notes="timeout halt (FR-3.3): exceeded 600s")])

HALT_BUDGET = _man("s", "r", status=RUN_PARKED, current_step="b", steps=[
    _step("b", "agent_task", "halted",
          notes="budget halt (FR-3.3): step cost $5.10 exceeds budget")])

HALT_BOTH = _man("s", "r", status=RUN_PARKED, current_step="b", steps=[
    _step("b", "agent_task", "halted",
          notes="timeout halt and budget halt both mentioned somehow")])

HALT_NEITHER = _man("s", "r", status=RUN_PARKED, current_step="b", steps=[
    _step("b", "agent_task", "halted", notes="halted for an unrecognized reason")])

FAILED = _man("s", "r", status=RUN_FAILED, current_step="b", steps=[
    _step("b", "agent_task", "failed", notes="test failure: 2 failed")])

REJECTED = _man("s", "r", status=RUN_FAILED, current_step="g", steps=[
    _step("g", "human_gate", "failed", notes="rejected: insufficient evidence")])

DONE = _man("s", "r", status=RUN_DONE, current_step=None, steps=[
    _step("b", "agent_task", "done")])


@pytest.mark.parametrize(
    "man, state, controls",
    [
        (GATE, I.GATE, [I.APPROVE, I.REJECT]),
        (GATE_CONFLICT, I.GATE, [I.APPROVE, I.REJECT]),
        (ESC_UPSTREAM, I.ESCALATION, [I.RESUME]),
        (ESC_OPEN, I.ESCALATION, [I.RESUME]),
        (ESC_F009, I.ESCALATION, [I.RESUME]),
        (INTERRUPTED, I.INTERRUPTED_STATE, [I.RESUME]),
        (HALT_TIMEOUT, I.HALT_TIMEOUT, [I.RESUME]),
        (HALT_BUDGET, I.HALT_BUDGET, [I.RESUME]),
        (HALT_BOTH, I.HALT_GENERIC, [I.RESUME]),
        (HALT_NEITHER, I.HALT_GENERIC, [I.RESUME]),
        (FAILED, I.FAILED_STATE, []),
        (REJECTED, I.REJECTED, []),
        (DONE, I.DONE_STATE, []),
    ],
)
def test_resume_intel_table(man, state, controls):
    out = resume_intel(man)
    assert out.state == state
    assert out.available_controls == controls


def test_escalation_never_offers_approve_reject():
    # FR-5.2: a cycle park is Resume-only — the engine offers no approve/reject.
    for man in (ESC_UPSTREAM, ESC_OPEN, ESC_F009):
        out = resume_intel(man)
        assert I.APPROVE not in out.available_controls
        assert I.REJECT not in out.available_controls


def test_escalation_sub_kinds_named():
    assert I.SUB_UPSTREAM in resume_intel(ESC_UPSTREAM).sub_kinds
    assert I.SUB_OPEN_BLOCKER in resume_intel(ESC_OPEN).sub_kinds
    assert I.SUB_UNRESOLVED in resume_intel(ESC_F009).sub_kinds
    # Finding ids are extracted from the note for the panel (not "FR-10.4").
    assert resume_intel(ESC_UPSTREAM).escalated_finding_ids == ["F-003"]


def test_extract_finding_ids_skips_fr_tokens():
    assert I.extract_finding_ids("FR-10.4 and FR-10.5 affect F-003, F-R1-X") == [
        "F-003",
        "F-R1-X",
    ]


# --------------------------------------------------------------------------- #
# gate resolution — human_gate show: artifacts (FR-4.2/4.3)
# --------------------------------------------------------------------------- #
def _gate_run_with_artifacts(repo: Path) -> Path:
    (repo / "runs").mkdir(exist_ok=True)
    man = _man("demo", "run-1", status=RUN_PARKED, current_step="impl-gate",
               steps=[_step("impl-gate", "human_gate", "parked",
                            notes="awaiting human decision; review: findings.json, plan.md")])
    run_dir = _write_run(repo, "demo", "run-1", man)
    # The snapshot pipeline the gate's show: is read from (FR-4.2).
    (run_dir / "pipeline.yaml").write_text(
        "name: standard\nversion: 1\nstages:\n"
        "  - id: impl\n    steps:\n"
        "      - {id: impl-gate, type: human_gate, show: [findings.json, plan.md]}\n"
    )
    arts = run_dir / "artifacts"
    arts.mkdir()
    (arts / "findings.json").write_text(json.dumps({
        "findings": [{"id": "F-001", "severity": "major", "category": "correctness",
                      "location": "x.py:1", "claim": "bug", "evidence": "y",
                      "suggested_fix": None}],
        "open_questions": [], "summary": "one"}))
    # plan.md lives at the slug-dir artifact root (FR-4.2 second resolution).
    (repo / "runs" / "demo" / "plan.md").write_text("# Plan\n\nthe plan body\n")
    return run_dir


def test_gate_resolves_show_artifacts(fixture_repo):
    _gate_run_with_artifacts(fixture_repo)
    view = GateResolver(_store(fixture_repo)).gate("demo")
    assert view.kind == "gate" and view.gate_id == "impl-gate"
    by_name = {a.name: a for a in view.artifacts}
    assert by_name["findings.json"].kind == "findings"
    assert by_name["findings.json"].source == "artifacts"
    assert by_name["findings.json"].parsed["findings"][0]["id"] == "F-001"
    assert by_name["plan.md"].kind == "markdown"
    assert by_name["plan.md"].source == "slug"
    assert "the plan body" in by_name["plan.md"].content


def test_gate_show_traversal_rejected(fixture_repo):
    # A pipeline-/user-selected show: name that traverses is rejected (F-006).
    run_dir = _gate_run_with_artifacts(fixture_repo)
    (run_dir / "pipeline.yaml").write_text(
        "name: standard\nversion: 1\nstages:\n"
        "  - id: impl\n    steps:\n"
        "      - {id: impl-gate, type: human_gate, show: ['../../../etc/passwd']}\n"
    )
    with pytest.raises(UnsafePath):
        GateResolver(_store(fixture_repo)).gate("demo")


def test_gate_404_when_not_parked(fixture_repo):
    (fixture_repo / "runs").mkdir(exist_ok=True)
    man = _man("demo", "run-1", status=RUN_DONE, current_step=None,
               steps=[_step("b", "agent_task", "done")])
    _write_run(fixture_repo, "demo", "run-1", man)
    with pytest.raises(NoPendingGate):
        GateResolver(_store(fixture_repo)).gate("demo")


# --------------------------------------------------------------------------- #
# escalation surface (FR-4.6)
# --------------------------------------------------------------------------- #
def _escalation_run(repo: Path) -> Path:
    (repo / "runs").mkdir(exist_ok=True)
    man = _man("demo", "run-1", status=RUN_PARKED, current_step="impl-cycle", steps=[
        _step("impl-cycle", "adversarial_cycle", "parked",
              notes="escalation: finding(s) whose fix lands in an upstream "
                    "artifact (FR-10.4 upstream invalidation): F-003")])
    run_dir = _write_run(repo, "demo", "run-1", man)
    arts = run_dir / "artifacts"
    arts.mkdir()
    (arts / "findings.json").write_text(json.dumps({
        "findings": [{"id": "F-003", "severity": "blocking", "category": "spec-gap",
                      "location": "plan.md §3", "claim": "plan is wrong",
                      "evidence": "contradicts PRD", "suggested_fix": "amend plan"}],
        "open_questions": [], "summary": "one"}))
    (arts / "triage.json").write_text(json.dumps({"verdicts": [
        {"finding_id": "F-003", "verdict": "legitimate", "reasoning": "real gap",
         "action": "defer", "confidence": "high", "target_artifact": "plan.md"}]}))
    return run_dir


def test_escalation_view_assembles_evidence(fixture_repo):
    _escalation_run(fixture_repo)
    view = GateResolver(_store(fixture_repo)).gate("demo")
    assert view.kind == "escalation" and view.gate_type == "adversarial_cycle"
    assert view.escalated_finding_ids == ["F-003"]
    assert view.target_artifacts == ["plan.md"]
    ef = view.escalated[0]
    assert ef.finding_id == "F-003" and ef.severity == "blocking"
    assert ef.claim == "plan is wrong"
    assert ef.verdict == "legitimate" and ef.target_artifact == "plan.md"
    assert ef.reasoning == "real gap"


def test_escalation_handoff_assembles_prompt_only(fixture_repo):
    # FR-4.7: the hand-off assembles a prompt string and spawns nothing.
    _escalation_run(fixture_repo)
    view = GateResolver(_store(fixture_repo)).gate("demo")
    h = handoff_prompt(view)
    assert "F-003" in h.prompt and "plan.md" in h.prompt
    assert "read-only" in h.prompt.lower()
    assert h.invocation.startswith("claude")


# --------------------------------------------------------------------------- #
# deterministic phase diff (FR-4.3)
# --------------------------------------------------------------------------- #
def _commit(repo: Path, name: str, body: str) -> str:
    (repo / name).write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"add {name}")
    return git(repo, "rev-parse", "HEAD").strip()


def test_diff_selects_phase_plus_fix_rounds(fixture_repo):
    (fixture_repo / "runs").mkdir(exist_ok=True)
    base = git(fixture_repo, "rev-parse", "HEAD").strip()  # pre-P1 state
    p1 = _commit(fixture_repo, "a.py", "P1\n")
    p1_fix = _commit(fixture_repo, "a.py", "P1 fixed\n")
    p2 = _commit(fixture_repo, "b.py", "P2\n")
    p2_fix = _commit(fixture_repo, "b.py", "P2 fixed\n")
    man = _man("demo", "run-1", status=RUN_PARKED, current_step="p2-gate",
               steps=[_step("p2-cycle", "adversarial_cycle", "done", base_sha=p1_fix),
                      _step("p2-gate", "human_gate", "parked", notes="review")],
               commits=[
                   CommitRecord(step_id="p1-cycle", phase="P1", sha=p1),
                   CommitRecord(step_id="p1-cycle", phase="P1.1", sha=p1_fix),
                   CommitRecord(step_id="p2-cycle", phase="P2", sha=p2),
                   CommitRecord(step_id="p2-cycle", phase="P2.1", sha=p2_fix),
               ])
    _write_run(fixture_repo, "demo", "run-1", man)
    view = GateResolver(_store(fixture_repo)).diff("demo")
    assert view.phase == "P2"
    # `to` is the last fix round of P2; `from` is the prior phase's last commit.
    assert view.to_sha == p2_fix
    assert view.from_sha == p1_fix
    assert "b.py" in view.diff and "P2 fixed" in view.diff
    assert "a.py" not in view.diff  # P1 is not part of this phase's range


def test_diff_first_phase_uses_step_base_sha(fixture_repo):
    (fixture_repo / "runs").mkdir(exist_ok=True)
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    p1 = _commit(fixture_repo, "a.py", "P1\n")
    man = _man("demo", "run-1", status=RUN_PARKED, current_step="p1-gate",
               steps=[_step("p1-cycle", "adversarial_cycle", "done", base_sha=base),
                      _step("p1-gate", "human_gate", "parked")],
               commits=[CommitRecord(step_id="p1-cycle", phase="P1", sha=p1)])
    _write_run(fixture_repo, "demo", "run-1", man)
    view = GateResolver(_store(fixture_repo)).diff("demo")
    assert view.phase == "P1" and view.from_sha == base and view.to_sha == p1


def test_diff_empty_case_sentinel_with_fallback(fixture_repo):
    # A gate before any phase commit exists → "no committed diff" + artifact.
    run_dir = _gate_run_with_artifacts(fixture_repo)  # no commits in manifest
    view = GateResolver(_store(fixture_repo)).diff("demo")
    assert view.no_committed_diff is True
    assert view.diff is None
    assert view.fallback is not None and view.fallback.name == "findings.json"


def test_diff_explicit_revs_override(fixture_repo):
    (fixture_repo / "runs").mkdir(exist_ok=True)
    base = git(fixture_repo, "rev-parse", "HEAD").strip()
    p1 = _commit(fixture_repo, "a.py", "P1\n")
    man = _man("demo", "run-1", status=RUN_PARKED, current_step="g",
               steps=[_step("g", "human_gate", "parked")])
    _write_run(fixture_repo, "demo", "run-1", man)
    view = GateResolver(_store(fixture_repo)).diff("demo", from_sha=base, to_sha=p1)
    assert view.from_sha == base and view.to_sha == p1 and "a.py" in view.diff


def test_diff_rejects_unsafe_rev(fixture_repo):
    (fixture_repo / "runs").mkdir(exist_ok=True)
    man = _man("demo", "run-1", status=RUN_PARKED, current_step="g",
               steps=[_step("g", "human_gate", "parked")])
    _write_run(fixture_repo, "demo", "run-1", man)
    with pytest.raises(UnsafePath):
        GateResolver(_store(fixture_repo)).diff(
            "demo", from_sha="--upload-pack=evil", to_sha="HEAD"
        )


# --------------------------------------------------------------------------- #
# HTTP control surface (fake supervisor: argv/guards without spawning)
# --------------------------------------------------------------------------- #
class _Proc:
    def __init__(self, slug, verb):
        self.slug, self.run_id, self.pid, self.verb = slug, "run-x", 7, verb
        self.log_path = Path("/tmp/x.log")


class _FakeSup:
    def __init__(self):
        self.calls: list[tuple] = []
        self._lock = None

    def approve(self, slug, *, gate=None, notes=None):
        self.calls.append(("approve", slug, gate, notes))
        return _Proc(slug, "approve")

    def resume(self, slug):
        self.calls.append(("resume", slug))
        return _Proc(slug, "resume")

    def reject(self, slug, notes, *, gate=None):
        self.calls.append(("reject", slug, notes, gate))
        return _Proc(slug, "reject")

    def driving_lock(self):
        return self._lock

    def is_owned(self, s, r):
        return False

    def is_attached(self, s, r):
        return False

    def reap(self):
        pass


def _client(repo: Path, sup=None, handoff_enabled=False) -> TestClient:
    (repo / "runs").mkdir(parents=True, exist_ok=True)
    app = create_app(_store(repo), token=TOKEN, supervisor=sup,
                     handoff_enabled=handoff_enabled)
    return TestClient(app)


def test_approve_launches_sanctioned_verb(tmp_path):
    sup = _FakeSup()
    client = _client(tmp_path / "repo", sup=sup)
    resp = client.post("/api/runs/demo/approve", json={"notes": "lgtm"},
                       headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 200 and resp.json()["status"] == "approving"
    assert sup.calls == [("approve", "demo", None, "lgtm")]


def test_resume_launches_sanctioned_verb(tmp_path):
    sup = _FakeSup()
    client = _client(tmp_path / "repo", sup=sup)
    resp = client.post("/api/runs/demo/resume", headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 200 and resp.json()["status"] == "resuming"
    assert sup.calls == [("resume", "demo")]


def test_reject_requires_notes_and_confirm(tmp_path):
    sup = _FakeSup()
    client = _client(tmp_path / "repo", sup=sup)
    # Missing confirm → 400, never reaches the supervisor (FR-10.7).
    r1 = client.post("/api/runs/demo/reject", json={"notes": "no good"},
                     headers={TOKEN_HEADER: TOKEN})
    assert r1.status_code == 400 and sup.calls == []
    # Missing notes → 422 (pydantic-required body field, FR-4.4).
    r2 = client.post("/api/runs/demo/reject", json={"confirm": True},
                     headers={TOKEN_HEADER: TOKEN})
    assert r2.status_code == 422 and sup.calls == []
    # Both present → reject runs.
    r3 = client.post("/api/runs/demo/reject", json={"notes": "no good", "confirm": True},
                     headers={TOKEN_HEADER: TOKEN})
    assert r3.status_code == 200
    assert sup.calls == [("reject", "demo", "no good", None)]


def test_approve_fails_closed_under_worktree_lock(tmp_path):
    from gauntlet.web.supervisor import LockInfo

    sup = _FakeSup()
    sup._lock = LockInfo(slug="other", run_id="run-y", pid=99, live=True)
    client = _client(tmp_path / "repo", sup=sup)
    resp = client.post("/api/runs/demo/approve", headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 409 and sup.calls == []  # FR-10.5


def test_control_endpoints_require_token(tmp_path):
    sup = _FakeSup()
    client = _client(tmp_path / "repo", sup=sup)
    assert client.post("/api/runs/demo/approve").status_code == 401
    assert client.post("/api/runs/demo/resume").status_code == 401
    assert client.post("/api/runs/demo/reject", json={"notes": "x", "confirm": True}).status_code == 401
    assert sup.calls == []


def test_control_503_without_supervisor(tmp_path):
    client = _client(tmp_path / "repo", sup=None)
    for verb in ("approve", "resume"):
        assert client.post(f"/api/runs/demo/{verb}",
                           headers={TOKEN_HEADER: TOKEN}).status_code == 503


# --------------------------------------------------------------------------- #
# read endpoints: resume_intel, gate, diff, judge-audit, handoff (over HTTP)
# --------------------------------------------------------------------------- #
def test_api_run_includes_resume_intel(fixture_repo):
    _escalation_run(fixture_repo)
    client = _client(fixture_repo)
    body = client.get("/api/runs/demo", headers={TOKEN_HEADER: TOKEN}).json()
    assert body["resume_intel"]["state"] == "escalation"
    assert body["resume_intel"]["available_controls"] == ["resume"]


def test_api_gate_endpoint(fixture_repo):
    _gate_run_with_artifacts(fixture_repo)
    client = _client(fixture_repo)
    resp = client.get("/api/runs/demo/gate", headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 200 and resp.json()["kind"] == "gate"


def test_api_handoff_disabled_then_enabled(fixture_repo):
    _escalation_run(fixture_repo)
    off = _client(fixture_repo, handoff_enabled=False)
    assert off.get("/api/runs/demo/handoff", headers={TOKEN_HEADER: TOKEN}).status_code == 404
    on = _client(fixture_repo, handoff_enabled=True)
    body = on.get("/api/runs/demo/handoff", headers={TOKEN_HEADER: TOKEN}).json()
    assert "F-003" in body["prompt"] and body["invocation"].startswith("claude")


def test_api_judge_audit(fixture_repo):
    run_dir = _gate_run_with_artifacts(fixture_repo)
    (run_dir / "judge-audit.jsonl").write_text(
        json.dumps({"tool": "Bash", "decision": "deny", "source": "policy",
                    "rationale": "blocked", "latency_ms": 3}) + "\n"
        "not-json-skip-me\n"
        + json.dumps({"tool": "Read", "decision": "allow", "source": "fast"}) + "\n"
    )
    client = _client(fixture_repo)
    body = client.get("/api/runs/demo/judge-audit", headers={TOKEN_HEADER: TOKEN}).json()
    assert [e["tool"] for e in body["entries"]] == ["Bash", "Read"]  # bad line skipped


def test_diff_page_renders(fixture_repo):
    _gate_run_with_artifacts(fixture_repo)
    client = _client(fixture_repo)
    resp = client.get("/runs/demo/diff", headers={TOKEN_HEADER: TOKEN})
    assert resp.status_code == 200
    assert "no committed diff" in resp.text.lower()


def test_detail_page_renders_gate_panel(fixture_repo):
    # The recovery panel renders Approve/Reject forms for a parked gate (FR-4.4)
    # and surfaces the show: evidence.
    _gate_run_with_artifacts(fixture_repo)
    client = _client(fixture_repo)
    html = client.get("/runs/demo", headers={TOKEN_HEADER: TOKEN}).text
    assert "data-approve" in html and "data-reject" in html
    assert "data-resume" not in html  # not a resume verb for a gate (FR-5.2)
    assert "findings.json" in html


def test_detail_page_renders_escalation_panel(fixture_repo):
    # A cycle escalation renders Resume-only — never Approve/Reject (FR-4.6/5.2).
    _escalation_run(fixture_repo)
    client = _client(fixture_repo)
    html = client.get("/runs/demo", headers={TOKEN_HEADER: TOKEN}).text
    assert "data-resume" in html
    assert "data-approve" not in html and "data-reject" not in html
    assert "F-003" in html and "plan.md" in html  # escalated evidence + target
